"""Unit tests: configuration, captions, redaction, token providers, endpoints, MP4 boxes, schedule parsing,
JSON schemas and ``init``. No network, no ffmpeg."""
from __future__ import annotations

import datetime as dt
import io
import json
import os
import shutil
import stat
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from helpers import SRC, clean_env, read, run_cli, write_json
from mock_graph import IG

from ig_publish.captions import caption_problems, squash
from ig_publish.config import CaptionRules, Season, parse_config, read_config_file
from ig_publish.errors import ConfigError, GraphError, TokenError
from ig_publish.graph import REAL_GRAPH, _drop_field, _encode_params, resolve_endpoints
from ig_publish.media import atoms, ffmpeg_version
from ig_publish.redact import Redactor
from ig_publish.schedule import DoneFile, parse_schedule, to_epoch, why_line
from ig_publish.timeutil import parse_aware, parse_ts, parse_when
from ig_publish.tokens import (
    TokenConfig,
    keychain_store_command,
    read_token,
    read_token_file,
    scrubbed_env,
    token_notes,
)

ROOT = os.path.dirname(SRC)
TEMPLATES = os.path.join(SRC, 'ig_publish', 'templates')
EXAMPLES = os.path.join(ROOT, 'examples')


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


class TmpDirTest(unittest.TestCase):
    def setUp(self) -> None:
        self.d = tempfile.mkdtemp(prefix='igp-unit-')

    def tearDown(self) -> None:
        shutil.rmtree(self.d, ignore_errors=True)


