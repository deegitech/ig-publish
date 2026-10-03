# Running the scheduler on AWS EC2 with Docker

This guide runs `ig-publish schedule run --apply` around the clock on a small EC2 instance. The design keeps the
access token out of images, environment variables, command lines, logs and disks:

```text
 AWS SSM Parameter Store                 EC2 host                                   container (uid 10001)
 SecureString  ──aws ssm get-parameter──▶ systemd oneshot ──▶ /run/ig-publish/token ──(read-only mount)──▶ ig-publish
 /example/ig-publish/access-token        (fetch-ig-token.sh)  tmpfs, 0600, uid 10001              schedule run --apply
```

* the token lives in SSM as a `SecureString` (encrypted with KMS);
* at boot, a systemd oneshot writes it to `/run/ig-publish/token`: `/run` is a tmpfs, so the token never touches the
  disk; the file is mode `0600` and owned by the container user;
* the container runs as a non-root user with a read-only root filesystem, no capabilities, `--init`, CPU and memory
  limits, and `--restart unless-stopped`.

Every name below (`/example/ig-publish/access-token`, `us-east-1`, `111122223333`, `/srv/ig-publish`) is a placeholder:
use your own.

## 1. The instance

* Any small Linux instance works; the scheduler sleeps most of the time. Running `prep` (ffmpeg) on the instance
  is slow on the smallest sizes; you can prepare media on your workstation instead (step 6).
* Install Docker Engine and the AWS CLI v2.
* Require IMDSv2 and keep the metadata hop limit at **1**. Containers on the default bridge network then cannot reach
  the instance's credentials; only the host can (and only the host needs them).

## 2. Store the token in SSM

Get a long-lived token first ([setup.md, steps 3 to 5](setup.md#3-create-the-meta-app)). From your workstation, without putting the token in your shell history, on a command line or in a file (it goes
from a silent prompt through a pipe to the AWS CLI):

```bash
read -rs TOKEN && printf '%s' "$TOKEN" | aws ssm put-parameter --region us-east-1 \
    --name /example/ig-publish/access-token --type SecureString --value file:///dev/stdin --overwrite; unset TOKEN
```

## 3. Let the instance read only that parameter

Attach an instance role with a policy like this one (add `kms:Decrypt` on your key if the parameter uses a
customer-managed KMS key):

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": "ssm:GetParameter",
      "Resource": "arn:aws:ssm:us-east-1:111122223333:parameter/example/ig-publish/access-token"
    }
  ]
}
```

## 4. Fetch the token at boot

Install the script and the unit from [`examples/systemd/`](../examples/systemd):

```bash
sudo install -m 0755 examples/systemd/fetch-ig-token.sh /usr/local/sbin/fetch-ig-token.sh
sudo install -m 0644 examples/systemd/ig-publish-token.service /etc/systemd/system/ig-publish-token.service
sudoedit /etc/systemd/system/ig-publish-token.service      # set AWS_REGION and IG_TOKEN_PARAM
sudo systemctl daemon-reload
sudo systemctl enable --now ig-publish-token.service
sudo stat -c '%a %u:%g %n' /run/ig-publish/token            # expect: 600 10001:10001 /run/ig-publish/token
```

The unit runs before `docker.service`, so after a reboot the file exists by the time Docker restarts the container.
If the fetch fails anyway (no network or instance credentials yet), systemd retries it every 30 seconds; meanwhile
ig-publish treats the missing token file as a temporary stop (exit 75) and the scheduler retries the line. Nothing
is published without a token.

**Rotating the token** (a long-lived token lasts about 60 days, see
[setup.md, step 9](setup.md#9-token-expiry-and-renewal)): update the SSM parameter as in step 2, then
`sudo systemctl restart ig-publish-token` and check with `run doctor` (step 8). Every scheduled run is a fresh
process that reads the file again, so the container does not need a restart.

## 5. The data directory

```bash
sudo mkdir -p /srv/ig-publish/secrets
sudo cp examples/ig-publish.server.json /srv/ig-publish/ig-publish.json   # edit: ig_user_id, username, schedule
sudo cp examples/notify-webhook.py /srv/ig-publish/                         # optional notifications
read -rs URL && printf '%s' "$URL" | sudo sh -c 'umask 077; cat > /srv/ig-publish/secrets/webhook'; unset URL  # optional
sudo chown -R 10001:10001 /srv/ig-publish
sudo chmod 0700 /srv/ig-publish/secrets
```

The server example reads the token with `"token": {"source": "file", "path": "/run/secrets/ig-publish/token"}`;
that is where the container sees the host directory `/run/ig-publish`.

Notifications: the example's `notify_command` runs `notify-webhook.py --url-file /data/secrets/webhook`, which posts
each message to the chat webhook whose URL is in that file (inside the container `/srv/ig-publish` is `/data`). The
file must be owned by uid 10001 with mode `0600`, or the notifier refuses it. Unlike the token it lives on disk; if
you prefer, keep it in SSM too and extend `fetch-ig-token.sh`. Without the file (or with `"notify_command": []`) the
scheduler still runs and writes `WARNING: notify_command exited 1` to its log.

`/srv/ig-publish` holds the manifest, the schedule, your source files, `media/` and `state/`. The state directory is
the record of what was published: back it up (for example to an encrypted S3 bucket) and never commit it.

## 6. Media

Either run `prep` on the instance (step 8), or prepare on your workstation and copy the result. `prep` records
SHA-256 hashes of each source and output, so copy the **sources and** `media/` (including `media/prep.json`) with
the same relative paths, for example:

```bash
rsync -a videos media manifest.json schedule.txt host:/srv/ig-publish/
ssh host sudo chown -R 10001:10001 /srv/ig-publish
```

## 7. Build the image

On the instance (or build elsewhere and push to your registry):

```bash
git clone https://github.com/deegitech/ig-publish.git && cd ig-publish
docker build -f docker/Dockerfile -t ig-publish:0.1.0 .
```

## 8. Check before you schedule

```bash
run() {
  docker run --rm --init --user 10001:10001 --read-only --tmpfs /tmp --cap-drop ALL \
    --security-opt no-new-privileges -v /srv/ig-publish:/data \
    -v /run/ig-publish:/run/secrets/ig-publish:ro ig-publish:0.1.0 "$@"
}
run doctor           # every setup check in order, with the fix for each problem (read-only)
run prep             # only if you did not copy prepared media
run plan             # the queue and the live-account preflight
run schedule dry     # what the scheduler would run now and next
```

## 9. Start the scheduler

```bash
docker run -d --name ig-publish \
  --init --restart unless-stopped --stop-timeout 600 \
  --user 10001:10001 --read-only --tmpfs /tmp \
  --cap-drop ALL --security-opt no-new-privileges \
  --cpus 0.5 --memory 512m --pids-limit 128 \
  --log-opt max-size=10m --log-opt max-file=3 \
  -v /srv/ig-publish:/data \
  -v /run/ig-publish:/run/secrets/ig-publish:ro \
  ig-publish:0.1.0 schedule run --apply
