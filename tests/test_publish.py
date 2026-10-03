"""End-to-end tests of the CLI against a local mock of the Graph API and rupload (no real network).

The CLI runs as a subprocess (``python -m ig_publish``) exactly as a user or the scheduler runs it. Test videos
are generated with ffmpeg in a temporary directory and are deliberately NOT Instagram-ready (B-frames, edit list,
moov at the end, 192 kbps audio), so ``prep`` has real work to do.
"""
from __future__ import annotations

import datetime as dt
import fcntl
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest

from helpers import HAVE_FFMPEG, clean_env, deep_merge, make_clip, make_still, read, run_cli, write_json
from mock_graph import IG, MOCK, TOKEN, USERNAME, iso, start

LINK = 'Link in bio.'
TAGS = '#example #reels #test'
CAP1 = f'Reel one: a quick demo.\nTry the new mode.\n{LINK}\n{TAGS}'
CAP2 = f'Reel two: behind the scenes.\nMade by hand.\n{LINK}\n{TAGS}'
BASE_CFG = {
    'account': {'ig_user_id': IG, 'username': USERNAME},
    'token': {'source': 'env'},
    'spacing': {'story_seconds': 0, 'reel_seconds': 0},
    'timing': {'poll_seconds': 0.02, 'poll_timeout_seconds': 5, 'guard_wait_seconds': 0.02,
               'republish_wait_seconds': 0.02, 'rate_retry_seconds': 0.01, 'read_retry_backoff_seconds': 0.01},
    'captions': {'required_text': [LINK], 'min_hashtags': 3, 'max_hashtags': 8, 'banned_words': ['giveaway'],
                 'licensed_music': ['Example Song Title', 'The Example Band'], 'forbid_curly_quotes': True},
    'sources': {'deny': [r'-draft\.mp4$']},
    'seasons': {'holiday': {'start': '2030-12-01T00:00:00-08:00', 'end': '2031-01-01T00:00:00+14:00',
                            'caption_words': ['christmas']}},
}


