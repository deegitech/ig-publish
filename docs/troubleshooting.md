# Troubleshooting

Every error ig-publish knows, what it means and the fix. The tool prints the same fix as a `fix:` line under the
error (also in scheduler notifications), so this page is for the context around it.

**Start with `ig-publish doctor`.** It checks the whole setup in order (configuration, manifest, FFmpeg, state,
token, permissions, Instagram account, Page link, quota) and prints the fix under every `✗`. It only reads.

- Quoted messages are what Meta or ig-publish prints (Meta's wording varies); the codes are `code` /
  `error_subcode` from Meta's error object (ig-publish prints them as `code 190/463`). Meanings follow Meta's error-code tables (Graph API "Handling
  errors" and the Instagram Platform error codes). **observed (Oct 2026)** marks what we saw in production and did
  not find in Meta's documentation.
- Console paths: Meta renames screens often, so the labels given are the ones we have seen. **Names may differ.**
- Exit codes: `0` done, `1` error or a stop after something was written (look before retrying), `2` usage error,
  `75` stopped **before** anything was written for a reason that passes with time (safe to run again later; the
  scheduler does).

Sections: [While generating the token](#while-generating-the-token) · [Token and login](#token-and-login) ·
[Permissions and access](#permissions-and-access) ·
[Account, Page and identity](#account-page-and-identity) · [Publishing](#publishing) ·
[Rate limits and spam protection](#rate-limits-and-spam-protection) · [Uploads and network](#uploads-and-network) ·
[Preflight stops](#preflight-stops) · [State, lock and delete](#state-lock-and-delete) ·
[Token sources](#token-sources) · [Configuration, manifest and FFmpeg](#configuration-manifest-and-ffmpeg) ·
[Scheduler](#scheduler)

## While generating the token

These come from Meta's pages, before ig-publish is involved.

| Message | Meaning | Fix |
|---|---|---|
| "Invalid Scopes: instagram_content_publish ..." in the login dialog | The permission is not added to the app's use case yet. | App Dashboard → **Use cases** → **Manage messaging & content on Instagram** → **Customize** → **API setup with Facebook login** → add the permission ([setup.md, step 3.5](setup.md#3-create-the-meta-app)), then generate the token again. |
| The dialog does not offer your Instagram account | The account is not a professional account, or it is not linked to the Facebook Page. | Switch it to Business and link it to the Page ([setup.md, steps 2.1 and 2.2](setup.md#2-prepare-the-instagram-account-and-the-page)), then generate the token again. |
| "Feature unavailable": Facebook Login is currently unavailable for this app | The app is in Development mode and your Facebook account has no role on it. | Someone with admin rights on the app adds you: App Dashboard → **App roles** → **Roles** → **Add People** (Developer or Tester; [setup.md, step 3.6](setup.md#3-create-the-meta-app)). Accept the invitation (developers.facebook.com → your notifications or **Requests**), then generate the token again. Names may differ. |

## Token and login

"Make a new token" always means: [Graph API Explorer](https://developers.facebook.com/tools/explorer) → your app →
**Generate Access Token** (tick BOTH the Facebook Page and the Instagram account) → **(i)** → Access Token Debugger
→ **Extend Access Token** → store it again ([setup.md, step 6](setup.md#6-store-the-token-safely)) →
`ig-publish doctor`. Details: [setup.md, steps 4 and 9](setup.md#4-generate-the-access-token).

| Error | Meaning | Fix |
|---|---|---|
| `190/463` "Session has expired" | The token expired. Long-lived tokens last about 60 days. | Make a new token. Set a reminder for the next renewal ([setup.md, step 9](setup.md#9-token-expiry-and-renewal)). |
| `190/460` | The token was invalidated by a Facebook password change or a security reset. | Make a new token. |
| `190/467` | The token is no longer valid: logged out, revoked or replaced. | Make a new token. |
| `190/458` | The app is not authorized for this Facebook account (it was removed, or the dialog was not accepted). | Make a new token and accept the dialog. |
| `190/459` | The Facebook account is checkpointed. | Log in at www.facebook.com and complete the security check, then make a new token. |
| `190/464` | The Facebook account is not confirmed. | Log in at www.facebook.com and confirm it, then make a new token. |
| `190/492` | Invalid session: your account no longer has a suitable role on the Page. | Check Page access (Page → Settings → Page access, or Business settings → Accounts → Pages), then make a new token. |
| `190` "Invalid OAuth access token", "Cannot parse access token", "Malformed access token" | The token is invalid: expired, revoked, truncated, or of the wrong kind. | Make a new token. A token of exactly 128 characters was cut by the interactive Keychain prompt: store it with the `"$(pbpaste)"` form. Tokens that start with `IG` come from Instagram Login and do not work here. |
| `102` | The session behind the token is no longer valid. | Make a new token. |
| `! The token is exactly 128 characters long` (`doctor`, `check`) | Meta tokens are about 200 characters; `security add-generic-password ... -w` without a value cuts input at 128: observed (Oct 2026). | Store it again: `security add-generic-password -U -a "$USER" -s ig-publish -w "$(pbpaste)"` (type the line, copy the token, press Enter, then `pbcopy </dev/null`). |
| `! The token starts with "IG"` (`doctor`) | An Instagram Login token (`graph.instagram.com`). ig-publish uses the Instagram API with Facebook Login. | Make the token in the Graph API Explorer ([setup.md, step 4](setup.md#4-generate-the-access-token)). |
| The token works in the Explorer but not here, a few hours later | You stored the short-lived Explorer token (about 1-2 hours), not the extended one. | Extend it in the Access Token Debugger and store the long-lived token. |
| The token leaked (pasted into a chat, a file, a repository, a screenshot) | Anyone with it can post as you until it stops working. | Revoke it now: on Facebook, **Settings & privacy** → **Settings** → **Business integrations** (or **Apps and websites**) → remove your app (later calls fail with `190/458`), or change your Facebook password (`190/460`). Names may differ. Then make a new token and store it again everywhere you keep it (Keychain, file or SSM). |

## Permissions and access

| Error | Meaning | Fix |
|---|---|---|
| `doctor`: `✗ permissions ... is missing (needed to publish)` / `check`: `missing for publishing: ...` | The token lacks `instagram_basic`, `instagram_content_publish` or `pages_read_engagement`. | Graph API Explorer → **Permissions** → **Add a Permission** (sometimes a free-text box: type the name) → generate a new token, tick both the Page and the Instagram account, extend and store it. An existing token never gains permissions. If the permission is not offered, add it to the app: App Dashboard → **Use cases** → **Manage messaging & content on Instagram** → **Customize** → **API setup with Facebook login**. |
| `doctor`: `... was declined in the login dialog` | You unticked that permission in the Facebook dialog. | Generate the token again and use **Edit access** / **Edit settings** in the dialog to allow it. |
| `10`, `200`-`299`, `3` "Permissions error", "Application does not have permission" | A permission is missing or was removed. | `ig-publish doctor` names it; add it as above and generate a new token. |
| `100/33` "Unsupported get request. Object with ID '...' does not exist, cannot be loaded due to missing permissions, or does not support this operation" | The Instagram account (or another object) is not visible to this token. | `[account] ig_user_id` must be the numeric Instagram account ID (`GET me/accounts?fields=name,instagram_business_account` → `instagram_business_account.id`), not the @username or the Page ID; tick both the Page and the Instagram account when you generate the token. |
| `100` "Tried accessing nonexisting field (...) on node type (Page)" | `ig_user_id` holds a Facebook Page ID. | Use the Instagram account ID instead ([setup.md, step 5](setup.md#5-find-the-instagram-account-id)). |
| `✗ Deleting needs the instagram_manage_contents permission` | `delete` needs a permission that is not in the default set. | Add `instagram_manage_contents` to the app and the token (as above), store the new token, run `delete` again. |
| `! pages_show_list is missing` / Page link "not checked" | Without it, `doctor` and `check` cannot list your Pages. Publishing does not need it. | Add `pages_show_list` the next time you generate a token. |

## Account, Page and identity

| Error | Meaning | Fix |
|---|---|---|
| `31/3858385` "Please verify your identity" | Meta wants an identity check on the Facebook account (seen on ad creation: running ads continued while new ones were blocked). | Open [facebook.com/accountquality](https://www.facebook.com/accountquality) (or Ads Manager / Meta Business Suite) and follow the banner. Observed (Oct 2026): it sometimes clears just by visiting. Then run the command again. |
| `2207050` | The Instagram account is inactive, checkpointed or restricted. | Open the Instagram app, sign in and complete what it asks, then run the command again. |
| Stories fail, reels work; `doctor`: `account type ... MEDIA_CREATOR` | A Creator account. Creator accounts cannot publish stories through the API: observed (Oct 2026). | Instagram app → profile → menu → **Settings and activity** → **Account type and tools** → **Switch account type** → **Business**. |
| `doctor`: `Page ... your tasks are ...; publishing needs MANAGE or CREATE_CONTENT` / `check`: `needs MANAGE or CREATE_CONTENT` | Your Facebook account has no content role on the Page. | Page → **Settings** → **Page access**: full control or task access for content (Business portfolio: Business settings → **Accounts** → **Pages** → the Page → **People** → Content or Full control). Then make a new token. |
| `doctor`: `Page ... is linked to Instagram account X, not to Y` / `check`: `this Page is linked to Instagram account ...` | `ig_user_id` names a different account than the Page's. | If X is your account, set `[account] ig_user_id = "X"`; otherwise link the right account (below). |
| `doctor`: `no Page linked to Instagram account ... was found` / `check`: `! No Page linked to Instagram account` | The account is not linked to a Page you can see, the Page was not ticked in the token dialog, or your Page access comes through a Business portfolio. | Link it: Facebook → your Page → **Settings** → **Linked accounts** → **Instagram** → **Connect** (or Instagram app → **Edit profile** → **Page**). Generate the token again with both the Page and the account ticked. Through a Business portfolio: also grant `business_management` (Meta's docs then ask for `ads_read` or `ads_management` too) and set `[account] page_id`. |
| `doctor`: `... is @someone, the configuration expects @other` / `check`: `expected @...` | The token reaches a different Instagram account than `[account] username` says. | Correct `ig_user_id` or `username`; with several accounts, use one configuration and one state file per account. |
| Publishing fails on a Page that requires Page Publishing Authorization | Some Pages need Page Publishing Authorization (PPA) before anything can be published for them. | Complete PPA in the Page settings, then run the command again. |

## Publishing

ig-publish uploads every container of a run and waits until all are `FINISHED` before it publishes anything; after
any `media_publish` error it reads the container first. A `PUBLISHED` container is recorded and never published
again.

| Error | Meaning | Fix |
|---|---|---|
| HTTP `5xx`, codes `1`, `2`, `-1`, `-2`, `is_transient`, a network error or an answer without a media ID (`media_publish answered without a media ID`) on `media_publish`; `Stopped: KEY: the media_publish outcome is unknown` | Temporary Meta error. The post may have gone out anyway: `media_publish` can answer 5xx although the post is live. | **Never repeat the call by hand.** Run the same ig-publish command later: it reads the container first, records `PUBLISHED`, and publishes only a `FINISHED` container. |
| `2207008` | Temporary error while publishing a container. | ig-publish retries once or twice (30 s - 2 min apart, Meta's guidance). If it persists, run the same command later; it makes a new container if Meta asks. |
| `2207027` "The media is not ready for publishing" | The container is not `FINISHED` yet. | Run the same command; ig-publish waits for `FINISHED` before publishing. |
| `2207006`, `2207020`, `2207032`, `2207053` | The container cannot be used any more (not found, expired, creation or upload failed). | Run the same command: the item gets a new container; nothing is posted twice. |
| `2207026` (unsupported video format); `Stopped: KEY: container ERROR` | Instagram rejected the video. Nothing in the run was published. | `ig-publish prep` (or check your own export with `ig-publish verify FILE --kind story` or `--kind reel`), then run the same command. |
| `2207052`, `9004` (media could not be fetched) | Instagram could not read the media. With resumable uploads this usually means the upload was incomplete. (With Instagram Login and a `video_url`, Meta's fetcher sometimes rejects valid URLs.) | Run the same command: a fresh container is uploaded. For big files or a slow uplink raise `[timing] upload_timeout_seconds`. |
| `Stopped: N container(s) did not become FINISHED within ... min` | Meta was still processing. Nothing was published. | Run the same command (it reuses the containers); for long reels raise `[timing] poll_timeout_seconds`. |
| `✗ KEY: media_publish was refused - not published` | A permanent refusal for that container. | Read Meta's message in the same output. If it says Meta wants a new container, the same command makes one. |
| `✗ KEY: the container answer has no id` | Meta answered without a container ID. Nothing was published. | Run the same command later; report it if it repeats. |
| HTTP 200 with an `error` object in the body | Meta sometimes answers 200 with an error inside: observed (Oct 2026). ig-publish treats it as the error it contains. | Look up the code in this page. |
| `Stopped: KEY: container ... was sent to media_publish before ... and cannot be read now` | An earlier attempt may have published; the container is unreadable now, so replacing it could post twice. | Run the same command later. If the post is live, record it with `ig-publish import`; only if you are sure it is not live, delete that item's entry from the state file. |
| `read back: ... ✗ expected STORY` or `read-back failed (the post is live and recorded)` | The post is live and recorded; only the read-back did not match or failed. | Nothing to fix for publishing; `ig-publish status` looks the details up again. |

## Rate limits and spam protection

| Error | Meaning | Fix |
|---|---|---|
| `2207051` "action blocked", "We restrict certain activity" | Instagram treated the activity as spam. | ig-publish holds for 24 hours. Do not post by hand meanwhile; check the Instagram app for a notice ("Tell us" if it is a mistake). Afterwards post less in a row: at most about 10 posts in 3 hours, one or two reels a day. |
| `2207042` | The account reached its API publishing limit (a rolling 24-hour quota; observed (Oct 2026) `quota_total` 100, some Meta docs say 50; stories count). | Wait. ig-publish holds until `content_publishing_limit` has room; `ig-publish status` shows the quota. |
| `4`, `17`, `32`, `613`, `80001`, `80002`, `80004`, HTTP `429` | Rate limited (app, user, Page, Instagram or ads use-case limits). | When `media_publish` was refused, ig-publish holds for at least an hour (`[safety] rate_hold_minutes`) or Meta's `estimated_time_to_regain_access` when that is longer. On a read (`doctor`, `check`, `plan`, the preflight of `publish --apply`) nothing is held: wait about an hour and run the same command again. Either way, keep other tools on the same app or account quiet meanwhile. |
| `341` "Application limit reached" | Temporary throttling of the app. | Wait, then run the same command. |
| `368` "Temporarily blocked for policies violations" | A temporary block. | Stop posting, check the Instagram and Facebook apps for notices, wait before trying again. |
| `Stopped: Meta API usage is at N% (threshold 85%)`; `doctor`: `✗ API usage` and the remaining checks `not run` | Meta's usage headers (`X-App-Usage`, `X-Business-Use-Case-Usage`) crossed `[api] usage_stop_percent`, so ig-publish sent nothing more. | The window rolls over within about an hour; the same command continues where it stopped (run `ig-publish doctor` again for the checks it could not run). |
| `Stopped: a hold is active` (exit 75) | A hold from an earlier stop code is still running. | Wait until the time shown; the scheduler retries. Override only knowingly: `--ignore-hold`. |

## Uploads and network

| Error | Meaning | Fix |
|---|---|---|
| Upload timed out / network error during `UPLOAD` | Uploads to `rupload.facebook.com` can time out mid-file: observed (Oct 2026). | Run the same command: the item gets a new container and nothing is posted twice. For big files or a slow uplink raise `[timing] upload_timeout_seconds` (default 900). |
| `upload not confirmed (no "success": true in the answer)` | rupload answered without confirming the bytes. | Run the same command: a new container is uploaded. |
| `Unexpected upload address; the token was not sent` | The API returned an upload URL on another host; ig-publish refuses to send the token there. | Run again later; report it if it repeats (replace IDs and paths in the output with placeholders). |
| `no answer \| message: network: ...` on a read; `doctor`: `✗ Meta API reachable` | No connection to `graph.facebook.com`. | Check the connection, VPN or proxy. In `publish --apply` a read error before the first write exits 75 and is safe to repeat. |
| `HTTP 302` / any 3xx | ig-publish never follows redirects (the token would travel along). | Check for a proxy or captive portal between you and `graph.facebook.com`. |

## Preflight stops

Before the first write of `publish --apply`, ig-publish compares the live account with its state file. These stops
write nothing; most exit 75.

| Error | Meaning | Fix |
|---|---|---|
| `N post(s) in the last 24 h are NOT in the state file` (exit 75) | Posts made by hand, by another tool or with another state file. | Expected: `ig-publish ack --apply` records them (nothing is posted). Manifest items posted elsewhere: `ig-publish import RECORD.json --apply`. One run only: `--ack-unknown-posts`. |
| `Stopped: the quota counts N API posts in the last 24 h, the state file only M` (exit 75) | Another tool may be publishing through the API. | As above (`ack` / `import`), or `--ack-unknown-posts` for one run. |
| `a live post already has this exact caption` (exit 1) | A reel with the same caption is live: probably a duplicate. | If you do mean to post it again: `--repost KEY`. |
| `N posts in the last 3 h (>= 10)` (exit 75) | The burst guard: posting in bursts raises the `2207051` risk (our heuristic). | Wait until the time shown; `[safety]` tunes it; `--ignore-hold` overrides knowingly. |
| `last feed/reel post ...; reel spacing is 30 min` (exit 75) | Reel spacing across runs (our heuristic). | Wait; `[spacing] reel_seconds` or `--reel-gap` changes it. |
| `Stopped: not enough quota: N left, M selected` (exit 75) | The rolling 24-hour quota cannot take the whole selection. | Wait, or narrow the run (`--limit N`). |
| `season "x" opens ...` (exit 75) / `the window of season "x" closed ...` (exit 1) | The item is tagged with a season whose window is not open. | Wait for the window, or edit `[seasons.x]` / the item's `season`. |
| `✗ Not prepared: KEY (...)` (exit 1) | The media is missing, changed since `prep`, or the prep settings changed. | `ig-publish prep`. |
| `--keys: ... cannot be published now (reason above)` | Selected keys are published together or not at all; one is parked (deleted, awaiting approval ...). | Fix the reason shown (`--approve KEY`, `--repost KEY`) or leave that key out. |

## State, lock and delete

| Error | Meaning | Fix |
|---|---|---|
| `Stopped: another ig-publish run holds the lock` (exit 75) | One writer at a time per state file. | Wait for the other run; the scheduler retries. |
| `The state file ... is not valid JSON` | The record of what was published is damaged. | Restore it from your backup. Publishing without it could post things twice. |
| `The state file ... belongs to Instagram account ...` | One state file per account. | Point `[paths] state` at this account's own state file. |
| `Stopped: the state file ... is missing, but the journal ... records N publish/import(s)` | The state file was lost or `[paths] state` moved. | Restore the state file, or point `[paths] state` back at it. |
| `doctor`: `... is not writable for this user` | Publishing could not record what it posted. | `sudo chown -R "$(id -u)" DIR`; for the Docker image the owner must be uid 10001. |
| `KEY: the media ID is only a CANDIDATE` | A story found by time only; it could be another post. | Check it on Instagram, then `ig-publish delete KEY --confirm-media ID --apply`. |
| `KEY: the media ID is unknown` / `posted outside this tool` | ig-publish cannot identify the post safely. | Delete it in the Instagram app. |

## Token sources

| Error | Meaning | Fix |
|---|---|---|
| `No access token: environment variable IG_ACCESS_TOKEN is empty or not set` | Source `env` (the default) and the variable is not set in this shell. | `read -rs IG_ACCESS_TOKEN && export IG_ACCESS_TOKEN` (paste; nothing is echoed or kept in the history), or configure a lasting source ([setup.md, step 6](setup.md#6-store-the-token-safely)). |
| `No Keychain item for service 'ig-publish' and account '...'` | Nothing stored yet under that name. | Type `security add-generic-password -U -a "$USER" -s ig-publish -w "$(pbpaste)"` without pressing Enter, copy the token, press Enter, then `pbcopy </dev/null`. |
| `The Keychain could not be read (... "User interaction is not allowed" ...)` (exit 75 before writes) | The item may well be there, but the login keychain is locked and there is no desktop session to unlock it (SSH, launchd, cron). | Unlock it (`security unlock-keychain`) or run ig-publish from a logged-in desktop session. For unattended runs use `source = "file"`. Do not store the token again. |
| `The Keychain did not answer within 30 s` (exit 75 before writes) | The keychain is locked or a permission dialog is waiting. | Unlock it (`security unlock-keychain`) or answer the dialog. For unattended runs (scheduler, SSH) use `source = "file"`. |
| `Token source "keychain" needs the macOS security tool` | The Keychain exists on macOS only. | Use `source = "file"` or `"ssm"` elsewhere. |
| `The value in ... is a shell command, not a token` | The clipboard still held the command when it was stored (two lines pasted at once, or command and token pasted together). | Store it again with the command printed under the error (Keychain: type the command, then copy the token, then press Enter). |
| `The access token from ... contains whitespace` / `is wrapped in quotes` | Something besides the token was stored. | Store only the token: one line, no spaces, no quotes. |
| `Token file not found: PATH` (exit 75 before writes) | The file does not exist (yet). On a server the boot unit writes it. | `mkdir -p -m 700 "$(dirname PATH)" && read -rs T && (umask 077 && printf '%s' "$T" > PATH); unset T`, or check the token unit (`systemctl status ig-publish-token`, [deploy-ec2.md](deploy-ec2.md)). |
| `Token file PATH is accessible to group/others (mode 0644)` | The file must be private. | `chmod 600 PATH` |
| `Token file PATH is owned by uid N, not by the current user` / `Cannot open token file` | Another user owns it. | `sudo chown "$(id -u)" PATH`, or run ig-publish as that user (the Docker image runs as uid 10001). |
| `Token file PATH is not a regular file` / `larger than 1 MiB` / `not UTF-8 text` | The path points at something that is not a token file. | Point `[token] path` at a file that holds only the token. |
| `` `aws ssm get-parameter` failed (exit N) `` (exit 75 before writes) | Wrong name or region, or the machine may not read it. | Check `[token] ssm_parameter` / `ssm_region`; IAM needs `ssm:GetParameter` on the parameter, plus `kms:Decrypt` for a customer-managed key. |
| `` `aws ssm get-parameter` did not finish within 30 s `` | No route to SSM, or the instance credentials are not available yet. | Check the network (internet or a VPC endpoint); raise `[token] timeout_seconds` on a slow network. |
| `Token source "ssm" needs the AWS CLI` | `aws` is not installed. | Install the AWS CLI v2, or let a boot unit write a 0600 file and use `source = "file"`. |
| `IG_PUBLISH_TEST_TOKEN is set, but the API endpoints are the real Meta ones` | A test-suite variable leaked into your shell. | `unset IG_PUBLISH_TEST_TOKEN` |
| `IG_PUBLISH_API_BASE / IG_PUBLISH_UPLOAD_BASE exist only for local test servers` | Test-only overrides are set. | `unset IG_PUBLISH_API_BASE IG_PUBLISH_UPLOAD_BASE` |

## Configuration, manifest and FFmpeg

| Error | Meaning | Fix |
|---|---|---|
| `No configuration file` | No `ig-publish.toml` / `ig-publish.json` here, no `--config`, no `IG_PUBLISH_CONFIG`. | `ig-publish init` in your project folder, or pass `--config PATH`. |
| `Unknown key(s) in [section]: ...` / `Unknown section(s)` | A typo. Unknown keys are errors on purpose, so a typo never switches a guard off. | Fix the key; the annotated reference is [`examples/ig-publish.toml`](../examples/ig-publish.toml). |
| `invalid TOML: Cannot declare ('token',) twice` | A section header appears twice: `ig-publish init` already wrote `[account]` and `[token]`, and a second copy was added. | Move the keys into the existing section and delete the second header. |
| `invalid TOML: Cannot overwrite a value` | A key appears twice in one section. | Keep one of the two lines. |
| `[account] ig_user_id ... is not valid` / `is required` | It must be the numeric Instagram account ID. | [setup.md, step 5](setup.md#5-find-the-instagram-account-id). |
| `Manifest errors in ...` | Invalid items: unknown fields, a missing source file, caption rules, a `src` matching `[sources] deny`, an unknown season. Right after `ig-publish init` the three example items have no files (`doctor` says so). | Replace the examples with your own files; fix the listed entries (`src` is relative to `[paths] source_root`); `ig-publish plan --offline` checks again. |
| `The manifest is for Instagram account ..., the configuration for ...` | Manifest and configuration name different accounts. | Use the matching pair. |
| `ffmpeg and ffprobe not found on PATH` | FFmpeg is not installed. Only `prep` and `verify` need it. | macOS `brew install ffmpeg`; Debian 12 / Ubuntu 24.04 `sudo apt-get install ffmpeg`; older systems: the Docker image or a static build. |
| `FFmpeg 5.1 or newer is needed for prep (found 4.4)` | `prep` uses `-fps_mode` (FFmpeg 5.1+). Ubuntu 22.04 ships 4.4. | Use the Docker image or a static FFmpeg build. |
| `doctor`: `FFmpeg ... lacks libx264` (or `aac`, or a filter) | A minimal FFmpeg build. | Install a full build (Homebrew `ffmpeg`, the Debian/Ubuntu `ffmpeg` package) or use the Docker image. |
| `✗ N file(s) failed verification` / `ffprobe could not read FILE` / `ffmpeg failed for FILE` | A source cannot be converted to the Instagram profile. | Read the problems listed per file; `ig-publish verify FILE --kind story` shows what is missing; re-export the source. |
| `Reading a TOML configuration on Python 3.10 needs the tomli package` | Python 3.10 has no built-in TOML reader. | `pip install tomli` (normally installed with ig-publish), or use `ig-publish.json`. |
| `[schedule] timezone 'X' is not a known IANA time zone` | A wrong name, or no time zone data. | Use a name like `Europe/Berlin` or `UTC`; on minimal systems install the `tzdata` package. |

## Scheduler

| Error | Meaning | Fix |
|---|---|---|
| `` `schedule run` publishes the due lines ... Add --apply `` (exit 2) | `run` and `once` publish, so they need `--apply`. | `ig-publish schedule run --apply`; to preview: `ig-publish schedule dry`. |
| `MISSED, not published: KEYS - ... past its time` | The line was more than `max_late_hours` late (a sleeping laptop, a reboot). Nothing was posted in a burst. | Add a line with a new time. Run the scheduler on a server; a Mac stops when the lid closes. |
| `Waiting: KEYS - ...` / `RETRY:` | The run stopped before writing anything (exit 75). | Usually nothing to do: it retries every `retry_seconds` until `max_late_hours`, then marks the line `missed`. If the message has a token or permission `fix:` line (an expired token, for example), act on it before `max_late_hours` and the line still goes out. |
| `FAILED: KEYS - exit N: ...` | A stop after a write, or a permanent error. Not retried. | Read the reason (and its `fix:`) in the notification and the schedule log; to retry the line, delete its lines from `state/schedule.done`. |
| `WARNING: notify_command exited 1` | The notifier failed; the scheduler keeps running. | Check `[schedule] notify_command`; for the webhook example the URL file must be yours with mode `0600`. |

Something missing here? Open an issue with the output of `ig-publish doctor` after replacing your name, the
account and Page IDs, the @username and paths with placeholders (ig-publish redacts only tokens).
