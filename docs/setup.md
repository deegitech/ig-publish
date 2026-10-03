# Setup from zero (about 15 minutes)

This guide takes you from nothing to a first dry run: an Instagram account that the API can publish to, a Meta
app, a long-lived access token stored safely, and `ig-publish doctor` confirming every piece. Each step says
exactly where to click.

**Before you start**

- Meta renames console screens often. Every click path below gives the menu path plus the labels we have seen.
  **Names may differ** on your screen; look for the closest match.
- **observed (Oct 2026)** marks platform behaviour we saw while running ig-publish in production but did not find
  in Meta's documentation. Everything else follows Meta's documentation at the time of writing.
- The token is a password for your account. In every step it goes from the Meta page straight into a secret store:
  never into your project files, a chat or an email, and you never type it out on a command line.
- All IDs and names here are placeholders: `17841401234567890` (Instagram account), `1234567890` (Facebook Page),
  `you@example.com`.

| Step | What you do | Time |
|---|---|---|
| [0](#0-prerequisites) | Check the prerequisites | 1 min |
| [1](#1-install-ig-publish) | Install ig-publish and start a project | 2 min |
| [2](#2-prepare-the-instagram-account-and-the-page) | Business account, linked Page, story archive | 2 min |
| [3](#3-create-the-meta-app) | Create the Meta app and add the permissions | 4 min |
| [4](#4-generate-the-access-token) | Generate the token and extend it to about 60 days | 3 min |
| [5](#5-find-the-instagram-account-id) | Find the numeric Instagram account ID | 1 min |
| [6](#6-store-the-token-safely) | Store the token (Keychain, file, SSM or environment) | 1 min |
| [7](#7-check-the-setup-with-doctor) | Put your files into the manifest, run `ig-publish doctor` | 1 min |
| [8](#8-your-first-run) | Your first run: dry run first, then one post | - |
| [9](#9-token-expiry-and-renewal) | Token expiry and renewal | - |

Stuck on an error? [troubleshooting.md](troubleshooting.md) lists every error ig-publish knows, with its fix.

## 0. Prerequisites

- **An Instagram professional account of type Business.** Creator accounts cannot publish stories through the
  API: observed (Oct 2026). Step 2 shows how to switch; it is free and reversible.
- **A Facebook Page linked to that Instagram account**, and a Facebook account (yours) with full control or
  content access on that Page.
- **A Meta developer account**: sign in at [developers.facebook.com](https://developers.facebook.com) with that
  Facebook account and register (**Get started**; Meta may ask you to verify the account, for example with a phone
  number, and to accept the platform terms). It is free.
- Two-factor authentication on that Facebook account is strongly recommended: the token acts as you (Settings →
  Accounts Center → Password and security → Two-factor authentication; names may differ).
- **A computer** with macOS or Linux, Python 3.10 or newer and FFmpeg 5.1 or newer (Windows: use WSL or Docker).
- **No paid plan, no App Review and no business verification.** Your app can stay in **Development** mode: it works
  for every Facebook account that has a role on the app, and you, as its creator, are its admin.

## 1. Install ig-publish

```bash
pipx install "git+https://github.com/deegitech/ig-publish.git"
mkdir my-posts && cd my-posts
ig-publish init          # writes ig-publish.toml, manifest.json and schedule.txt (never overwrites)
```

No pipx yet? Install it first: `brew install pipx && pipx ensurepath` (macOS) or
`sudo apt install pipx && pipx ensurepath` (Debian, Ubuntu), then open a new terminal so that `ig-publish` is on
your `PATH`.

Or without pipx, in a virtual environment (Debian, Ubuntu: `sudo apt install python3-venv` first):

```bash
python3 -m venv ~/.venvs/ig-publish
~/.venvs/ig-publish/bin/pip install "git+https://github.com/deegitech/ig-publish.git"
. ~/.venvs/ig-publish/bin/activate     # in every new terminal; then `ig-publish` works
```

(Or link the command into a folder on your `PATH` once:
`mkdir -p ~/.local/bin && ln -s ~/.venvs/ig-publish/bin/ig-publish ~/.local/bin/`.)

FFmpeg: `brew install ffmpeg` (macOS) or `sudo apt-get install ffmpeg` (Debian 12, Ubuntu 24.04; Ubuntu 22.04
ships FFmpeg 4.4, which is too old: use the Docker image or a static build).

## 2. Prepare the Instagram account and the Page

1. **Switch to a Business account.** Instagram app → your profile → menu (☰) → **Settings and activity** →
   **Account type and tools** → **Switch to professional account** → pick a category → **Business**.
   Already a Creator account: **Account type and tools** → **Switch account type** → **Switch to business
   account**. (Names may differ.)
2. **Link the account to your Facebook Page**, one of:
   - Facebook → switch into your Page → **Settings** → **Linked accounts** → **Instagram** → **Connect account**;
   - Instagram app → **Edit profile** → **Page** (under *Public business information*) → connect your Page;
   - Meta Business Suite → **Settings** → **Instagram accounts** → **Connect**.

   (Names may differ.)
3. **Check your role on the Page.** Your Facebook account needs full control or task access for content.
   Page → **Settings** → **Page access** (or **Page roles** on older Pages). If the Page belongs to a Business
   portfolio: [business.facebook.com/settings](https://business.facebook.com/settings) → **Accounts** → **Pages** →
   your Page → **People** (or **Assign people**) → **Content** or **Full control**. (Names may differ.)
4. **Keep the story archive on**, so that API stories can be added to Highlights later: Instagram app → menu →
   **Settings and activity** → **Archiving and downloading** → **Save story to archive** on. API-published stories
   do go to the archive: observed (Oct 2026). (Names may differ.)

## 3. Create the Meta app

1. [developers.facebook.com](https://developers.facebook.com) → **My Apps** → **Create App**.
2. **App details**: an app name (only you see it, e.g. `my-publisher`) and a contact email (`you@example.com`) →
   **Next**.
3. **Use cases**: choose **Manage messaging & content on Instagram** → **Next**. In the older wizard: **Other** →
   app type **Business**. Reusing an existing app is fine too.
4. **Business**: pick your Business portfolio, or **I don't want to connect a business portfolio yet** → **Next**
   → **Create app** (Meta may ask for your Facebook password).
5. In the app dashboard: **Use cases** (left menu) → **Manage messaging & content on Instagram** → **Customize**
   (or **Edit**) → **API setup with Facebook login** (not *with Instagram login*) → in the permissions list,
   **Add** each of:

   | Permission | Why |
   |---|---|
   | `instagram_basic` | read the account |
   | `instagram_content_publish` | publish stories and reels |
   | `pages_read_engagement` | read the Page linked to the account |
   | `pages_show_list` | list your Pages (`doctor` and `check` use it to verify the link) |
   | `business_management` | optional: needed when your access to the Page comes through a Business portfolio |
   | `instagram_manage_contents` | optional: only `ig-publish delete` needs it; it is not in the default set |

   Permissions often have to be added one by one. If your Page role comes through a Business portfolio, Meta's
   documentation also asks for `ads_read` or `ads_management`; those come with the use case **Create & manage ads
   with Marketing API** (names may differ).
6. Leave the app in **Development** mode. If someone else will generate tokens, add them under **App roles** →
   **Roles** → **Add People** (Developer or Tester). (Names may differ.)

> Why Facebook Login and not Instagram Login? With Facebook Login (`graph.facebook.com`) ig-publish uploads your
> files directly to Meta (resumable upload), so no public hosting is needed. The Instagram Login API
> (`graph.instagram.com`) needs a public `video_url` for every file, and Meta's media fetcher sometimes rejects
> valid URLs (error `2207052` / `9004`). Instagram Login tokens (they start with `IG`) do not work with ig-publish.

## 4. Generate the access token

1. Open the [Graph API Explorer](https://developers.facebook.com/tools/explorer) (developers.facebook.com →
   **Tools** → **Graph API Explorer**).
2. Right-hand panel: **Meta App** → your app. **User or Page** → **User Token** (or **Get User Access Token**).
3. **Permissions** → **Add a Permission**: add `instagram_basic`, `instagram_content_publish`, `pages_show_list`,
   `pages_read_engagement` (and `business_management` / `instagram_manage_contents` if you added them in step 3).
   The picker is sometimes a free-text box rather than a dropdown: then type each name.
4. **Generate Access Token**. A Facebook dialog opens: continue as yourself, and when it asks which Pages and
   which Instagram accounts the app may use, **tick BOTH your Facebook Page AND your Instagram account**. Ticking
   only one of them is the most common setup mistake. Save.
5. The token in the Explorer is short-lived (about an hour or two). Click the **(i)** next to it → **Open in
   Access Token Tool**. In the [Access Token Debugger](https://developers.facebook.com/tools/debug/accesstoken),
   click **Extend Access Token** at the bottom (Meta may ask for your password). A long-lived token appears,
   valid for **about 60 days**; the debugger shows the expiry date. Note that date for step 9.
6. Leave that page open. You copy the long-lived token in step 6, at the moment the store command needs it.

## 5. Find the Instagram account ID

ig-publish needs the numeric ID of the Instagram account, not the @username and not the Page ID.

1. In the Graph API Explorer, replace the query with `me/accounts?fields=name,instagram_business_account` and click
   **Submit**.
2. The answer lists your Pages:

   ```json
   {"data": [{"name": "Your Page", "instagram_business_account": {"id": "17841401234567890"}, "id": "1234567890"}]}
   ```

3. Put the IDs into `ig-publish.toml`. `ig-publish init` already wrote an `[account]` section: edit the lines in
   it (remove the `#` in front of `username` and `page_id` if you use them). Do not add a second `[account]`
   header; TOML refuses a section that appears twice.

   ```toml
   [account]
   ig_user_id = "17841401234567890"   # instagram_business_account.id, in quotes
   username = "your.brand"            # optional: check and doctor fail if the token sees another account
   page_id = "1234567890"             # optional: the Page's "id"
   ```

No `instagram_business_account` in the answer? The Instagram account is not linked to that Page (step 2.2), you did
not tick it in the dialog (step 4.4), or it is not a professional account (step 2.1).

An empty `data` list? Your Page role probably comes through a Business portfolio. Add `business_management` to the
token (step 4.3) and ask the Page directly: `1234567890?fields=instagram_business_account` with your Page ID (Page
→ **About** → **Page transparency**, or Business settings → **Accounts** → **Pages**; names may differ).

## 6. Store the token safely

Pick one place. ig-publish reads the token from there on every run and never writes it anywhere.

| Where | Good for | `[token]` in ig-publish.toml |
|---|---|---|
| [macOS Keychain](#macos-keychain) | a Mac you work on | `source = "keychain"`, `keychain_service = "ig-publish"` |
| [Environment variable](#environment-variable) | a quick test in one terminal | `source = "env"` (the default) |
| [A file only you can read](#a-0600-file) | Linux, Docker, a Mac over SSH | `source = "file"`, `path = "..."` |
| [AWS SSM Parameter Store](#aws-ssm-parameter-store) | servers on AWS | `source = "ssm"`, `ssm_parameter = "..."` |

### macOS Keychain

```bash
security add-generic-password -U -a "$USER" -s ig-publish -w "$(pbpaste)"
```

1. Type or paste this line into the terminal, but **do not press Enter yet**.
2. Copy the long-lived token in the Access Token Debugger.
3. Press Enter. Then clear the clipboard: `pbcopy </dev/null`.

Why exactly like this:

- **Never leave `-w` without a value.** `security` then asks for the password interactively and silently cuts the
  input at 128 characters; Meta tokens are about 200 characters long: observed (Oct 2026). `doctor` flags a token
  of exactly 128 characters.
- **Never paste the command and the token in one go**, and never paste two lines at once: the shell runs the first
  line while the clipboard still holds the command text, and that text is stored as the "token". `doctor` and
  every command recognise a stored command and say so.
- The shell history records the literal text `"$(pbpaste)"`, not the token. `-U` replaces an existing item, so the
  same line renews the token later.
- If you use a clipboard manager, delete the token from its history too.
- For the instant `security` runs, the token is an argument of that process, and other users of the same Mac can
  see process arguments. On a shared Mac, use [a 0600 file](#a-0600-file) instead.

Then point ig-publish at it. `ig-publish init` already wrote a `[token]` section into `ig-publish.toml`: change
the lines in that section (and remove the `#` in front of the ones you need); do not add a second `[token]` header,
and never put the token itself in this file:

```toml
[token]
source = "keychain"
keychain_service = "ig-publish"
```

### Environment variable

For a quick test in the current terminal only:

```bash
read -rs IG_ACCESS_TOKEN && export IG_ACCESS_TOKEN
```

Paste the token and press Enter; nothing is shown and nothing goes into the shell history. The variable is gone
when the terminal closes. This is the default source (`source = "env"`).

### A 0600 file

```bash
install -d -m 700 ~/.ig-publish
read -rs T && (umask 077 && printf '%s' "$T" > ~/.ig-publish/token); unset T
```

In the existing `[token]` section of `ig-publish.toml`:

```toml
[token]
source = "file"
path = "~/.ig-publish/token"
```

ig-publish refuses the file unless it is a regular file, owned by the user that runs ig-publish, with mode `0600`
or stricter. With Docker the container user is uid 10001: see
[Running on a server](../README.md#running-on-a-server).

### AWS SSM Parameter Store

```bash
read -rs T && printf '%s' "$T" | aws ssm put-parameter --region us-east-1 \
    --name /example/ig-publish/access-token --type SecureString --value file:///dev/stdin --overwrite; unset T
```

In the existing `[token]` section of `ig-publish.toml`:

```toml
[token]
source = "ssm"
ssm_parameter = "/example/ig-publish/access-token"
ssm_region = "us-east-1"
```

The machine needs `ssm:GetParameter` on that parameter (plus `kms:Decrypt` for a customer-managed key). On EC2 the
[deployment guide](deploy-ec2.md) instead lets a boot unit write the token to a tmpfs file, so the container never
holds AWS credentials.

## 7. Check the setup with doctor

First put your own files into `manifest.json`. `ig-publish init` wrote three example items there whose files do not
exist, so `doctor` reports `✗ manifest` until you replace them (the format: README,
[The manifest](../README.md#the-manifest)):

```bash
$EDITOR manifest.json   # your files, in publishing order; src paths are relative to the project folder
ig-publish doctor
```

`doctor` runs every check in setup order and only reads: it writes no file and no request changes anything on
Meta's side. It never prints the token, only its length. Every `✗` has a `fix:` line under it; `!` marks a note that
does not fail; `-` marks a check skipped because an earlier one failed. Exit code 0 means no problems, 1 means at
least one `✗`. `ig-publish doctor --offline` runs only the local checks (no token, no network).

A healthy setup, before the first `prep` (placeholders):

```text
$ ig-publish doctor
ig-publish doctor: read-only checks in setup order. Nothing is written; the token is never shown.
ig-publish 0.1.0 | Python 3.12.4 | Darwin arm64

Local setup
  ✓ configuration      ig-publish.toml | Instagram account 17841401234567890 (@your.brand) | token from macOS Keychain service 'ig-publish'
  ✓ manifest           manifest.json | 3 item(s): 2 stories, 1 reel(s); every source file found
  ✓ ffmpeg / ffprobe   FFmpeg 7.1.1 | libx264, aac and the filters prep uses
  ! prepared media     3 of 3 pending item(s) not prepared yet: teaser-1, teaser-2, reel-launch
      fix: ig-publish prep
  ✓ state              state/ig-state.json: not created yet (the first publish --apply creates it, mode 0600)

Token
  ✓ token              read from macOS Keychain service 'ig-publish': 201 characters (the value is never shown)

Meta API (read-only requests)
  ✓ token works        owner Your Name (1000001) | Graph v26.0
  ✓ permissions        instagram_basic, instagram_content_publish, pages_read_engagement (instagram_manage_contents not granted: only `delete --apply` needs it)
  ✓ Instagram account  @your.brand (17841401234567890)
  ! account type       not reported by the API. Stories need a Business account. Creator accounts cannot publish stories through the API: observed (Oct 2026)
      fix: check it once in the app: Instagram app -> your profile -> menu -> Settings and activity -> Account type and tools -> Switch account type -> Business (names may differ). https://github.com/deegitech/ig-publish/blob/main/docs/setup.md#2-prepare-the-instagram-account-and-the-page
  ✓ Page link          Page Your Page (1234567890) is linked to the account | your tasks include CREATE_CONTENT, MANAGE
  ✓ quota              0/100 API posts used in the rolling 24 h | 100 left
  ✓ API usage          highest Meta usage header 2% (ig-publish stops sending at 85%)

✓ doctor: no problems found (13 checks, 2 note(s) marked !). Next: ig-publish prep && ig-publish plan
Reminder: a long-lived token lasts about 60 days from when you extended it. The Access Token Debugger shows its expiry date; renew it before then: https://github.com/deegitech/ig-publish/blob/main/docs/setup.md#9-token-expiry-and-renewal
```

And what a problem looks like (here a permission was not added before the token was generated):

```text
Meta API (read-only requests)
  ✓ token works        owner Your Name (1000001) | Graph v26.0
  ✗ permissions        instagram_content_publish is missing (needed to publish)
      fix: Graph API Explorer (developers.facebook.com/tools/explorer) -> your app -> Permissions -> "Add a Permission" (sometimes a free-text field: type the name) -> instagram_content_publish -> Generate Access Token -> in the dialog tick BOTH the Facebook Page and the Instagram account -> extend the token and store it again (an existing token never gains permissions): https://github.com/deegitech/ig-publish/blob/main/docs/setup.md#4-generate-the-access-token
           Not offered? Add it to the app first: App Dashboard -> Use cases -> "Manage messaging & content on Instagram" -> Customize -> "API setup with Facebook login" (names may differ): https://github.com/deegitech/ig-publish/blob/main/docs/setup.md#3-create-the-meta-app
  - Meta API           the remaining checks need the publishing permissions

✗ doctor: 1 problem(s): permissions. Fix them from the top (each fix is printed under its ✗), then run `ig-publish doctor` again.
```

Asking for help with this output? ig-publish redacts only the token: replace your name, the account and Page IDs,
the @username and any paths with placeholders before you post it anywhere.

What `doctor` checks, in order:

| Check | Fails when |
|---|---|
| configuration | no `ig-publish.toml` / `ig-publish.json`, or an invalid or unknown key |
| manifest | invalid items, a missing source file, a caption rule broken |
| ffmpeg / ffprobe | missing, older than 5.1, or a build without `libx264`, `aac` or the filters `prep` uses |
| prepared media | never fails; notes the pending items that still need `ig-publish prep` |
| state | the state directory is not writable, the state file is unreadable or belongs to another account, or it is missing while the journal records publishes (an active hold is a note) |
| token | the configured source has no token, the file is not private, the Keychain item is missing or the keychain is locked, the stored value is a shell command or has quotes or spaces (a 128-character or `IG…` token is a note) |
| token works | `GET /me` fails (expired, revoked, truncated token ...); the fix depends on Meta's error code. No answer, a redirect, a rate limit or a temporary Meta error shows as `Meta API reachable` instead |
| permissions | `instagram_basic`, `instagram_content_publish` or `pages_read_engagement` is missing or declined |
| Instagram account | `ig_user_id` cannot be read with this token, or it is another @username than `[account] username` |
| account type | the API reports a non-Business account and the manifest has stories (when the API does not report the type: a note) |
| Page link | the Page is linked to another Instagram account, your tasks lack MANAGE / CREATE_CONTENT, or no linked Page is found (a note when the Instagram account itself is readable) |
| quota | `content_publishing_limit` cannot be read (no room left is a note: it passes with time) |
| API usage | Meta's usage headers reached `[api] usage_stop_percent` before every check ran: the rest are listed as `-` not run, and `doctor` asks you to run it again in about an hour (reaching it only after the last check is a note) |

`ig-publish check` is the shorter account summary from before `doctor` existed; it stays available.

## 8. Your first run

Dry runs first. Nothing is published without `--apply`.

```bash
ig-publish prep                      # re-encode to media/<key>.mp4 and verify (local only)
ig-publish plan                      # the queue, the live-account preflight and what --apply would do
ig-publish publish                   # the real command as a dry run: reads the account, writes nothing
ig-publish publish --canary --apply  # publish only the first pending item
ig-publish status                    # what is live, when stories leave the tray, the quota
```

Then publish the rest with `ig-publish publish --apply`, or schedule it (README: "Scheduler"; for a scheduler that
runs around the clock use a server: a Mac stops when the lid closes).

Keep the account safe from spam protection: no more than about 10 posts in 3 hours, and space reels out (one or two
a day is safe). ig-publish enforces a burst guard and reel spacing by default (our heuristics, not published Meta
rules). Add API stories to Highlights in the Instagram app while they are live (24 hours) or later from the archive.

What the API cannot do, so you do it in the app: Highlights (create, add, cover, rename), the profile photo, the
bio and links (bio text can also be edited at instagram.com on a desktop; links only in the app), pinning posts,
and link, music or poll stickers on stories.

## 9. Token expiry and renewal

- A long-lived user token lasts **about 60 days** from when you extended it. `doctor` cannot show the expiry date
  (reading it would put the token into a URL, which ig-publish never does); the
  [Access Token Debugger](https://developers.facebook.com/tools/debug/accesstoken) shows it.
- **Set a calendar reminder** for about 50 days after the day you extended the token, and repeat it every time you
  renew.
- **To renew**, repeat [step 4](#4-generate-the-access-token) (generate, tick both the Page and the Instagram
  account, extend) and store the new token in the same place:
  - Keychain: the same `security add-generic-password -U ... -w "$(pbpaste)"` line (`-U` replaces the old value);
  - file: the same `read -rs T && ...` line (it overwrites the file);
  - SSM: the same `put-parameter ... --overwrite` line; on the EC2 setup also run
    `sudo systemctl restart ig-publish-token`;

  then run `ig-publish doctor`.
- When the token has expired, Meta answers code `190` (subcode `463`) and ig-publish prints the renewal steps under
  the error. Scheduled runs stop before writing anything (exit 75): the scheduler retries the line every
  `retry_seconds` and marks it `missed` after `max_late_hours`, and the notification (if you set `notify_command`)
  carries the fix. Renew within that window and the line still goes out; otherwise add it to the schedule again.
- A token also stops working early when you change your Facebook password (`190/460`), when the session is ended
  or the app is removed from your account (`190/467`, `190/458`), or when Meta checkpoints your account
  (`190/459`). The fix is the same: log in at facebook.com if Meta asks for something, then make a new token.
- An existing token never gains permissions: after adding one (for example `instagram_manage_contents` for
  `delete`), generate a new token.
- For servers, Meta documents system-user tokens in a Business portfolio that can be created without an expiry
  date (Business settings → Users → System users). This guide does not cover them step by step.

## Common mistakes

> [!WARNING]
> - **Ticking only the Page or only the Instagram account** in the token dialog (step 4.4). Tick both.
> - **Using the @username or the Page ID** as `ig_user_id`. It is the `instagram_business_account` ID (step 5).
> - **A Creator account.** Stories fail through the API: observed (Oct 2026). Switch to Business (step 2.1).
> - **A second `[account]` or `[token]` header** in `ig-publish.toml`. `ig-publish init` already wrote both
>   sections: edit them (TOML refuses a section that appears twice).
> - **Copying the short-lived token** from the Explorer instead of the extended one: it stops working within
>   about two hours.
> - **A truncated token**: `security add-generic-password ... -w` without a value cuts input at 128 characters.
>   Use the `"$(pbpaste)"` form.
> - **Storing the command instead of the token**: pasting the command and the token together, or pasting two
>   lines at once. Paste the command first, then copy the token, then press Enter.
> - **Expecting an old token to gain a new permission.** Generate a new token after adding a permission.
> - **Instagram Login instead of Facebook Login**: tokens that start with `IG` and `graph.instagram.com` do not
>   work with ig-publish (step 3).
> - **Posting in bulk.** More than about 10 posts in 3 hours risks an "action blocked" (`2207051`).
> - **Retrying `media_publish` by hand after an error.** A 5xx can come back although the post went live. Run the
>   same ig-publish command instead: it reads the container first and never posts twice.
> - **Turning off "Save story to archive"**: API stories then cannot be added to Highlights after 24 hours.
> - **Running the scheduler on a laptop**: it stops when the lid closes and late lines are skipped. Use a server
>   ([deploy-ec2.md](deploy-ec2.md)).
> - **Putting the token in `ig-publish.toml`**, a repository, a chat or a command line. Use one of the stores in
>   step 6.

Next: [the README](../README.md) (manifest, scheduler, security model), [troubleshooting.md](troubleshooting.md)
and [instagram-api.md](instagram-api.md) (what the API can and cannot do).
