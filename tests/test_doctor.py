"""``ig-publish doctor`` and the error hints.

The doctor runs as a subprocess (``python -m ig_publish doctor``) against the local mock of the Graph API; FFmpeg
is a fake shell script on ``PATH`` (shell built-ins only), so these tests need neither the network nor FFmpeg.
The hint table is unit-tested directly. All IDs are placeholders.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import time
import unittest

from helpers import clean_env, run_cli, write_json
from mock_graph import IG, MOCK, PAGE, TOKEN, USERNAME, start

from ig_publish.errors import GraphError, UsageLimit
from ig_publish.hints import (
    BY_CODE,
    BY_SUBCODE,
    OUTCOME_UNKNOWN,
    RATE_HOLD,
    REDIRECT,
    explain,
    fix_line,
    hint,
    hint_for,
)

FAKE_FFMPEG = '''#!/bin/sh
case "$*" in
  *-encoders*)
    [ "$FAKE_DROP" = libx264 ] || echo ' V....D libx264              libx264 H.264 / AVC / MPEG-4 AVC'
    echo ' A....D aac                  AAC (Advanced Audio Coding)' ;;
  *-filters*)
    for f in scale pad crop setsar format anullsrc; do echo " .. $f              V->V       test filter"; done ;;
  *) echo "ffmpeg version ${FAKE_VERSION:-7.1} Copyright (c) 2000-2030 the FFmpeg developers" ;;
esac
'''
FAKE_FFPROBE = '''#!/bin/sh
echo "ffprobe version ${FAKE_VERSION:-7.1} Copyright (c) 2007-2030 the FFmpeg developers"
'''
OTHER_IG = '17841409876543210'  # placeholder: "another" Instagram account
DEAD_PROXY = {'HTTPS_PROXY': 'http://127.0.0.1:9', 'https_proxy': 'http://127.0.0.1:9', 'NO_PROXY': '', 'no_proxy': ''}
EXPIRED = (400, 190, 463, 'Error validating access token: Session has expired.')
NOT_VISIBLE = (400, 100, 33, f"Unsupported get request. Object with ID '{IG}' does not exist, cannot be loaded due to "
                             f"missing permissions, or does not support this operation.")
DEAD_API = {'IG_PUBLISH_API_BASE': 'http://127.0.0.1:9', 'IG_PUBLISH_UPLOAD_BASE': 'http://127.0.0.1:9'}


def have_toml() -> bool:
    try:
        import tomllib  # noqa: F401
        return True
    except ModuleNotFoundError:
        try:
            import tomli  # noqa: F401
            return True
        except ModuleNotFoundError:
            return False


class DoctorTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.srv = start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.srv.shutdown()
        cls.srv.server_close()

    def setUp(self) -> None:
        MOCK.reset()
        self.d = tempfile.mkdtemp(prefix='igp-doctor-')
        self.bin = self.tools(os.path.join(self.d, 'bin'), ffmpeg=True)
        with open(os.path.join(self.d, 'a.mp4'), 'wb') as f:
            f.write(b'\x00\x00\x00\x18ftypisom')
        self.items = [{'key': 'st-1', 'kind': 'story', 'group': 'G', 'src': 'a.mp4'},
                      {'key': 'reel-1', 'kind': 'reel', 'group': 'R', 'src': 'a.mp4', 'caption': 'Hello.'}]
        write_json(os.path.join(self.d, 'manifest.json'), {'items': self.items})
        self.cfg = write_json(os.path.join(self.d, 'ig-publish.json'),
                              {'account': {'ig_user_id': IG, 'username': USERNAME}})

    def tearDown(self) -> None:
        shutil.rmtree(self.d, ignore_errors=True)
        self.assertEqual(MOCK.violations, [], 'protocol violations seen by the mock')

    @staticmethod
    def tools(path: str, *, ffmpeg: bool) -> str:
        os.makedirs(path, exist_ok=True)
        for name, body in (('ffmpeg', FAKE_FFMPEG), ('ffprobe', FAKE_FFPROBE)):
            if name == 'ffmpeg' and not ffmpeg:
                continue
            with open(os.path.join(path, name), 'w') as f:
                f.write(body)
            os.chmod(os.path.join(path, name), 0o755)
        return path

    def env(self, extra: dict | None = None, *, test_server: bool = True) -> dict:
        e = {'PATH': self.bin}  # only the fake tools: a real FFmpeg on this machine must not mask a failure
        if test_server:
            e.update({'IG_PUBLISH_API_BASE': MOCK.base, 'IG_PUBLISH_UPLOAD_BASE': MOCK.base,
                      'IG_PUBLISH_TEST_TOKEN': MOCK.token})
        else:
            e.update(DEAD_PROXY)  # real endpoints: any request that slipped out would fail locally
        return clean_env(dict(e, **(extra or {})))

    def doctor(self, *args: str, rc: int = 0, cfg: str | None = 'default', env: dict | None = None,
               cwd: str | None = None) -> str:
        p = run_cli(self.cfg if cfg == 'default' else cfg, 'doctor', *args, env=env or self.env(), cwd=cwd)
        out = p.stdout + p.stderr
        self.assertEqual(p.returncode, rc, out)
        self.assertNotIn(MOCK.token, out, 'the token was printed')
        self.assertNotIn('Traceback', out)
        self.assertEqual([e for e in MOCK.log if e['method'] != 'GET'], [], 'doctor must only read')
        return out

    def files(self) -> list[str]:
        return sorted(os.listdir(self.d))

    def assertRow(self, out: str, head: str, detail: str) -> None:  # noqa: N802 - unittest naming style
        """A result row: ``<mark> <check name>``, column padding, then the detail."""
        self.assertRegex(out, re.escape(head) + r' +' + re.escape(detail))

    # ------------------------------------------------------------------ all good
    def test_01_a_healthy_setup_passes_and_writes_nothing(self) -> None:
        before = self.files()
        out = self.doctor()
        for needle in ('✓ configuration', '✓ manifest', '2 item(s): 1 story, 1 reel(s)', '✓ ffmpeg / ffprobe',
                       'FFmpeg 7.1', '! prepared media', '2 of 2 pending item(s) not prepared yet',
                       'fix: ig-publish prep', '✓ state', 'not created yet', '✓ token',
                       'IG_PUBLISH_TEST_TOKEN (local test server): 34 characters (the value is never shown)',
                       '✓ token works', 'owner Mock Owner (1000001)', '✓ permissions',
                       'instagram_manage_contents not granted', '✓ Instagram account', f'@{USERNAME} ({IG})',
                       '! account type', 'not reported by the API', 'Account type and tools', '✓ Page link',
                       f'Example Page ({PAGE}) is linked', '✓ quota', '0/50 API posts used', '✓ API usage',
                       '✓ doctor: no problems found', 'Reminder: a long-lived token lasts about 60 days'):
            self.assertIn(needle, out)
        self.assertNotIn('✗', out)
        self.assertEqual(self.files(), before, 'doctor created files')
        MOCK.account_type = 'BUSINESS'
        MOCK.scopes.append('instagram_manage_contents')
        out = self.doctor()
        self.assertRow(out, '✓ account type', 'Business')
        self.assertNotIn('instagram_manage_contents not granted', out)

    def test_02_offline_runs_only_the_local_checks(self) -> None:
        out = self.doctor('--offline', env=self.env(test_server=False))
        self.assertRow(out, '- token', 'skipped (--offline: no token, no network)')
        self.assertRow(out, '- Meta API', 'skipped (--offline)')
        self.assertNotIn('Reminder:', out)
        self.assertEqual(MOCK.log, [])

    # ------------------------------------------------------------------ token and permissions
    def test_03_expired_token_gets_the_renewal_steps(self) -> None:
        MOCK.me_error = EXPIRED
        out = self.doctor(rc=1)
        self.assertIn('✗ token works', out)
        self.assertIn('code 190/463', out)
        self.assertIn('fix: The token expired (long-lived tokens last about 60 days)', out)
        self.assertIn('Extend Access Token', out)
        self.assertIn('the remaining checks need a working token', out)
        self.assertIn('✗ doctor: 1 problem(s): token works', out)
        self.assertEqual([e['path'] for e in MOCK.log], ['/v26.0/me'])

    def test_04_missing_and_declined_permissions(self) -> None:
        MOCK.scopes.remove('instagram_content_publish')
        out = self.doctor(rc=1)
        self.assertRow(out, '✗ permissions', 'instagram_content_publish is missing (needed to publish)')
        self.assertIn('fix: Graph API Explorer (developers.facebook.com/tools/explorer)', out)
        self.assertIn('tick BOTH the Facebook Page and the Instagram account', out)
        self.assertIn('"Manage messaging & content on Instagram" -> Customize', out)
        self.assertIn('the remaining checks need the publishing permissions', out)
        MOCK.declined = ['instagram_content_publish']
        out = self.doctor(rc=1)
        self.assertIn('instagram_content_publish was declined in the login dialog', out)
        self.assertIn('"Edit access"', out)

    def test_05_token_sources_fail_with_their_fix(self) -> None:
        out = self.doctor(rc=1, env=self.env(test_server=False))  # source "env", variable not set
        self.assertRow(out, '✗ token', 'No access token: environment variable IG_ACCESS_TOKEN')
        self.assertIn('fix: in this shell: read -rs IG_ACCESS_TOKEN && export IG_ACCESS_TOKEN', out)
        self.assertRow(out, '- Meta API', 'skipped: needs a readable token')
        tok = os.path.join(self.d, 'token')
        with open(tok, 'w') as f:
            f.write('value-in-a-loose-file')
        os.chmod(tok, 0o644)
        cfg = write_json(os.path.join(self.d, 'file.json'),
                         {'account': {'ig_user_id': IG}, 'token': {'source': 'file', 'path': tok}})
        out = self.doctor(rc=1, cfg=cfg, env=self.env(test_server=False))
        self.assertIn('is accessible to group/others (mode 0644)', out)
        self.assertIn(f'fix: chmod 600 {tok}', out)
        self.assertNotIn('value-in-a-loose-file', out)
        with open(tok, 'w') as f:
            f.write('security add-generic-password -U -a me -s ig-publish -w x')
        os.chmod(tok, 0o600)
        out = self.doctor(rc=1, cfg=cfg, env=self.env(test_server=False))
        self.assertIn('is a shell command, not a token', out)
        self.assertIn('never pasted together', out)
        self.assertIn(f"mkdir -p -m 700 {self.d} && read -rs T && (umask 077 && printf '%s' \"$T\" > {tok})", out)
        self.assertEqual(MOCK.log, [])

    def test_06_a_128_character_token_is_flagged(self) -> None:
        old = MOCK.token
        MOCK.token = 'E' * 128
        try:
            out = self.doctor(env=self.env())
        finally:
            MOCK.token = old
        self.assertIn('128 characters (the value is never shown)', out)
        self.assertRow(out, '! token', 'The token is exactly 128 characters long')
        self.assertIn('"$(pbpaste)"', out)

    # ------------------------------------------------------------------ account and Page
    def test_07_account_not_visible_and_page_problems(self) -> None:
        MOCK.ig_error = NOT_VISIBLE
        MOCK.pages = []
        out = self.doctor(rc=1)
        self.assertRow(out, '✗ Instagram account', f'{IG} cannot be read')
        self.assertIn('ig_user_id must be the numeric Instagram account ID, not the @username or the Page ID', out)
        self.assertRow(out, '- account type', 'needs the Instagram account')
        self.assertRow(out, '✗ Page link', f'no Page linked to Instagram account {IG}')
        self.assertIn('Settings -> Linked accounts -> Instagram -> Connect', out)
        MOCK.ig_error = None
        MOCK.pages = [{'id': PAGE, 'name': 'Example Page', 'tasks': ['ANALYZE'],
                       'instagram_business_account': {'id': IG}}]
        out = self.doctor(rc=1)
        self.assertIn('your tasks are ANALYZE; publishing needs MANAGE or CREATE_CONTENT', out)
        self.assertIn('fix: give your Facebook account full control of the Page', out)
        MOCK.pages = [{'id': PAGE, 'name': 'Example Page', 'tasks': ['MANAGE'],
                       'instagram_business_account': {'id': OTHER_IG}}]
        cfg = write_json(os.path.join(self.d, 'page.json'), {'account': {'ig_user_id': IG, 'page_id': PAGE}})
        out = self.doctor(rc=1, cfg=cfg)
        self.assertIn(f'is linked to Instagram account {OTHER_IG}, not to {IG}', out)
        self.assertIn(f'set [account] ig_user_id = "{OTHER_IG}"', out)
        MOCK.pages = None
        cfg = write_json(os.path.join(self.d, 'who.json'), {'account': {'ig_user_id': IG, 'username': 'someone.else'}})
        out = self.doctor(rc=1, cfg=cfg)
        self.assertIn(f'{IG} is @{USERNAME}, the configuration expects @someone.else', out)

    def test_08_creator_account_fails_only_with_stories(self) -> None:
        MOCK.account_type = 'MEDIA_CREATOR'
        out = self.doctor(rc=1)
        self.assertRow(out, '✗ account type', 'MEDIA_CREATOR: Creator accounts cannot publish stories')
        self.assertIn('observed (Oct 2026)', out)
        write_json(os.path.join(self.d, 'manifest.json'), {'items': self.items[1:]})
        out = self.doctor()
        self.assertIn('! account type', out)
        self.assertIn('the manifest has no stories', out)

    def test_09_quota_used_up_is_a_note(self) -> None:
        MOCK.quota_used, MOCK.quota_total = 50, 50
        out = self.doctor()
        self.assertRow(out, '! quota', '50/50 API posts used in the rolling 24 h | 0 left')

    # ------------------------------------------------------------------ local problems
    def test_10_no_configuration(self) -> None:
        empty = os.path.join(self.d, 'empty')
        os.makedirs(empty)
        out = self.doctor(rc=1, cfg=None, cwd=empty)
        self.assertRow(out, '✗ configuration', 'No configuration file')
        self.assertIn('fix: in your project folder run `ig-publish init`', out)
        self.assertRow(out, '- token', 'skipped')
        self.assertEqual(MOCK.log, [])
        out = self.doctor(rc=1, env=self.env({'IG_PUBLISH_API_BASE': 'https://graph.example.com'}))
        self.assertRow(out, '✗ API endpoints', 'IG_PUBLISH_API_BASE / IG_PUBLISH_UPLOAD_BASE exist only for local test')
        self.assertIn('fix: unset IG_PUBLISH_API_BASE and IG_PUBLISH_UPLOAD_BASE', out)
        self.assertEqual(MOCK.log, [])

    def test_11_manifest_and_ffmpeg_problems(self) -> None:
        write_json(os.path.join(self.d, 'manifest.json'),
                   {'items': [{'key': 'st-1', 'kind': 'story', 'group': 'G', 'src': 'missing.mp4'}]})
        out = self.doctor(rc=1)
        self.assertIn('✗ manifest', out)
        self.assertIn('source file not found: missing.mp4', out)
        self.assertIn('`ig-publish plan --offline` checks them again', out)
        write_json(os.path.join(self.d, 'manifest.json'), {'items': self.items})
        out = self.doctor(rc=1, env=self.env({'FAKE_VERSION': '4.4.2-0ubuntu0.22.04.1'}))
        self.assertIn('FFmpeg 4.4.2-0ubuntu0.22.04.1: `prep` needs 5.1 or newer', out)
        self.assertIn('fix: install FFmpeg 5.1 or newer', out)
        out = self.doctor(rc=1, env=self.env({'FAKE_DROP': 'libx264'}))
        self.assertIn('lacks libx264', out)
        nobin = self.tools(os.path.join(self.d, 'probe-only'), ffmpeg=False)
        out = self.doctor(rc=1, env=self.env({'PATH': nobin}))
        self.assertRow(out, '✗ ffmpeg / ffprobe', 'ffmpeg not found on PATH')
        self.assertIn('brew install ffmpeg', out)

    def test_12_state_problems_and_hold(self) -> None:
        st = os.path.join(self.d, 'state', 'ig-state.json')
        write_json(st, {'ig_user': OTHER_IG, 'items': {}})
        out = self.doctor(rc=1)
        self.assertIn(f'belongs to Instagram account {OTHER_IG}', out)
        self.assertIn('fix: use one state file per account', out)
        hold = {'reason': 'rate limited (code 4)', 'key': 'st-1', 'until_ts': time.time() + 3600,
                'until': '2030-01-15T12:00:00+00:00', 'set_at': '2030-01-15T11:00:00+00:00', 'code': 4}
        write_json(st, {'ig_user': IG, 'items': {'st-1': {'status': 'published', 'kind': 'story'}}, 'hold': hold})
        os.chmod(st, 0o600)
        out = self.doctor()
        self.assertIn('1 published item(s) recorded', out)
        self.assertRow(out, '! hold', 'rate limited (code 4) (st-1)')
        self.assertIn('--ignore-hold', out)
        os.remove(st)
        with open(os.path.join(self.d, 'state', 'ig-journal.jsonl'), 'w') as f:
            f.write(json.dumps({'method': 'POST', 'path': f'v26.0/{IG}/media_publish', 'status': 200}) + '\n')
        out = self.doctor(rc=1)
        self.assertIn('is missing, but the journal records 1 publish/import(s)', out)
        self.assertIn('fix: restore the state file from your backup', out)

    # ------------------------------------------------------------------ the CLI appends the same fixes
    def test_13_cli_errors_carry_a_fix_line(self) -> None:
        MOCK.me_error = EXPIRED
        p = run_cli(self.cfg, 'check', env=self.env())
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn('✗ Meta API: GET me: HTTP 400 | code 190/463', p.stderr)
        self.assertIn('\n  fix: The token expired', p.stderr)
        p = run_cli(self.cfg, 'check', env=self.env(test_server=False))
        self.assertIn('✗ token: No access token: environment variable IG_ACCESS_TOKEN', p.stderr)
        self.assertIn('\n  fix: in this shell: read -rs IG_ACCESS_TOKEN', p.stderr)
        self.assertNotIn(TOKEN, p.stdout + p.stderr)
        MOCK.me_error = (400, 4, None, 'Application request limit reached')  # reads retry it: keep the backoff short
        fast = write_json(os.path.join(self.d, 'fast.json'),
                          {'account': {'ig_user_id': IG}, 'timing': {'read_retry_backoff_seconds': 0.01}})
        p = run_cli(fast, 'check', env=self.env())
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn('fix: Rate limited: Meta is limiting requests', p.stderr)
        self.assertNotIn('holds', p.stderr)  # check sets no hold

    # ------------------------------------------------------------------ incomplete runs and matching fixes
    def test_14_usage_limit_lists_the_checks_not_run_and_fails(self) -> None:
        MOCK.usage = 90  # from the first answer on: nothing more may be sent
        out = self.doctor(rc=1)
        self.assertRow(out, '✓ token works', 'owner Mock Owner')
        self.assertRow(out, '✗ API usage', 'Stopped: Meta API usage is at 90% (threshold 85%)')
        self.assertIn('then run `ig-publish doctor` again', out)
        for name in ('permissions', 'Instagram account', 'account type', 'Page link', 'quota'):
            self.assertRow(out, f'- {name}', 'not run: Meta API usage is above [api] usage_stop_percent')
        self.assertIn('incomplete: 5 check(s) not run', out)
        self.assertNotIn('no problems found', out)
        self.assertEqual([e['path'] for e in MOCK.log], ['/v26.0/me'])

    def test_15_a_rate_limit_or_no_answer_is_not_blamed_on_the_token(self) -> None:
        MOCK.me_error = (400, 4, None, 'Application request limit reached')
        out = self.doctor(rc=1)
        self.assertRow(out, '✗ Meta API reachable', 'GET me: HTTP 400 | code 4 | type OAuthException')
        self.assertIn('fix: Rate limited: Meta is limiting requests from this app or account', out)
        self.assertNotIn('holds for at least an hour', out)  # doctor never sets a hold
        self.assertNotIn('token works', out)
        self.assertIn('the remaining checks need an answer from Meta', out)
        out = self.doctor(rc=1, env=self.env(DEAD_API))
        self.assertRow(out, '✗ Meta API reachable', 'GET me: no answer | message: network:')
        self.assertNotIn('code None', out)
        self.assertNotIn('type None', out)
        self.assertIn('fix: Network error: check the connection, VPN or proxy', out)

    def test_16_configuration_errors_get_a_matching_fix(self) -> None:
        env = self.env(test_server=False)
        if have_toml():
            twice = os.path.join(self.d, 'twice.toml')
            with open(twice, 'w') as f:  # what a newcomer gets by pasting a [token] block under init's file
                f.write(f'[account]\nig_user_id = "{IG}"\n\n[token]\nsource = "env"\n\n[token]\nsource = "keychain"\n')
            out = self.doctor('--offline', rc=1, cfg=twice, env=env)
            self.assertIn("Cannot declare ('token',) twice", out)
            self.assertIn('move these keys into the existing section and delete the second header', out)
        cfg = write_json(os.path.join(self.d, 'handle.json'), {'account': {'ig_user_id': '@example.brand'}})
        out = self.doctor('--offline', rc=1, cfg=cfg, env=env)
        self.assertIn('ig_user_id is the numeric Instagram account ID in quotes', out)
        self.assertIn('setup.md#5-find-the-instagram-account-id', out)
        cfg = write_json(os.path.join(self.d, 'typo.json'),
                         {'account': {'ig_user_id': IG}, 'api': {'usage_stop_pct': 80}})
        out = self.doctor('--offline', rc=1, cfg=cfg, env=env)
        self.assertRow(out, '✗ configuration', 'Unknown key(s) in [api]: usage_stop_pct')
        self.assertIn('https://github.com/deegitech/ig-publish/blob/main/examples/ig-publish.toml', out)
        cfg = write_json(os.path.join(self.d, 'kc.json'),
                         {'account': {'ig_user_id': IG}, 'token': {'source': 'keychain'}})
        out = self.doctor('--offline', rc=1, cfg=cfg, env=env)
        self.assertIn('fix: complete the [token] section for your source', out)

    @unittest.skipUnless(have_toml(), 'no TOML reader (Python 3.10 without tomli)')
    def test_17_examples_left_from_init_are_named(self) -> None:
        proj = os.path.join(self.d, 'project')
        p = run_cli(None, 'init', proj, env=self.env(test_server=False))
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        out = self.doctor('--offline', rc=1, cfg=os.path.join(proj, 'ig-publish.toml'), env=self.env(test_server=False))
        self.assertRow(out, '✗ manifest', 'Manifest errors in')
        self.assertIn('still lists the examples from `ig-publish init` (story-teaser-1, story-teaser-2, reel-launch): '
                      'replace them with your own files', out)
        self.assertIn('`ig-publish plan --offline` checks them again', out)
        self.assertNotIn('still lists the examples', self.doctor())  # the test project has its own items


class HintsTest(unittest.TestCase):
    def test_every_hint_is_one_line(self) -> None:
        for table in (BY_SUBCODE, BY_CODE, {'hold': RATE_HOLD, 'no id': OUTCOME_UNKNOWN, '3xx': REDIRECT}):
            for code, text in table.items():
                with self.subTest(code=code):
                    self.assertTrue(text.strip())
                    self.assertNotIn('\n', text)

    def test_mapping(self) -> None:
        cases = [
            ((190, 463), 'The token expired'), ((190, 460), 'password change'), ((190, 458), 'not authorized'),
            ((190, 459), 'checkpointed'), ((190, 464), 'not confirmed'), ((190, 467), 'no longer valid'),
            ((190, 492), 'role on the Page'), ((190, None), 'exactly 128 characters'), ((102, None), 'session'),
            ((10, None), 'permission is missing'), ((200, None), 'permission is missing'),
            ((299, None), 'permission is missing'), ((100, 33), 'numeric Instagram account ID'),
            ((31, 3858385), 'verify your identity'), ((9, 2207051), 'spam'), ((9, 2207042), 'publishing limit'),
            ((25, 2207050), 'checkpointed or restricted'), ((24, 2207008), 'Temporary publishing error'),
            ((9, 2207027), 'not ready yet'), ((24, 2207006), 'new container'), ((-2, 2207020), 'new container'),
            ((-1, 2207032), 'new container'), ((-1, 2207053), 'new container'), ((352, 2207026), 'ig-publish prep'),
            ((9004, 2207052), 'could not fetch'), ((9004, None), 'could not fetch'),
            ((341, None), 'Application limit'), ((368, None), 'policy'),
            ((1, None), 'Temporary Meta error'), ((2, None), 'Temporary Meta error'),
            ((-1, None), 'Temporary Meta error'), ((-2, None), 'Temporary Meta error'),
        ]
        cases += [((c, None), 'Rate limited') for c in (4, 17, 32, 613, 80001, 80002, 80004)]
        for (code, sub), needle in cases:
            with self.subTest(code=code, sub=sub):
                self.assertIn(needle, hint_for(code, sub, 400) or '')
        self.assertIn('observed (Oct 2026)', hint_for(31, 3858385))
        self.assertIn('Extend Access Token', hint_for(190, 463))
        self.assertIn('numeric Instagram account ID', hint_for(100, None, 400, NOT_VISIBLE[3]))
        self.assertIn('holds a Facebook Page ID', hint_for(100, None, 400, '(#100) Tried accessing nonexisting field '
                                                                     '(username) on node type (Page)'))
        self.assertIn('Rate limited', hint_for(None, None, 429))
        self.assertIn('Temporary Meta error', hint_for(None, None, 503))
        self.assertIn('Network error', hint_for(None, None, 0, 'network: timed out', 'GET me'))
        self.assertIn('upload did not complete', hint_for(None, None, 0, 'network: timed out', 'UPLOAD 424242'))
        self.assertIn('upload did not complete', hint_for(None, None, 200, 'upload not confirmed', 'UPLOAD 424242'))
        self.assertIn('spam', hint_for('9', '2207051', 400))  # numeric strings work too
        for code, sub, status, msg in ((100, None, 400, 'Invalid parameter'), (12345, None, 400, ''),
                                       (True, None, 400, ''), (None, None, 400, ''), (None, None, None, '')):
            with self.subTest(code=code, msg=msg):
                self.assertIsNone(hint_for(code, sub, status, msg))

    def test_explain_and_fix_line(self) -> None:
        e = GraphError(400, {'code': 190, 'error_subcode': 463, 'message': 'Session has expired', 'type': 'OAuth'},
                       'GET me')
        self.assertEqual(hint(e), BY_SUBCODE[463])
        self.assertTrue(explain(e).startswith('GET me: HTTP 400 | code 190/463'))
        self.assertIn('\n  fix: The token expired', explain(e))
        plain = GraphError(400, {'code': 100, 'message': 'Invalid parameter'}, 'GET x')
        self.assertEqual(explain(plain), plain.text())
        self.assertEqual(fix_line(plain), '')
        self.assertEqual(explain(UsageLimit('Stopped: usage')), 'Stopped: usage')
        self.assertIsNone(hint(ValueError('x')))
        self.assertIn('fix: Rate limited', explain(GraphError(429, {'message': 'Too many'}, 'GET me')))

    def test_redirects_transient_errors_rate_holds_and_unknown_outcomes(self) -> None:
        self.assertEqual(hint_for(None, None, 302), REDIRECT)
        self.assertIn('captive portal', hint_for(None, None, 307, '', 'GET me'))
        self.assertIn('fix: Meta answered with a redirect', explain(GraphError(302, {'message': '{}'}, 'GET me')))
        self.assertIsNone(hint_for(12345, None, 400))
        self.assertIn('Temporary Meta error', hint_for(12345, None, 400, transient=True))
        self.assertIn('Temporary Meta error', hint(GraphError(400, {'code': 12345, 'is_transient': True,
                                                                    'message': 'x'}, 'GET me')))
        self.assertIn('spam', hint_for(4, 2207051, 400, transient=True))  # the subcode still wins
        # Only a refused media_publish sets a hold; reads (doctor, check ...) and other writes do not.
        for code, status in ((4, 400), (80002, 400), (None, 429)):
            with self.subTest(code=code, status=status):
                self.assertEqual(hint_for(code, None, status, '', f'POST {IG}/media_publish'), RATE_HOLD)
                for where in ('GET me', f'POST {IG}/media', ''):
                    h = hint_for(code, None, status, '', where) or ''
                    self.assertIn('Wait about an hour', h)
                    self.assertNotIn('holds', h)
        no_id = GraphError(200, {'message': 'no id in the answer: {}'}, 'POST media_publish', unknown_outcome=True)
        self.assertEqual(hint(no_id), OUTCOME_UNKNOWN)
        self.assertNotIn('VPN', explain(no_id))
        self.assertEqual(no_id.text(), 'POST media_publish: HTTP 200 | message: no id in the answer: {}')
        self.assertEqual(GraphError(0, {'message': 'network: timed out'}, 'GET me').text(),
                         'GET me: no answer | message: network: timed out')


if __name__ == '__main__':
    unittest.main(verbosity=2)