@unittest.skipUnless(HAVE_FFMPEG, 'ffmpeg/ffprobe not installed')
class PublishTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.srv = start()
        cls.tmp = tempfile.mkdtemp(prefix='igp-test-')
        cls.media = os.path.join(cls.tmp, 'media')
        s1, s2 = os.path.join(cls.tmp, 'src-a.mp4'), os.path.join(cls.tmp, 'src-b.mp4')
        make_clip(s1, 3.4, 'testsrc2', 440)
        make_clip(s2, 3.6, 'testsrc', 660)
        cls.sources = {'a': s1, 'b': s2}
        cls.items = [
            {'key': 'st-a1', 'kind': 'story', 'group': 'ALPHA', 'src': 'src-a.mp4'},
            {'key': 'st-a2', 'kind': 'story', 'group': 'ALPHA', 'src': 'src-a.mp4'},
            {'key': 'st-b1', 'kind': 'story', 'group': 'BETA', 'src': 'src-b.mp4'},
            {'key': 'reel-1', 'kind': 'reel', 'group': 'REELS', 'src': 'src-b.mp4', 'caption': CAP1,
             'share_to_feed': True, 'thumb_offset': 1360},
            {'key': 'reel-2', 'kind': 'reel', 'group': 'REELS', 'src': 'src-a.mp4', 'caption': CAP2,
             'thumb_offset': 360},
        ]
        cls.keys = [i['key'] for i in cls.items]
        cls.manifest = write_json(os.path.join(cls.tmp, 'manifest.json'), {'ig_user_id': IG, 'items': cls.items})
        cls.all_outputs: list[str] = []
        cfg = cls.write_cfg(os.path.join(cls.tmp, 'prep-run'))
        p = cls.run_cls(cfg, 'prep')
        cls.prep_out = p.stdout + p.stderr
        if p.returncode != 0:
            raise AssertionError('prep failed:\n' + cls.prep_out)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.srv.shutdown()
        cls.srv.server_close()
        leaked = [o for o in cls.all_outputs if TOKEN in o]
        shutil.rmtree(cls.tmp, ignore_errors=True)
        assert not leaked, 'token leaked into the output'

    @classmethod
    def write_cfg(cls, d: str, manifest: str | None = None, **over) -> str:
        cfg = deep_merge(BASE_CFG, {'paths': {'manifest': manifest or cls.manifest, 'source_root': cls.tmp,
                                              'media_dir': cls.media,
                                              'state': os.path.join(d, 'state', 'ig-state.json'),
                                              'journal': os.path.join(d, 'state', 'ig-journal.jsonl')}})
        return write_json(os.path.join(d, 'ig-publish.json'), deep_merge(cfg, over))

    @classmethod
    def env(cls, extra: dict | None = None) -> dict:
        return clean_env(dict({'IG_PUBLISH_API_BASE': MOCK.base, 'IG_PUBLISH_UPLOAD_BASE': MOCK.base,
                               'IG_PUBLISH_TEST_TOKEN': TOKEN}, **(extra or {})))

    @classmethod
    def run_cls(cls, cfg: str, *args: str, extra: dict | None = None, env: dict | None = None):
        p = run_cli(cfg, *args, env=env or cls.env(extra))
        cls.all_outputs.append(p.stdout + p.stderr)
        return p

    def setUp(self) -> None:
        MOCK.reset()
        self.dir = tempfile.mkdtemp(dir=self.tmp)
        self.cfg = self.write_cfg(self.dir)
        self.state_path = os.path.join(self.dir, 'state', 'ig-state.json')
        self.journal_path = os.path.join(self.dir, 'state', 'ig-journal.jsonl')
        self.lock_path = os.path.join(self.dir, 'state', 'ig-state.lock')
        self.expect_violations = False

    def tearDown(self) -> None:
        for blob in (read(self.state_path), read(self.journal_path), read(os.path.join(self.media, 'prep.json'))):
            self.assertNotIn(TOKEN, blob, 'token written to a file')
        for out in self.all_outputs:
            self.assertNotIn(TOKEN, out, 'token printed')
        if not self.expect_violations:
            self.assertEqual(MOCK.violations, [], 'protocol violations seen by the mock')

    # ------------------------------------------------------------------ helpers
    def ig(self, *args: str, cfg: str | None = None, extra: dict | None = None, env: dict | None = None,
           rc: int | None = 0):
        p = self.run_cls(cfg or self.cfg, *args, extra=extra, env=env)
        if rc is not None:
            self.assertEqual(p.returncode, rc, f'exit {p.returncode}, wanted {rc}:\n{p.stdout}\n{p.stderr}')
        return p

    def cfg_with(self, manifest_items: list[dict] | None = None, name: str = 'alt', **over) -> str:
        manifest = None
        if manifest_items is not None:
            manifest = write_json(os.path.join(self.dir, f'manifest-{name}.json'), {'items': manifest_items})
        d = os.path.join(self.dir, f'cfg-{name}')
        os.makedirs(d, exist_ok=True)
        cfg = deep_merge(BASE_CFG, {'paths': {'manifest': manifest or self.manifest, 'source_root': self.tmp,
                                              'media_dir': self.media, 'state': self.state_path,
                                              'journal': self.journal_path}})
        return write_json(os.path.join(d, 'ig-publish.json'), deep_merge(cfg, over))

    def state(self) -> dict:
        return json.loads(read(self.state_path) or '{}')

    def save_state(self, st: dict) -> None:
        write_json(self.state_path, st)

    def journal(self) -> list[dict]:
        return [json.loads(line) for line in read(self.journal_path).splitlines() if line.strip()]

    def key_of(self, cid: str) -> str:
        return next(k for k, r in self.state()['items'].items()
                    if r.get('container_id') == cid or any(h.get('container_id') == cid for h in r.get('history', [])))

    def published_keys(self) -> list[str]:
        return [self.key_of(c) for c in MOCK.published]

    def calls(self, method: str, suffix: str) -> list[dict]:
        return [e for e in MOCK.log if e['method'] == method and e['path'].endswith(suffix)]

    def writes(self) -> list[dict]:
        return [e for e in MOCK.log if e['method'] != 'GET']

    def mock_mid(self, cid: str) -> str:
        return next(m['id'] for m in MOCK.media.values() if m['cid'] == cid)

    # ------------------------------------------------------------------ prep
    def test_01_prep_outputs_meet_spec_and_skip_on_rerun(self) -> None:
        from ig_publish.config import PrepSettings
        from ig_publish.media import verify
        self.assertIn('5/5 file(s) meet the spec', self.prep_out)
        self.assertEqual(self.prep_out.count('encoded in'), 2, self.prep_out)       # two sources, two encodes
        self.assertEqual(self.prep_out.count('copy (same source as'), 3, self.prep_out)  # three twins
        for it in self.items:
            rep = verify(os.path.join(self.media, f"{it['key']}.mp4"), it['kind'], PrepSettings())
            self.assertTrue(rep['ok'], (it['key'], rep['problems']))
            self.assertEqual(rep['has_b_frames'], 0)
            self.assertTrue(rep['moov_first'])
            self.assertFalse(rep['elst'])
            self.assertLessEqual(rep['abitrate'], 130_000)
            self.assertEqual((rep['width'], rep['height'], rep['fps']), (1080, 1920, 30))
            self.assertEqual(rep['x264']['bframes'], 0)
        bad = verify(self.sources['a'], 'story', PrepSettings())  # the raw source: verification must catch it
        joined = ' | '.join(bad['problems'])
        self.assertFalse(bad['ok'])
        for needle in ('B-frames present', 'edit list (elst) present', 'moov box is not first', 'audio bitrate'):
            self.assertIn(needle, joined)
        p = self.ig('prep')
        self.assertEqual(p.stdout.count('skipped (source unchanged)'), 5, p.stdout)
        self.assertNotIn('encoded in', p.stdout)

    # ------------------------------------------------------------------ read-only commands
    def test_02_plan_without_token_still_lists(self) -> None:
        p = self.ig('plan', extra={'IG_PUBLISH_TEST_TOKEN': ''})
        self.assertIn('no token', p.stdout)
        for k in self.keys:
            self.assertIn(k, p.stdout)
        self.assertIn('| Reel one: a quick demo.', p.stdout)  # pending reels: the full caption
        self.assertIn(f'| {LINK}', p.stdout)
        self.assertEqual(MOCK.log, [], 'plan without a token must not call the API')
        for path in (self.state_path, self.journal_path, self.lock_path):
            self.assertFalse(os.path.exists(path), path)

    def test_03_plan_offline_and_publish_dry_run_write_nothing(self) -> None:
        p = self.ig('plan', '--offline')
        self.assertIn('--offline', p.stdout)
        self.assertEqual(MOCK.log, [])
        p = self.ig('publish')  # no --apply: reads only (quota + live account)
        self.assertIn('Nothing was written', p.stdout)
        self.assertIn('preflight is clean', p.stdout)
        self.assertEqual(self.writes(), [])
        for path in (self.state_path, self.journal_path, self.lock_path):
            self.assertFalse(os.path.exists(path), path)

    def test_04_check(self) -> None:
        p = self.ig('check')
        for needle in ('Mock Owner', f'@{USERNAME}', 'instagram_content_publish', 'publishing permissions present',
                       'Quota: 0/50', 'Page: Example Page', 'MANAGE', 'PPA', 'instagram_manage_contents missing',
                       'check passed'):
            self.assertIn(needle, p.stdout)
        self.assertEqual(self.writes(), [])
        self.assertNotIn('debug_token', json.dumps(MOCK.log))
        MOCK.scopes.append('instagram_manage_contents')
        p = self.ig('check')
        self.assertIn('instagram_manage_contents present', p.stdout)
        MOCK.page_tasks = ['ANALYZE']  # no content task on the Page -> check fails
        p = self.ig('check', rc=1)
        self.assertIn('needs MANAGE or CREATE_CONTENT', p.stdout)
        MOCK.page_tasks = ['MANAGE']
        MOCK.scopes = [s for s in MOCK.scopes if s != 'instagram_content_publish']
        p = self.ig('check', rc=1)
        self.assertIn('missing for publishing: instagram_content_publish', p.stdout)
        cfg = self.cfg_with(name='who', account={'ig_user_id': IG, 'username': 'someone.else'})
        MOCK.scopes.append('instagram_content_publish')
        p = self.ig('check', cfg=cfg, rc=1)
        self.assertIn('expected @someone.else', p.stdout)

    # ------------------------------------------------------------------ publishing
    def test_05_publish_all_containers_first_in_order_then_no_double(self) -> None:
        p = self.ig('publish', '--apply')
        creates = self.calls('POST', f'/{IG}/media')
        uploads = [e for e in MOCK.log if e['path'].startswith('/ig-api-upload/')]
        pubs = self.calls('POST', '/media_publish')
        self.assertEqual((len(creates), len(uploads), len(pubs)), (5, 5, 5))
        self.assertLess(max(e['seq'] for e in creates + uploads), min(e['seq'] for e in pubs),
                        'a publish happened before every container was ready')
        self.assertEqual(self.published_keys(), self.keys, 'publish order != manifest order')
        st = self.state()['items']
        for it in self.items:
            r = st[it['key']]
            self.assertEqual(r['status'], 'published')
            self.assertTrue(r['media_id'] and r['permalink'] and r['published_at'] and r['container_id'])
            self.assertEqual(r['media_product_type'], 'STORY' if it['kind'] == 'story' else 'REELS')
            form = MOCK.containers[r['container_id']]['form']
            if it['kind'] == 'reel':
                self.assertEqual((form['caption'], form['share_to_feed'], form['thumb_offset']),
                                 (it['caption'], 'true', str(it['thumb_offset'])))
            else:
                self.assertEqual(form, {'media_type': 'STORIES', 'upload_type': 'resumable'})
            self.assertEqual(MOCK.containers[r['container_id']]['uploaded'],
                             os.path.getsize(os.path.join(self.media, f"{it['key']}.mp4")))
        j = self.journal()
        res = [x for x in j if x['method'] != 'INTENT']
        self.assertEqual(sum(1 for x in res if x['method'] == 'UPLOAD'), 5)
        self.assertEqual(sum(1 for x in res if x['path'].endswith('/media_publish')), 5)
        self.assertEqual(sum(1 for x in res if x['method'] == 'POST' and x['path'].endswith(f'{IG}/media')), 5)
        intents = [i for i, x in enumerate(j) if x['method'] == 'INTENT']
        self.assertEqual(len(intents), 15)  # every write: intent first, result right after
        for i in intents:
            self.assertEqual(j[i + 1]['method'], j[i]['body']['method'])
            self.assertEqual(j[i + 1]['path'], j[i]['path'])
        self.assertIn('✓ 5/5', p.stdout)
        self.assertIn('(0) preflight', p.stdout)
        p = self.ig('publish', '--apply')  # run again: nothing is published twice
        self.assertIn('Nothing to publish', p.stdout)
        self.assertEqual(len(MOCK.publish_calls), 5)

    def test_06_5xx_but_published_is_recorded_not_republished(self) -> None:
        MOCK.publish_fail = {2: '500_published'}
        p = self.ig('publish', '--apply')
        self.assertIn('but the container is PUBLISHED', p.stdout)
        cids = [c for c, _ in MOCK.publish_calls]
        self.assertEqual(len(cids), 5)
        self.assertEqual(len(set(cids)), 5, 'media_publish was retried blindly')
        r = self.state()['items']['st-a2']
        # a story ID found by a time match is kept as a CANDIDATE only, never as media_id
        self.assertEqual((r['status'], r.get('media_id'), r['media_id_candidate']),
                         ('published', None, self.mock_mid(r['container_id'])))
        self.assertEqual(self.published_keys(), self.keys)

    def test_07_5xx_not_published_stops_then_resume_publishes_once(self) -> None:
        MOCK.publish_fail = {2: '500'}
        p = self.ig('publish', '--apply', rc=1)
        self.assertIn('outcome is unknown', p.stdout + p.stderr)
        self.assertEqual(len(MOCK.publish_calls), 2)
        st = self.state()['items']
        self.assertEqual((st['st-a1']['status'], st['st-a2']['status']), ('published', 'publish_unknown'))
        self.assertNotEqual(st['st-b1'].get('status'), 'published')
        self.assertNotIn('hold', self.state(), 'a 5xx is not a stop code: no hold')
        MOCK.publish_fail = {}
        self.ig('publish', '--apply')
        self.assertEqual(MOCK.n_create, 5, 'containers are reused on resume')
        self.assertEqual(self.published_keys(), self.keys)
        self.assertEqual(sum(1 for c, _ in MOCK.publish_calls if c == st['st-a1']['container_id']), 1)

    def test_07b_media_publish_200_without_an_id_is_an_unknown_outcome(self) -> None:
        MOCK.publish_fail = {2: '200_no_id'}  # HTTP 200, no media ID, and the post did NOT go live
        p = self.ig('publish', '--apply', rc=1)
        out = p.stdout + p.stderr
        self.assertIn('st-a2: media_publish answered without a media ID - reading the container first', out)
        self.assertIn('outcome is unknown', out)
        self.assertIn('fix: Meta answered media_publish without a media ID', out)
        self.assertNotIn('was refused', out)  # never claimed as "not published"
        self.assertNotIn('VPN', out)  # not a network problem
        st = self.state()['items']
        self.assertEqual((st['st-a1']['status'], st['st-a2']['status']), ('published', 'publish_unknown'))
        MOCK.publish_fail = {}
        self.ig('publish', '--apply')  # the container is read first: FINISHED, so it is published (once)
        self.assertEqual(self.published_keys(), self.keys)

    def test_07c_media_publish_200_without_an_id_but_live_is_recorded(self) -> None:
        MOCK.publish_fail = {2: '200_no_id_published'}
        p = self.ig('publish', '--apply')
        self.assertIn('media_publish answered without a media ID but the container is PUBLISHED', p.stdout)
        self.assertEqual(len(MOCK.publish_calls), 5)
        self.assertEqual(self.published_keys(), self.keys)

    def test_08_error_container_publishes_nothing_then_resume(self) -> None:
        MOCK.container_error_at = {3}
        p = self.ig('publish', '--apply', rc=1)
        out = p.stdout + p.stderr
        self.assertIn('NOTHING was published', out)
        self.assertIn('2207026', out)
        self.assertEqual(MOCK.publish_calls, [])
        self.assertEqual(self.state()['items']['st-b1']['status'], 'error')
        MOCK.container_error_at = set()
        self.ig('publish', '--apply')
        self.assertEqual(MOCK.n_create, 6, 'only the ERROR container is re-created')
        self.assertEqual(self.published_keys(), self.keys)
        self.assertEqual(self.state()['items']['st-b1']['history'][0]['why'], 'status ERROR')

    def test_09_2207051_not_published_holds_24h_then_ignore_hold_resumes_without_double(self) -> None:
        MOCK.publish_fail = {2: (400, 9, 2207051)}
        p = self.ig('publish', '--apply', rc=1)
        out = p.stdout + p.stderr
        for needle in ('Stopped', '2207051', 'spam', 'was not published', 'Hold until', 'action blocked',
                       '--ignore-hold'):
            self.assertIn(needle, out)
        self.assertEqual((len(MOCK.publish_calls), len(MOCK.published)), (2, 1))
        st = self.state()
        self.assertEqual((st['items']['st-a1']['status'], st['items']['st-a2']['status']), ('published', 'error'))
        self.assertEqual(st['hold']['subcode'], 2207051)
        self.assertGreater(st['hold']['until_ts'], time.time() + 23.9 * 3600)
        n_log = len(MOCK.log)
        p = self.ig('publish', '--apply', rc=75)  # the hold is active: no writes, a temporary stop
        self.assertIn('a hold is active', p.stdout + p.stderr)
        self.assertEqual([e for e in MOCK.log[n_log:] if e['method'] != 'GET'], [])
        MOCK.publish_fail = {}
        p = self.ig('publish', '--apply', '--ignore-hold')
        self.assertIn('hold overridden on purpose', p.stdout)
        self.assertEqual(self.published_keys(), self.keys)
        self.assertEqual(MOCK.n_create, 5)
        self.assertEqual(sum(1 for c, _ in MOCK.publish_calls if c == st['items']['st-a1']['container_id']), 1)

    def test_10_rate_limit_429_stops_and_holds_an_hour(self) -> None:
        MOCK.publish_fail = {1: (429, 4, None)}
        p = self.ig('publish', '--apply', rc=1)
        self.assertIn('rate limited', p.stdout + p.stderr)
        self.assertIn('fix: Rate limited: ig-publish holds for at least an hour', p.stdout + p.stderr)
        self.assertEqual((len(MOCK.publish_calls), len(MOCK.published)), (1, 0))
        st = self.state()
        self.assertEqual(st['items']['st-a1']['status'], 'error')  # container read FINISHED: certainly not live
        self.assertGreater(st['hold']['until_ts'], time.time() + 3500)
        p = self.ig('publish', '--apply', rc=75)
        self.assertIn('a hold is active', p.stdout + p.stderr)
        self.assertEqual(len(MOCK.publish_calls), 1)

    def test_11_canary_limit_only(self) -> None:
        self.ig('publish', '--canary', '--apply')
        self.assertEqual((MOCK.n_create, self.published_keys()), (1, ['st-a1']))
        self.ig('publish', '--limit', '2', '--apply')
        self.assertEqual((MOCK.n_create, self.published_keys()), (3, ['st-a1', 'st-a2', 'st-b1']))
        self.ig('publish', '--only', 'reels', '--apply')
        self.assertEqual(self.published_keys(), self.keys)
        self.assertEqual(len(MOCK.publish_calls), 5)

    def test_12_crash_after_publish_before_state_write(self) -> None:
        self.ig('publish', '--apply')
        st = self.state()
        r = st['items']['st-b1']
        lost_mid = r['media_id']
        r['status'] = 'ready'  # the process died after media_publish, before writing the state
        for k in ('media_id', 'permalink', 'published_at', 'media_product_type', 'timestamp'):
            r.pop(k, None)
        self.save_state(st)
        p = self.ig('publish', '--apply')
        self.assertIn('ALREADY PUBLISHED', p.stdout)
        self.assertEqual(len(MOCK.publish_calls), 5, 'must not publish again')
        r = self.state()['items']['st-b1']
        self.assertEqual((r['status'], r['media_id_candidate']), ('published', lost_mid))

    def test_13_poll_timeout_publishes_nothing_and_stale_container_is_recreated(self) -> None:
        MOCK.never_finish = True
        cfg = self.cfg_with(name='slow', timing={'poll_timeout_seconds': 0.3})
        p = self.ig('publish', '--apply', cfg=cfg, rc=1)
        self.assertIn('did not become FINISHED', p.stdout + p.stderr)
        self.assertEqual((MOCK.publish_calls, MOCK.n_create), ([], 5))
        st = self.state()
        st['items']['st-a1']['container_created_ts'] -= 24 * 3600
        self.save_state(st)
        MOCK.never_finish = False
        self.ig('publish', '--apply')
        self.assertEqual(MOCK.n_create, 6, 'only the 24 h old container is re-created')
        self.assertIn('>= 23 h', self.state()['items']['st-a1']['history'][0]['why'])
        self.assertEqual(self.published_keys(), self.keys)

    def test_14_usage_stop_at_85_percent(self) -> None:
        MOCK.usage = 90
        p = self.ig('check', rc=1)
        self.assertIn('Meta API usage is at 90%', p.stdout + p.stderr)
        self.assertEqual(len(MOCK.log), 1)
        MOCK.reset()
        MOCK.usage = 90
        p = self.ig('publish', '--apply', rc=75)  # before any write: temporary
        self.assertIn('usage is at 90%', p.stdout + p.stderr)
        self.assertEqual(self.writes(), [])

    def test_15_quota_too_small_stops_before_any_write(self) -> None:
        MOCK.quota_total = 2
        p = self.ig('publish', '--apply', rc=75)
        self.assertIn('not enough quota', p.stdout + p.stderr)
        self.assertIn('--limit 2 would publish only: st-a1, st-a2', p.stdout + p.stderr)
        self.assertEqual(self.writes(), [])

    def test_16_delete_needs_manage_contents_then_key_stays_parked_until_repost(self) -> None:
        self.ig('publish', '--only', 'ALPHA', '--apply')
        mid = self.state()['items']['st-a1']['media_id']
        self.ig('delete', 'st-a1')
        self.assertEqual(self.calls('DELETE', mid), [])
        p = self.ig('delete', 'st-a1', '--apply', rc=1)  # the token lacks instagram_manage_contents
        self.assertIn('instagram_manage_contents', p.stdout + p.stderr)
        self.assertEqual(self.calls('DELETE', mid), [])
        MOCK.scopes.append('instagram_manage_contents')
        p = self.ig('delete', 'st-a1', '--apply')
        self.assertIn('deleted st-a1', p.stdout)
        self.assertIn(f'deleted_id {mid}', p.stdout)
        self.assertEqual(len(self.calls('DELETE', mid)), 1)
        r = self.state()['items']['st-a1']
        self.assertEqual((r['status'], r['history'][-1]['media_id']), ('deleted', mid))
        self.assertTrue(any(x['method'] == 'DELETE' for x in self.journal()))
        p = self.ig('publish', '--only', 'ALPHA', '--canary', '--apply')  # a deleted key never comes back by itself
        self.assertIn('Nothing to publish', p.stdout)
        self.assertIn('--repost st-a1', p.stdout)
        self.assertEqual(MOCK.n_create, 2)
        self.ig('publish', '--only', 'ALPHA', '--repost', 'st-a1', '--apply')
        self.assertEqual(MOCK.n_create, 3)
        self.assertEqual(self.state()['items']['st-a1']['status'], 'published')
        self.assertEqual(len(MOCK.published), 3)

    def test_17_story_gap_is_honoured(self) -> None:
        self.ig('publish', '--only', 'ALPHA', '--story-gap', '0.4', '--apply')
        (_, t1), (_, t2) = MOCK.publish_calls
        self.assertGreaterEqual(t2 - t1, 0.4)

    # ------------------------------------------------------------------ token safety
    def test_18_test_token_refused_unless_both_bases_are_loopback(self) -> None:
        for g, r in (('https://graph.invalid', MOCK.base), (MOCK.base, 'https://rupload.invalid'), (MOCK.base, '')):
            p = self.ig('check', extra={'IG_PUBLISH_API_BASE': g, 'IG_PUBLISH_UPLOAD_BASE': r}, rc=1)
            self.assertIn('exist only for local test servers', p.stdout + p.stderr)
        self.assertEqual(MOCK.log, [])

    def test_19_token_echoed_by_server_is_masked(self) -> None:
        MOCK.echo_token_on = 'content_publishing_limit'
        p = self.ig('check', rc=1)
        self.assertIn('Bearer ***', p.stdout + p.stderr)

    def test_19b_bare_token_in_an_error_is_masked(self) -> None:
        MOCK.echo_raw_on = 'content_publishing_limit'
        p = self.ig('check', rc=1)
        self.assertIn('token was ***; please check it', p.stdout + p.stderr)

    def test_20_status_table(self) -> None:
        self.ig('publish', '--canary', '--apply')
        p = self.ig('status')
        self.assertIn('st-a1', p.stdout)
        self.assertIn('published: 1', p.stdout)
        self.assertIn('Quota: 1/50', p.stdout)
        self.assertIn('1 live story leaves the tray at ', p.stdout)  # one time, not "between X and X"
        self.assertIn('add it to Highlights (ALPHA) in the Instagram app', p.stdout)  # only groups with live stories
        p = self.ig('status', '--offline')
        self.assertIn('Quota: --offline', p.stdout)

    def test_21_crash_while_publishing_then_old_container_is_not_republished(self) -> None:
        self.ig('publish', '--apply')
        st = self.state()
        r = st['items']['st-a1']
        lost_mid = r['media_id']
        r['status'] = 'publishing'  # media_publish went out, the process died; the next run is a day later
        r['container_created_ts'] -= 24 * 3600
        for k in ('media_id', 'permalink', 'published_at'):
            r.pop(k, None)
        self.save_state(st)
        p = self.ig('publish', '--apply')
        self.assertIn('ALREADY PUBLISHED', p.stdout)
        self.assertEqual((len(MOCK.publish_calls), MOCK.n_create), (5, 5),
                         'an old published container must never be posted again')
        r = self.state()['items']['st-a1']
        self.assertEqual((r['status'], r['media_id_candidate']), ('published', lost_mid))

    def test_21b_unreadable_on_reconcile_then_published_container_is_never_replaced(self) -> None:
        """The status-first guard in the container step, on its own: reconcile cannot read the container,
        the container step reads PUBLISHED and must record it, however old the container is."""
        self.ig('publish', '--apply')
        st = self.state()
        r = st['items']['st-b1']
        r['status'] = 'publishing'
        r['container_created_ts'] -= 24 * 3600
        for k in ('media_id', 'permalink', 'published_at'):
            r.pop(k, None)
        self.save_state(st)
        MOCK.status_fail = {MOCK.n_status + 1: (400, 100)}  # the next status read (reconcile) fails
        p = self.ig('publish', '--apply', rc=75)  # first line of defence: the live post is unknown to the state
        self.assertIn('NOT in the state file', p.stdout + p.stderr)
        MOCK.status_fail = {MOCK.n_status + 1: (400, 100)}
        p = self.ig('publish', '--apply', '--ack-unknown-posts')  # second line: the container step reads it
        self.assertIn('ALREADY PUBLISHED (published by an earlier run)', p.stdout)
        self.assertEqual((len(MOCK.publish_calls), MOCK.n_create), (5, 5))
        self.assertEqual(self.state()['items']['st-b1']['status'], 'published')

    def test_21c_unknown_outcome_and_unreadable_container_stops(self) -> None:
        """If the last attempt's outcome is unknown and the container cannot be read, nothing is guessed."""
        self.ig('publish', '--canary', '--apply')
        st = self.state()
        st['items']['st-a1']['status'] = 'publish_unknown'
        for k in ('media_id', 'permalink', 'published_at'):
            st['items']['st-a1'].pop(k, None)
        self.save_state(st)
        MOCK.status_fail = {MOCK.n_status + 1: (400, 100), MOCK.n_status + 2: (400, 100)}
        p = self.ig('publish', '--canary', '--apply', '--ack-unknown-posts', rc=1)
        self.assertIn('cannot be read', p.stdout + p.stderr)
        self.assertEqual((len(MOCK.publish_calls), MOCK.n_create), (1, 1))

    def test_21d_refused_publish_that_goes_live_later_is_never_replaced_while_unreadable(self) -> None:
        """media_publish is refused with a 'new container' subcode and the container reads FINISHED, so the record
        is 'error' + retire. If Meta publishes that container later and it cannot be read on the next run, the run
        must stop instead of creating and publishing a second container."""
        MOCK.publish_fail = {1: (400, 9, 2207032)}
        self.ig('publish', '--keys', 'st-a1', '--apply', rc=1)
        r = self.state()['items']['st-a1']
        self.assertEqual((r['status'], r['retire']), ('error', '9/2207032'))
        MOCK.publish_fail = {}
        MOCK.make_live(r['container_id'])  # it went live after all
        MOCK.status_fail = {MOCK.n_status + 1: (400, 100), MOCK.n_status + 2: (400, 100)}
        p = self.ig('publish', '--keys', 'st-a1', '--apply', '--ack-unknown-posts', rc=1)
        self.assertIn('cannot be read', p.stdout + p.stderr)
        self.assertEqual((len(MOCK.publish_calls), MOCK.n_create, len(MOCK.published)), (1, 1, 1))
        p = self.ig('publish', '--keys', 'st-a1', '--apply')  # readable again: recorded, never posted again
        self.assertIn('ALREADY PUBLISHED', p.stdout)
        self.assertEqual((len(MOCK.publish_calls), MOCK.n_create, len(MOCK.published)), (1, 1, 1))
        self.assertEqual(self.state()['items']['st-a1']['status'], 'published')

    # ------------------------------------------------------------------ media_publish errors
    def test_22_403_code4_2207051_but_post_went_live_is_recorded_held_and_never_reposted(self) -> None:
        MOCK.publish_fail = {2: {'http': 403, 'code': 4, 'sub': 2207051, 'transient': False, 'published': True}}
        p = self.ig('publish', '--apply', rc=1)
        out = p.stdout + p.stderr
        self.assertIn('WAS PUBLISHED (container PUBLISHED)', out)
        self.assertIn('spam', out)
        self.assertIn('action blocked', out)
        self.assertNotIn('st-a2 was not published', out)
        st = self.state()
        r = st['items']['st-a2']
        self.assertEqual(r['status'], 'published')
        self.assertEqual(r['media_id_candidate'], self.mock_mid(r['container_id']))
        self.assertEqual(st['hold']['subcode'], 2207051)
        self.assertEqual((len(MOCK.publish_calls), len(MOCK.published)), (2, 2))
        p = self.ig('publish', '--apply', rc=75)
        self.assertIn('a hold is active', p.stdout + p.stderr)
        self.assertEqual(len(MOCK.publish_calls), 2)
        self.ig('publish', '--apply', '--ignore-hold')
        self.assertEqual(self.published_keys(), self.keys)
        self.assertEqual(len(MOCK.publish_calls), 5)
        self.assertEqual(sum(1 for c, _ in MOCK.publish_calls if c == r['container_id']), 1, 'st-a2 posted twice')

    def test_23_2207051_marked_transient_still_stops_as_spam(self) -> None:
        MOCK.publish_fail = {2: {'http': 400, 'code': 4, 'sub': 2207051, 'transient': True}}
        p = self.ig('publish', '--apply', rc=1)
        out = p.stdout + p.stderr
        self.assertIn('spam', out)
        self.assertNotIn('Run the same command in a few minutes', out)
        st = self.state()
        self.assertEqual((st['items']['st-a2']['status'], st['hold']['subcode']), ('error', 2207051))

    def test_24_2207008_retried_once_then_published(self) -> None:
        MOCK.publish_fail = {2: (400, 24, 2207008)}
        p = self.ig('publish', '--apply')
        self.assertIn('retry 1/2', p.stdout)
        self.assertEqual(self.published_keys(), self.keys)
        self.assertEqual((len(MOCK.publish_calls), MOCK.n_create), (6, 5))

    def test_25_2207008_persisting_retires_container_and_rerun_builds_a_new_one(self) -> None:
        MOCK.publish_fail = {2: (400, 24, 2207008), 3: (400, 24, 2207008), 4: (400, 24, 2207008)}
        p = self.ig('publish', '--apply', rc=1)
        self.assertIn('new container', p.stdout + p.stderr)
        r = self.state()['items']['st-a2']
        self.assertEqual((r['status'], r['retire']), ('error', '24/2207008'))
        old = r['container_id']
        MOCK.publish_fail = {}
        self.ig('publish', '--apply')
        self.assertEqual(MOCK.n_create, 6)
        self.assertEqual(self.published_keys(), self.keys)
        self.assertNotIn(old, MOCK.published)

    def test_26_minus1_2207032_new_container_on_rerun(self) -> None:
        MOCK.publish_fail = {2: (400, -1, 2207032)}
        self.ig('publish', '--apply', rc=1)
        self.assertEqual(self.state()['items']['st-a2']['retire'], '-1/2207032')
        MOCK.publish_fail = {}
        self.ig('publish', '--apply')
        self.assertEqual(MOCK.n_create, 6)
        self.assertEqual(self.published_keys(), self.keys)

    def test_27_2207042_holds_until_quota_has_room(self) -> None:
        MOCK.publish_fail = {2: (400, 9, 2207042)}
        self.ig('publish', '--apply', rc=1)
        self.assertTrue(self.state()['hold']['until_quota'])
        MOCK.publish_fail = {}
        p = self.ig('publish', '--apply')  # the quota has room (49 left): the hold lifts by itself
        self.assertIn('the quota has room again', p.stdout)
        self.assertNotIn('hold', self.state())
        self.assertEqual(self.published_keys(), self.keys)

    def test_28_80002_on_publish_stops_with_regain_time_and_holds(self) -> None:
        MOCK.buc_regain = 90
        MOCK.publish_fail = {1: (400, 80002, None)}
        p = self.ig('publish', '--apply', rc=1)
        out = p.stdout + p.stderr
        self.assertIn('rate limited (code 80002)', out)
        self.assertIn('90 min', out)
        self.assertGreater(self.state()['hold']['until_ts'], time.time() + 89 * 60)

    def test_29_80002_on_container_poll_is_retried(self) -> None:
        MOCK.status_fail = {3: (400, 80002)}
        p = self.ig('publish', '--apply')
        self.assertIn('usage limit (code 80002)', p.stdout)
        self.assertEqual(self.published_keys(), self.keys)

    def test_30_upload_without_success_true_is_a_failure(self) -> None:
        MOCK.upload_reply = {3: (200, {'debug_info': {'retriable': False, 'type': 'ProcessingFailedError',
                                                      'message': 'x'}})}
        p = self.ig('publish', '--apply', rc=1)
        self.assertIn('upload not confirmed', p.stdout + p.stderr)
        self.assertEqual(MOCK.publish_calls, [])
        self.assertFalse(self.state()['items']['st-b1']['uploaded'])
        MOCK.upload_reply = {MOCK.n_upload + 1: (200, b'OK')}  # a non-JSON body is not a success either
        p = self.ig('publish', '--apply', rc=1)
        self.assertIn('upload not confirmed', p.stdout + p.stderr)
        MOCK.upload_reply = {}
        self.ig('publish', '--apply')
        self.assertEqual(self.published_keys(), self.keys)

    def test_30b_upload_url_on_another_host_is_refused_before_the_token_is_sent(self) -> None:
        for uri in ('http://127.0.0.1:9/ig-api-upload/v26.0/1', f'{MOCK.base}/elsewhere/1'):
            MOCK.reset()
            MOCK.upload_uri = uri
            p = self.ig('publish', '--canary', '--apply', rc=1)
            self.assertIn('Unexpected upload address; the token was not sent', p.stdout + p.stderr)
            self.assertEqual([e for e in MOCK.log if e['path'].startswith(('/ig-api-upload', '/elsewhere'))], [])
            self.assertEqual(MOCK.publish_calls, [])

    # ------------------------------------------------------------------ preflight: live account, burst, quota
    def test_31_unknown_live_post_blocks_until_acknowledged(self) -> None:
        MOCK.extra_live = [{'id': '616161000001', 'media_product_type': 'STORY', 'media_type': 'VIDEO',
                            'timestamp': iso(time.time() - 600),
                            'permalink': 'https://www.instagram.com/stories/example.brand/616161000001/'}]
        p = self.ig('publish', '--apply', rc=75)
        self.assertIn('NOT in the state file', p.stdout + p.stderr)
        self.assertEqual(self.writes(), [])
        p = self.ig('plan')  # plan shows it too (read-only)
        self.assertIn('NOT in the state file', p.stdout)
        self.ig('publish', '--canary', '--apply', '--ack-unknown-posts')
        self.assertIn('616161000001', self.state()['acknowledged'])
        self.ig('publish', '--apply')  # an acknowledged post is not asked about again
        self.assertEqual(self.published_keys(), self.keys)

    def test_31b_ack_records_posts_made_by_hand_without_publishing(self) -> None:
        """A story posted in the app blocks scheduler-style runs (exit 75) until `ack --apply` records it."""
        MOCK.extra_live = [{'id': '616161000004', 'media_product_type': 'STORY', 'media_type': 'VIDEO',
                            'timestamp': iso(time.time() - 300),
                            'permalink': 'https://www.instagram.com/stories/example.brand/616161000004/'}]
        p = self.ig('publish', '--keys', 'st-a1', '--apply', rc=75)
        self.assertIn('ig-publish ack --apply', p.stdout + p.stderr)
        p = self.ig('ack')  # dry run: lists the post, writes nothing
        self.assertIn('616161000004', p.stdout)
        self.assertIn('Nothing was written', p.stdout)
        self.assertFalse(os.path.exists(self.state_path))
        p = self.ig('ack', '--apply')
        self.assertIn('1 post(s) acknowledged', p.stdout)
        self.assertIn('616161000004', self.state()['acknowledged'])
        self.assertEqual(self.writes(), [], 'ack never writes to the API')
        self.assertIn('ACK', [j['method'] for j in self.journal()])
        self.ig('publish', '--keys', 'st-a1', '--apply')
        self.assertEqual(self.published_keys(), ['st-a1'])
        p = self.ig('ack', '--apply')
        self.assertIn('Nothing to acknowledge', p.stdout)
        os.remove(self.state_path)  # the journal records a publish: ack must not start a fresh state file
        p = self.ig('ack', '--apply', rc=1)
        self.assertIn('is missing', p.stdout + p.stderr)
        self.assertFalse(os.path.exists(self.state_path))

    def test_32_quota_higher_than_state_blocks(self) -> None:
        MOCK.quota_used = 3
        p = self.ig('publish', '--apply', rc=75)
        self.assertIn('the quota counts 3 API posts', p.stdout + p.stderr)
        self.assertEqual(self.writes(), [])
        self.ig('publish', '--apply', '--ack-unknown-posts')
        self.assertEqual(self.published_keys(), self.keys)

    def test_33_reel_caption_already_live_blocks_until_repost(self) -> None:
        MOCK.extra_live = [{'id': '616161000002', 'media_product_type': 'REELS', 'media_type': 'VIDEO', 'caption': CAP1,
                            'timestamp': iso(time.time() - 3 * 86400),
                            'permalink': 'https://www.instagram.com/reel/HAND1/'}]
        p = self.ig('publish', '--only', 'REELS', '--apply', rc=1)
        out = p.stdout + p.stderr
        self.assertIn('reel-1: a live post already has this exact caption', out)
        self.assertIn('--repost reel-1', out)
        self.assertEqual(self.writes(), [])
        self.ig('publish', '--only', 'REELS', '--repost', 'reel-1', '--apply')
        self.assertEqual(self.published_keys(), ['reel-1', 'reel-2'])

    def test_34_burst_guard(self) -> None:
        cfg = self.cfg_with(name='burst', safety={'burst_max_posts': 3})
        self.ig('publish', '--limit', '3', '--apply', cfg=cfg)
        p = self.ig('publish', '--apply', cfg=cfg, rc=75)
        self.assertIn('3 posts in the last 3 h', p.stdout + p.stderr)
        self.assertEqual(len(MOCK.publish_calls), 3)
        self.ig('publish', '--apply', '--ignore-hold', cfg=cfg)
        self.assertEqual(self.published_keys(), self.keys)

    def test_35_reel_gap_across_runs(self) -> None:
        self.ig('publish', '--only', 'REELS', '--canary', '--reel-gap', '3600', '--apply')
        p = self.ig('publish', '--only', 'REELS', '--reel-gap', '3600', '--apply', rc=75)
        self.assertIn('reel spacing is 60 min', p.stdout + p.stderr)
        self.ig('publish', '--only', 'REELS', '--reel-gap', '3600', '--apply', '--ignore-hold')
        self.assertEqual(self.published_keys(), ['reel-1', 'reel-2'])

    def test_36_already_live_and_needs_approval_are_skipped(self) -> None:
        items = [dict(i) for i in self.items if i['kind'] == 'reel']
        items[0]['already_live'] = {'media_id': '616161000077', 'permalink': 'https://www.instagram.com/reel/HAND/',
                                    'at': '2030-01-15T12:00:00+0000', 'via': 'posted in the app'}
        items[1]['needs_approval'] = 'waiting for the music decision'
        cfg = self.cfg_with(items, name='live')
        p = self.ig('plan', '--offline', cfg=cfg)
        self.assertIn('ALREADY LIVE (outside this tool', p.stdout)
        self.assertIn('AWAITING APPROVAL', p.stdout)
        p = self.ig('publish', '--apply', cfg=cfg)
        self.assertIn('Nothing to publish', p.stdout)
        self.assertIn('--approve reel-2', p.stdout)
        self.assertEqual(self.writes(), [])
        self.ig('publish', '--approve', 'reel-2', '--apply', cfg=cfg)
        self.assertEqual(self.published_keys(), ['reel-2'])
        self.ig('publish', '--repost', 'reel-1', '--apply', cfg=cfg)
        self.assertEqual(self.published_keys(), ['reel-2', 'reel-1'])
        p = self.ig('publish', '--repost', 'nope', '--apply', cfg=cfg, rc=1)
        self.assertIn('not in the manifest', p.stdout + p.stderr)

    # ------------------------------------------------------------------ import, lost state, locks
    def test_37_import_marks_outside_posts_published_and_publish_skips_them(self) -> None:
        now = time.time()
        rec = {'b1': {'group': 'BETA', 'src': 'src-b.mp4', 'product': 'STORY', 'container': '424299000001',
                      'media_id': '29999999999999901', 'at': iso(now - 90),
                      'permalink': 'https://www.instagram.com/stories/example.brand/29999999999999901/'},
               'a1': {'group': 'ALPHA', 'src': 'src-a.mp4', 'product': 'STORY', 'media_id': '29999999999999902',
                      'at': iso(now - 60)}}
        path = write_json(os.path.join(self.dir, 'record.json'), rec)
        p = self.ig('import', path, rc=1)  # src-a is used by two stories: ambiguous -> nothing is written
        self.assertIn('2 match(es)', p.stdout + p.stderr)
        self.assertFalse(os.path.exists(self.state_path))
        rec['a1'] = {'key': 'reel-2', 'media_id': '29999999999999902'}  # "at" missing
        write_json(path, rec)
        p = self.ig('import', path, rc=1)
        self.assertIn('"at" (ISO 8601 publish time) is required', p.stdout + p.stderr)
        del rec['a1']
        write_json(path, rec)
        p = self.ig('import', path)
        self.assertIn('Nothing was written', p.stdout)
        self.assertFalse(os.path.exists(self.state_path))
        self.ig('import', path, '--apply')
        r = self.state()['items']['st-b1']
        self.assertEqual((r['status'], r['media_id'], r['container_id'], r['published_at_source']),
                         ('published', '29999999999999901', '424299000001', 'record'))
        self.assertLess(abs(dt.datetime.fromisoformat(r['published_at']).timestamp() - (now - 90)), 2)
        self.assertEqual(sum(1 for x in self.journal() if x['method'] == 'IMPORT'), 1)
        p = self.ig('import', path, '--apply')
        self.assertIn('already imported', p.stdout)
        self.ig('publish', '--apply')
        self.assertEqual(self.published_keys(), [k for k in self.keys if k != 'st-b1'])
        os.remove(self.state_path)  # the state is lost but the journal has publishes -> --apply stops
        p = self.ig('publish', '--apply', rc=1)
        self.assertIn('state file', p.stdout + p.stderr)
        self.assertIn('is missing', p.stdout + p.stderr)

    def test_38_run_lock_blocks_a_second_writer(self) -> None:
        os.makedirs(os.path.dirname(self.lock_path), exist_ok=True)
        with open(self.lock_path, 'a+') as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            p = self.ig('publish', '--apply', rc=75)
            self.assertIn('another ig-publish run holds the lock', p.stdout + p.stderr)
            p = self.ig('delete', 'st-a1', '--apply', rc=75)
            self.assertIn('another ig-publish run holds the lock', p.stdout + p.stderr)
            self.ig('plan', '--offline')  # read-only commands ignore the lock
        self.assertEqual(MOCK.log, [])

    def test_39_two_concurrent_runs_never_double_post(self) -> None:
        MOCK.latency = 0.02
        env = self.env()
        cmd = [sys.executable, '-m', 'ig_publish', '--config', self.cfg, 'publish', '--apply']
        procs = [subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                  stdin=subprocess.DEVNULL) for _ in range(2)]
        outs = [p.communicate(timeout=300) for p in procs]
        self.all_outputs.extend(o + e for o, e in outs)
        self.assertIn(0, [p.returncode for p in procs])
        loser = [o + e for (o, e), p in zip(outs, procs, strict=True) if p.returncode != 0 or 'Nothing to publish' in o]
        self.assertEqual(len(loser), 1, outs)
        self.assertTrue('holds the lock' in loser[0] or 'Nothing to publish' in loser[0], loser[0])
        self.assertEqual((MOCK.n_create, len(MOCK.published)), (5, 5))
        self.assertEqual(self.published_keys(), self.keys)

    def test_40_candidate_media_id_needs_confirm_to_delete(self) -> None:
        MOCK.publish_fail = {2: '500_published'}
        self.ig('publish', '--only', 'ALPHA', '--apply')
        MOCK.scopes.append('instagram_manage_contents')
        cand = self.state()['items']['st-a2']['media_id_candidate']
        p = self.ig('delete', 'st-a2', '--apply', rc=1)
        self.assertIn('CANDIDATE', p.stdout + p.stderr)
        self.assertEqual(self.calls('DELETE', cand), [])
        self.ig('delete', 'st-a2', '--confirm-media', cand, '--apply')
        self.assertEqual(len(self.calls('DELETE', cand)), 1)

    def test_41_real_token_sources_are_never_used_with_test_endpoints(self) -> None:
        """A forgotten IG_PUBLISH_API_BASE must not carry a real token to another server."""
        bind = os.path.join(self.dir, 'bin')
        os.makedirs(bind)
        marker = os.path.join(self.dir, 'security-called')
        with open(os.path.join(bind, 'security'), 'w') as f:
            f.write(f'#!/bin/sh\necho called >> "{marker}"\necho fake-keychain-value\n')
        os.chmod(os.path.join(bind, 'security'), 0o755)
        cfg = self.cfg_with(name='kc', token={'source': 'keychain', 'keychain_service': 'ig-publish-test'})
        env = clean_env({'PATH': f"{bind}:{os.environ.get('PATH', '')}", 'IG_PUBLISH_API_BASE': MOCK.base,
                         'IG_PUBLISH_UPLOAD_BASE': MOCK.base})
        p = self.ig('check', cfg=cfg, env=env, rc=1)
        self.assertIn('No access token: IG_PUBLISH_TEST_TOKEN', p.stdout + p.stderr)
        # and the test token is refused against the real endpoints (a dead proxy catches any slip)
        env = clean_env({'PATH': f"{bind}:{os.environ.get('PATH', '')}", 'IG_PUBLISH_TEST_TOKEN': TOKEN,
                         'HTTPS_PROXY': 'http://127.0.0.1:9', 'https_proxy': 'http://127.0.0.1:9', 'NO_PROXY': '',
                         'no_proxy': ''})
        p = self.ig('check', cfg=cfg, env=env, rc=1)
        self.assertIn('only works together with a local test server', p.stdout + p.stderr)
        self.assertFalse(os.path.exists(marker), 'the Keychain was read although the endpoints were overridden')
        self.assertEqual(MOCK.log, [])

    def test_41b_missing_token_file_is_a_temporary_stop_before_any_write(self) -> None:
        """On a server the token file is written by another unit; until it exists, --apply exits 75."""
        missing = os.path.join(self.dir, 'no-such-token')
        cfg = self.cfg_with(name='tokfile', token={'source': 'file', 'path': missing})
        dead_proxy = {'HTTPS_PROXY': 'http://127.0.0.1:9', 'https_proxy': 'http://127.0.0.1:9', 'NO_PROXY': '',
                      'no_proxy': ''}  # real endpoints: any request that slipped out would fail locally
        p = self.ig('publish', '--canary', '--apply', cfg=cfg, env=clean_env(dead_proxy), rc=75)
        self.assertIn('Token file not found', p.stdout + p.stderr)
        p = self.ig('check', cfg=cfg, env=clean_env(dead_proxy), rc=1)
        self.assertIn('Token file not found', p.stdout + p.stderr)
        self.assertFalse(os.path.exists(self.journal_path))
        self.assertEqual(MOCK.log, [])

    def test_42_stdout_is_masked_even_for_200_bodies(self) -> None:
        MOCK.me_echo = True
        p = self.ig('check')
        self.assertIn('Mock Owner Bearer ***', p.stdout)

    # ------------------------------------------------------------------ content guards
    def test_43_manifest_content_guards(self) -> None:
        base = dict(self.items[3])
        draft = os.path.join(self.tmp, 'clip-draft.mp4')
        shutil.copyfile(self.sources['b'], draft)
        cases = [
            (dict(base, caption=CAP1.replace('a quick demo', 'giveaway time')), 'banned word'),
            (dict(base, caption=CAP1.replace('Try the new mode.', 'Music: example-song title!')), 'licensed music'),
            (dict(base, caption=CAP1 + ' #ExampleSongTitle'), 'licensed music'),
            (dict(base, caption=CAP1.replace(LINK, 'Get it now.')), 'required text missing'),
            (dict(base, caption=CAP1 + ' #a #b #c #d #e #f'), '9 hashtags (max 8)'),
            (dict(base, caption=CAP1.replace('a quick demo', 'it’s a quick demo')), 'curly quotes'),
            (dict(base, caption=CAP1.replace('a quick demo', 'Christmas night')), 'may only appear'),
            (dict(base, season='xmas'), "season 'xmas' is not defined"),
            (dict(base, src='clip-draft.mp4'), 'deny pattern'),
            (dict(base, thumb_ofset=10), 'unknown field(s) thumb_ofset'),
            (dict(self.items[0], caption='Stories take no caption'), 'not supported for API stories'),
            (dict(base, src='missing.mp4'), 'source file not found'),
            (dict(base, key='Bad Key'), 'key must be'),
        ]
        for n, (item, needle) in enumerate(cases):
            cfg = self.cfg_with([item], name=f'bad-{n}')
            p = self.ig('plan', '--offline', cfg=cfg, rc=1)
            self.assertIn(needle, p.stdout + p.stderr, (n, item))
        self.assertEqual(MOCK.log, [])

    # ------------------------------------------------------------------ --keys (the scheduler publishes per line)
    def test_44_keys_publish_exactly_the_named_items(self) -> None:
        p = self.ig('publish', '--keys', 'reel-2')  # dry run: only the selection is shown
        self.assertIn('--keys reel-2', p.stdout)
        self.assertIn('Nothing was written', p.stdout)
        self.assertNotIn('st-a1', p.stdout)
        self.assertEqual(self.writes(), [])
        self.ig('publish', '--keys', 'reel-2', '--apply')
        self.assertEqual(self.published_keys(), ['reel-2'])
        p = self.ig('publish', '--keys', 'reel-2', '--apply')  # a restart: no double post, exit 0
        self.assertIn('Nothing to publish', p.stdout)
        self.assertEqual(len(MOCK.publish_calls), 1)
        self.ig('publish', '--keys', 'st-b1,st-a1', '--apply')  # manifest order, not argument order
        self.assertEqual(self.published_keys(), ['reel-2', 'st-a1', 'st-b1'])
        for args, needle, rc in ((['--keys', 'nope'], 'not in the manifest', 1),
                                 (['--keys', 'st-a2', '--only', 'ALPHA'], '--keys and --only', 1),
                                 (['--keys', ','], 'invalid key list', 2)):
            p = self.ig('publish', *args, '--apply', rc=rc)
            self.assertIn(needle, p.stdout + p.stderr)
        self.assertEqual(len(MOCK.publish_calls), 3)
        run = [j for j in self.journal() if j.get('method') == 'RUN' and (j.get('body') or {}).get('want_keys')]
        self.assertTrue(run, 'the RUN journal line carries want_keys')

    def test_45_keys_with_a_parked_item_writes_nothing(self) -> None:
        items = [dict(i) for i in self.items if i['kind'] == 'reel']
        items[1]['needs_approval'] = 'the owner decides'
        cfg = self.cfg_with(items, name='parked')
        p = self.ig('publish', '--keys', 'reel-2', cfg=cfg)  # dry run: says it would stop, exit 0
        self.assertIn('--apply would stop', p.stdout)
        p = self.ig('publish', '--keys', 'reel-1,reel-2', '--apply', cfg=cfg, rc=1)
        self.assertIn('cannot be published now', p.stdout + p.stderr)
        self.assertEqual(self.writes(), [])  # reel-1 was not published either
        self.ig('publish', '--keys', 'reel-2', '--approve', 'reel-2', '--apply', cfg=cfg)
        self.assertEqual(self.published_keys(), ['reel-2'])

    # ------------------------------------------------------------------ seasons
    def test_46_season_caption_words(self) -> None:
        hol = CAP1.replace('a quick demo', 'Christmas night')
        good = self.cfg_with([dict(self.items[3], caption=hol, season='holiday')], name='season-ok')
        self.ig('plan', '--offline', cfg=good)
        bad = self.cfg_with([dict(self.items[3], caption=hol)], name='season-bad')
        p = self.ig('plan', '--offline', cfg=bad, rc=1)
        self.assertIn('"christmas" may only appear in items with "season": "holiday"', p.stdout + p.stderr)
        self.assertEqual(MOCK.log, [])

    def test_47_exit_75_only_before_any_write(self) -> None:
        MOCK.stories_fail = (400, 100)  # the live account cannot be read: before any write -> temporary
        p = self.ig('publish', '--apply', rc=75)
        out = p.stdout + p.stderr
        self.assertIn('read, before any write', out)
        self.assertIn('exit 75', out)
        self.assertEqual(self.writes(), [])
        MOCK.stories_fail = None
        MOCK.extra_live = [{'id': '616161000003', 'media_product_type': 'REELS', 'media_type': 'VIDEO', 'caption': CAP1,
                            'timestamp': iso(time.time() - 3 * 86400),
                            'permalink': 'https://www.instagram.com/reel/HAND2/'}]
        p = self.ig('publish', '--only', 'REELS', '--apply', rc=1)  # same caption live: needs a decision
        self.assertIn('already has this exact caption', p.stdout + p.stderr)
        MOCK.extra_live = []
        MOCK.publish_fail = {1: (400, 100, None)}  # a stop AFTER a write -> 1 (the scheduler does not retry)
        p = self.ig('publish', '--canary', '--apply', rc=1)
        self.assertIn('refused', p.stdout + p.stderr)
        self.assertTrue(self.writes())

    def test_48_season_window_blocks_apply_outside_it(self) -> None:
        hol = CAP1.replace('a quick demo', 'Christmas night')
        cfg = self.cfg_with([dict(self.items[3], caption=hol, season='holiday'), dict(self.items[4])], name='window')
        early, late, inside = '2030-11-30T23:59:00-08:00', '2031-01-01T10:00:00+00:00', '2030-12-01T08:00:00+00:00'
        p = self.ig('plan', '--offline', '--at', early, cfg=cfg)
        self.assertIn('outside the season window', p.stdout)
        p = self.ig('publish', '--apply', '--at', early, cfg=cfg, rc=75)
        self.assertIn('opens', p.stdout + p.stderr)
        p = self.ig('publish', '--apply', '--at', late, cfg=cfg, rc=1)
        self.assertIn('closed', p.stdout + p.stderr)
        self.assertEqual(MOCK.log, [])
        p = self.ig('plan', '--offline', '--at', inside, cfg=cfg)
        self.assertNotIn('outside the season window', p.stdout)
        self.ig('publish', '--apply', '--at', inside, cfg=cfg)
        self.assertEqual(self.published_keys(), ['reel-1', 'reel-2'])

    # ------------------------------------------------------------------ transport and files
    def test_49_redirects_are_never_followed(self) -> None:
        MOCK.redirect_on = 'content_publishing_limit'
        p = self.ig('check', rc=1)
        self.assertIn('HTTP 302', p.stdout + p.stderr)
        self.assertFalse(any(e['path'].startswith('/stolen') for e in MOCK.log))

    def test_50_http_200_with_an_error_body_is_an_error(self) -> None:
        MOCK.error_200_on = 'content_publishing_limit'
        p = self.ig('check', rc=1)
        self.assertIn('error inside a 200 answer', p.stdout + p.stderr)

    def test_51_state_file_of_another_account_is_refused(self) -> None:
        self.save_state({'ig_user': '17841499999999999', 'items': {}})
        p = self.ig('publish', '--apply', rc=1)
        self.assertIn('belongs to Instagram account 17841499999999999', p.stdout + p.stderr)
        self.assertEqual(self.writes(), [])

    def test_52_state_and_journal_are_private(self) -> None:
        self.ig('publish', '--canary', '--apply')
        for path in (self.state_path, self.journal_path):
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600, path)

    def test_52b_bad_option_values_are_refused_before_anything_runs(self) -> None:
        for args, needle in ((['--story-gap', '-1'], 'must be a number of seconds >= 0'),
                             (['--reel-gap', 'nan'], 'must be a number of seconds >= 0'),
                             (['--story-gap', 'inf'], 'must be a number of seconds >= 0'),
                             (['--limit', '0'], 'must be a whole number >= 1'),
                             (['--limit', '-2'], 'must be a whole number >= 1'),
                             (['--at', 'nan'], 'not a time')):
            p = self.ig('publish', *args, '--apply', rc=2)
            self.assertIn(needle, p.stderr)
        p = self.ig('publish', '--offline', '--apply', rc=1)
        self.assertIn('--offline cannot be combined with --apply', p.stderr)
        self.assertEqual(MOCK.log, [])
        self.assertFalse(os.path.exists(self.state_path))

    def test_52c_malformed_answers_end_redacted_with_exit_1(self) -> None:
        MOCK.create_without_id = True  # HTTP 200 without "id" for a new container
        p = self.ig('publish', '--canary', '--apply', rc=1)
        self.assertIn('the container answer has no id', p.stdout + p.stderr)
        self.assertNotIn('Traceback', p.stdout + p.stderr)
        self.assertEqual(MOCK.publish_calls, [])
        MOCK.create_without_id = False
        MOCK.quota_duration_echo = True  # a field of the wrong type that echoes the token: an unexpected exception
        p = self.ig('check', rc=1)
        self.assertIn('unexpected error (ValueError)', p.stderr)
        self.assertIn('Bearer ***', p.stderr)
        self.assertIn('IG_PUBLISH_DEBUG=1', p.stderr)
        self.assertNotIn('Traceback', p.stderr)
        p = self.ig('check', extra={'IG_PUBLISH_DEBUG': '1'}, rc=1)
        self.assertIn('Traceback', p.stderr)
        self.assertNotIn(TOKEN, p.stderr)  # the traceback is redacted too

    def test_53_dry_run_of_unprepared_item_says_so(self) -> None:
        items = [dict(self.items[0], key='st-new')]
        cfg = self.cfg_with(items, name='unprep')
        p = self.ig('publish', '--offline', cfg=cfg)
        self.assertIn('Not prepared: st-new (not prepared)', p.stdout)
        p = self.ig('publish', '--apply', cfg=cfg, rc=1)
        self.assertIn('Not prepared', p.stdout + p.stderr)
        self.assertEqual(self.writes(), [])


