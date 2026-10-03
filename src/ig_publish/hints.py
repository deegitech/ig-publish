"""One-line fixes for known Meta / Instagram API errors.

Every Graph API error that reaches the user gets a ``fix:`` line under it when the error is a known one: the CLI,
the publish stops, ``doctor`` and the scheduler notifications all use :func:`hint_for` / :func:`explain`. The
meanings follow Meta's error-code tables (Graph API "Handling errors", Instagram Platform error codes); where a
fix rests on what we saw while running this tool rather than on Meta's documentation, the text says "observed".
The full table, with more detail, is ``docs/troubleshooting.md``.
"""
from __future__ import annotations

from typing import Any

DOCS = 'https://github.com/deegitech/ig-publish/blob/main/docs/'
SETUP_TOKEN = f'{DOCS}setup.md#4-generate-the-access-token'
SETUP_STORE = f'{DOCS}setup.md#6-store-the-token-safely'
SETUP_RENEW = f'{DOCS}setup.md#9-token-expiry-and-renewal'
TROUBLESHOOTING = f'{DOCS}troubleshooting.md'

# The token steps, short enough for one line (docs/setup.md has every click).
TOKEN_STEPS = ('Graph API Explorer -> Generate Access Token (tick BOTH the Facebook Page and the Instagram account) '
               '-> Access Token Debugger -> Extend Access Token, store it again and run `ig-publish doctor` '
               f'({SETUP_RENEW})')
NEW_TOKEN = f'make a new token: {TOKEN_STEPS}'

# Rate limits: only a refused media_publish sets a hold (publisher.set_hold); reads (doctor, check, plan, the
# preflight of publish --apply) and other writes just stop.
RATE = ('Rate limited: Meta is limiting requests from this app or account for now. Wait about an hour, then run the '
        'same command again, and keep other tools that use the same app or account quiet meanwhile.')
RATE_HOLD = ("Rate limited: ig-publish holds for at least an hour (or Meta's estimate when that is longer). Run the "
             'same command later, and keep other tools that use the same app or account quiet meanwhile.')
TRANSIENT = ('Temporary Meta error. After a write the outcome may be unknown: never repeat the call by hand; run the '
             'same ig-publish command later, it reads the container first and records what went live.')
OUTCOME_UNKNOWN = ('Meta answered media_publish without a media ID, so whether the post went live is unknown. Never '
                   'repeat the call by hand: run the same ig-publish command later, it reads the container first '
                   '(PUBLISHED is recorded, FINISHED is published).')
REDIRECT = ('Meta answered with a redirect (HTTP 3xx), and ig-publish never follows one because the token would travel '
            'along: check for a proxy, VPN or captive portal (a hotel or office login page) between this machine and '
            'graph.facebook.com, then run the same command again.')
NEW_CONTAINER = ('This container cannot be used any more: run the same command; the item gets a new container and '
                 'nothing is posted twice.')
NOT_FETCHED = ('Instagram could not fetch the media. With resumable uploads this usually means the upload was '
               'incomplete: run the same command (a fresh container is uploaded); for big files or a slow uplink '
               'raise [timing] upload_timeout_seconds.')
PERMISSION = ('A permission is missing or was removed (`ig-publish doctor` names it). Add it in the Graph API Explorer '
              f'and generate a new token, because an existing token never gains permissions: {TOKEN_STEPS}')
NOT_VISIBLE = ('The object does not exist for this token: [account] ig_user_id must be the numeric Instagram account '
               'ID (not the @username or the Page ID), and the token must include that account (tick BOTH the Page '
               'and the Instagram account in the token dialog). `ig-publish doctor` checks both.')
PAGE_ID_USED = ('[account] ig_user_id holds a Facebook Page ID: use the Instagram account ID instead (Graph API '
                'Explorer -> GET me/accounts?fields=name,instagram_business_account -> instagram_business_account id).')
UPLOAD_FAILED = ('The upload did not complete. Uploads to rupload.facebook.com can time out mid-file: observed (Oct '
                 '2026). Run the same command: the item gets a new container and nothing is posted twice. For big '
                 'files or a slow uplink raise [timing] upload_timeout_seconds.')
NETWORK = ('Network error: check the connection, VPN or proxy, then run the same command (after a write, ig-publish '
           'checks what happened before it does anything else).')

RATE_CODES = (4, 17, 32, 613, 80001, 80002, 80004)