# ---------------------------------------------------------------------------------------------- config
class ConfigTest(TmpDirTest):
    def parse(self, data: dict):
        return parse_config(data, base_dir=Path(self.d))

    def test_minimal_config_and_defaults(self) -> None:
        c = self.parse({'account': {'ig_user_id': IG}})
        self.assertEqual((c.ig_user_id, c.api_version, c.token.source, c.token.env_var), (IG, 'v26.0', 'env',
                                                                                         'IG_ACCESS_TOKEN'))
        self.assertEqual(c.state, Path(self.d).resolve() / 'state' / 'ig-state.json')
        self.assertEqual(c.lock_path.name, 'ig-state.lock')
        self.assertEqual((c.spacing.story_seconds, c.spacing.reel_seconds), (10.0, 1800.0))
        self.assertEqual((c.safety.burst_max_posts, c.safety.burst_window_seconds), (10, 3 * 3600.0))
        self.assertEqual((c.prep.width, c.prep.height, c.prep.fps, c.prep.fit), (1080, 1920, 30, 'pad'))
        self.assertEqual(c.schedule.max_late_seconds, 6 * 3600.0)
        self.assertEqual(c.captions.max_length, 2200)

    def test_unknown_keys_and_sections_are_errors(self) -> None:
        with self.assertRaisesRegex(ConfigError, r'Unknown key\(s\) in \[spacing\]: reel_secnds'):
            self.parse({'account': {'ig_user_id': IG}, 'spacing': {'reel_secnds': 5}})
        with self.assertRaisesRegex(ConfigError, r'Unknown section\(s\): acount'):
            self.parse({'acount': {'ig_user_id': IG}})
        c = self.parse({'_comment': 'ok', 'account': {'ig_user_id': IG, '_note': 'comments are allowed'}})
        self.assertEqual(c.ig_user_id, IG)

    def test_values_are_validated(self) -> None:
        bad = [
            ({'account': {}}, 'ig_user_id is required'),
            ({'account': {'ig_user_id': '@brand'}}, 'not valid'),
            ({'account': {'ig_user_id': IG}, 'api': {'version': '26'}}, 'like "v26.0"'),
            ({'account': {'ig_user_id': IG}, 'token': {'source': 'vault'}}, 'source must be one of'),
            ({'account': {'ig_user_id': IG}, 'token': {'source': 'file'}}, 'path is required'),
            ({'account': {'ig_user_id': IG}, 'token': {'source': 'keychain'}}, 'keychain_service is required'),
            ({'account': {'ig_user_id': IG}, 'token': {'source': 'ssm'}}, 'ssm_parameter is required'),
            ({'account': {'ig_user_id': IG}, 'captions': {'max_length': 5000}}, 'out of range'),
            ({'account': {'ig_user_id': IG}, 'captions': {'max_hashtags': 31}}, 'out of range'),
            ({'account': {'ig_user_id': IG}, 'captions': {'min_hashtags': 5, 'max_hashtags': 2}}, 'larger than'),
            ({'account': {'ig_user_id': IG}, 'spacing': {'reel_seconds': '30'}}, 'must be a number'),
            ({'account': {'ig_user_id': IG}, 'sources': {'deny': ['(']}}, 'invalid regular expression'),
            ({'account': {'ig_user_id': IG}, 'prep': {'width': 1081}}, 'even numbers'),
            ({'account': {'ig_user_id': IG}, 'prep': {'audio_kbps': 192}}, 'out of range'),
            ({'account': {'ig_user_id': IG}, 'prep': {'fit': 'zoom'}}, 'pad, crop or stretch'),
            ({'account': {'ig_user_id': IG}, 'schedule': {'timezone': 'Mars/Olympus'}}, 'not a known IANA'),
            ({'account': {'ig_user_id': IG}, 'seasons': {'x': {'start': '2030-12-01T00:00:00',
                                                               'end': '2031-01-01T00:00:00Z'}}}, 'no UTC offset'),
            ({'account': {'ig_user_id': IG}, 'seasons': {'x': {'start': '2031-01-01T00:00:00Z',
                                                               'end': '2030-12-01T00:00:00Z'}}}, 'before end'),
            ({'account': {'ig_user_id': IG}, 'seasons': {'X Y': {'start': '2030-12-01T00:00:00Z',
                                                                 'end': '2031-01-01T00:00:00Z'}}}, 'lower-case'),
        ]
        for data, needle in bad:
            with self.subTest(needle=needle), self.assertRaises(ConfigError) as cm:
                self.parse(data)
            self.assertIn(needle, str(cm.exception))

    def test_paths_and_seasons(self) -> None:
        c = self.parse({'account': {'ig_user_id': IG}, 'paths': {'state': '/abs/state.json', 'media_dir': 'm'},
                        'token': {'source': 'file', 'path': 'secrets/token'},
                        'seasons': {'holiday': {'start': '2030-12-01T00:00:00-08:00', 'end': '2031-01-01T00:00:00Z',
                                                'caption_words': ['Christmas']}}})
        self.assertEqual(str(c.state), '/abs/state.json')
        self.assertEqual(c.media_dir, Path(self.d).resolve() / 'm')
        self.assertEqual(c.token.path, str(Path(self.d).resolve() / 'secrets' / 'token'))
        s = c.seasons['holiday']
        self.assertEqual(s.start, parse_aware('2030-12-01T08:00:00Z'))
        self.assertEqual(s.caption_words, ('christmas',))

    def test_season_bounds_may_be_native_toml_datetimes(self) -> None:
        minus8 = dt.timezone(dt.timedelta(hours=-8))
        c = self.parse({'account': {'ig_user_id': IG},
                        'seasons': {'holiday': {'start': dt.datetime(2030, 12, 1, tzinfo=minus8),
                                                'end': '2031-01-01T00:00:00Z'}}})
        self.assertEqual(c.seasons['holiday'].start, parse_aware('2030-12-01T08:00:00Z'))
        self.assertEqual(c.seasons['holiday'].start_text, '2030-12-01T00:00:00-08:00')
        for value, needle in ((dt.datetime(2030, 12, 1), 'has no UTC offset'),
                              (dt.date(2030, 12, 1), 'must be a date and time with a UTC offset')):
            with self.subTest(value=value), self.assertRaisesRegex(ConfigError, needle):
                self.parse({'account': {'ig_user_id': IG},
                            'seasons': {'x': {'start': value, 'end': '2031-01-01T00:00:00Z'}}})

    @unittest.skipUnless(have_toml(), 'needs tomllib (3.11+) or tomli')
    def test_native_toml_datetime_file(self) -> None:
        p = os.path.join(self.d, 'ig-publish.toml')
        with open(p, 'w', encoding='utf-8') as f:
            f.write(f'[account]\nig_user_id = "{IG}"\n[seasons.holiday]\nstart = 2030-12-01T00:00:00-08:00\n'
                    f'end = 2031-01-01T00:00:00+14:00\n')
        c = parse_config(read_config_file(Path(p)), base_dir=Path(self.d))
        self.assertEqual(c.seasons['holiday'].end, parse_aware('2030-12-31T10:00:00Z'))

    @unittest.skipUnless(have_toml(), 'needs tomllib (3.11+) or tomli')
    def test_toml_template_and_examples_load(self) -> None:
        for path in (os.path.join(TEMPLATES, 'ig-publish.toml'), os.path.join(EXAMPLES, 'ig-publish.toml')):
            c = parse_config(read_config_file(Path(path)), base_dir=Path(self.d))
            self.assertEqual(c.token.source, 'env')
        with open(os.path.join(TEMPLATES, 'ig-publish.toml'), encoding='utf-8') as a, \
                open(os.path.join(EXAMPLES, 'ig-publish.toml'), encoding='utf-8') as b:
            self.assertEqual(a.read(), b.read(), 'examples/ig-publish.toml drifted from the init template')

    def test_json_server_example_loads(self) -> None:
        c = parse_config(read_config_file(Path(EXAMPLES, 'ig-publish.server.json')), base_dir=Path(self.d))
        self.assertEqual(c.token.source, 'file')
        self.assertEqual(c.schedule.timezone, 'UTC')


