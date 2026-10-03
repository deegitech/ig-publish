"""``ig-publish doctor``: read-only setup checks, in setup order, with the exact fix under every failure.

Local checks first (configuration, manifest, FFmpeg, prepared media, state directory, token source), then
read-only Graph API calls (the token works, permissions, the Instagram account, its type, the Page link, quota,
API usage). A check that cannot run because an earlier one failed is skipped.

Guarantees: nothing is written (no state, journal, lock or media file), no request changes anything on Meta's side,
the token is never printed (every line passes through the redactor; only its length is shown) and every ``✗`` has
a ``fix:`` line. Exit code 0 when nothing failed, 1 otherwise; ``!`` notes do not fail.
"""
from __future__ import annotations

import json
import os
import platform
import shutil
import stat
import subprocess
import sys
import time
from importlib import resources
from pathlib import Path
from typing import Any, TextIO

from . import __version__
from .config import Config, load_config
from .errors import ConfigError, GraphError, Stop, TokenError, UsageLimit
from .hints import DOCS, RATE_CODES, SETUP_RENEW, SETUP_STORE, SETUP_TOKEN, TROUBLESHOOTING, hint
from .manifest import load_manifest
from .media import MIN_FFMPEG, ffmpeg_version, recipe
from .publisher import PAGE_TASKS, REQUIRED_SCOPES, Options, Publisher
from .redact import redact
from .state import load_json
from .tokens import TEST_TOKEN_ENV, scrubbed_env, token_notes

OK, FAIL, NOTE, SKIP = '✓', '✗', '!', '-'
# What `prep` asks of FFmpeg: encoders and filters (media.encoder_args / media.video_filter / the silent track).
ENCODERS = ('libx264', 'aac')
FILTERS = ('scale', 'pad', 'crop', 'setsar', 'format', 'anullsrc')
SETUP_ACCOUNT = f'{DOCS}setup.md#2-prepare-the-instagram-account-and-the-page'
SETUP_APP = f'{DOCS}setup.md#3-create-the-meta-app'
SETUP_ID = f'{DOCS}setup.md#5-find-the-instagram-account-id'
EXAMPLE_CONFIG = 'https://github.com/deegitech/ig-publish/blob/main/examples/ig-publish.toml'
MANIFEST_DOCS = 'https://github.com/deegitech/ig-publish#the-manifest'
# The read-only Meta API checks, in order (a usage stop lists the ones that did not run).
API_CHECKS = ('token works', 'permissions', 'Instagram account', 'account type', 'Page link', 'quota')

INSTALL_FFMPEG = ('install FFmpeg 5.1 or newer: macOS `brew install ffmpeg`; Debian 12 / Ubuntu 24.04 `sudo apt-get '
                  'install ffmpeg`; older systems (Ubuntu 22.04 ships 4.4): use the Docker image or a static build. '
                  'Only `prep` and `verify` need it.')
ADD_PERMISSION = ('Graph API Explorer (developers.facebook.com/tools/explorer) -> your app -> Permissions -> "Add a '
                  'Permission" (sometimes a free-text field: type the name) -> {perm} -> Generate Access Token -> in '
                  'the dialog tick BOTH the Facebook Page and the Instagram account -> extend the token and store it '
                  'again (an existing token never gains permissions): ' + SETUP_TOKEN + '\n'
                  'Not offered? Add it to the app first: App Dashboard -> Use cases -> "Manage messaging & content on '
                  'Instagram" -> Customize -> "API setup with Facebook login" (names may differ): ' + SETUP_APP)
LINK_PAGE = ('(1) Link the Instagram account to your Facebook Page: Facebook -> switch to the Page -> Settings -> '
             'Linked accounts -> Instagram -> Connect (or in the Instagram app: Edit profile -> Page -> Connect; names '
             'may differ).\n'
             '(2) Generate the token again and tick BOTH the Page and the Instagram account in the dialog.\n'
             '(3) If your access to the Page comes through a Business portfolio, also grant business_management '
             "(Meta's docs then ask for ads_read or ads_management too) and set [account] page_id. " + SETUP_ACCOUNT)
FIND_ID = ('[account] ig_user_id must be the numeric Instagram account ID, not the @username or the Page ID: Graph '
           'API Explorer -> GET me/accounts?fields=name,instagram_business_account -> the instagram_business_account '
           'id of your Page. When you generate the token, tick BOTH the Page and the Instagram account. ' + SETUP_ID)