```

Or use [`docker/compose.yaml`](../docker/compose.yaml), which sets the same options and the same command.

* `schedule run --apply` is what publishes. Without a command the image runs `schedule dry`, which only prints what
  would run and exits; `schedule run` without `--apply` refuses to start.
* `--init` puts a minimal init process at PID 1, so `SIGTERM` reaches the scheduler and child processes are reaped.
* `--stop-timeout 600` gives a running publish time to finish: on `SIGTERM` the scheduler lets it complete and
  records it, then exits. If it is killed anyway, the state file still prevents a double post on the next start.
* `--restart unless-stopped` brings the scheduler back after crashes and reboots.
* Uploads stream from disk, so memory use does not grow with file size.

## 10. Operating it

| Task | Command |
|---|---|
| What runs now / next | `docker exec ig-publish ig-publish schedule dry` |
| Add a run | append a line to `/srv/ig-publish/schedule.txt` (re-read every minute) |
| Scheduler log | `docker logs ig-publish`, or `/srv/ig-publish/state/schedule.log` (both redacted) |
| A post was made by hand | `docker exec ig-publish ig-publish ack --apply` (otherwise scheduled runs stop with exit 75 until that post is 24 h old) |
| What is published | `docker exec ig-publish ig-publish status` |
| Retry a failed line | delete its lines from `state/schedule.done` |
| Stop safely | `docker stop -t 600 ig-publish` |

A line that ends with exit 75 (nothing written: hold, recent posts the state file does not know, API quota/state
mismatch, spacing, burst, quota, usage limit, read error, missing token) is retried every 15 minutes until it is
`max_late_hours` late, then marked `missed`. Any other failure is marked `fail` and not retried. Set
`notify_command` to hear about both.