# ---------------------------------------------------------------------------------------------- captions
class CaptionTest(unittest.TestCase):
    def test_rules(self) -> None:
        rules = CaptionRules(required_text=('Link in bio.',), min_hashtags=1, max_hashtags=3,
                             banned_words=('giveaway',), licensed_music=('Song Title', 'The Band'),
                             forbid_curly_quotes=True)
        seasons = {'holiday': Season('holiday', 0, 1, '', '', ('christmas',))}
        ok = 'A new level.\nLink in bio.\n#game'
        self.assertEqual(caption_problems(ok, None, rules, seasons), [])
        cases = [
            (ok.replace('#game', ''), '0 hashtags (min 1)'),
            (ok + ' #a #b #c', '4 hashtags (max 3)'),
            (ok.replace('Link in bio.', ''), 'required text missing'),
            (ok + ' #GiveawayFriday', "banned word 'giveaway'"),
            (ok + ' Music: song-title!', "licensed music 'Song Title'"),
            (ok + ' #SongTitle', "licensed music 'Song Title'"),
            (ok + ' by the band', "licensed music 'The Band'"),
            (ok + ' Merry #Christmas', 'may only appear in items with "season": "holiday"'),
            (ok.replace('A new', 'It’s a new'), 'curly quotes'),
            (ok + ' ' + ' '.join(f'@u{i}' for i in range(21)), '21 @-mentions (max 20)'),
            ('x' * 2201, 'characters (max 2200)'),
        ]
        for cap, needle in cases:
            with self.subTest(needle=needle):
                self.assertTrue(any(needle in p for p in caption_problems(cap, None, rules, seasons)),
                                caption_problems(cap, None, rules, seasons))
        self.assertEqual(caption_problems(ok + ' Merry #Christmas', 'holiday', rules, seasons), [])
        self.assertEqual(squash('Ça-va #Été!'), 'çavaété')


# ---------------------------------------------------------------------------------------------- redaction
class RedactTest(unittest.TestCase):
    def test_patterns_and_registered_secret(self) -> None:
        r = Redactor()
        self.assertEqual(r('x EAAexampleexampleexample1 y'), 'x EAA*** y')
        self.assertEqual(r('GET /me?access_token=abc123&fields=id'), 'GET /me?access_token=***&fields=id')
        fake_header = 'Authorization: Bearer abcdefghijkl0123'  # prepublish-audit:allow secret.authorization-header
        self.assertEqual(r(fake_header), 'Authorization: Bearer ***')
        self.assertEqual(r('OAuth abcdefghijkl0123'), 'OAuth ***')
        self.assertEqual(r('a Bearer token and OAuthException'), 'a Bearer token and OAuthException')
        r.register('plain-secret-value')
        self.assertEqual(r('{"name": "plain-secret-value"}'), '{"name": "***"}')
        r.clear()
        self.assertEqual(r('plain-secret-value'), 'plain-secret-value')