ACCOUNT_TYPE = ('Instagram app -> your profile -> menu -> Settings and activity -> Account type and tools -> Switch '
                'account type -> Business (names may differ). ' + SETUP_ACCOUNT)
PAGE_ACCESS = ('give your Facebook account full control of the Page or task access for content: Page -> Settings -> '
               'Page access (with a Business portfolio: Business settings -> Accounts -> Pages -> the Page -> assign '
               'people -> Content or Full control; names may differ), then generate a new token.')


class Doctor:
    def __init__(self, config: str | None = None, *, offline: bool = False, out: TextIO | None = None) -> None:
        self.config = config
        self.offline = offline
        self._out = out
        self.results: list[tuple[str, str]] = []
        self.ig_visible = False
        self.granted: set[str] = set()
        self.needs_prep = True
        self.not_run: list[str] = []

    # ------------------------------------------------------------------ output
    def say(self, line: str = '') -> None:
        out = self._out or sys.stdout
        out.write(redact(line) + '\n')
        out.flush()

    def add(self, mark: str, name: str, detail: str, fix: str = '') -> None:
        self.results.append((mark, name))
        lines = str(detail).split('\n')
        self.say(f'  {mark} {name:<18} {lines[0]}')
        for line in lines[1:]:
            self.say(f'    {"":<18} {line}')
        if fix:
            parts = fix.split('\n')
            self.say(f'      fix: {parts[0]}')
            for line in parts[1:]:
                self.say(f'           {line}')

    def section(self, title: str) -> None:
        self.say(f'\n{title}')

    @staticmethod
    def short(path: Any) -> str:
        """``path`` relative to the current directory when it lies below it, else as is."""
        p = os.path.abspath(str(path))
        rel = os.path.relpath(p)
        return p if rel.startswith('..') else rel

    # ------------------------------------------------------------------ run
    def run(self) -> int:
        self.say('ig-publish doctor: read-only checks in setup order. Nothing is written; the token is never shown.')
        self.say(f'ig-publish {__version__} | Python {platform.python_version()} | {platform.system()} '
                 f'{platform.machine()}')
        self.section('Local setup')
        cfg = self.check_config()
        items: list[dict] = []
        pub: Publisher | None = None
        if cfg:
            items = self.check_manifest(cfg)
            pub = self.publisher(cfg)
        self.check_ffmpeg(cfg.token.env_var if cfg else None)
        if cfg and pub:
            self.check_prepared(pub, items)
            self.check_state(pub)
        self.section('Token')
        token_ok = False
        if self.offline:
            self.add(SKIP, 'token', 'skipped (--offline: no token, no network)')
        elif pub is None:
            self.add(SKIP, 'token', 'skipped: needs the configuration (see the ✗ above)')
        else:
            token_ok = self.check_token(pub)
        self.section('Meta API (read-only requests)')
        if self.offline:
            self.add(SKIP, 'Meta API', 'skipped (--offline)')
        elif not token_ok or pub is None:
            self.add(SKIP, 'Meta API', 'skipped: needs a readable token (above)')
        else:
            self.check_api(pub, items)
        return self.summary(token_ok)

    def summary(self, token_ok: bool) -> int:
        failed = [n for m, n in self.results if m == FAIL]
        notes = sum(1 for m, _n in self.results if m == NOTE)
        checks = sum(1 for m, _n in self.results if m in (OK, FAIL, NOTE))
        self.say('')
        if failed:
            self.say(f"✗ doctor: {len(failed)} problem(s): {', '.join(dict.fromkeys(failed))}. Fix them from the top "
                     f'(each fix is printed under its ✗), then run `ig-publish doctor` again.')
            if self.not_run:
                self.say(f"  incomplete: {len(self.not_run)} check(s) not run ({', '.join(self.not_run)}); run "
                         f'`ig-publish doctor` again in about an hour.')
            self.say(f'  Every error ig-publish knows, with its fix: {TROUBLESHOOTING}')
        else:
            nxt = 'ig-publish prep && ig-publish plan' if self.needs_prep else 'ig-publish plan'
            self.say(f"✓ doctor: no problems found ({checks} checks{f', {notes} note(s) marked !' if notes else ''}). "
                     f'Next: {nxt}')
        if token_ok and 'token works' not in failed:
            self.say(f'Reminder: a long-lived token lasts about 60 days from when you extended it. The Access Token '
                     f'Debugger shows its expiry date; renew it before then: {SETUP_RENEW}')
        return 1 if failed else 0

    # ------------------------------------------------------------------ local checks
    def check_config(self) -> Config | None:
        try:
            cfg = load_config(self.config)
        except ConfigError as e:
            msg = str(e)
            self.add(FAIL, 'configuration', msg, self.config_fix(msg))
            return None
        self.add(OK, 'configuration', f'{self.short(cfg.path)} | Instagram account {cfg.ig_user_id}'
                 + (f' (@{cfg.username})' if cfg.username else '') + f' | token from {cfg.token.describe()}')
        return cfg

    @staticmethod
    def config_fix(msg: str) -> str:
        """The fix for a configuration error, chosen from its message."""
        if msg.startswith(('No configuration file', 'Configuration file not found')):
            return ('in your project folder run `ig-publish init` (it writes ig-publish.toml, manifest.json and '
                    'schedule.txt), or pass --config PATH / set IG_PUBLISH_CONFIG')
        if 'Cannot declare' in msg:
            return ('that [section] is in the file twice: `ig-publish init` already wrote every section, so move these '
                    'keys into the existing section and delete the second header')
        if 'Cannot overwrite a value' in msg:
            return 'a key is set twice in one section (the file from `ig-publish init` already has it): keep one line'
        if msg.startswith('[account] ig_user_id'):  # not valid / must be a string / is required
            return ('[account] ig_user_id is the numeric Instagram account ID in quotes, e.g. ig_user_id = '
                    '"17841401234567890", not the @username or the Page ID: ' + SETUP_ID)
        if msg.startswith('[token]'):
            return f'complete the [token] section for your source ({SETUP_STORE})'
        return ('edit the file as the message says (unknown keys are errors on purpose, so a typo never switches a '
                f'guard off); the annotated example: {EXAMPLE_CONFIG}')

    @staticmethod
    def init_examples(cfg: Config) -> list[str]:
        """Keys of the example items `ig-publish init` wrote that are still in the manifest, with no source file."""
        template = resources.files('ig_publish') / 'templates' / 'manifest.json'
        try:
            tpl = json.loads(template.read_text(encoding='utf-8'))
            man = load_json(cfg.manifest, {})
        except (OSError, ValueError):
            return []
        examples = {(i.get('key'), i.get('src')) for i in tpl.get('items', []) if isinstance(i, dict)}
        rows = man.get('items') if isinstance(man, dict) else None
        return [str(i.get('key')) for i in rows or []
                if isinstance(i, dict) and (i.get('key'), i.get('src')) in examples
                and not os.path.isfile(os.path.join(cfg.source_root, str(i.get('src'))))]

    def check_manifest(self, cfg: Config) -> list[dict]:
        try:
            _man, items = load_manifest(cfg)
        except ConfigError as e:
            root = self.short(cfg.source_root)
            fix = (f'fix the entries listed (src paths are relative to [paths] source_root = {root}); `ig-publish plan '
                   f'--offline` checks them again')
            left = self.init_examples(cfg)
            if left:
                fix = (f'{self.short(cfg.manifest)} still lists the examples from `ig-publish init` '
                       f"({', '.join(left)}): replace them with your own files, in publishing order "
                       f'({MANIFEST_DOCS}).\n' + fix)
            self.add(FAIL, 'manifest', str(e), fix)
            return []
        n_story = sum(1 for i in items if i['kind'] == 'story')
        self.add(OK, 'manifest', f'{self.short(cfg.manifest)} | {len(items)} item(s): {n_story} stor'
                 f"{'y' if n_story == 1 else 'ies'}, {len(items) - n_story} reel(s); every source file found")
        return items

    def publisher(self, cfg: Config) -> Publisher | None:
        try:
            return Publisher(cfg, Options(), out=self._out)
        except ConfigError as e:
            self.add(FAIL, 'API endpoints', str(e),
                     'unset IG_PUBLISH_API_BASE and IG_PUBLISH_UPLOAD_BASE (they exist for the test suite only)')
            return None

    def _ffmpeg_lists(self, tool: str, flag: str, env: dict[str, str]) -> set[str]:
        r = subprocess.run([tool, '-hide_banner', flag], stdin=subprocess.DEVNULL, capture_output=True, text=True,
                           timeout=30, env=env)
        names = set()
        for line in (r.stdout or '').splitlines():
            parts = line.split()
            if len(parts) >= 2 and not line.startswith(' ---'):
                names.add(parts[1])
        return names

    def check_ffmpeg(self, token_env_var: str | None = None) -> None:
        env = scrubbed_env(token_env_var)
        ffmpeg, ffprobe = shutil.which('ffmpeg'), shutil.which('ffprobe')
        missing = [n for n, p in (('ffmpeg', ffmpeg), ('ffprobe', ffprobe)) if not p]
        if missing:
            self.add(FAIL, 'ffmpeg / ffprobe', f"{' and '.join(missing)} not found on PATH", INSTALL_FFMPEG)
            return
        assert ffmpeg and ffprobe
        try:
            first = subprocess.run([ffmpeg, '-version'], stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                   timeout=30, env=env).stdout.split('\n', 1)[0]
            probe_ok = subprocess.run([ffprobe, '-version'], stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                      timeout=30, env=env).returncode == 0
            encoders = self._ffmpeg_lists(ffmpeg, '-encoders', env)
            filters = self._ffmpeg_lists(ffmpeg, '-filters', env)
        except (OSError, subprocess.SubprocessError) as e:
            self.add(FAIL, 'ffmpeg / ffprobe', f'could not run FFmpeg: {type(e).__name__}: {e}', INSTALL_FFMPEG)
            return
        ver = ffmpeg_version(first)
        shown = first.split(' Copyright')[0].replace('ffmpeg version ', 'FFmpeg ') or 'FFmpeg (version unknown)'
        if ver is not None and ver < MIN_FFMPEG:
            self.add(FAIL, 'ffmpeg / ffprobe', f'{shown}: `prep` needs {MIN_FFMPEG[0]}.{MIN_FFMPEG[1]} or newer',
                     INSTALL_FFMPEG)
            return
        if not probe_ok:
            self.add(FAIL, 'ffmpeg / ffprobe', 'ffprobe does not run (`ffprobe -version` failed)', INSTALL_FFMPEG)
            return
        lack = [e for e in ENCODERS if e not in encoders] + [f'filter {f}' for f in FILTERS if f not in filters]
        if lack:
            self.add(FAIL, 'ffmpeg / ffprobe', f"{shown} lacks {', '.join(lack)}",
                     'install a full FFmpeg build (Homebrew ffmpeg, the Debian/Ubuntu ffmpeg package) or use the '
                     'Docker image; minimal builds leave libx264 out')
            return
        self.add(OK, 'ffmpeg / ffprobe', f"{shown}{' (version not checked: a git build)' if ver is None else ''} | "
                                         f"{', '.join(ENCODERS)} and the filters prep uses")

    def check_prepared(self, pub: Publisher, items: list[dict]) -> None:
        if not items:
            return
        self.needs_prep = False
        try:
            idx = load_json(pub.cfg.prep_index, {})
            st = load_json(pub.cfg.state, {})
        except (ValueError, OSError):
            idx, st = {}, {}  # check_state reports an unreadable state file
        idx = idx if isinstance(idx, dict) else {}
        recs = (st.get('items') if isinstance(st, dict) else None) or {}
        rcp = recipe(pub.cfg.prep)
        pending = [it for it in items if not it.get('already_live')
                   and (recs.get(it['key']) or {}).get('status') not in ('published', 'deleted')]
        todo = []
        for it in pending:
            rec = idx.get(it['key']) or {}
            if not (rec.get('ok') and rec.get('recipe') == rcp and rec.get('kind') == it['kind']
                    and os.path.isfile(pub.out_path(it['key']))):
                todo.append(it['key'])
        if not pending:
            self.add(OK, 'prepared media', 'nothing pending in the manifest')
        elif todo:
            self.needs_prep = True
            shown = ', '.join(todo[:6]) + ('...' if len(todo) > 6 else '')
            self.add(NOTE, 'prepared media', f'{len(todo)} of {len(pending)} pending item(s) not prepared yet: {shown}',
                     'ig-publish prep')
        else:
            self.add(OK, 'prepared media', f'{len(pending)} pending item(s) prepared ({pub.rel(pub.cfg.prep_index)}; '
                                           f'`plan` and `publish` re-check the file hashes)')

    def check_state(self, pub: Publisher) -> None:
        cfg = pub.cfg
        rel = pub.rel(cfg.state)
        d = Path(cfg.state).parent
        while not d.exists() and d != d.parent:
            d = d.parent
        if not os.access(d, os.W_OK | os.X_OK):
            uid = os.getuid() if hasattr(os, 'getuid') else '?'
            self.add(FAIL, 'state', f'{d} is not writable for this user (uid {uid}): publishing could not record '
                                    f'what it posted',
                     f'give this user the directory: sudo chown -R "$(id -u)" {d} (the Docker image runs as uid 10001: '
                     f'chown -R 10001:10001 on the host)')
            return
        if not cfg.state.exists():
            n = pub.journal.publish_count()
            if n:
                self.add(FAIL, 'state', f'{rel} is missing, but the journal records {n} publish/import(s); --apply '
                                        f'refuses to run',
                         'restore the state file from your backup (publishing without it could post things twice), '
                         'or point [paths] state back at it')
            else:
                self.add(OK, 'state', f'{rel}: not created yet (the first publish --apply creates it, mode 0600)')
            return
        try:
            st = pub.load_state()
        except Stop as e:
            other = 'belongs to Instagram account' in str(e)
            self.add(FAIL, 'state', str(e), 'use one state file per account: set [paths] state for this account'
                     if other else 'restore the state file from your backup')
            return
        except OSError as e:
            self.add(FAIL, 'state', f'{rel} cannot be read: {e.strerror}',
                     f'the user that runs ig-publish must own it: sudo chown -R "$(id -u)" {cfg.state.parent}')
            return
        mode = stat.S_IMODE(os.stat(cfg.state).st_mode)
        n_pub = sum(1 for r in st['items'].values() if r.get('status') == 'published')
        self.add(OK, 'state', f'{rel}: {n_pub} published item(s) recorded')
        if mode & 0o077:
            self.add(NOTE, 'state', f'{rel} is readable by group or others (mode {mode:04o})', f'chmod 600 {rel}')
        h = st.get('hold')
        if h and float(h.get('until_ts') or 0) > time.time():
            self.add(NOTE, 'hold', f"{h.get('reason')} ({h.get('key')}); --apply waits (exit 75)", pub.hold_text(h))

    # ------------------------------------------------------------------ token
    def check_token(self, pub: Publisher) -> bool:
        test = pub.endpoints.test_mode
        where = f'{TEST_TOKEN_ENV} (local test server)' if test else pub.cfg.token.describe()
        try:
            tok = pub.client.token()
        except TokenError as e:
            self.add(FAIL, 'token', str(e), e.fix or f'see "{where}" in {SETUP_STORE}')
            return False
        except ConfigError as e:
            self.add(FAIL, 'token', str(e), f'complete the [token] section ({SETUP_STORE})')
            return False
        self.add(OK, 'token', f'read from {where}: {len(tok)} characters (the value is never shown)')
        for note in token_notes(tok):
            self.add(NOTE, 'token', note)
        return True

    # ------------------------------------------------------------------ Meta API
    @staticmethod
    def api_fix(e: GraphError, fallback: str = '') -> str:
        return hint(e) or fallback or f'look the error up in {TROUBLESHOOTING}'

    def check_api(self, pub: Publisher, items: list[dict]) -> None:
        try:
            self._check_api(pub, items)
        except UsageLimit as e:
            # Nothing more is sent above the threshold, so the remaining checks could not run: not a pass.
            done = {n for _m, n in self.results}
            self.not_run = [n for n in API_CHECKS if n not in done]
            self.add(FAIL, 'API usage', str(e), "wait about an hour (Meta's usage window rolls over), then run "
                                                '`ig-publish doctor` again; nothing was written')
            for name in self.not_run:
                self.add(SKIP, name, 'not run: Meta API usage is above [api] usage_stop_percent')

    @staticmethod
    def not_the_token(e: GraphError) -> bool:
        """Errors that say nothing about the token: no answer, a redirect, rate limits, temporary Meta errors."""
        code = e.code if isinstance(e.code, int) and not isinstance(e.code, bool) else None
        return (e.status == 0 or 300 <= e.status <= 399 or e.status == 429 or e.status >= 500
                or code in RATE_CODES or code in (1, 2, -1, -2, 341) or bool(e.err.get('is_transient')))

    def _check_api(self, pub: Publisher, items: list[dict]) -> None:
        c, cfg = pub.client, pub.cfg
        try:
            me = c.get('me', {'fields': 'id,name'}, retries=1)
        except GraphError as e:
            if self.not_the_token(e):
                self.add(FAIL, 'Meta API reachable', e.text(), self.api_fix(e))
                self.add(SKIP, 'Meta API', 'the remaining checks need an answer from Meta')
            else:
                self.add(FAIL, 'token works', e.text(), self.api_fix(e))
                self.add(SKIP, 'Meta API', 'the remaining checks need a working token')
            return
        self.add(OK, 'token works', f"owner {me.get('name')} ({me.get('id')}) | Graph {cfg.api_version}")
        if not self.check_permissions(pub):
            self.add(SKIP, 'Meta API', 'the remaining checks need the publishing permissions')
            return
        self.check_account(pub)
        self.check_account_type(pub, items)
        self.check_page(pub)
        self.check_quota(pub)
        pct = c.usage_pct()
        self.add(OK if pct < cfg.safety.usage_stop_percent else NOTE, 'API usage',
                 f'highest Meta usage header {pct}% (ig-publish stops sending at {cfg.safety.usage_stop_percent}%)')

    def check_permissions(self, pub: Publisher) -> bool:
        try:
            rows = pub.client.get('me/permissions', retries=1).get('data', [])
        except GraphError as e:
            self.add(FAIL, 'permissions', e.text(), self.api_fix(e))
            return False
        status = {str(p.get('permission')): str(p.get('status')) for p in rows if isinstance(p, dict)}
        self.granted = {p for p, s in status.items() if s == 'granted'}
        ok = True
        for perm in REQUIRED_SCOPES:
            if perm in self.granted:
                continue
            ok = False
            if status.get(perm) == 'declined':
                self.add(FAIL, 'permissions', f'{perm} was declined in the login dialog',
                         'generate the token again and, in the dialog, choose "Edit access" / "Edit settings" (names '
                         'may differ) to allow it - an existing token never gains permissions.\n'
                         + ADD_PERMISSION.format(perm=perm).split('\n', 1)[1])
            else:
                self.add(FAIL, 'permissions', f'{perm} is missing (needed to publish)',
                         ADD_PERMISSION.format(perm=perm))
        if not ok:
            return False
        extra = []
        if 'pages_show_list' not in self.granted:
            extra.append('pages_show_list not granted: the Page check below cannot list your Pages')
        if 'instagram_manage_contents' not in self.granted:
            extra.append('instagram_manage_contents not granted: only `delete --apply` needs it')
        self.add(OK, 'permissions', ', '.join(REQUIRED_SCOPES) + (f" ({'; '.join(extra)})" if extra else ''))
        return True

    def check_account(self, pub: Publisher) -> None:
        cfg = pub.cfg
        try:
            ig = pub.client.get(cfg.ig_user_id, {'fields': 'id,username'}, retries=1)
        except GraphError as e:
            code = e.code if isinstance(e.code, int) else None
            access = code in (10, 100) or (code is not None and 200 <= code <= 299)
            self.add(FAIL, 'Instagram account', f'{cfg.ig_user_id} cannot be read: {e.text()}',
                     FIND_ID if access else self.api_fix(e, FIND_ID))
            return
        user = ig.get('username')
        if cfg.username and user != cfg.username:
            self.add(FAIL, 'Instagram account', f'{cfg.ig_user_id} is @{user}, the configuration expects '
                                                f'@{cfg.username}',
                     f'set [account] ig_user_id to the account you mean ({SETUP_ID}), or correct [account] username')
            return
        self.ig_visible = True
        self.add(OK, 'Instagram account', f"@{user} ({ig.get('id', cfg.ig_user_id)})")

    def check_account_type(self, pub: Publisher, items: list[dict]) -> None:
        if not self.ig_visible:
            self.add(SKIP, 'account type', 'needs the Instagram account (above)')
            return
        try:
            r = pub.client.get(pub.cfg.ig_user_id, {'fields': 'account_type'}, retries=1)
        except GraphError:
            r = {}  # the API does not report the field for this account (or this API version)
        kind = r.get('account_type') if isinstance(r, dict) else None
        stories = any(it['kind'] == 'story' for it in items)
        if kind is None:
            self.add(NOTE, 'account type', 'not reported by the API. Stories need a Business account. Creator accounts '
                                           'cannot publish stories through the API: observed (Oct 2026)',
                     f'check it once in the app: {ACCOUNT_TYPE}')
        elif str(kind).upper() == 'BUSINESS':
            self.add(OK, 'account type', 'Business')
        else:
            self.add(FAIL if stories else NOTE, 'account type',
                     f'{kind}: Creator accounts cannot publish stories through the API - observed (Oct 2026)'
                     + ('' if stories else '; the manifest has no stories'), f'switch to Business: {ACCOUNT_TYPE}')

    def check_page(self, pub: Publisher) -> None:
        cfg = pub.cfg
        try:
            accts = pub.client.get_all('me/accounts', {'fields': 'id,name,tasks,instagram_business_account'}, cap=500)
        except GraphError as e:
            self.add(FAIL, 'Page link', f'me/accounts cannot be read: {e.text()}', self.api_fix(e, LINK_PAGE))
            return
        page: dict[str, Any] | None
        if cfg.page_id:
            page = next((a for a in accts if str(a.get('id')) == cfg.page_id), None)
        else:
            page = next((a for a in accts if str((a.get('instagram_business_account') or {}).get('id'))
                         == cfg.ig_user_id), None)
        if page:
            tasks = set(page.get('tasks') or [])
            linked = str((page.get('instagram_business_account') or {}).get('id') or '')
            name = f"{page.get('name')} ({page.get('id')})"
            if linked != cfg.ig_user_id:
                self.add(FAIL, 'Page link', f'Page {name} is linked to Instagram account {linked or "(none)"}, not to '
                                            f'{cfg.ig_user_id}',
                         (f'if {linked} is your account, set [account] ig_user_id = "{linked}"; otherwise ' if linked
                          else '') + LINK_PAGE)
            elif not tasks & PAGE_TASKS:
                self.add(FAIL, 'Page link', f"Page {name}: your tasks are {', '.join(sorted(tasks)) or 'none'}; "
                                            f'publishing needs MANAGE or CREATE_CONTENT', PAGE_ACCESS)
            else:
                self.add(OK, 'Page link', f"Page {name} is linked to the account | your tasks include "
                                          f"{', '.join(sorted(tasks & PAGE_TASKS))}")
            return
        if cfg.page_id:
            self.add(FAIL, 'Page link', f'Page {cfg.page_id} is not in me/accounts: the token owner has no task on '
                                        f'it, or the Page was not ticked in the token dialog', LINK_PAGE)
        elif 'pages_show_list' not in self.granted:
            self.add(NOTE, 'Page link', 'not checked: pages_show_list is not granted',
                     ADD_PERMISSION.format(perm='pages_show_list').split('\n', 1)[0])
        else:
            self.add(NOTE if self.ig_visible else FAIL, 'Page link',
                     f'no Page linked to Instagram account {cfg.ig_user_id} was found in me/accounts', LINK_PAGE)

    def check_quota(self, pub: Publisher) -> None:
        try:
            r = pub.client.get(f'{pub.cfg.ig_user_id}/content_publishing_limit', {'fields': 'config,quota_usage'},
                               retries=1)
        except GraphError as e:
            self.add(FAIL, 'quota', f'content_publishing_limit cannot be read: {e.text()}', self.api_fix(e))
            return
        d = (r.get('data') or [{}])[0] if isinstance(r, dict) else {}
        conf = d.get('config') or {}
        total, used = conf.get('quota_total'), d.get('quota_usage')
        if not (isinstance(total, int) and isinstance(used, int)):
            self.add(NOTE, 'quota', f'not reported in the usual form (quota_total {total!r}, quota_usage {used!r})')
            return
        left = total - used
        text = f'{used}/{total} API posts used in the rolling 24 h | {left} left'
        if left <= 0:
            self.add(NOTE, 'quota', text + ': publishing waits (exit 75) until older posts leave the window',
                     'wait; `ig-publish status` shows the quota')
        else:
            self.add(OK, 'quota', text)


def run_doctor(config: str | None = None, *, offline: bool = False, out: TextIO | None = None) -> int:
    return Doctor(config, offline=offline, out=out).run()