@unittest.skipUnless(HAVE_FFMPEG, 'ffmpeg/ffprobe not installed')
class PrepExtrasTest(unittest.TestCase):
    """Still images, letterboxing and copying sources that already meet the spec."""

    def test_still_image_pad_and_compliant_copy(self) -> None:
        from ig_publish.config import PrepSettings
        from ig_publish.media import verify
        tmp = tempfile.mkdtemp(prefix='igp-prep-')
        try:
            make_still(os.path.join(tmp, 'card.png'), '1280x720')
            make_clip(os.path.join(tmp, 'wide.mp4'), 3.2, 'testsrc2', 550, size='1280x720')
            items = [{'key': 'card', 'kind': 'story', 'group': 'CARDS', 'src': 'card.png', 'still_seconds': 4},
                     {'key': 'wide', 'kind': 'story', 'group': 'CARDS', 'src': 'wide.mp4'}]
            write_json(os.path.join(tmp, 'manifest.json'), {'items': items})
            cfg = write_json(os.path.join(tmp, 'ig-publish.json'), {'account': {'ig_user_id': IG}})
            p = run_cli(cfg, 'prep', env=clean_env())
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
            for key, secs in (('card', 4.0), ('wide', 3.2)):
                rep = verify(os.path.join(tmp, 'media', f'{key}.mp4'), 'story', PrepSettings())
                self.assertTrue(rep['ok'], rep['problems'])
                self.assertEqual((rep['width'], rep['height']), (1080, 1920))  # letterboxed, not stretched
                self.assertAlmostEqual(rep['duration'], secs, delta=0.25)
            # a source that already meets the spec is copied, not re-encoded
            shutil.copyfile(os.path.join(tmp, 'media', 'wide.mp4'), os.path.join(tmp, 'ready.mp4'))
            items.append({'key': 'ready', 'kind': 'story', 'group': 'CARDS', 'src': 'ready.mp4'})
            write_json(os.path.join(tmp, 'manifest.json'), {'items': items})
            p = run_cli(cfg, 'prep', env=clean_env())
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
            self.assertIn('copied (the source already meets the spec)', p.stdout)
            p = run_cli(cfg, 'verify', os.path.join(tmp, 'wide.mp4'), '--kind', 'story', env=clean_env())
            self.assertEqual(p.returncode, 1)
            self.assertIn('B-frames present', p.stdout)
            self.assertIn("stricter than Instagram's own limits", p.stdout)
            empty = os.path.join(tmp, 'elsewhere')  # no configuration file here: the default prep profile is used
            os.makedirs(empty)
            p = run_cli(None, 'verify', os.path.join(tmp, 'media', 'wide.mp4'), '--kind', 'story', env=clean_env(),
                        cwd=empty)
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
            self.assertIn('the defaults; no configuration file found', p.stdout)
            self.assertIn('meets the prep profile for a story', p.stdout)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == '__main__':
    unittest.main(verbosity=2)