# ---------------------------------------------------------------------------------------------- tokens
class TokenTest(TmpDirTest):
    def env(self, **kw):
        e = {k: v for k, v in os.environ.items() if not k.startswith(('IG_PUBLISH_', 'IG_ACCESS_TOKEN'))}
        e.update(kw)
        return mock.patch.dict(os.environ, e, clear=True)

    def fake_tool(self, name: str, body: str) -> str:
        bind = os.path.join(self.d, 'bin')
        os.makedirs(bind, exist_ok=True)
        p = os.path.join(bind, name)
        with open(p, 'w') as f:
            f.write('#!/bin/sh\n' + body)
        os.chmod(p, 0o755)
        return bind

    def test_env_source(self) -> None:
        with self.env(IG_ACCESS_TOKEN='  value-from-env  '):
            self.assertEqual(read_token(TokenConfig(), test_mode=False), 'value-from-env')
        with self.env(), self.assertRaisesRegex(TokenError, 'environment variable IG_ACCESS_TOKEN'):
            read_token(TokenConfig(), test_mode=False)
        with self.env(MY_TOKEN='a b'), self.assertRaisesRegex(TokenError, 'whitespace'):
            read_token(TokenConfig(env_var='MY_TOKEN'), test_mode=False)

    def test_file_source_requires_private_regular_file(self) -> None:
        p = os.path.join(self.d, 'token')
        with open(p, 'w') as f:
            f.write('value-from-file\n')
        os.chmod(p, 0o644)
        with self.assertRaisesRegex(TokenError, 'group/others') as cm:
            read_token_file(p)
        self.assertEqual(cm.exception.fix, f'chmod 600 {p}')
        os.chmod(p, 0o600)
        with self.env():
            self.assertEqual(read_token(TokenConfig(source='file', path=p), test_mode=False), 'value-from-file')
        with self.assertRaisesRegex(TokenError, 'not a regular file'):
            os.chmod(self.d, 0o700)
            read_token_file(self.d)
        with self.assertRaisesRegex(TokenError, 'not found'):
            read_token_file(os.path.join(self.d, 'missing'))

    def test_keychain_source_uses_security_w_and_times_out_as_temporary(self) -> None:
        args = os.path.join(self.d, 'args')
        bind = self.fake_tool('security', f'echo "$@" > "{args}"\necho value-from-keychain\n')
        cfg = TokenConfig(source='keychain', keychain_service='ig-publish-test', keychain_account='me')
        with self.env(PATH=f"{bind}:{os.environ.get('PATH', '')}"):
            self.assertEqual(read_token(cfg, test_mode=False), 'value-from-keychain')
        self.assertEqual(read(args).split(), ['find-generic-password', '-a', 'me', '-s', 'ig-publish-test', '-w'])
        bind = self.fake_tool('security', 'sleep 5\n')
        with self.env(PATH=f"{bind}:{os.environ.get('PATH', '')}"), self.assertRaises(TokenError) as cm:
            read_token(TokenConfig(source='keychain', keychain_service='s', timeout_seconds=1), test_mode=False)
        self.assertTrue(cm.exception.temporary)

    def test_ssm_source_uses_with_decryption_and_redacts_errors(self) -> None:
        args = os.path.join(self.d, 'args')
        bind = self.fake_tool('aws', f'echo "$@" > "{args}"\necho value-from-ssm\n')
        cfg = TokenConfig(source='ssm', ssm_parameter='/example/ig-publish/access-token', ssm_region='us-east-1')
        with self.env(PATH=f"{bind}:{os.environ.get('PATH', '')}"):
            self.assertEqual(read_token(cfg, test_mode=False), 'value-from-ssm')
        a = read(args).split()
        self.assertEqual(a[:4], ['ssm', 'get-parameter', '--name', '/example/ig-publish/access-token'])
        self.assertIn('--with-decryption', a)
        self.assertEqual(a[a.index('--region') + 1], 'us-east-1')
        bind = self.fake_tool('aws', 'echo "AccessDenied access_token=leaky123" >&2\nexit 254\n')
        with self.env(PATH=f"{bind}:{os.environ.get('PATH', '')}"), self.assertRaises(TokenError) as cm:
            read_token(cfg, test_mode=False)
        self.assertIn('exit 254', str(cm.exception))
        self.assertIn('access_token=***', str(cm.exception))
        self.assertNotIn('leaky123', str(cm.exception))

    def test_missing_keychain_item_explains_a_non_truncating_way_to_store_it(self) -> None:
        bind = self.fake_tool('security', 'exit 44\n')
        with self.env(PATH=f"{bind}:{os.environ.get('PATH', '')}"), self.assertRaises(TokenError) as cm:
            read_token(TokenConfig(source='keychain', keychain_service='ig-publish'), test_mode=False)
        self.assertIn("No Keychain item for service 'ig-publish'", str(cm.exception))
        fix = cm.exception.fix
        self.assertIn('security add-generic-password -U -a "$USER" -s ig-publish -w "$(pbpaste)"', fix)
        self.assertIn('do not press Enter yet', fix)
        self.assertIn('pbcopy </dev/null', fix)
        self.assertIn('128 characters', fix)
        self.assertIn("-a 'my account'", keychain_store_command('ig-publish', 'my account'))

    def test_stored_command_text_quotes_and_shape_notes(self) -> None:
        """The clipboard held the command, not the token: say so (and never echo the value)."""
        stored = 'security add-generic-password -U -a me -s ig-publish -w "$(pbpaste)"'
        with self.env(IG_ACCESS_TOKEN=stored), self.assertRaises(TokenError) as cm:
            read_token(TokenConfig(), test_mode=False)
        self.assertIn('is a shell command, not a token', str(cm.exception))
        self.assertIn('never pasted together', cm.exception.fix)
        self.assertIn('read -rs IG_ACCESS_TOKEN && export IG_ACCESS_TOKEN', cm.exception.fix)  # the env source's own
        self.assertNotIn('pbpaste', str(cm.exception))
        with self.env(IG_ACCESS_TOKEN='"EAAquotedvalue0123456789"'), self.assertRaises(TokenError) as cm:
            read_token(TokenConfig(), test_mode=False)
        self.assertIn('wrapped in quotes', str(cm.exception))
        self.assertNotIn('EAAquoted', str(cm.exception) + cm.exception.fix)
        with self.env(), self.assertRaises(TokenError) as cm:
            read_token(TokenConfig(), test_mode=False)
        self.assertIn('read -rs IG_ACCESS_TOKEN && export IG_ACCESS_TOKEN', cm.exception.fix)
        self.assertEqual(token_notes('E' * 200), [])
        self.assertIn('exactly 128 characters', token_notes('E' * 128)[0])
        self.assertIn('Instagram Login token', token_notes('IGAA' + 'x' * 150)[0])

    def test_locked_keychain_is_not_reported_as_a_missing_item(self) -> None:
        """Only exit 44 means "no such item"; a locked keychain (ssh, launchd, cron) must not ask to store it again."""
        bind = self.fake_tool('security', 'echo "security: SecKeychainSearchCopyNext: User interaction is not '
                                          'allowed." >&2\nexit 36\n')
        cfg = TokenConfig(source='keychain', keychain_service='ig-publish')
        with self.env(PATH=f"{bind}:{os.environ.get('PATH', '')}"), self.assertRaises(TokenError) as cm:
            read_token(cfg, test_mode=False)
        msg, fix = str(cm.exception), cm.exception.fix
        self.assertIn("The Keychain could not be read (service 'ig-publish', `security` exit 36", msg)
        self.assertIn('User interaction is not allowed', msg)
        self.assertNotIn('No Keychain item', msg)
        self.assertTrue(cm.exception.temporary)
        self.assertIn('security unlock-keychain', fix)
        self.assertIn('source = "file"', fix)
        self.assertNotIn('add-generic-password', fix)  # the item is there: do not store it again
        bind = self.fake_tool('security', 'exit 0\n')  # the item exists but holds nothing
        with self.env(PATH=f"{bind}:{os.environ.get('PATH', '')}"), self.assertRaises(TokenError) as cm:
            read_token(cfg, test_mode=False)
        self.assertIn("Keychain service 'ig-publish' is empty or not set", str(cm.exception))
        self.assertIn('-w "$(pbpaste)"', cm.exception.fix)

    def test_fixes_carry_the_store_command_of_the_source(self) -> None:
        folder = os.path.join(self.d, 'new folder')
        with self.assertRaises(TokenError) as cm:
            read_token_file(os.path.join(folder, 'token'))
        self.assertTrue(cm.exception.temporary)
        self.assertTrue(cm.exception.fix.startswith(f"mkdir -p -m 700 '{folder}' && read -rs T && (umask 077 && "
                                                    f"printf '%s' \"$T\" > '{folder}/token'); unset T"),
                        cm.exception.fix)
        bind = self.fake_tool('security', "echo 'security add-generic-password -U -a me -s ig-publish -w x'\n")
        with self.env(PATH=f"{bind}:{os.environ.get('PATH', '')}"), self.assertRaises(TokenError) as cm:
            read_token(TokenConfig(source='keychain', keychain_service='ig-publish'), test_mode=False)
        self.assertIn('is a shell command, not a token', str(cm.exception))
        self.assertIn('security add-generic-password -U -a "$USER" -s ig-publish -w "$(pbpaste)"', cm.exception.fix)
        quoted = os.path.join(self.d, 'quoted')
        with open(quoted, 'w') as f:
            f.write('"EAAquotedvalue0123456789"')
        os.chmod(quoted, 0o600)
        with self.env(), self.assertRaises(TokenError) as cm:
            read_token(TokenConfig(source='file', path=quoted), test_mode=False)
        self.assertIn('wrapped in quotes', str(cm.exception))
        self.assertIn("read -rs T && (umask 077 && printf '%s' \"$T\" > ", cm.exception.fix)
        self.assertNotIn('EAAquoted', str(cm.exception) + cm.exception.fix)

    def test_helper_processes_get_no_token_variable(self) -> None:
        base = {'PATH': '/bin', 'IG_ACCESS_TOKEN': 'a', 'MY_TOKEN': 'b', 'IG_PUBLISH_TEST_TOKEN': 'c',
                'IG_PUBLISH_CONFIG': 'd', 'AWS_REGION': 'us-east-1'}
        self.assertEqual(scrubbed_env('MY_TOKEN', base), {'PATH': '/bin', 'AWS_REGION': 'us-east-1'})
        self.assertEqual(scrubbed_env(None, base), {'PATH': '/bin', 'MY_TOKEN': 'b', 'AWS_REGION': 'us-east-1'})
        args = os.path.join(self.d, 'env-seen')
        bind = self.fake_tool('aws', f'env > "{args}"\necho value-from-ssm\n')
        cfg = TokenConfig(source='ssm', ssm_parameter='/example/ig-publish/access-token')
        with self.env(PATH=f"{bind}:{os.environ.get('PATH', '')}", IG_ACCESS_TOKEN='stale-value-in-env'):
            self.assertEqual(read_token(cfg, test_mode=False), 'value-from-ssm')
        self.assertNotIn('stale-value-in-env', read(args))
        self.assertIn('AWS_PAGER=', read(args))

    def test_test_mode_uses_only_the_test_token(self) -> None:
        cfg = TokenConfig(source='keychain', keychain_service='never-read')
        with self.env(IG_PUBLISH_TEST_TOKEN='value-for-tests', PATH='/nonexistent'):
            self.assertEqual(read_token(cfg, test_mode=True), 'value-for-tests')
            with self.assertRaisesRegex(TokenError, 'only works together with a local test server'):
                read_token(cfg, test_mode=False)


