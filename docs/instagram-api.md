# Instagram API notes

What ig-publish relies on, what the API cannot do, and how each error is handled.

Meta changes the platform often. Statements below are either **documented** (from Meta's developer
documentation at the time of writing, October 2026: re-check before you rely on them) or **observed (date)**,
meaning we saw it while running this tool. Observed behaviour is not a promise from Meta.

## Getting started

The full click-by-click setup is [setup.md](setup.md) (about 15 minutes). In short:

1. A **Business** Instagram account linked to a Facebook Page on which you have a content role.
2. A Meta app with the use case **Manage messaging & content on Instagram** → **API setup with Facebook login**,
   and the permissions below.
3. In the [Graph API Explorer](https://developers.facebook.com/tools/explorer), a user token for the app (tick BOTH
   the Page and the Instagram account in the dialog), extended to a long-lived one (about 60 days) in the
   [Access Token Debugger](https://developers.facebook.com/tools/debug/accesstoken).
4. The numeric account ID from `GET me/accounts?fields=name,instagram_business_account` in `[account] ig_user_id`;
   the token in one of the [token sources](../README.md#token-sources), never in the configuration file.
5. `ig-publish doctor` checks all of it and prints the fix for anything missing
   ([troubleshooting.md](troubleshooting.md) has every error).

## Which API

- **Instagram API with Facebook Login** (`graph.facebook.com`). The Instagram account must be a **Business**
  account linked to a Facebook Page. Creator accounts cannot publish stories through the API: observed (Oct 2026).
- The newer *Instagram API with Instagram Login* (`graph.instagram.com`, `IG…` tokens) is **not supported**.
- Permissions (documented): `instagram_basic`, `instagram_content_publish`, `pages_read_engagement`.
  `pages_show_list` lets `ig-publish doctor` and `check` list your Pages. If your role on the Page comes through a
  Meta Business portfolio, Meta also requires `business_management` and `ads_management` or `ads_read`.
  Deleting posts needs `instagram_manage_contents`.
- A Page that requires **Page Publishing Authorization (PPA)** cannot publish until PPA is complete.
- Tokens: long-lived user tokens expire (about 60 days, documented). System-user tokens created in a Business
  portfolio can be non-expiring. Check a token's expiry and scopes in Meta's Access Token Debugger.

## The publishing flow ig-publish uses

1. `POST /{ig-user-id}/media` with `media_type=STORIES` or `REELS` and `upload_type=resumable`
   (reels also send `caption`, `share_to_feed` and optionally `thumb_offset`). The answer holds a **container ID**
   and an upload URI.
2. `POST https://rupload.facebook.com/ig-api-upload/{version}/{container-id}` with the raw file bytes and the
   headers `Authorization: OAuth <token>`, `offset: 0` and `file_size: <bytes>`. No public URL, S3 bucket or CDN is
   needed. Success is accepted only as `{"success": true}`.
3. `GET /{container-id}?fields=status_code,status,video_status` until `FINISHED` (or `ERROR` / `EXPIRED`).
4. `POST /{ig-user-id}/media_publish` with `creation_id={container-id}`.
5. `GET /{media-id}?fields=permalink,media_product_type,timestamp` to read the result back.

ig-publish runs steps 1-3 for **every** selected item before step 4 for **any** of them, and runs step 4 in the
manifest order.

## What the API cannot do

Documented limitations at the time of writing:

- **Stickers** on stories (link, poll, question, mention, location, music ...) cannot be added through the API.
- **Links** in stories: the link sticker is app-only.
- **Music** from Instagram's library cannot be attached. The audio in your file is the audio that is posted.
- **Highlights** cannot be created or edited through the API. Add API stories to a Highlight in the app while they
  are live (24 hours), or later from the story archive if archiving is on. `ig-publish status` prints when your
  live stories leave the tray.
- **Story captions**: the API takes no caption for stories (ig-publish rejects one in the manifest).

Not implemented in ig-publish (the API supports them, this tool does not, yet): images and carousels, reel covers
from a file (`cover_url` needs a public URL; use `thumb_offset`), collaborators, user tags, locations and
`audio_name`. Still images *are* supported for stories by turning them into short videos during `prep`.

## Limits

| What | Limit | Source |
|---|---|---|
| API posts per account | a moving 24-hour quota: observed (Oct 2026) `quota_total` **100**; some Meta docs say **50** (a carousel counts as one) | observed and documented; ig-publish reads the live value from `content_publishing_limit` (`quota_total`, `quota_usage`) instead of assuming one |
| Story video | **3-60 s**, up to 100 MB | documented |
| Reel video | 3 s - 15 min, up to 300 MB | documented |
| Caption | 2,200 characters, 30 hashtags, 20 @-mentions | documented |
| Container lifetime | expires 24 h after creation (`EXPIRED`) | documented; ig-publish re-creates containers older than 23 h |
| API usage | rolling usage percentages in `X-App-Usage` / `X-Business-Use-Case-Usage`, with `estimated_time_to_regain_access` (minutes) | documented; ig-publish sends nothing more above 85% (configurable) |

ig-publish counts every API post (stories included) against the quota and compares the live `quota_usage` with
its own records. If the live number is higher, another tool may be posting through the API and the run stops with
exit 75 (`ig-publish ack --apply` records the account's unknown recent posts; `--ack-unknown-posts` overrides the
check for one run).

## Errors and what ig-publish does

The meanings paraphrase Meta's error-code table; the reactions are this tool's. What *you* do about each error
(and about the token, permission and setup errors not listed here) is in [troubleshooting.md](troubleshooting.md);
ig-publish prints the same fix as a `fix:` line under the error.

| Code | Meaning | ig-publish |
|---|---|---|
| `2207051` | activity restricted / looks like spam ("action blocked") | stop the run; read the container to record whether the post went live; **24 h hold** |
| `2207042` | the account reached its API publishing limit | stop; hold until the quota has room again (at most 24 h) |
| `2207008` | temporary error while publishing a container | retry once or twice, 30 s - 2 min apart (Meta's guidance); then the next run creates a new container |
| `2207006`, `2207020`, `2207032`, `2207053` | the container cannot be used (not found, expired, creation or upload failed) | never retry that container; the next run creates a new one |
| `2207026` | unsupported video format | the container is `ERROR`: nothing in the run is published; check `prep` |
| `2207052`, `9004` | the media could not be fetched | as above |
| `4`, `17`, `32`, `613`, `80001`, `80002`, `80004`, HTTP `429` | rate limited | on `media_publish`: stop and hold for at least an hour (or Meta's estimate if longer); on any other request: stop without a hold. Reads that hit `80001`/`80002` wait and retry up to three times |
| HTTP `5xx`, network errors, `is_transient`, codes `1`/`2`/`-1`/`-2`, a `media_publish` answer without a media ID | the outcome is unknown | read the container: `PUBLISHED` is recorded; otherwise the item is marked `publish_unknown` and the run stops. **No blind retry**: the next run reads the container again before doing anything |

A *hold* is written to the state file, so it survives restarts; `--apply` refuses to run while it is active
(exit 75) unless you pass `--ignore-hold`.

## Behaviour we design for

- **An error does not mean "not published".** Any distributed API can fail after the work is done. ig-publish
  never decides from the error alone: it reads the container status first, and a `PUBLISHED` container is recorded
  and never published again, however old it is.
- **observed (Oct 2026):** Meta answered HTTP 200 with an `error` object in the body. The client treats such an
  answer as an error.
- **observed (Oct 2026):** rupload confirms a good upload with `{"success": true}`; other bodies (`debug_info`,
  plain text) meant the bytes were not accepted, so ig-publish treats them as failures.
- **Story media IDs after an unclear publish.** When a publish answer was lost but the container is `PUBLISHED`,
  ig-publish looks for the post on the account: reels by their exact caption, stories by time (an unknown video
  story within 30 seconds). A story found by time is only a **candidate**: `delete` refuses it unless you confirm the
  ID with `--confirm-media`.
- **Heuristics, not rules.** The burst guard (10 posts in 3 hours) and the reel spacing (30 minutes) are our own
  conservative defaults to stay clear of spam detection. Meta does not publish such thresholds; tune them in the
  configuration.