# Subcodes are specific: they win over the code.
BY_SUBCODE: dict[int, str] = {
    # OAuth (code 190 or 102): Meta's "authentication error subcodes".
    458: f'The app is not authorized for this Facebook account (removed, or the dialog was not accepted): {NEW_TOKEN}',
    459: ('The Facebook account is checkpointed: log in at www.facebook.com and complete the security check, then '
          f'{NEW_TOKEN}'),
    460: f'The token was invalidated by a password change or a security reset: {NEW_TOKEN}',
    463: f'The token expired (long-lived tokens last about 60 days): {NEW_TOKEN}',
    464: f'The Facebook account is not confirmed: log in at www.facebook.com and confirm it, then {NEW_TOKEN}',
    467: f'The token is no longer valid (logged out, revoked or replaced): {NEW_TOKEN}',
    492: ('Invalid session: your Facebook account no longer has a suitable role on the Page. Check the Page access '
          '(Page settings -> Page access, or Business settings -> Pages; names may differ), then '
          f'{NEW_TOKEN}'),
    # Identity checkpoint.
    3858385: ('Meta wants you to verify your identity: open facebook.com/accountquality (or Ads Manager / Meta '
              'Business Suite) and follow the banner; observed (Oct 2026): it sometimes clears just by visiting. '
              'Then run the command again.'),
    # Instagram content publishing.
    2207051: ('Instagram treated the activity as spam ("action blocked"); ig-publish holds for 24 h. Do not post by '
              'hand meanwhile, look for a "We restrict certain activity" notice in the Instagram app ("Tell us" if '
              'it is a mistake), and afterwards post less in a row (one or two reels a day, at most about 10 posts '
              'in 3 h).'),
    2207042: ('The account reached its API publishing limit (a rolling 24 h quota; `ig-publish status` shows it). '
              'Wait until it has room; ig-publish lifts the hold by itself.'),
    2207050: ('The Instagram account is inactive, checkpointed or restricted: open the Instagram app, sign in and '
              'complete what it asks, then run the command again.'),
    2207008: ('Temporary publishing error, already retried by ig-publish. Run the same command in a few minutes: it '
              'reads the container first and makes a new one if Meta asks for it.'),
    2207027: 'The container was not ready yet: run the same command; ig-publish waits for FINISHED before publishing.',
    2207006: NEW_CONTAINER,
    2207020: NEW_CONTAINER,
    2207032: NEW_CONTAINER,
    2207053: NEW_CONTAINER,
    2207026: ('Unsupported video format: run `ig-publish prep` (or check your own export with `ig-publish verify '
              'FILE --kind story|reel`) and publish the prepared file.'),
    2207052: NOT_FETCHED,
}

BY_CODE: dict[int, str] = {
    190: (f'The token is invalid (expired, revoked, truncated or of the wrong kind): {NEW_TOKEN}. A token of exactly '
          '128 characters was cut by the interactive Keychain prompt; Instagram Login tokens (they start with "IG") '
          'do not work with ig-publish.'),
    102: f'The session behind the token is no longer valid: {NEW_TOKEN}',
    3: PERMISSION,
    10: PERMISSION,
    9004: NOT_FETCHED,
    341: 'Application limit reached (temporary): wait, then run the same command again.',
    368: ('Temporarily blocked for a policy reason: stop posting, check the Instagram and Facebook apps for notices, '
          'and wait before you try again.'),
    **dict.fromkeys(RATE_CODES, RATE),
    1: TRANSIENT, 2: TRANSIENT, -1: TRANSIENT, -2: TRANSIENT,
}


def _int(v: Any) -> int | None:
    if isinstance(v, bool):
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def hint_for(code: Any = None, subcode: Any = None, status: int | None = None, message: str = '',
             where: str = '', *, transient: bool = False) -> str | None:
    """The one-line fix for an error, or ``None`` when it is not a known one.

    ``code`` / ``subcode`` are Meta's ``code`` and ``error_subcode``, ``status`` the HTTP status (``0`` for a
    network failure), ``message`` Meta's message, ``where`` the request (``"UPLOAD ..."`` for uploads,
    ``"POST .../media_publish"`` for publishing) and ``transient`` Meta's ``is_transient`` flag.
    """
    c, s = _int(code), _int(subcode)
    msg = (message or '').lower()
    publish = 'media_publish' in (where or '')  # only a refused media_publish sets a hold
    if s is not None and s in BY_SUBCODE:
        return BY_SUBCODE[s]
    if c == 100 and 'on node type (page)' in msg:
        return PAGE_ID_USED
    if c == 100 and (s == 33 or 'unsupported get request' in msg or 'does not exist, cannot be loaded' in msg):
        return NOT_VISIBLE
    if c is not None and 200 <= c <= 299:
        return PERMISSION
    if c is not None and c in BY_CODE:
        return RATE_HOLD if publish and c in RATE_CODES else BY_CODE[c]
    if transient:
        return TRANSIENT
    if status == 429:
        return RATE_HOLD if publish else RATE
    if status is not None and 300 <= status <= 399:
        return REDIRECT
    upload = (where or '').startswith('UPLOAD')
    if status == 0:
        return UPLOAD_FAILED if upload else NETWORK
    if upload and c is None:
        return UPLOAD_FAILED
    if status is not None and status >= 500:
        return TRANSIENT
    return None


def hint(err: BaseException) -> str | None:
    """:func:`hint_for` for a :class:`~ig_publish.errors.GraphError`; ``None`` for anything else."""
    e = getattr(err, 'err', None)
    if not isinstance(e, dict):
        return None
    if getattr(err, 'unknown_outcome', False):  # a write answered without the expected ID (publisher.publish_one)
        return OUTCOME_UNKNOWN
    return hint_for(e.get('code'), e.get('error_subcode'), getattr(err, 'status', None), str(e.get('message') or ''),
                    str(getattr(err, 'where', '') or ''), transient=bool(e.get('is_transient')))


def fix_line(err: BaseException) -> str:
    """``"\\n  fix: ..."`` for a known error, else ``""`` (for appending to a multi-line message)."""
    h = hint(err)
    return f'\n  fix: {h}' if h else ''


def explain(err: BaseException) -> str:
    """The error text plus, when the error is a known one, an indented ``fix:`` line."""
    text = err.text() if callable(getattr(err, 'text', None)) else str(err)
    return text + fix_line(err)