# ---------------------------------------------------------------------------------------------- endpoints, graph
class EndpointTest(unittest.TestCase):
    def test_overrides_must_be_loopback_and_paired(self) -> None:
        self.assertEqual(resolve_endpoints({}).graph, REAL_GRAPH)
        e = resolve_endpoints({'IG_PUBLISH_API_BASE': 'http://127.0.0.1:9/', 'IG_PUBLISH_UPLOAD_BASE': 'http://localhost:9'})
        self.assertTrue(e.test_mode)
        self.assertEqual(e.graph, 'http://127.0.0.1:9')
        for env in ({'IG_PUBLISH_API_BASE': 'http://127.0.0.1:9'},
                    {'IG_PUBLISH_API_BASE': 'https://graph.example.com', 'IG_PUBLISH_UPLOAD_BASE': 'http://127.0.0.1:9'},
                    {'IG_PUBLISH_API_BASE': 'http://127.0.0.1.example.com', 'IG_PUBLISH_UPLOAD_BASE': 'http://127.0.0.1:9'}):
            with self.subTest(env=env), self.assertRaises(ConfigError):
                resolve_endpoints(env)

    def test_helpers(self) -> None:
        self.assertEqual(_drop_field('id,status,status_code,video_status', 'video_status'), 'id,status,status_code')
        self.assertEqual(_drop_field('id,a{b,c},d', 'a'), 'id,d')
        self.assertEqual(_encode_params({'share_to_feed': True, 'x': None, 'n': 5}),
                         {'share_to_feed': 'true', 'n': '5'})
        e = GraphError(400, {'message': 'bad Bearer abcdefghijkl0123', 'code': 100, 'error_subcode': 33}, 'GET x')
        self.assertIn('code 100/33', e.text())
        self.assertIn('Bearer ***', e.text())
        self.assertEqual((e.code, e.subcode), (100, 33))


