# ig-publish

**Publish Instagram Stories and Reels from files on your disk through the official Graph API: no public hosting,
no double posts, and no runaway script getting your account action-blocked.**

[![CI](https://github.com/deegitech/ig-publish/actions/workflows/ci.yml/badge.svg)](https://github.com/deegitech/ig-publish/actions/workflows/ci.yml)
[![CodeQL](https://github.com/deegitech/ig-publish/actions/workflows/codeql.yml/badge.svg)](https://github.com/deegitech/ig-publish/actions/workflows/codeql.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.10-3.13](https://img.shields.io/badge/python-3.10%20%7C%203.11%20%7C%203.12%20%7C%203.13-blue.svg)](pyproject.toml)
[![Runtime dependencies: 0 on Python 3.11+](https://img.shields.io/badge/runtime%20dependencies-0%20on%203.11%2B-brightgreen.svg)](pyproject.toml)

[Türkçe](README.tr.md)

```text
$ ig-publish publish --apply
(apply) Instagram publish - account 17841401234567890 | selection: 3 item(s)
  = already live: teaser-1 -> PUBLISHED 2030-01-15T20:44 | https://www.instagram.com/stories/example.brand/535353000001/
 1  LAUNCH     teaser-2                     story   5.0s  media/teaser-2.mp4 OK
    [pending]
 2  REELS      reel-launch                  reel    8.0s  media/reel-launch.mp4 OK
    [pending] | cover frame 1500 ms
    Launch day.
(0) preflight
  Quota: 1/50 API posts used (rolling 24 h) | 49 left | state file: 1 API post(s) in the last 24 h | this run: at most 2
(1) containers: 2
  ✎ teaser-2: container 424242000002 (STORIES) | uploading 0.3 MB
  ✎ reel-launch: container 424242000003 (REELS) | uploading 0.5 MB
    ... processing: 2 container(s) (teaser-2, reel-launch)
    ✓ ready teaser-2
    ✓ ready reel-launch
(2) publishing: 2 item(s), in manifest order
  ✓ published teaser-2 -> media 535353000002
    read back: STORY | 2030-01-15T20:45:02+0000 | https://www.instagram.com/stories/example.brand/535353000002/
    ... waiting 30 min (reel-launch at about 2030-01-15 21:15 +00:00 (21:15 UTC))
  ✓ published reel-launch -> media 535353000003
    read back: REELS | 2030-01-15T21:15:02+0000 | https://www.instagram.com/reel/MOCK3/
✓ 2/2 item(s) live. State: ig-publish status
```

*A real run against the test suite's mock of the Graph API (`tests/mock_graph.py`), blank lines removed: one story,
then a reel 30 minutes later (the default reel spacing).*

## First 15 minutes

1. **Set up Meta from zero** with [docs/setup.md](docs/setup.md): a Business account linked to a Facebook Page, a
   Meta app, a long-lived token stored in the Keychain, a file or AWS SSM, and the numeric account ID. Every click
   is written down.
2. **Install, start a project and check** (no pipx yet? [setup.md, step 1](docs/setup.md#1-install-ig-publish)):

   ```bash
   pipx install "git+https://github.com/deegitech/ig-publish.git"
   mkdir my-posts && cd my-posts
   ig-publish init      # ig-publish.toml, manifest.json, schedule.txt
   $EDITOR ig-publish.toml manifest.json   # your account ID and [token] source; your files instead of the examples
   ig-publish doctor    # read-only: checks the setup in order and prints the fix for every ✗
   ```

3. **Dry run, then one post:** `ig-publish prep && ig-publish plan`, then `ig-publish publish --canary --apply`.

An error code? [docs/troubleshooting.md](docs/troubleshooting.md) lists every error ig-publish knows, what it means
and the fix. ig-publish prints the same fix as a `fix:` line under the error.

## Why

Publishing through the Instagram Graph API looks like two calls. In practice it is containers, uploads, polling,
quotas, rate limits and error codes that sometimes mean "it was posted anyway". A script that retries after an
HTTP 500 can post twice. A script that loops over thirty files can trip Instagram's spam protection. A script that
dies halfway leaves you guessing what went out.

ig-publish is the careful version. You describe the posts in a manifest, in order. Every command is a dry run until
you add `--apply`. Every write is journaled, every error is resolved by asking Instagram what actually happened,
and re-running a command never publishes the same item twice (how: [Security model](#security-model)).

## Features

- **Stories and Reels** from local video files. Still images become short video stories.
- **Resumable upload** straight to `rupload.facebook.com`: no public URL, S3 bucket or CDN.
- **`prep`**: re-encodes every file with ffmpeg to a conservative Instagram profile (H.264 High, no B-frames,
  closed GOP, AAC <= 128 kbps 48 kHz, `moov` first, no edit list, 3-60 s stories) and **verifies** the result
  independently.
- **All or nothing before the first post**: every container of a run is uploaded and `FINISHED` before anything is
  published; then items go out in strict manifest order.
- **Never re-publishes**: a state file plus a container status check after *any* error. A crash at any point is
  safe to re-run.
- **Account safety**: holds on spam, limit and rate-limit codes; a burst guard; reel spacing across runs; a quota
  check; a stop when the account has recent posts the tool does not know about; a stop when a reel with the same
  caption is already live.
- **Content guards**: caption limits, banned words, a licensed-music guard, required text, season-only words, and
  source-path deny patterns.
- **Season windows**: an item tagged with a season goes out only inside its time window.
- **A scheduler** with a done-file, automatic retries for temporary stops, protection against late bursts and
  notifications. It publishes only when started with `--apply`.
- **Secure by design**: the token comes from an environment variable, a `0600` file, the macOS Keychain or AWS SSM,
  and never appears on a command line, in a URL, a log or a state file.
- **Docker image** (non-root, read-only) and an [EC2 deployment guide](docs/deploy-ec2.md).
- **No runtime dependencies** on Python 3.11+ (standard library only; Python 3.10 also installs the small `tomli`
  package to read TOML), plus ffmpeg for `prep`.

## How it works

```text
manifest.json ──▶ prep (ffmpeg + verify) ──▶ plan (dry run + live-account preflight) ──▶ publish --apply
                                                                                            │
   (0) preflight: lock, hold, reconcile half-done items, unknown posts, duplicates, burst, spacing, quota
   (1) for every item: create container ─▶ upload bytes (rupload) ─▶ poll until FINISHED
   (2) in manifest order: media_publish ─▶ read back permalink ─▶ state file (atomic) + journal
```

## Requirements

- Python 3.10 or newer on Linux or macOS (Windows: use WSL or Docker).
- FFmpeg 5.1 or newer (`ffmpeg` and `ffprobe`) for `prep`.
- An Instagram **Business** account linked to a **Facebook Page**. Creator accounts cannot publish stories through
  the API: observed (Oct 2026).
- A Meta app and a long-lived access token (Facebook Login) with `instagram_basic`, `instagram_content_publish` and
  `pages_read_engagement`: every click in [docs/setup.md](docs/setup.md).

## Install

```bash
pipx install "git+https://github.com/deegitech/ig-publish.git"
ig-publish --version
```

No pipx? `brew install pipx && pipx ensurepath` (macOS) or `sudo apt install pipx && pipx ensurepath` (Debian,
Ubuntu), then open a new terminal. Or a virtual environment:

```bash
python3 -m venv ~/.venvs/ig-publish
~/.venvs/ig-publish/bin/pip install "git+https://github.com/deegitech/ig-publish.git"
. ~/.venvs/ig-publish/bin/activate   # in every new terminal (or link bin/ig-publish into a folder on your PATH)
ig-publish --version
```

With Docker: `docker build -f docker/Dockerfile -t ig-publish:0.1.0 .`

## Quickstart

```bash
mkdir my-posts && cd my-posts
ig-publish init                      # writes ig-publish.toml, manifest.json and schedule.txt
$EDITOR ig-publish.toml              # [account] ig_user_id (the numeric ID, not the @username)
$EDITOR manifest.json                # your files, in publishing order

read -rs IG_ACCESS_TOKEN && export IG_ACCESS_TOKEN   # paste the token: not echoed, not in shell history
                                     # (lasting stores - Keychain, a 0600 file, AWS SSM: docs/setup.md, step 6)

ig-publish doctor                    # every setup check in order, with the fix for each problem (read-only)
ig-publish prep                      # media/<key>.mp4, re-encoded and verified
ig-publish plan                      # the queue, the live-account preflight, what --apply would do
ig-publish publish --canary --apply  # only the first pending item
ig-publish publish --apply           # the rest
ig-publish status
```

## Commands

| Command | What it does | Writes? |
|---|---|---|
| `init [DIR]` | write an example config, manifest and schedule (never overwrites) | local files |
| `doctor [--offline]` | the whole setup in order: configuration, manifest, FFmpeg, state, token, permissions, Instagram account and type, Page link, quota; prints the fix under every `✗` (exit 1 if any) | no |
| `check` | the short account summary: token owner, permissions, Page tasks and link, account, quota, API usage | no |
| `prep [--only G] [--keys K]` | re-encode sources to `media/<key>.mp4` and verify them; unchanged files are skipped | local files |
| `verify FILE --kind story\|reel` | check one file against the prep profile (`[prep]`; stricter than Instagram's limits); works without a configuration file | no |
| `plan` | the queue with full captions, the live-account preflight and what `--apply` would do | no |
| `publish [--apply]` | publish the selection (a dry run without `--apply`) | with `--apply` |
| `status [--offline]` | state table, hold, when live stories leave the tray, quota | no* |
| `delete KEY [--confirm-media ID] [--apply]` | delete a post published by this tool | with `--apply` |
| `import RECORD.json [--apply]` | record posts of manifest items published elsewhere so they are never posted again | with `--apply` (state only) |
| `ack [--apply]` | acknowledge recent posts the state file does not know (made by hand or by another tool), so the preflight stops asking about them | with `--apply` (state only) |
| `schedule run\|once --apply` / `schedule dry [--now T]` | the scheduler (see below); `run` and `once` refuse to start without `--apply` | with `--apply` |

\* `status` may complete half-finished records (media IDs) from the API; it never posts.

Selection and safety options for `plan` and `publish` (the last two are for `publish` only):

| Option | Meaning |
|---|---|
| `--only GROUP[,GROUP]` | manifest groups; `STORIES` and `REELS` select a kind |
| `--keys KEY[,KEY]` | exactly these keys, in manifest order; all of them or none |
| `--canary` / `--limit N` | only the first item / at most N items |
| `--story-gap S` / `--reel-gap S` | override the spacing for this run |
| `--repost KEY` | publish a key again although it is live or was deleted (on purpose) |
| `--approve KEY` | release an item marked `needs_approval` |
| `--offline` | no token and no network (skips quota and account checks); dry runs only |
| `--at TIME` | evaluate season windows at TIME (dry runs only) |
| `--ack-unknown-posts` | (publish only) continue although the account has recent posts the state file does not know, and acknowledge them; `ig-publish ack --apply` does this without publishing |
| `--ignore-hold` | (publish only) override an active hold, the burst guard and reel spacing (knowingly) |

### Exit codes

| Code | Meaning |
|---|---|
| `0` | done |
| `1` | error, or a stop **after** something was written: needs a look, do not retry blindly |
| `2` | command-line usage error |
| `75` | stopped **before anything was written**, for a reason that passes with time (lock, hold, recent posts the state file does not know, API quota/state mismatch, burst guard, reel spacing, quota, usage limit, read error, missing token file, season not open yet). Running the same command later is safe; the scheduler does exactly that |

## Configuration

`ig-publish.toml` (or `ig-publish.json`, same structure; a [JSON Schema](src/ig_publish/schemas/config.schema.json)
is included). The file is found via `--config`, `$IG_PUBLISH_CONFIG`, or `./ig-publish.toml` / `./ig-publish.json`.
Relative paths are relative to the configuration file. **Unknown keys are errors**, so a typo never silently
disables a guard. The annotated example is [`examples/ig-publish.toml`](examples/ig-publish.toml).

| Key | Default | Meaning |
|---|---|---|
| `account.ig_user_id` | *(required)* | numeric ID of the Instagram Business account (not the @username) |
| `account.username` | - | `check` fails if the token sees a different username |
| `account.page_id` | - | the linked Facebook Page (otherwise found through `me/accounts`) |
| `api.version` | `"v26.0"` | Graph API version to pin |
| `api.usage_stop_percent` | `85` | send no further request above this usage percentage |
| `paths.manifest` | `"manifest.json"` | the manifest |
| `paths.source_root` | `"."` | manifest `src` paths are relative to this |
| `paths.media_dir` | `"media"` | prepared files and `prep.json` |
| `paths.state` | `"state/ig-state.json"` | what was published (back it up) |
| `paths.journal` | `"state/ig-journal.jsonl"` | append-only log of every API write |
| `token.source` | `"env"` | `env`, `file`, `keychain` or `ssm` |
| `token.env_var` | `"IG_ACCESS_TOKEN"` | for `env` |
| `token.path` | - | for `file`: mode `0600` or stricter, owned by you |
| `token.keychain_service` / `keychain_account` | - / `$USER` | for `keychain` (macOS) |
| `token.ssm_parameter` / `ssm_region` / `ssm_profile` | - | for `ssm` (`SecureString`, read with the `aws` CLI) |
| `token.timeout_seconds` | `30` | for `keychain` and `ssm` |
| `spacing.story_seconds` | `10` | between stories of one run |
| `spacing.reel_seconds` | `1800` | between reels, also across runs (checked against the account) |
| `safety.burst_window_hours` / `burst_max_posts` | `3` / `10` | wait if the account already has this many posts in the window |
| `safety.rate_hold_minutes` | `60` | minimum hold after a rate-limit answer |
| `safety.container_max_age_hours` | `23` | older containers are re-created (they expire at 24 h) |
| `timing.poll_seconds` / `poll_timeout_seconds` | `15` / `600` | how often a processing container is read / give up (nothing is published) after this |
| `timing.guard_wait_seconds` | `10` | between container reads after a `media_publish` error |
| `timing.republish_wait_seconds` | `45` | before retrying `media_publish` after error `2207008` |
| `timing.rate_retry_seconds` / `read_retry_backoff_seconds` | `60` / `5` | before re-reading after an Instagram usage limit / a failed read (times the attempt number) |
| `timing.http_timeout_seconds` / `upload_timeout_seconds` | `60` / `900` | per API request (POSTs get twice this) / per upload: raise them for long reels or a slow uplink |
| `captions.required` | `true` | every reel needs a caption |
| `captions.max_length` | `2200` | Instagram's limit; can only be lowered |
| `captions.min_hashtags` / `max_hashtags` | `0` / `30` | 30 is Instagram's limit |
| `captions.required_text` | `[]` | strings every reel caption must contain |
| `captions.banned_words` | `[]` | case-insensitive substring match (catches hashtags too) |
| `captions.licensed_music` | `[]` | titles, artists or lyrics that must never be named (ignores case, spaces, punctuation and `#`) |
| `captions.forbid_curly_quotes` | `false` | house style |
| `sources.deny` | `[]` | regular expressions; a matching `src` may not be published through the API |
| `seasons.NAME.start` / `end` | - | ISO 8601 with a UTC offset; `end` is exclusive |
| `seasons.NAME.caption_words` | `[]` | words allowed only in items of this season |
| `prep.width` / `height` / `fps` | `1080` / `1920` / `30` | output format |
| `prep.fit` | `"pad"` | `pad` (letterbox), `crop` or `stretch` for sources that are not 9:16 |
| `prep.crf` / `preset` / `max_video_kbps` | `20` / `"medium"` / `4500` | x264 settings |
| `prep.audio_kbps` | `128` | Instagram's limit is 128 |
| `prep.threads` / `nice` | `2` / `10` | keep encoding light |
| `prep.still_seconds` | `5` | length of a story made from a still image (3-60) |
| `schedule.*` | see [Scheduler](#scheduler) | |

Environment variables: `IG_PUBLISH_CONFIG`, `IG_ACCESS_TOKEN` (or your `token.env_var`), `IG_PUBLISH_DEBUG=1`
(print a redacted traceback after an unexpected error). `IG_PUBLISH_API_BASE`, `IG_PUBLISH_UPLOAD_BASE` and
`IG_PUBLISH_TEST_TOKEN` exist only for tests against a local mock server.

### Token sources

The token is never stored in the configuration file. Pick one `[token] source`:

| Source | Setup |
|---|---|
| `env` (default) | `read -rs IG_ACCESS_TOKEN && export IG_ACCESS_TOKEN` (pasted, not echoed, not in the shell history) |
| `file` | a regular file owned by the user that runs ig-publish, mode `0600`; anything else is refused |
| `keychain` (macOS) | type `security add-generic-password -U -a "$USER" -s ig-publish -w "$(pbpaste)"` without pressing Enter, copy the token, press Enter, then `pbcopy </dev/null`; set `keychain_service = "ig-publish"`. Never leave `-w` without a value: Meta tokens are about 200 characters, and that prompt cuts input at 128, observed (Oct 2026) |
| `ssm` (AWS) | a `SecureString` parameter, read with `aws ssm get-parameter --with-decryption` (see [docs/deploy-ec2.md](docs/deploy-ec2.md)) |

How to get a token and the numeric account ID, and each store step by step: [docs/setup.md](docs/setup.md).
`ig-publish doctor` checks the stored token without ever printing it.

## The manifest

`manifest.json` lists what to publish. **The order of `items` is the publishing order.** A
[JSON Schema](src/ig_publish/schemas/manifest.schema.json) is included for editor validation; ig-publish checks the
same rules itself, plus file existence and the caption rules. Full example: [`examples/manifest.json`](examples/manifest.json).

```json
{
  "$schema": "https://raw.githubusercontent.com/deegitech/ig-publish/main/src/ig_publish/schemas/manifest.schema.json",
  "items": [
    {"key": "teaser-1", "kind": "story", "group": "LAUNCH", "src": "videos/teaser-1.mp4"},
    {"key": "teaser-2", "kind": "story", "group": "LAUNCH", "src": "images/card.png", "still_seconds": 5},
    {"key": "reel-launch", "kind": "reel", "group": "REELS", "src": "videos/launch.mp4",
     "caption": "Launch day.\nLink in bio.\n#example", "share_to_feed": true, "thumb_offset": 1500}
  ]
}
```

| Field | Applies to | Meaning |
|---|---|---|
| `key` | all | stable ID (`a-z`, `0-9`, `-`) used in the state file, on the command line and in the schedule |
| `kind` | all | `story` or `reel` |
| `group` | all | free label for `--only`; for stories, the Highlight you plan to add them to |
| `src` | all | source file, relative to `paths.source_root` |
| `caption` | reels | up to 2,200 characters, 30 hashtags, 20 @-mentions |
| `share_to_feed` | reels | also show the reel in the grid (default `true`) |
| `thumb_offset` | reels | cover frame, in milliseconds |
| `season` | all | publish only inside `[seasons.NAME]`'s window |
| `still_seconds` | image stories | length of the generated video |
| `needs_approval` | all | hold the item back until a run passes `--approve KEY` (the value is the reason) |
| `already_live` | all | `{"media_id", "permalink", "at"?, "via"?}`: posted outside this tool; counts as published |
| `note`, `x-*`, `_*` | all | your own notes and metadata (ignored) |

## Content guards

All manifest checks run on every command, offline, before anything else. A failing check stops the command.

- **Limits** from Meta's reference: caption length, hashtags, @-mentions; stories take no caption.
- **`banned_words`**: words that must never appear, matched as substrings, so `#bigsale` is caught by `sale`.
- **Licensed-music guard** (`licensed_music`): the API cannot attach Instagram's music library, so a post made
  through the API carries only the audio in your file. If you also make versions with licensed tracks (added by hand
  in the app), list those tracks' titles and artists here: an API caption that names a track the post does not
  carry is misleading and invites copyright trouble. Matching ignores case, spaces, punctuation and `#`.
- **`sources.deny`**: keep files that must not go out through the API (drafts, cuts meant for adding licensed
  music by hand in the app ...) out of the queue, e.g. `['-draft\.mp4$', '(^|/)licensed-audio/']`.
- **`required_text`**: a call to action or a disclosure every caption must contain.
- **Seasons**: `caption_words` of a season may only appear in items tagged with that season.

## Seasons

```toml
[seasons.holiday]
start = "2030-12-01T00:00:00-08:00"   # inclusive: midnight in your westernmost market, so it is Dec 1 for everyone
end = "2031-01-01T00:00:00+14:00"     # exclusive: the first moment it is Jan 1 anywhere
caption_words = ["christmas", "xmas"]
```

An item with `"season": "holiday"` is published only inside the window. Before the window `--apply` stops with
exit 75 (the scheduler retries); after it, with exit 1. `plan --at 2030-12-01T09:00:00Z` shows what would happen at
another time.

## Scheduler

`ig-publish schedule run --apply` publishes the lines of a schedule file at their times, one at a time, each as
`ig-publish publish --keys KEYS --apply` in a separate process. Without `--apply`, `run` and `once` refuse to
start (exit 2); `schedule dry` only previews:

```text
# schedule.txt: "YYYY-MM-DD HH:MM key[,key...]", times in [schedule] timezone
2030-11-03 18:30 teaser-1,teaser-2
2030-11-04 12:00 reel-launch
```

| Outcome of a line | Recorded as | What happens next |
|---|---|---|
| exit 0 | `ok` | never runs again (restarts are safe) |
| exit 75 (nothing written) | `retry` | retried every `retry_seconds` until `max_late_hours` late, then `missed`; one notification |
| any other exit | `fail` | not retried (delete its lines from the done file to retry); notification |
| more than `max_late_hours` late | `missed` | not published (no burst after a sleeping laptop or a reboot); notification |

Lines run in file order, at least `gap_seconds` apart; the file is re-read every tick, so you can append lines
while it runs (`run` keeps running after the last line until it is stopped). `SIGTERM` or Ctrl-C lets a running
publish finish and record itself before the scheduler exits.
`schedule dry [--now "YYYY-MM-DD HH:MM"]` shows what runs now and next and checks every line offline (keys exist,
nothing held back, media prepared, season window open at the line's time). `schedule once --apply` runs a single
tick (handy from cron). The log goes to `log_file` and to stdout (`docker logs`, journald); with `once`, the routine
lines ("started", "WAITING") go to the file only, so an idle cron tick prints nothing.

If you post something by hand while lines are scheduled, the next run stops with exit 75 ("NOT in the state file")
and keeps retrying; `ig-publish ack --apply` records those posts and the next retry continues.

| Key | Default | Meaning |
|---|---|---|
| `schedule.file` | `"schedule.txt"` | the schedule |
| `schedule.timezone` | local time | IANA name, e.g. `"UTC"` or `"Europe/Berlin"` |
| `schedule.done_file` / `log_file` | `"state/schedule.done"` / `"state/schedule.log"` | outcomes / redacted log |
| `schedule.gap_seconds` | `1860` | between the end of one run and the start of the next |
| `schedule.max_late_hours` | `6` | later than this, a line is `missed` |
| `schedule.poll_seconds` / `retry_seconds` | `60` / `900` | tick interval / retry interval after exit 75 |
| `schedule.notify_command` | `[]` | e.g. `["python3", "/data/notify-webhook.py", "--url-file", "/data/secrets/webhook"]`; the message is appended as the last argument ([example](examples/notify-webhook.py)); a failing notifier is logged |
| `schedule.command` | this ig-publish | advanced: the publisher command line before `publish --keys ...` |

## Examples

| File | What it shows |
|---|---|
| [`examples/ig-publish.toml`](examples/ig-publish.toml) | the annotated configuration (the same file `ig-publish init` writes) |
| [`examples/ig-publish.server.json`](examples/ig-publish.server.json) | JSON configuration for Docker / EC2: token from a file, UTC schedule, notifications |
| [`examples/manifest.json`](examples/manifest.json) | stories, a still image, reels, a season item, an already-live item and one awaiting approval |
| [`examples/schedule.txt`](examples/schedule.txt) | schedule lines |
| [`examples/import-record.json`](examples/import-record.json) | recording posts that were made outside ig-publish |
| [`examples/notify-webhook.py`](examples/notify-webhook.py) | a scheduler notifier for chat webhooks (the URL is read from a `0600` file you own) |
| [`examples/systemd/`](examples/systemd) | the unit and script that fetch the token from AWS SSM into tmpfs |

Common recipes:

```bash
ig-publish publish --only REELS --limit 1 --apply        # one reel a day (from cron or the scheduler)
ig-publish publish --only LAUNCH --story-gap 30 --apply  # one Highlight group, 30 s between stories
ig-publish plan --keys reel-launch --at 2030-12-01T09:00:00Z   # would this go out at that time?
ig-publish publish --approve reel-own-music --apply      # release an item marked needs_approval
ig-publish publish --repost teaser-1 --canary --apply    # publish a live (or deleted) item again, on purpose
ig-publish verify my-edit.mp4 --kind story               # does my own export pass the prep profile?
ig-publish ack --apply                                   # I posted something by hand: stop asking about it
```

## Running on a server

- **Docker**: [`docker/Dockerfile`](docker/Dockerfile) (python:3.12-slim pinned by digest, ffmpeg, runs as uid
  10001; the default command is `schedule dry`, which never publishes) and
  [`docker/compose.yaml`](docker/compose.yaml) (`schedule run --apply`, `init`, `restart: unless-stopped`, read-only
  root, no capabilities, CPU and memory limits, the token mounted read-only).
- **AWS EC2**: [docs/deploy-ec2.md](docs/deploy-ec2.md): the token stays in SSM Parameter Store and is written by a
  systemd oneshot to a tmpfs file (`0600`) that only the container user can read.
- **Any other Linux host with Docker**: write the token file yourself. The container user (uid 10001) must own it
  and it must be mode `0600`, or ig-publish refuses it. `/run` is a tmpfs, so the file is gone after a reboot
  (write it again, or automate it like the EC2 guide):

  ```bash
  sudo install -d -m 0755 /run/ig-publish
  read -rs T && printf '%s' "$T" | sudo sh -c 'umask 077; cat > /run/ig-publish/token && chown 10001:10001 /run/ig-publish/token'; unset T
  ```

  Then mount it read-only: `-v /run/ig-publish:/run/secrets/ig-publish:ro`, with `"token": {"source": "file",
  "path": "/run/secrets/ig-publish/token"}` in the configuration.

## Security model

**Nothing is published, deleted or imported without `--apply`**, and the scheduler too only publishes when
started with `schedule run --apply`. `publish`, `delete`, `import` and `ack` are dry runs by default; `plan`, `check`,
`doctor`, `verify` and `status` never post (`status` may complete local state records, such as a media ID it could not read
before; `prep` writes prepared copies of your media).

**Trust boundary**: treat `ig-publish.toml` like a script: `notify_command` and `command` run programs, and
`./ig-publish.toml` is picked up from the current directory. Never run ig-publish with a configuration or manifest
you did not write.

**The token**
- comes from exactly one configured source: an environment variable, a file (refused unless it is a regular file,
  owned by you and not accessible to group or others), the macOS Keychain (`security ... -w`) or AWS SSM
  (`aws ssm get-parameter --with-decryption`). The Keychain and SSM tools receive the item *name*; the token comes
  back on a pipe, never on a command line;
- travels only in the `Authorization` header: never in a URL or a form body. Paged results are followed by cursor,
  never by the `paging.next` URL (which can embed a token). HTTP redirects are not followed;
- is sent only to `graph.facebook.com` and `rupload.facebook.com`. Upload URLs returned by the API are checked
  against the rupload host before the token is attached. The test-only endpoint overrides accept loopback
  addresses only, and in that mode no real token source is ever read;
- is redacted (exact value and token-like patterns, `access_token=` parameters, `Bearer`/`OAuth` header values)
  from everything printed or written: stdout, stderr (also after an unexpected error), the journal, the state file
  and the scheduler log;
- is kept out of helper processes: ffmpeg, ffprobe, the Keychain and AWS tools and your `notify_command` run
  without the token variable (and without any `IG_PUBLISH_*` variable) in their environment.

**No double posts**
- The state file is the single source of truth: written atomically (temporary file + rename), `fsync`ed, mode
  `0600`. The journal records an `INTENT` line before every API write and the result after it.
- One writer at a time (`flock` run lock). If the state file is missing but the journal shows publishes, `--apply`
  refuses to run. A state file from another account is refused.
- Every container is uploaded and `FINISHED` before the first `media_publish`. After **any** publish error the
  container is read first: `PUBLISHED` is recorded and never published again; an unknown outcome stops the run
  without retrying. A container that was ever sent to `media_publish` is never abandoned and re-created while its
  status cannot be read, whatever its age or the earlier error.
- Containers are fingerprinted (file hash, caption, options): a changed file or caption gets a new container.
- Every published item is read back (permalink, product type) and recorded.

**Account safety**: holds on spam/limit/rate-limit codes that survive restarts; burst guard; reel spacing; quota
check; unknown-post and duplicate-caption checks; season windows. Overrides are explicit flags that are printed.

**What it does not protect against**: a compromised machine or account, someone else holding your token, or
changes on Meta's side. Keep the state directory backed up and private.

Supply chain: no runtime dependencies on Python 3.11+ (`tomli` on 3.10); CI runs the tests on Python 3.10-3.13, a
gitleaks secret scan (pinned checksum) and CodeQL; GitHub Actions are pinned to commit SHAs and the Docker base image
to a digest; Dependabot watches pip, GitHub Actions and the Docker base image.

## Instagram API limits and honest caveats

- **Not possible through the API**: stickers, links in stories, Instagram's music library and Highlights. Add
  stories to Highlights in the app within 24 hours (`status` tells you when they expire).
- **Quota**: a moving 24-hour limit per account: observed (Oct 2026) `quota_total` 100; some Meta docs say 50.
  ig-publish reads the live value (`content_publishing_limit`) before every run.
- **Business accounts**: Creator accounts cannot publish stories through the API: observed (Oct 2026).
- **Stories are 3-60 s**, reels 3 s - 15 min. `prep` and `verify` enforce this.
- The burst guard and reel spacing are **our conservative heuristics**, not published Meta rules. The burst guard
  counts posts already on the account before a run; it does not limit the size of one run (use `--limit`; a dry run
  warns about large runs).
- The automated tests run against a **local mock** of the Graph API built from the documentation and our own
  observations. Meta's real behaviour can differ and change; start with `plan`, `--canary` and an account you can
  afford to experiment with.
- Not supported yet: images and carousels (still images become video stories), reel covers from a file,
  collaborators, user and location tags, `audio_name`, `appsecret_proof`, the Instagram Login API, Windows.
- Uploads are sent in one request from offset 0; an interrupted upload is not resumed mid-file (the next run
  creates a fresh container).
- `prep` needs FFmpeg 5.1 or newer (it says so if yours is older); `verify` and the prep profile are stricter than
  Instagram's own limits (exact size, constant frame rate, no B-frames).
- Messages are in English only.

More detail: [docs/instagram-api.md](docs/instagram-api.md) (the API, its limits and how ig-publish reacts to each
error) and [docs/troubleshooting.md](docs/troubleshooting.md) (every error with its fix).

## FAQ

**Something does not work. Where do I start?** `ig-publish doctor`. It checks the setup in order and prints the fix
under every problem; errors during other commands carry a `fix:` line too. The full list is
[docs/troubleshooting.md](docs/troubleshooting.md).

**My token stopped working.** Long-lived tokens last about 60 days; Meta then answers code `190/463` and ig-publish
prints the renewal steps. Renew before that: [docs/setup.md, step 9](docs/setup.md#9-token-expiry-and-renewal).

**Why not Meta Business Suite?** It is fine for a handful of posts. ig-publish is for when your posts are files
produced by a pipeline, live in version control, need review, a guaranteed order and automation.

**It stopped with "NOT in the state file".** The account has posts from the last 24 hours that ig-publish did not
make (posted by hand or by another tool). `ig-publish ack` lists them and `ig-publish ack --apply` acknowledges them
(state file only, nothing is posted); the scheduler continues on its next retry. If they are manifest items you
posted elsewhere, record them with `ig-publish import` instead, so they are never published again.
`publish --ack-unknown-posts` acknowledges them as part of a single manual run. Acknowledged posts are not asked
about again.

**What does exit 75 mean?** Nothing was written and the reason passes with time. Run the same command later; the
scheduler does this automatically.

**My laptop slept (or the process was killed) in the middle of a run.** Run the same command again. Half-finished
items are reconciled from their container status before anything else; nothing is posted twice.

**How do I publish something again on purpose?** `--repost KEY`. Published and deleted keys are otherwise never
published again.

**Can I use my own encoder?** Yes: if a source already passes verification, `prep` copies it instead of
re-encoding. `ig-publish verify FILE --kind reel` shows what is missing. It checks against the prep profile
(`[prep]` settings, or the defaults when there is no configuration file), which is stricter than Instagram's own
limits: a valid 60 fps or Main-profile export is reported as not matching.

**Is this affiliated with Meta or Instagram?** No. Instagram and Facebook are trademarks of Meta Platforms, Inc.

## Development

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest                      # or: python -m unittest discover -s tests
ruff check src tests
```

The tests are fully offline: a local mock of the Graph API and rupload, synthetic videos made with ffmpeg, fake
`security`, `aws` and FFmpeg tools. See [CONTRIBUTING.md](CONTRIBUTING.md); report vulnerabilities privately as described in
[SECURITY.md](SECURITY.md).

## About / Built by DEEGITECH

ig-publish is made by [DEEGITECH](https://github.com/deegitech), a small software and game studio
(DEEGITECH Teknoloji ve Yazılım Ltd. Şti.). We wrote it while shipping our iOS game
[Wide Molly Hooked](https://apps.apple.com/app/id6813081261), to post its Stories and Reels without babysitting the
API, and we use it ourselves. We hope it saves you the same trouble. Issues and pull requests are welcome.

## License

Released under the [MIT License](LICENSE).