# ---------------------------------------------------------------------------------------------- MP4 boxes
def box(typ: bytes, payload: bytes = b'') -> bytes:
    return struct.pack('>I4s', 8 + len(payload), typ) + payload


class AtomsTest(TmpDirTest):
    def write(self, data: bytes) -> str:
        p = os.path.join(self.d, 'x.mp4')
        with open(p, 'wb') as f:
            f.write(data)
        return p

    def test_moov_first_and_edit_list(self) -> None:
        moov = box(b'moov', box(b'trak', box(b'edts', box(b'elst', b'\x00' * 8)) + box(b'mdia')))
        a = atoms(self.write(box(b'ftyp', b'isom') + moov + box(b'mdat', b'\x00' * 32)))
        self.assertEqual((a['top'], a['moov_first'], a['elst']), (['ftyp', 'moov', 'mdat'], True, True))
        moov2 = box(b'moov', box(b'trak', box(b'mdia')))
        a = atoms(self.write(box(b'ftyp', b'isom') + box(b'mdat', b'\x00' * 32) + moov2))
        self.assertEqual((a['moov_first'], a['elst']), (False, False))
        big = struct.pack('>I4sQ', 1, b'mdat', 16 + 8) + b'\x00' * 8  # 64-bit box size
        a = atoms(self.write(box(b'ftyp', b'isom') + big + moov2))
        self.assertEqual(a['top'], ['ftyp', 'mdat', 'moov'])


# ---------------------------------------------------------------------------------------------- schedule
class ScheduleParseTest(TmpDirTest):
    def test_lines_and_time_zones(self) -> None:
        lines, errs = parse_schedule('# comment\n2030-01-02 10:00 a1,b-2   # note\n\n2030-01-02 1000 a1\n'
                                     '2030-01-02 10:00 A1\n2030-01-02 10:00 a1 extra\n2030-13-02 10:00 a1', 'UTC')
        self.assertEqual([ln.id for ln in lines], ['2030-01-02 10:00 a1,b-2'])
        self.assertEqual(len(errs), 4)
        self.assertEqual(to_epoch('2030-01-02', '10:00', 'UTC') - to_epoch('2030-01-02', '10:00', 'Asia/Tokyo'),
                         9 * 3600)

    def test_done_file_and_reason_line(self) -> None:
        d = DoneFile(os.path.join(self.d, 'done'))
        d.mark('L1', 'retry rc=75', 'at=100 next=200')
        d.mark('L1', 'retry rc=75', 'at=300 next=400')
        d.mark('L2', 'ok rc=0', 'start=10 end=500')
        self.assertEqual(d.retry_info('L1'), (400.0, 2))
        self.assertIsNone(d.status('L1'))
        self.assertEqual(d.status('L2'), 'ok rc=0')
        self.assertEqual(d.last_end(), 500.0)
        self.assertEqual(stat.S_IMODE(os.stat(d.path).st_mode), 0o600)
        self.assertEqual(why_line(['a', 'Stopped: x', '  ✗ y']), '✗ y')
        self.assertEqual(why_line(['a', 'Stopped: x']), 'Stopped: x')
        self.assertEqual(why_line(['a', 'b', '']), 'b')
        self.assertEqual(why_line(['x', '✗ Meta API: code 190/463', '  fix: make a new token', 'y']),
                         '✗ Meta API: code 190/463 | fix: make a new token')

    def test_parse_when(self) -> None:
        self.assertEqual(parse_when('1893456000.5'), 1893456000.5)  # 2030-01-01T00:00:00.5Z
        self.assertEqual(parse_when('2030-01-02T12:00:00Z'), parse_ts('2030-01-02T12:00:00Z'))
        for bad in ('nan', 'inf', '2030-01-02T12:00:00', 'soon'):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                parse_when(bad)

    def test_parse_ts(self) -> None:
        self.assertEqual(parse_ts('2030-01-02T12:00:00+0000'), parse_ts('2030-01-02T12:00:00Z'))
        self.assertEqual(parse_ts(None), 0.0)
        self.assertEqual(parse_ts('garbage'), 0.0)


# ---------------------------------------------------------------------------------------------- ffmpeg tools
FAKE_FFPROBE = '''#!/bin/sh
env > "$FAKE_DIR/ffprobe-env"
echo '{"format": {"duration": "1.0"}, "streams": []}'
'''
FAKE_FFMPEG = '''#!/bin/sh
env > "$FAKE_DIR/ffmpeg-env"
echo "ffmpeg version $FAKE_VERSION Copyright (c) 2000-2026 the FFmpeg developers"
'''


class FfmpegToolsTest(TmpDirTest):
    def setUp(self) -> None:
        super().setUp()
        self.bin = os.path.join(self.d, 'bin')
        os.makedirs(self.bin)
        for name, body in (('ffprobe', FAKE_FFPROBE), ('ffmpeg', FAKE_FFMPEG)):
            with open(os.path.join(self.bin, name), 'w') as f:
                f.write(body)
            os.chmod(os.path.join(self.bin, name), 0o755)
        self.clip = os.path.join(self.d, 'clip.mp4')
        with open(self.clip, 'wb') as f:
            f.write(b'\x00\x00\x00\x18ftypisom' + b'\x00' * 16)

    def env(self, version: str) -> dict:
        return clean_env({'PATH': f"{self.bin}:{os.environ.get('PATH', '')}", 'FAKE_DIR': self.d,
                          'FAKE_VERSION': version, 'IG_ACCESS_TOKEN': 'value-that-must-stay-here'})

    def test_version_parsing(self) -> None:
        for line, want in (('ffmpeg version 8.1.2 Copyright (c) 2000-2026', (8, 1)),
                           ('ffmpeg version n7.0.1 Copyright', (7, 0)),
                           ('ffmpeg version 4.4.2-0ubuntu0.22.04.1 Copyright', (4, 4)),
                           ('ffmpeg version 5.1.6-0+deb12u1 Copyright', (5, 1)),
                           ('ffmpeg version N-113000-g0123456789 Copyright', None),
                           ('ffmpeg version 2030-01-01-git-0123456789-full_build', None), ('', None)):
            with self.subTest(line=line):
                self.assertEqual(ffmpeg_version(line), want)

    def test_verify_without_a_config_and_without_the_token(self) -> None:
        p = run_cli(None, 'verify', self.clip, '--kind', 'story', env=self.env('4.4.2-0ubuntu0.22.04.1'), cwd=self.d)
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)  # the fake file fails the profile
        self.assertIn('the defaults; no configuration file found', p.stdout)
        self.assertIn('does not meet the prep profile for a story', p.stdout)  # ffprobe only: any version
        seen = read(os.path.join(self.d, 'ffprobe-env'))
        self.assertIn('FAKE_DIR=', seen)
        self.assertNotIn('value-that-must-stay-here', seen)

    def test_prep_needs_ffmpeg_5_1(self) -> None:
        write_json(os.path.join(self.d, 'manifest.json'),
                   {'items': [{'key': 'a1', 'kind': 'story', 'group': 'G', 'src': 'clip.mp4'}]})
        cfg = write_json(os.path.join(self.d, 'ig-publish.json'), {'account': {'ig_user_id': IG}})
        p = run_cli(cfg, 'prep', env=self.env('4.4.2-0ubuntu0.22.04.1'))
        self.assertEqual(p.returncode, 1)
        self.assertIn('FFmpeg 5.1 or newer is needed for `prep` (found 4.4)', p.stderr)
        self.assertNotIn('value-that-must-stay-here', read(os.path.join(self.d, 'ffmpeg-env')))
        self.assertFalse(os.path.exists(os.path.join(self.d, 'media', 'prep.json')))


# ---------------------------------------------------------------------------------------------- schemas
class SchemaTest(unittest.TestCase):
    def setUp(self) -> None:
        try:
            import jsonschema
        except ModuleNotFoundError:
            self.skipTest('jsonschema not installed (pip install -e ".[dev]")')
        self.js = jsonschema

    def load(self, name: str) -> dict:
        with open(os.path.join(SRC, 'ig_publish', 'schemas', name), encoding='utf-8') as f:
            return json.load(f)

    def test_manifest_schema(self) -> None:
        schema = self.load('manifest.schema.json')
        self.js.Draft202012Validator.check_schema(schema)
        v = self.js.Draft202012Validator(schema)
        for path in (os.path.join(TEMPLATES, 'manifest.json'), os.path.join(EXAMPLES, 'manifest.json')):
            with open(path, encoding='utf-8') as f:
                self.assertEqual(list(v.iter_errors(json.load(f))), [], path)
        bad_story = {'items': [{'key': 'a1', 'kind': 'story', 'group': 'G', 'src': 'a.mp4', 'caption': 'no'}]}
        self.assertTrue(list(v.iter_errors(bad_story)))
        typo = {'items': [{'key': 'r1', 'kind': 'reel', 'group': 'G', 'src': 'a.mp4', 'thumb_ofset': 1}]}
        self.assertTrue(list(v.iter_errors(typo)))

    def test_config_schema(self) -> None:
        schema = self.load('config.schema.json')
        self.js.Draft202012Validator.check_schema(schema)
        v = self.js.Draft202012Validator(schema)
        with open(os.path.join(EXAMPLES, 'ig-publish.server.json'), encoding='utf-8') as f:
            self.assertEqual(list(v.iter_errors(json.load(f))), [])
        self.assertTrue(list(v.iter_errors({'account': {'ig_user_id': IG}, 'spacing': {'reel_secnds': 1}})))


# ---------------------------------------------------------------------------------------------- init
class InitTest(TmpDirTest):
    def test_init_writes_templates_once(self) -> None:
        target = os.path.join(self.d, 'project')
        p = run_cli(None, 'init', target, env=clean_env())
        self.assertEqual(p.returncode, 0, p.stderr)
        for name in ('ig-publish.toml', 'manifest.json', 'schedule.txt'):
            self.assertTrue(os.path.isfile(os.path.join(target, name)), name)
        p = run_cli(None, 'init', target, env=clean_env())
        self.assertEqual(p.stdout.count('exists, left as is'), 3)
        if have_toml():
            p = run_cli(os.path.join(target, 'ig-publish.toml'), 'plan', '--offline', env=clean_env())
            self.assertEqual(p.returncode, 1)
            self.assertIn('source file not found: videos/story-teaser-1.mp4', p.stderr)

    def test_cli_without_config_explains_what_to_do(self) -> None:
        p = run_cli(None, 'plan', env=clean_env())
        self.assertEqual(p.returncode, 1)
        self.assertIn('ig-publish init', p.stderr)
        p = run_cli(None, '--version', env=clean_env())
        self.assertIn('ig-publish 0.1.0', p.stdout)


class LibraryTest(TmpDirTest):
    def test_public_api_plan_offline(self) -> None:
        import ig_publish
        with open(os.path.join(self.d, 'a.mp4'), 'wb') as f:
            f.write(b'\x00')
        write_json(os.path.join(self.d, 'manifest.json'),
                   {'items': [{'key': 'a1', 'kind': 'story', 'group': 'G', 'src': 'a.mp4'}]})
        cfg_path = write_json(os.path.join(self.d, 'ig-publish.json'), {'account': {'ig_user_id': IG}})
        buf = io.StringIO()
        with mock.patch.dict(os.environ, {k: v for k, v in os.environ.items()
                                          if not k.startswith('IG_PUBLISH_')}, clear=True):
            ig_publish.Publisher(ig_publish.load_config(cfg_path), ig_publish.Options(offline=True), out=buf).plan()
        self.assertIn('a1', buf.getvalue())
        self.assertIn('Nothing was written', buf.getvalue())
        self.assertEqual(ig_publish.EXIT_LATER, 75)
        self.assertTrue(sys.modules['ig_publish'].__version__)


if __name__ == '__main__':
    unittest.main(verbosity=2)
