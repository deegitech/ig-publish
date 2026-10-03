"""The publisher: ``check``, ``prep``, ``plan``, ``publish``, ``status``, ``delete`` and ``import``.

What ``publish --apply`` does, in order (every step stops the run before the next one if something is off):

0. **Preflight (read-only).** Run lock; lost-state guard; an active *hold* stops the run; half-finished items
   are reconciled from their container status; the live account is read (stories + recent posts) and the run
   stops for posts the state file does not know about, for a reel caption that is already live, for a burst of
   recent posts, for reel spacing and for quota.
1. **Containers.** Create (``upload_type=resumable``) or reuse one container per item, upload the bytes, then poll
   until *every* container is ``FINISHED``. If one errors or the wait times out, **nothing** is published.
2. **Publish** in strict manifest order, with spacing. After *any* ``media_publish`` error the container is read
   first: if it is ``PUBLISHED`` the post is recorded and never published again (no blind retries).

Stops before the first write exit with 75 (safe to retry later); stops after a write exit with 1.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, TextIO

from .config import Config, PrepSettings
from .errors import ConfigError, GraphError, Stop, TemporaryStop, TokenError, UsageLimit
from .graph import GraphClient, resolve_endpoints
from .hints import explain, fix_line
from .manifest import MEDIA_ID_RE, load_manifest
from .media import SPEC, encode, is_still, probe, recipe, require_tools, sha256, verify
from .redact import redact
from .state import Journal, load_json, run_lock, save_json
from .timeutil import clock, iso, now_iso, parse_ts
from .tokens import read_token, scrubbed_env, token_notes

DAY_S = 24 * 3600
ORIGIN = 'ig-publish'
# Meta error (sub)codes that stop the run and write a hold.
STOP_SUBCODES = {2207051: 'Instagram treated the post as spam / restricted activity (2207051)',
                 2207042: 'the API publishing limit for the account is reached (2207042)'}
# Rate limiting: 80002 Instagram, 80001 Page, 80004 ads; 4/17/32/613 app/user/page/call limits.
RATE_CODES = {4, 17, 32, 613, 80001, 80002, 80004}
# After these subcodes the same container is never tried again; the next run creates a new one.
NEW_CONTAINER_SUBCODES = {2207006, 2207008, 2207020, 2207032, 2207053}
# Meta's table: retry 2207008 once or twice within 30 s - 2 min, then use a new container.
RETRY_SUBCODES = {2207008}
CONTAINER_HINTS = {2207026: 'unsupported video format (2207026) - check the `prep` output and the source',
                   2207052: 'Instagram could not fetch the media (2207052) - the upload may be incomplete',
                   9004: 'Instagram could not fetch the media (9004) - the upload may be incomplete'}
INFLIGHT = ('created', 'uploaded', 'ready', 'publishing', 'publish_unknown', 'error', 'repost')
PAGE_TASKS = {'MANAGE', 'CREATE_CONTENT', 'PROFILE_PLUS_FULL_CONTROL', 'PROFILE_PLUS_CREATE_CONTENT'}
REQUIRED_SCOPES = ('instagram_basic', 'instagram_content_publish', 'pages_read_engagement')
TRANSIENT_FIELDS = ('container_id', 'container_created_at', 'container_created_ts', 'fingerprint', 'uploaded',
                    'uploaded_at', 'media_id', 'media_id_candidate', 'candidate_permalink', 'candidate_timestamp',
                    'permalink', 'published_at', 'published_at_source', 'timestamp', 'status_code', 'status_text',
                    'media_product_type', 'media_type', 'publish_attempt_at', 'publish_attempt_ts', 'publish_note',
                    'media_id_source', 'media_id_note', 'error', 'error_code', 'error_subcode', 'retire',
                    'readback_error', 'origin', 'imported_at', 'file', 'file_sha256', 'bytes')
STORY_FIELDS = 'id,timestamp,media_type,media_product_type,permalink'
MEDIA_FIELDS = 'id,caption,timestamp,media_type,media_product_type,permalink'


@dataclass
class Options:
    apply: bool = False
    canary: bool = False
    offline: bool = False
    ack_unknown_posts: bool = False
    ignore_hold: bool = False
    only: str | None = None
    keys: list[str] = field(default_factory=list)
    limit: int | None = None
    story_gap: float | None = None
    reel_gap: float | None = None
    repost: set[str] = field(default_factory=set)
    approve: set[str] = field(default_factory=set)
    confirm_media: str | None = None
    at: float | None = None
    pos: list[str] = field(default_factory=list)


@dataclass
class Preview:
    """Offline answer to "what would ``publish --keys K --apply`` do at time T?" (used by the scheduler)."""
    error: str | None = None
    todo: list[str] = field(default_factory=list)
    done: list[str] = field(default_factory=list)
    parked: list[str] = field(default_factory=list)
    unprepared: list[str] = field(default_factory=list)
    season: list[str] = field(default_factory=list)
    season_early: bool = True


def _norm(s: Any) -> str:
    return re.sub(r'\s+', ' ', str(s or '')).strip()


def _first_line(s: str | None) -> str:
    return (s or '').strip().split('\n')[0] if s else '-'


def verify_report(path: str, kind: str, prep: PrepSettings, say: Callable[[str], None], *, profile: str,
                  env: dict[str, str] | None = None) -> int:
    """``ig-publish verify``: check one file against the prep profile and print the report. Exit code 0 or 1."""
    require_tools(encoder=False, env=env)
    rep = verify(path, kind, prep, env)
    for k in ('duration', 'bytes', 'vcodec', 'profile', 'width', 'height', 'pix_fmt', 'fps', 'avg_fps',
              'has_b_frames', 'acodec', 'sample_rate', 'channels', 'abitrate', 'moov_first', 'elst', 'x264'):
        say(f'  {k:13} {rep.get(k)}')
    say(f'Checked against the prep profile ({profile}: {prep.width}x{prep.height}, constant {prep.fps} fps, H.264 High '
        f'without B-frames, AAC, moov first, no edit list), which is stricter than Instagram\'s own limits; '
        f'`ig-publish prep` produces it.')
    if rep['ok']:
        say(f'OK: {path} meets the prep profile for a {kind}.')
        return 0
    say(f'✗ {path} does not meet the prep profile for a {kind}:\n  ' + '\n  '.join(rep['problems']))
    return 1


class Publisher:
    def __init__(self, cfg: Config, opts: Options | None = None, *, out: TextIO | None = None,
                 client: GraphClient | None = None, sleep: Callable[[float], None] = time.sleep) -> None:
        self.cfg = cfg
        self.o = opts or Options()
        self._out = out
        self.sleep = sleep
        self.endpoints = resolve_endpoints()
        self.journal = Journal(cfg.journal)
        self.client = client or GraphClient(
            version=cfg.api_version, endpoints=self.endpoints,
            token_provider=lambda: read_token(cfg.token, test_mode=self.endpoints.test_mode),
            journal=self.journal, usage_stop_percent=cfg.safety.usage_stop_percent, timing=cfg.timing, sleep=sleep)
        self.items: list[dict] = []
        self.pre_write = False  # True from the start of `publish --apply` until the first live write

    # ------------------------------------------------------------------ output
    def say(self, *parts: Any) -> None:
        out = self._out or sys.stdout
        out.write(redact(' '.join(str(p) for p in parts)) + '\n')
        out.flush()

    def rel(self, p: Any) -> str:
        p = os.path.abspath(str(p))
        base = str(self.cfg.base_dir)
        return os.path.relpath(p, base) if p.startswith(base + os.sep) else p

    # ------------------------------------------------------------------ manifest, state, prep records
    def load(self) -> tuple[dict, list[dict]]:
        man, items = load_manifest(self.cfg, must_contain={'--repost': self.o.repost, '--approve': self.o.approve,
                                                           '--keys': self.o.keys})
        self.items = items
        return man, items

    def load_state(self) -> dict:
        try:
            st = load_json(self.cfg.state, {})
        except ValueError as e:
            raise Stop(f'The state file {self.rel(self.cfg.state)} is not valid JSON ({e}). Restore it from a backup; '
                       f'publishing without it could post things twice.') from None
        if not isinstance(st, dict):
            raise Stop(f'The state file {self.rel(self.cfg.state)} is not a JSON object.')
        if st.get('ig_user') and str(st['ig_user']) != self.cfg.ig_user_id:
            raise Stop(f'The state file {self.rel(self.cfg.state)} belongs to Instagram account {st["ig_user"]}, the '
                       f'configuration to {self.cfg.ig_user_id}. Use one state file per account.')
        st.setdefault('ig_user', self.cfg.ig_user_id)
        st.setdefault('items', {})
        return st

    def save_state(self, st: dict) -> None:
        st['updated_at'] = now_iso()
        save_json(self.cfg.state, st)

    def guard_lost_state(self) -> None:
        if self.cfg.state.exists():
            return
        n = self.journal.publish_count()
        if n:
            raise Stop(f'Stopped: the state file {self.rel(self.cfg.state)} is missing, but the journal '
                       f'({self.rel(self.cfg.journal)}) records {n} publish/import(s). The state may have been lost or '
                       f'[paths] state points somewhere else. Stopped so nothing is posted twice; restore the state '
                       f'file.')

    def out_path(self, key: str) -> str:
        return str(self.cfg.media_dir / f'{key}.mp4')

    def prep_index(self) -> dict:
        idx = load_json(self.cfg.prep_index, {})
        return idx if isinstance(idx, dict) else {}

    def prep_record(self, it: dict, idx: dict | None = None) -> tuple[dict | None, str]:
        """Is the item prepared? ``(record, '')`` or ``(None, reason)``. Hashes are recomputed, not trusted."""
        idx = self.prep_index() if idx is None else idx
        rec = idx.get(it['key'])
        out = self.out_path(it['key'])
        if not rec:
            return None, 'not prepared'
        if not rec.get('ok'):
            return None, 'failed verification'
        if rec.get('recipe') != recipe(self.cfg.prep):
            return None, 'prep settings changed'
        if rec.get('kind') != it['kind']:
            return None, 'kind changed'
        if not os.path.isfile(out):
            return None, 'prepared file missing'
        if not os.path.isfile(it['src_abs']) or sha256(it['src_abs']) != rec.get('src_sha256'):
            return None, 'source changed'
        if sha256(out) != rec.get('out_sha256'):
            return None, 'prepared file changed'
        return rec, ''

    def tool_env(self) -> dict[str, str]:
        """Environment for ffmpeg/ffprobe: the token variable removed (they never need it)."""
        return scrubbed_env(self.cfg.token.env_var)

    def token_state(self) -> tuple[bool, str]:
        """Is a token available? Never touches the network and never returns the token."""
        try:
            self.client.token()
            return True, ''
        except (TokenError, ConfigError) as e:
            return False, str(e)

    # ------------------------------------------------------------------ selection
    def select(self, items: list[dict]) -> list[dict]:
        if self.o.keys:
            if self.o.only:
                raise ConfigError('--keys and --only cannot be combined (--keys is an exact selection)')
            want_keys = set(self.o.keys)
            return [it for it in items if it['key'] in want_keys]  # manifest order, not argument order
        if not self.o.only:
            return list(items)
        want = {w.strip().upper() for w in self.o.only.split(',') if w.strip()}
        groups = {it['group'].upper() for it in items}
        bad = want - groups - {'STORIES', 'REELS'}
        if bad:
            raise ConfigError(f'Unknown group(s): {", ".join(sorted(bad))} (known: {", ".join(sorted(groups))}, '
                              f'plus STORIES and REELS)')
        return [it for it in items if it['group'].upper() in want
                or ('STORIES' in want and it['kind'] == 'story') or ('REELS' in want and it['kind'] == 'reel')]

    def selection(self, items: list[dict],
                  st: dict) -> tuple[list[dict], list[dict], list[dict], list[tuple[dict, str]]]:
        sel = self.select(items)
        done: list[dict] = []
        todo: list[dict] = []
        parked: list[tuple[dict, str]] = []
        for it in sel:
            k = it['key']
            rec = st['items'].get(k, {})
            s = rec.get('status')
            rep = k in self.o.repost
            if (it.get('already_live') or s == 'published') and not rep:
                done.append(it)
            elif s == 'deleted' and not rep:
                parked.append((it, f"deleted {str(rec.get('deleted_at') or '')[:16]} - to publish it again on "
                                   f"purpose: --repost {k}"))
            elif it.get('needs_approval') and k not in self.o.approve:
                parked.append((it, f"awaiting approval - {it['needs_approval']} (when approved: --approve {k})"))
            else:
                todo.append(it)
        if self.o.canary:
            todo = todo[:1]
        elif self.o.limit is not None:
            todo = todo[:max(0, self.o.limit)]
        return sel, done, todo, parked

    def gaps(self) -> dict[str, float]:
        g = {'story': float(self.cfg.spacing.story_seconds), 'reel': float(self.cfg.spacing.reel_seconds)}
        if self.o.story_gap is not None:
            g['story'] = self.o.story_gap
        if self.o.reel_gap is not None:
            g['reel'] = self.o.reel_gap
        return g

    def season_now(self) -> float:
        """Clock for season windows. ``--at`` is honoured for dry runs (and against a local test server)."""
        if self.o.at is not None and (not self.o.apply or self.endpoints.test_mode):
            return self.o.at
        return time.time()

    def season_block(self, todo: list[dict], now: float | None = None) -> tuple[list[str], bool]:
        """Items outside their season window -> (messages, all of them 'not open yet' = temporary)."""
        now = self.season_now() if now is None else now
        msgs: list[str] = []
        early = True
        for it in todo:
            s = self.cfg.seasons.get(it.get('season') or '')
            if not s:
                continue
            if now < s.start:
                msgs.append(f"{it['key']}: season \"{s.name}\" opens {clock(s.start)}")
            elif now >= s.end:
                msgs.append(f"{it['key']}: the window of season \"{s.name}\" closed {clock(s.end)}")
                early = False
        return msgs, early

    # ------------------------------------------------------------------ prep
    def prep(self) -> None:
        env = self.tool_env()
        require_tools(encoder=True, env=env)
        _man, items = self.load()
        items = self.select(items)
        os.makedirs(self.cfg.media_dir, exist_ok=True)
        idx = self.prep_index()
        rcp = recipe(self.cfg.prep)
        by_src: dict[tuple[str, str], str] = {}
        rows: list[tuple[dict, dict, str]] = []
        bad = 0
        p = self.cfg.prep
        self.say(f'Prep: {len(items)} item(s) -> {self.rel(self.cfg.media_dir)}/<key>.mp4 | recipe {rcp} '
                 f'(H.264 High, no B-frames, closed GOP, {p.width}x{p.height} {p.fps} fps, fit={p.fit}, CRF {p.crf}, '
                 f'<= {p.max_video_kbps}k; AAC {p.audio_kbps}k 48 kHz; no edit list; moov first)')
        for it in items:
            key, src, out = it['key'], it['src_abs'], self.out_path(it['key'])
            sh = sha256(src)
            rec, why = self.prep_record(it, idx)
            if rec:
                how = 'skipped (source unchanged)'
                rep = verify(out, it['kind'], p, env)
            else:
                tmp = out[:-4] + '.tmp.mp4'
                if os.path.exists(tmp):
                    os.remove(tmp)
                twin = by_src.get((sh, rcp))
                t0 = time.time()
                if twin:
                    try:
                        os.link(twin, tmp)
                    except OSError:
                        shutil.copyfile(twin, tmp)
                    how = f'copy (same source as {os.path.basename(twin)[:-4]})'
                elif not is_still(src) and verify(src, it['kind'], p, env)['ok']:
                    shutil.copyfile(src, tmp)
                    how = 'copied (the source already meets the spec)'
                else:
                    self.say(f'  ... {key}: encoding ({why})')
                    still = None
                    if is_still(src):
                        still = float(it.get('still_seconds') or p.still_seconds)
                    encode(src, tmp, p, still_seconds=still, env=env)
                    how = f'encoded in {time.time() - t0:.0f} s'
                rep = verify(tmp, it['kind'], p, env)
                if rep['ok']:
                    os.replace(tmp, out)
                else:
                    os.remove(tmp)
            if rep['ok']:
                idx[key] = {'kind': it['kind'], 'src': it['src'], 'src_sha256': sh, 'recipe': rcp,
                            'out': self.rel(out), 'out_sha256': sha256(out), 'verified_at': now_iso(), 'ok': True,
                            'report': {k: v for k, v in rep.items() if k != 'problems'}}
                by_src[(sh, rcp)] = out
            else:
                bad += 1
                idx[key] = {'kind': it['kind'], 'src': it['src'], 'src_sha256': sh, 'recipe': rcp, 'ok': False,
                            'problems': rep['problems'], 'verified_at': now_iso()}
            save_json(self.cfg.prep_index, idx)
            rows.append((it, rep, how))
        self.say(f"\n{'key':30} {'kind':5} {'secs':>6} {'MB':>5}  {'video':20} {'fps':>4} {'bf':>2} {'audio':>18}  "
                 f"{'moov':5} {'elst':4}  result")
        for it, r, how in rows:
            vid = f"{r['vcodec']} {r['profile']} {r['width']}x{r['height']}"
            aud = f"{r['acodec']} {r['sample_rate'] // 1000}k {r['channels']}ch {r['abitrate'] // 1000}k"
            self.say(f"{it['key']:30} {it['kind']:5} {r['duration']:5.2f}s {r['bytes'] / 1e6:5.1f}  {vid:20} "
                     f"{r['fps']:4g} {r['has_b_frames']!s:>2} {aud:>18}  {'first' if r['moov_first'] else 'LAST':5} "
                     f"{'YES' if r['elst'] else 'no':4}  "
                     + ('OK ' + how if r['ok'] else '✗ ' + '; '.join(r['problems'])))
        self.say(f'\n{len(rows) - bad}/{len(rows)} file(s) meet the spec (stories 3-60 s <= 100 MB, reels 3 s-15 min '
                 f'<= 300 MB; H.264 High 4:2:0 progressive, closed GOP, no B-frames, constant {p.fps} fps; AAC '
                 f'<= 48 kHz <= 128 kbps; moov first, no edit list).')
        if bad:
            raise Stop(f'✗ {bad} file(s) failed verification (see above).')

    def verify_file(self, path: str, kind: str) -> int:
        where = f'[prep] of {self.rel(self.cfg.path)}' if self.cfg.path else '[prep]'
        return verify_report(path, kind, self.cfg.prep, self.say, profile=where, env=self.tool_env())

    # ------------------------------------------------------------------ network helpers
    def rget(self, fn: Callable[..., Any], *a: Any, **kw: Any) -> Any:
        """A read that waits out Instagram/Page usage limits (80002/80001) up to three times."""
        for i in range(3):
            try:
                return fn(*a, **kw)
            except GraphError as e:
                if e.code not in (80001, 80002) or i == 2:
                    raise
                wait = self.cfg.timing.rate_retry_seconds * (i + 1)
                self.say(f'    ... Instagram usage limit (code {e.code}){self.regain_text()} - waiting {wait:g} s')
                self.sleep(wait)
        raise AssertionError('unreachable')

    def regain_text(self) -> str:
        m = self.client.regain_minutes()
        return f' | Meta: access back in about {m} min' if m else ''

    def quota(self) -> dict:
        r = self.rget(self.client.get, f'{self.cfg.ig_user_id}/content_publishing_limit',
                      {'fields': 'config,quota_usage'})
        d = (r.get('data') or [{}])[0]
        cfg = d.get('config') or {}
        total, used = cfg.get('quota_total'), d.get('quota_usage')
        left = (int(total) - int(used)) if isinstance(total, int) and isinstance(used, int) else None
        return {'total': total, 'used': used, 'left': left, 'duration_s': cfg.get('quota_duration')}

    @staticmethod
    def quota_text(q: dict) -> str:
        hrs = f"{int(q['duration_s']) // 3600} h" if q.get('duration_s') else '24 h'
        return f"Quota: {q['used']}/{q['total']} API posts used (rolling {hrs}) | {q['left']} left"

    def container(self, cid: str) -> dict:
        return self.client.get_fields(cid, 'id,status,status_code,video_status')

    @staticmethod
    def codes_in(text: str | None) -> list[int]:
        return [int(x) for x in re.findall(r'\b(\d{4,7})\b', text or '')]

    def granted(self) -> set[str]:
        return {p.get('permission') for p in self.client.get('me/permissions').get('data', [])
                if p.get('status') == 'granted'}

    # ------------------------------------------------------------------ the live account
    def known_ids(self, st: dict) -> dict[str, str]:
        """Media IDs we know about -> where from: state (incl. history), manifest already_live, acknowledged."""
        out: dict[str, str] = {}
        for k, r in st['items'].items():
            for x in [r] + list(r.get('history') or []):
                for f in ('media_id', 'media_id_candidate'):
                    if x.get(f):
                        out[str(x[f])] = k
        for it in self.items:
            al = it.get('already_live') or {}
            if al.get('media_id'):
                out[str(al['media_id'])] = f"{it['key']} (already_live)"
        for mid in st.get('acknowledged') or {}:
            out[str(mid)] = 'acknowledged'
        return out

    def snapshot(self) -> dict:
        """Read-only view of the account: live stories (24 h) and the 50 most recent feed/reel posts."""
        ig = self.cfg.ig_user_id
        stories = self.rget(self.client.get_all, f'{ig}/stories', {'fields': STORY_FIELDS}, 200)
        media = self.rget(self.client.get, f'{ig}/media', {'fields': MEDIA_FIELDS, 'limit': 50}).get('data', [])
        return {'stories': stories, 'media': media}

    def issues(self, st: dict, todo: list[dict], g: dict, snap: dict) -> tuple[list[tuple[str, str, str]], list[dict]]:
        """Preflight findings ``(kind, message, how to override)`` and the unknown posts of the last 24 h."""
        now = time.time()
        known = self.known_ids(st)
        sf = self.cfg.safety
        rows = [dict(r, _edge='STORY') for r in snap['stories']] + [dict(r, _edge='FEED/REELS') for r in snap['media']]
        unknown = [r for r in rows if parse_ts(r.get('timestamp')) >= now - DAY_S and str(r.get('id')) not in known]
        out: list[tuple[str, str, str]] = []
        if unknown:
            desc = ', '.join(f"{r.get('media_product_type') or r['_edge']} {r.get('id')} "
                             f"{str(r.get('timestamp', ''))[:16]}" for r in unknown[:5])
            desc += '...' if len(unknown) > 5 else ''
            out.append(('unknown', f'{len(unknown)} post(s) in the last 24 h are NOT in the state file ({desc}) - '
                                   f'posted by hand, by another script or with another state file. If that is '
                                   f'expected: `ig-publish ack --apply` records them (nothing is posted); manifest '
                                   f'items posted elsewhere can be recorded with `ig-publish import`',
                        '--ack-unknown-posts'))
        by_cap: dict[str, dict] = {}
        for r in snap['media']:
            by_cap.setdefault(_norm(r.get('caption')), r)
        for it in todo:
            if it['kind'] == 'reel' and it['key'] not in self.o.repost and _norm(it.get('caption')):
                hit = by_cap.get(_norm(it.get('caption')))
                if hit and str(hit.get('id')) != str(st['items'].get(it['key'], {}).get('media_id')):
                    out.append(('duplicate', f"{it['key']}: a live post already has this exact caption -> "
                                             f"{hit.get('permalink')} ({str(hit.get('timestamp', ''))[:16]})",
                                f"--repost {it['key']}"))
        stamps = sorted(t for t in (parse_ts(r.get('timestamp')) for r in rows) if t >= now - sf.burst_window_seconds)
        if todo and len(stamps) >= sf.burst_max_posts:
            until = stamps[len(stamps) - sf.burst_max_posts] + sf.burst_window_seconds
            out.append(('burst', f"{len(stamps)} posts in the last {sf.burst_window_seconds / 3600:g} h "
                                 f"(>= {sf.burst_max_posts}); posting in bursts raises the risk of an 'action blocked' "
                                 f"(2207051) -> wait until {clock(until)}", '--ignore-hold'))
        if todo and todo[0]['kind'] == 'reel' and g['reel'] > 0:
            mine = [parse_ts(r.get('published_at')) for r in st['items'].values()
                    if r.get('status') == 'published' and r.get('kind') == 'reel']
            last = max([parse_ts(r.get('timestamp')) for r in snap['media']] + mine + [0.0])
            if now - last < g['reel']:
                out.append(('gap', f"last feed/reel post {clock(last)}; reel spacing is {g['reel'] / 60:g} min -> "
                                   f"after {clock(last + g['reel'])}", '--ignore-hold'))
        return out, unknown

    def api_posts_24h(self, st: dict) -> int:
        """API posts of the last 24 h according to the state file (published, history, acknowledged)."""
        lim = time.time() - DAY_S - 600
        n = 0
        for r in st['items'].values():
            if r.get('status') == 'published' and parse_ts(r.get('published_at')) >= lim:
                n += 1
            n += sum(1 for h in r.get('history') or []
                     if h.get('published_at') and parse_ts(h.get('published_at')) >= lim)
        n += sum(1 for a in (st.get('acknowledged') or {}).values() if parse_ts(a.get('timestamp')) >= lim)
        return n

    def acknowledge(self, st: dict, unknown: list[dict]) -> None:
        """Record posts the state file did not know as acknowledged: the preflight stops asking about them."""
        ack = st.setdefault('acknowledged', {})
        for r in unknown:
            ack[str(r['id'])] = {'timestamp': r.get('timestamp'), 'permalink': r.get('permalink'),
                                 'product': r.get('media_product_type') or r.get('_edge'),
                                 'acknowledged_at': now_iso()}
        self.save_state(st)

    # ------------------------------------------------------------------ holds
    def hold_for(self, e: GraphError) -> tuple[float, bool]:
        sub, code = e.subcode, e.code
        if 2207051 in (sub, code):
            return float(DAY_S), False
        if 2207042 in (sub, code):
            return float(DAY_S), True
        return max(float(self.cfg.safety.rate_hold_seconds), self.client.regain_minutes() * 60.0), False

    @staticmethod
    def hold_text(h: dict) -> str:
        left = max(0.0, float(h.get('until_ts') or 0) - time.time())
        s = f"Hold until {clock(float(h.get('until_ts') or 0))} (about {left / 3600:.1f} h)"
        if h.get('until_quota'):
            s += ' or until the publishing quota has room again (content_publishing_limit)'
        if 2207051 in (h.get('subcode'), h.get('code')):
            s += (". Check the Instagram app for a 'We restrict certain activity' / 'action blocked' notice (if "
                  "there is one, use 'Tell us'), and don't post in bulk by hand meanwhile")
        return s + '. To override knowingly: --ignore-hold.'

    def set_hold(self, st: dict, reason: str, e: GraphError, key: str) -> str:
        secs, by_quota = self.hold_for(e)
        until = time.time() + secs
        st['hold'] = {'reason': reason, 'http': e.status, 'code': e.code, 'subcode': e.subcode, 'key': key,
                      'set_at': now_iso(), 'until': iso(until), 'until_ts': until, 'until_quota': by_quota}
        self.save_state(st)
        return self.hold_text(st['hold'])

    def end_hold(self, st: dict, why: str) -> None:
        h = st.pop('hold', None)
        if h:
            st.setdefault('holds', []).append(dict(h, ended_at=now_iso(), ended=why))
            self.save_state(st)
            self.say(f"  hold lifted ({why}): {h.get('reason')}")

    def check_hold(self, st: dict) -> None:
        h = st.get('hold')
        if not h:
            return
        if time.time() >= float(h.get('until_ts') or 0):
            self.end_hold(st, 'expired')
            return
        if h.get('until_quota'):
            q = self.quota()
            if q['left'] is not None and q['left'] > 0:
                self.end_hold(st, f"the quota has room again, {q['left']} left")
                return
        if self.o.ignore_hold:
            self.say(f"  ! hold overridden on purpose with --ignore-hold: {h.get('reason')} (until {h.get('until')})")
            return
        raise TemporaryStop(f"Stopped: a hold is active: {h.get('reason')} ({h.get('key')}, {h.get('set_at')}).\n"
                            f"  {self.hold_text(h)}")

    # ------------------------------------------------------------------ publishing internals
    def fingerprint(self, it: dict, prep_rec: dict) -> str:
        blob = [prep_rec['out_sha256'], SPEC[it['kind']]['media_type'], it.get('caption'), it.get('thumb_offset'),
                it.get('share_to_feed', True) if it['kind'] == 'reel' else None]
        return hashlib.sha256(json.dumps(blob, ensure_ascii=False).encode()).hexdigest()[:16]

    @staticmethod
    def container_params(it: dict) -> dict:
        p: dict[str, Any] = {'media_type': SPEC[it['kind']]['media_type'], 'upload_type': 'resumable'}
        if it['kind'] == 'reel':
            if it.get('caption'):
                p['caption'] = it['caption']
            p['share_to_feed'] = bool(it.get('share_to_feed', True))
            if it.get('thumb_offset') is not None:
                p['thumb_offset'] = int(it['thumb_offset'])
        return p

    def archive(self, rec: dict, why: str) -> None:
        keep = ('container_id', 'container_created_at', 'media_id', 'media_id_candidate', 'permalink', 'published_at',
                'status', 'origin')
        rec.setdefault('history', []).append({k: rec.get(k) for k in keep if rec.get(k) is not None}
                                             | {'archived_at': now_iso(), 'why': why})
        for k in TRANSIENT_FIELDS:
            rec.pop(k, None)
        rec['status'] = 'repost'

    def find_media(self, it: dict, st: dict, since: float) -> tuple[dict | None, int]:
        """The container is PUBLISHED but the media ID is unknown: look for the post on the account.
        Stories: an unknown VIDEO story within 30 s (a *candidate* only). Reels: the exact same caption."""
        known = self.known_ids(st)
        ig = self.cfg.ig_user_id
        if it['kind'] == 'story':
            rows = self.rget(self.client.get_all, f'{ig}/stories', {'fields': STORY_FIELDS}, 200)
            rows = [r for r in rows if str(r.get('id')) not in known and r.get('media_type') == 'VIDEO'
                    and abs(parse_ts(r.get('timestamp')) - since) <= 30]
        else:
            rows = self.rget(self.client.get, f'{ig}/media', {'fields': MEDIA_FIELDS, 'limit': 25}).get('data', [])
            rows = [r for r in rows if str(r.get('id')) not in known
                    and _norm(r.get('caption')) == _norm(it.get('caption'))
                    and parse_ts(r.get('timestamp')) >= since - 120]
        return (rows[0] if len(rows) == 1 else None), len(rows)

    def match_media(self, it: dict, st: dict) -> str:
        rec = st['items'][it['key']]
        if rec.get('media_id'):
            return f"media {rec['media_id']}"
        if rec.get('media_id_candidate'):
            return f"media CANDIDATE {rec['media_id_candidate']}"
        since = float(rec.get('publish_attempt_ts') or rec.get('container_created_ts') or 0)
        try:
            m, n = self.find_media(it, st, since)
        except GraphError as e:
            rec['media_id_note'] = f'account listing failed ({e.text()[:160]}); the next publish/status looks again'
            self.save_state(st)
            return f"media ? ({rec['media_id_note']})"
        if m and it['kind'] == 'reel':
            rec.update(media_id=str(m['id']), permalink=m.get('permalink'),
                       media_product_type=m.get('media_product_type'), timestamp=m.get('timestamp'),
                       media_id_source='account listing (exact caption match)')
            rec.pop('media_id_note', None)
            out = f"media {rec['media_id']} | {rec.get('permalink')}"
        elif m:
            rec.update(media_id_candidate=str(m['id']), candidate_permalink=m.get('permalink'),
                       candidate_timestamp=m.get('timestamp'),
                       media_id_source='CANDIDATE: account listing (+-30 s, VIDEO)',
                       media_id_note='story ID is only a CANDIDATE (time match); delete needs --confirm-media')
            out = f"media CANDIDATE {m['id']} | {m.get('permalink')}"
        else:
            rec['media_id_note'] = f'{n} candidate(s) on the account; the next publish/status looks again'
            out = f"media ? ({rec['media_id_note']})"
        self.save_state(st)
        return out

    def record_published(self, it: dict, st: dict, note: str) -> None:
        """The container is PUBLISHED: record that first (certain), then look for the media ID."""
        rec = st['items'][it['key']]
        rec.update(status='published',
                   published_at=rec.get('published_at') or rec.get('publish_attempt_at') or now_iso(),
                   publish_note=note, origin=rec.get('origin') or ORIGIN)
        for k in ('error', 'error_code', 'error_subcode', 'retire'):
            rec.pop(k, None)
        self.save_state(st)
        self.say(f"  = {it['key']}: ALREADY PUBLISHED ({note}) -> not published again | {self.match_media(it, st)}")

    def readback(self, it: dict, st: dict) -> None:
        rec = st['items'][it['key']]
        try:
            m = self.rget(self.client.get_fields, rec['media_id'],
                          'id,permalink,media_type,media_product_type,timestamp')
        except GraphError as e:
            rec['readback_error'] = e.text()
            self.save_state(st)
            self.say(f'    ! read-back failed (the post is live and recorded): {e.text()}')
            return
        rec.update(permalink=m.get('permalink'), media_type=m.get('media_type'),
                   media_product_type=m.get('media_product_type'), timestamp=m.get('timestamp'))
        rec.pop('readback_error', None)
        self.save_state(st)
        want = SPEC[it['kind']]['product']
        self.say(f"    read back: {m.get('media_product_type')} | {m.get('timestamp')} | {m.get('permalink')}"
                 + ('' if m.get('media_product_type') == want else f'  ✗ expected {want}'))

    def stop_reason(self, e: GraphError) -> str | None:
        for c in (e.subcode, e.code):
            if c in STOP_SUBCODES:
                return STOP_SUBCODES[c]
        if e.status == 429:
            return 'HTTP 429 - rate limited'
        if e.code in RATE_CODES:
            return f'rate limited (code {e.code}){self.regain_text()}'
        return None

    @staticmethod
    def unknown_outcome(e: GraphError) -> bool:
        return (e.unknown_outcome or e.status == 0 or e.status >= 500 or bool(e.err.get('is_transient'))
                or e.code in (1, 2, -1, -2))

    def settle(self, rec: dict) -> str:
        """Read the container up to three times; return as soon as it is PUBLISHED, ERROR or EXPIRED."""
        code: Any = None
        for i in range(3):
            try:
                code = self.rget(self.container, rec['container_id']).get('status_code')
            except GraphError as e2:
                code = f'unreadable: {e2.text()}'
            if code in ('PUBLISHED', 'ERROR', 'EXPIRED'):
                return code
            if i < 2:
                self.sleep(self.cfg.timing.guard_wait_seconds)
        return str(code)

    def publish_failed(self, it: dict, st: dict, e: GraphError) -> None:
        """``media_publish`` failed. Read the container FIRST (PUBLISHED -> record), then stop codes, then doubt."""
        key = it['key']
        rec = st['items'][key]
        sub = e.subcode
        answer = 'without a media ID' if e.unknown_outcome else f'{e.code}/{sub} HTTP {e.status}'
        self.say(f'  ! {key}: media_publish ' + ('answered without a media ID' if e.unknown_outcome else
                                                 f'failed ({e.code}/{sub}, HTTP {e.status})')
                 + ' - reading the container first')
        code = self.settle(rec)
        reason = self.stop_reason(e)
        if code == 'PUBLISHED':
            self.record_published(it, st, f'media_publish answered {answer} but the container is PUBLISHED')
            if reason:
                hold = self.set_hold(st, reason, e, key)
                raise Stop(f'Stopped: {reason}. {key} WAS PUBLISHED (container PUBLISHED) and is recorded; the rest of '
                           f'the run was not started.\n  {hold}\n  {explain(e)}')
            return
        confirmed = code in ('FINISHED', 'ERROR', 'EXPIRED')  # the container says it was not published
        retire = confirmed and sub in NEW_CONTAINER_SUBCODES
        if reason:
            status = 'error' if confirmed else 'publish_unknown'
        elif self.unknown_outcome(e):  # 5xx / network / transient: the post may still appear
            status = 'publish_unknown'
        else:
            status = 'error' if confirmed else 'publish_unknown'
        rec.update(status=status, status_code=code, error=e.text(), error_code=e.code, error_subcode=sub)
        if retire:
            rec['retire'] = f'{e.code}/{sub}'
        self.save_state(st)
        if reason:
            hold = self.set_hold(st, reason, e, key)
            what = (f'{key} was not published (container {code})' if confirmed else
                    f'the outcome for {key} is unknown (container {code}); the next run reads the container first')
            raise Stop(f'Stopped: {reason}. {what}; the rest of the run was not started.\n  {hold}\n  {explain(e)}')
        if rec['status'] == 'publish_unknown':
            raise Stop(f'Stopped: {key}: the media_publish outcome is unknown, container {code}. No automatic retry.\n'
                       f'  Run the same command in a few minutes: it reads the container first - PUBLISHED is '
                       f"recorded, FINISHED is {'replaced by a new container' if retire else 'published'}.\n"
                       f'  {explain(e)}')
        raise Stop(f'✗ {key}: media_publish was refused - not published (container {code}); stopped.'
                   + (' Meta asks for a new container: the same command creates one for this item.' if retire else '')
                   + f'\n  {explain(e)}')

    def publish_one(self, it: dict, st: dict) -> None:
        key = it['key']
        rec = st['items'][key]
        cid = rec['container_id']
        retries = 0
        while True:
            code = self.rget(self.container, cid).get('status_code')  # right before publishing
            if code == 'PUBLISHED':
                self.record_published(it, st, 'container already PUBLISHED')
                return
            if code != 'FINISHED':
                rec.update(status='error', status_code=code, error=f'container {code} before publishing')
                self.save_state(st)
                raise Stop(f'✗ {key}: container {code} (not FINISHED) - not published; stopped.')
            rec.update(status='publishing', publish_attempt_at=now_iso(), publish_attempt_ts=time.time())
            self.save_state(st)
            try:
                r = self.client.post(f'{self.cfg.ig_user_id}/media_publish', {'creation_id': cid}, key=key)
            except GraphError as e:
                if e.subcode in RETRY_SUBCODES and retries < 2 and not self.stop_reason(e):
                    c2 = self.settle(rec)
                    if c2 == 'PUBLISHED':
                        self.record_published(it, st, f'media_publish answered {e.code}/{e.subcode} but the container '
                                                      f'is PUBLISHED')
                        return
                    if c2 == 'FINISHED':
                        retries += 1
                        self.say(f'  ! {key}: media_publish {e.code}/{e.subcode} (temporary) - container FINISHED, '
                                 f'retry {retries}/2 in {self.cfg.timing.republish_wait_seconds:g} s')
                        self.sleep(self.cfg.timing.republish_wait_seconds)
                        continue
                self.publish_failed(it, st, e)
                return
            break
        mid = str(r.get('id') or '') if isinstance(r, dict) else ''
        if not mid:
            # A success status without a media ID: the post may be live or not, so it is handled like a 5xx.
            self.publish_failed(it, st, GraphError(200, {'message': f'no id in the answer: {json.dumps(r)[:200]}'},
                                                   'POST media_publish', unknown_outcome=True))
            return
        rec.update(status='published', media_id=mid, published_at=now_iso(), origin=ORIGIN)
        for k in ('error', 'error_code', 'error_subcode', 'publish_note', 'retire', 'media_id_note'):
            rec.pop(k, None)
        self.save_state(st)
        self.say(f'  ✓ published {key} -> media {mid}')
        self.readback(it, st)

    def containers(self, todo: list[dict], st: dict, preps: dict) -> list[dict]:
        """Step 1a: create or reuse a container per item and upload. PUBLISHED ones are recorded and dropped."""
        ready: list[dict] = []
        max_age = self.cfg.safety.container_max_age_seconds
        for it in todo:
            key = it['key']
            rec = st['items'].setdefault(key, {})
            if key in self.o.repost and rec.get('status') in ('published', 'deleted'):
                self.archive(rec, 'published again on purpose (--repost)')
                self.save_state(st)
                self.say(f'  ↻ {key}: --repost - the previous record moved to history; a new container follows')
            rec.update(kind=it['kind'], group=it['group'])
            fp = self.fingerprint(it, preps[key])
            cid = rec.get('container_id')
            if cid:
                age = time.time() - float(rec.get('container_created_ts') or 0)
                code = None
                if rec.get('uploaded'):
                    # Status FIRST: whatever its age or caption, a published container is never abandoned and
                    # re-created (that would be a double post after a crash).
                    try:
                        code = self.rget(self.container, cid).get('status_code')
                    except GraphError as e:
                        # Any container that went to media_publish may still go live, even after an error answer
                        # and a FINISHED read: never replace it while its status cannot be read.
                        if rec.get('status') in ('publishing', 'publish_unknown') or rec.get('publish_attempt_ts'):
                            raise Stop(f'Stopped: {key}: container {cid} was sent to media_publish before (status '
                                       f'{rec.get("status")}) and cannot be read now ({e.text()}).\n  Stopped so '
                                       f'nothing is posted twice: run the same command later (the container is read '
                                       f'first). If the post is live, record it with `ig-publish import`; if you are '
                                       f'sure it is not live and the container stays unreadable, delete the "{key}" '
                                       f'entry from {self.rel(self.cfg.state)} to start that item over.'
                                       + fix_line(e)) from None
                    if code == 'PUBLISHED':
                        self.record_published(it, st, 'published by an earlier run')
                        continue
                if not rec.get('uploaded'):
                    why = 'the upload had not completed'
                elif rec.get('retire'):
                    why = f"Meta asks for a new container ({rec['retire']})"
                elif rec.get('fingerprint') != fp:
                    why = 'the file or the caption changed'
                elif age >= max_age:
                    why = f'{age / 3600:.1f} h old (>= {max_age / 3600:g} h)'
                elif code in ('FINISHED', 'IN_PROGRESS'):
                    self.say(f'  = {key}: reusing container {cid} ({code}, {age / 60:.0f} min old)')
                    ready.append(it)
                    continue
                else:
                    why = f'status {code}'
                rec.setdefault('history', []).append({
                    'container_id': cid, 'created_at': rec.get('container_created_at'), 'status': rec.get('status'),
                    'status_code': rec.get('status_code'), 'retired_at': now_iso(), 'why': why})
                self.say(f'  ↻ {key}: old container {cid} dropped ({why})')
            r = self.client.post(f'{self.cfg.ig_user_id}/media', self.container_params(it), key=key)
            if not isinstance(r, dict) or not r.get('id'):
                raise Stop(f'✗ {key}: the container answer has no id: {redact(json.dumps(r))[:200]}. Nothing was '
                           f'published; stopped.')
            cid = str(r['id'])
            rec.update(container_id=cid, container_created_at=now_iso(), container_created_ts=time.time(),
                       fingerprint=fp, file=preps[key]['out'], file_sha256=preps[key]['out_sha256'], status='created',
                       status_code=None, uploaded=False)
            for k in ('media_id', 'media_id_candidate', 'candidate_permalink', 'candidate_timestamp', 'permalink',
                      'published_at', 'error', 'error_code', 'error_subcode', 'publish_note', 'publish_attempt_at',
                      'publish_attempt_ts', 'media_id_note', 'status_text', 'retire'):
                rec.pop(k, None)
            self.save_state(st)
            path = self.out_path(key)
            self.say(f"  ✎ {key}: container {cid} ({SPEC[it['kind']]['media_type']}) | uploading "
                     f"{os.path.getsize(path) / 1e6:.1f} MB")
            uri = r.get('uri') or f'{self.client.upload_base}/ig-api-upload/{self.cfg.api_version}/{cid}'
            self.client.upload(uri, path, key=key, container_id=cid)
            rec.update(uploaded=True, uploaded_at=now_iso(), status='uploaded', bytes=os.path.getsize(path))
            self.save_state(st)
            ready.append(it)
        return ready

    def poll(self, ready: list[dict], st: dict) -> list[dict]:
        """Step 1b: wait until every container is FINISHED. ERROR/EXPIRED or a timeout -> stop, nothing published."""
        t0 = time.time()
        left = list(ready)
        published_meanwhile: set[str] = set()
        warned: set[str] = set()
        tm = self.cfg.timing
        while left:
            nxt = []
            for it in left:
                rec = st['items'][it['key']]
                s = self.rget(self.container, rec['container_id'])
                code = s.get('status_code')
                rec.update(status_code=code, status_text=s.get('status'))
                up = (s.get('video_status') or {}).get('uploading_phase') or {}
                bt = up.get('bytes_transferred')
                if isinstance(bt, int) and rec.get('bytes') and bt != rec['bytes'] and it['key'] not in warned:
                    warned.add(it['key'])
                    self.say(f"    ! {it['key']}: Meta reports {bt} bytes received, the file has {rec['bytes']} "
                             f"(upload status {up.get('status')})")
                if code == 'FINISHED':  # i.e. not published (including after an unclear earlier attempt)
                    rec['status'] = 'ready'
                    self.say(f"    ✓ ready {it['key']}")
                elif code == 'PUBLISHED':
                    self.save_state(st)
                    self.record_published(it, st, 'container already PUBLISHED')
                    published_meanwhile.add(it['key'])
                elif code in ('ERROR', 'EXPIRED'):
                    hint = '; '.join(CONTAINER_HINTS[c] for c in self.codes_in(s.get('status')) if c in CONTAINER_HINTS)
                    rec.update(status='error', error=f"container {code}: {s.get('status')}")
                    self.save_state(st)
                    hint = f' - {hint}' if hint else ''
                    raise Stop(f"Stopped: {it['key']}: container {code} ({s.get('status')}){hint}.\n"
                               f'  NOTHING was published. Fix it and run the same command: this item gets a new '
                               f'container, the others are reused.')
                else:
                    nxt.append(it)
            self.save_state(st)
            if not nxt:
                break
            if time.time() - t0 > tm.poll_timeout_seconds:
                raise Stop(f"Stopped: {len(nxt)} container(s) did not become FINISHED within "
                           f"{tm.poll_timeout_seconds / 60:.1f} min ({', '.join(i['key'] for i in nxt)}). NOTHING was "
                           f"published; the same command reuses the containers.")
            self.say(f"    ... processing: {len(nxt)} container(s) ({', '.join(i['key'] for i in nxt[:4])}"
                     f"{'...' if len(nxt) > 4 else ''})")
            self.sleep(tm.poll_seconds)
            left = nxt
        return [it for it in ready if it['key'] not in published_meanwhile]

    def reconcile(self, st: dict, by_key: dict) -> None:
        """Start of a run (reads + state only): finish half-done publishes from the container status and look up
        missing media IDs."""
        for key in list(st['items']):
            rec = st['items'][key]
            it = by_key.get(key)
            if not it:
                continue
            s = rec.get('status')
            if s in INFLIGHT and rec.get('container_id') and rec.get('uploaded'):
                try:
                    code = self.rget(self.container, rec['container_id']).get('status_code')
                except GraphError:
                    continue  # containers() decides (and stops if the outcome is unknown)
                if code == 'PUBLISHED':
                    self.record_published(it, st, 'published by an earlier run (container PUBLISHED)')
                continue
            if s == 'published' and not rec.get('media_id') and not rec.get('media_id_candidate'):
                if it['kind'] == 'story' and time.time() - parse_ts(rec.get('published_at')) > DAY_S:
                    continue  # the story left the tray; the listing no longer shows it
                self.say(f'  ... {key}: looking up the media ID again -> {self.match_media(it, st)}')

    # ------------------------------------------------------------------ plan / dry-run helpers
    def state_label(self, it: dict, st: dict) -> str:
        s = st['items'].get(it['key'], {})
        al = it.get('already_live')
        if al and it['key'] not in self.o.repost:
            return f"ALREADY LIVE (outside this tool, {str(al.get('at') or '')[:16]}) {al.get('permalink')}"
        if s.get('status') == 'published':
            extra = ''
            if s.get('origin') and s.get('origin') != ORIGIN:
                extra += ' | posted outside ig-publish, imported'
            if not s.get('media_id'):
                extra += f" | ID CANDIDATE {s['media_id_candidate']}" if s.get('media_id_candidate') else ' | ID ?'
            return f"PUBLISHED {str(s.get('published_at') or '')[:16]}{extra}"
        if s.get('status') == 'deleted':
            return f"DELETED {str(s.get('deleted_at') or '')[:16]}"
        if it.get('needs_approval') and it['key'] not in self.o.approve:
            return 'AWAITING APPROVAL'
        return s.get('status') or 'pending'

    def item_line(self, n: int, it: dict, st: dict, idx: dict, full_caption: bool = False) -> str:
        rec, why = self.prep_record(it, idx)
        if rec:
            dur = f"{rec['report']['duration']:5.1f}s"
            f = rec['out']
            mark = 'OK'
        else:
            if is_still(it['src_abs']):
                dur = ' still'
            else:
                try:
                    dur = f"{float(probe(it['src_abs'], self.tool_env())['format']['duration']):5.1f}s"
                except (Stop, KeyError, ValueError, OSError):
                    dur = '    ?'
            f = it['src']
            mark = f'✗ {why}'
        to = f" | cover frame {it['thumb_offset']} ms" if it.get('thumb_offset') is not None else ''
        head = (f"{n:>2}  {it['group']:10} {it['key']:28} {it['kind']:5} {dur}  {f} {mark}\n"
                f"    [{self.state_label(it, st)}]{to}")
        if it['kind'] != 'reel':
            return head
        if not full_caption:
            return head + f"\n    {_first_line(it.get('caption'))}"
        return head + ''.join(f'\n    | {line}' for line in (it.get('caption') or '').split('\n'))

    def tray_line(self, st: dict, items: list[dict]) -> str | None:
        """When live stories leave the tray (the last moment to add them to a Highlight in the app)."""
        now = time.time()
        live: list[tuple[float, str]] = []  # (published at, group) of stories still in the tray
        for it in items:
            rec = st['items'].get(it['key'], {})
            if it['kind'] == 'story' and rec.get('status') == 'published':
                t = parse_ts(rec.get('published_at'))
                if t and now - t < DAY_S:
                    live.append((t, it['group']))
        if not live:
            return None
        first, last = clock(min(t for t, _g in live) + DAY_S), clock(max(t for t, _g in live) + DAY_S)
        groups = ', '.join(dict.fromkeys(g for _t, g in live))
        one = len(live) == 1
        when = f'at {first}' if first == last else f'between {first} and {last}'
        return (f"{len(live)} live stor{'y leaves' if one else 'ies leave'} the tray {when}: add "
                f"{'it' if one else 'them'} to Highlights ({groups}) in the Instagram app before that; afterwards "
                f"only from the archive (if story archiving is on).")

    def preps(self, todo: list[dict], idx: dict) -> tuple[dict, list[str]]:
        preps: dict[str, dict] = {}
        missing: list[str] = []
        for it in todo:
            rec, why = self.prep_record(it, idx)
            if rec:
                preps[it['key']] = rec
            else:
                missing.append(f"{it['key']} ({why})")
            to = it.get('thumb_offset')
            if rec and to is not None and to >= rec['report']['duration'] * 1000:
                missing.append(f"{it['key']} (thumb_offset {to} ms is beyond the end of the video)")
        return preps, missing

    def burst_line(self, st: dict) -> str | None:
        """Offline hint: do recent posts in the state file already hit the burst threshold?"""
        now = time.time()
        sf = self.cfg.safety
        stamps = sorted(t for t in (parse_ts(r.get('published_at')) for r in st['items'].values()
                                    if r.get('status') == 'published') if t >= now - sf.burst_window_seconds)
        if len(stamps) < sf.burst_max_posts:
            return None
        until = stamps[len(stamps) - sf.burst_max_posts] + sf.burst_window_seconds
        return (f'! The state file shows {len(stamps)} posts in the last {sf.burst_window_seconds / 3600:g} h '
                f'(>= {sf.burst_max_posts}): --apply stops until {clock(until)} (2207051 risk; override knowingly with '
                f'--ignore-hold).')

    def explain_apply(self, todo: list[dict], st: dict, g: dict) -> None:
        ns = sum(1 for i in todo if i['kind'] == 'story')
        nr = len(todo) - ns
        mb = 0.0
        reuse = 0
        sf, tm = self.cfg.safety, self.cfg.timing
        for it in todo:
            p = self.out_path(it['key'])
            mb += os.path.getsize(p) / 1e6 if os.path.exists(p) else 0
            r = st['items'].get(it['key'], {})
            if (r.get('container_id') and r.get('uploaded') and not r.get('retire') and it['key'] not in self.o.repost
                    and time.time() - float(r.get('container_created_ts') or 0) < sf.container_max_age_seconds):
                reuse += 1
        secs = sum(g[it['kind']] for it in todo[1:])
        self.say(f'\nWhat --apply does ({len(todo)} item(s): {ns} stor{"y" if ns == 1 else "ies"} + {nr} reel(s)):')
        self.say(f'  (0) preflight (read-only): hold, half-finished publishes, posts in the last 24 h that are not in '
                 f'the state file, a live reel with the same caption, >= {sf.burst_max_posts} posts in the last '
                 f'{sf.burst_window_seconds / 3600:g} h, reel spacing, quota.')
        self.say(f'  (1) {len(todo) - reuse} new container(s) (upload_type=resumable; reels: caption, share_to_feed, '
                 f'thumb_offset)' + (f', {reuse} reused (status read first)' if reuse else '')
                 + f'; files uploaded to rupload (about {mb:.0f} MB).')
        self.say(f'      Every container is polled every {tm.poll_seconds:g} s until FINISHED (at most '
                 f'{tm.poll_timeout_seconds / 60:.1f} min); if one errors or the wait times out, NOTHING is published.')
        self.say(f"  (2) media_publish in manifest order: stories {g['story']:g} s apart, reels {g['reel'] / 60:g} min "
                 f"apart (about {secs / 60:.1f} min of waiting); after each post the media ID and permalink are read "
                 f"back into {self.rel(self.cfg.state)}.")
        self.say('      After any media_publish error the container is read first (PUBLISHED -> recorded, never '
                 'published again); stop codes write a hold and end the run.')
        if nr:
            last_reel = [i['key'] for i in todo if i['kind'] == 'reel'][-1]
            self.say(f'      Grid: the last reel published ends up top-left -> {last_reel}.')
            self.say('      Suggested: one reel first (publish --only REELS --canary --apply), then one or two a day '
                     '(publish --only REELS --limit 1 --apply).')
        if ns:
            self.say('      Stories leave the tray after 24 h; adding them to Highlights is a manual step in the app. '
                     'API stories carry no stickers, links or music.')
        if len(todo) >= sf.burst_max_posts:
            self.say(f'      ! This run is {len(todo)} posts: posting in bulk raises the 2207051 risk - split it or '
                     f'widen the spacing.')

    def online_report(self, st: dict, todo: list[dict], g: dict) -> None:
        """plan / dry-run publish: quota + live-account preflight, read-only; findings are printed as warnings."""
        if self.o.offline:
            self.say('Quota and live account: --offline (not read; no token, no network)')
            return
        ok, why = self.token_state()
        if not ok:
            self.say(f'Quota and live account: not read - no token ({why})')
            return
        try:
            q = self.quota()
            self.say(self.quota_text(q))
            snap = self.snapshot()
        except (GraphError, UsageLimit) as e:
            self.say(f'Quota/live account could not be read - {explain(e)}')
            return
        found, _unknown = self.issues(st, todo, g, snap)
        n24 = sum(1 for r in snap['stories'] + snap['media'] if parse_ts(r.get('timestamp')) >= time.time() - DAY_S)
        mine = self.api_posts_24h(st)
        self.say(f"Live account: {len(snap['stories'])} live stor{'y' if len(snap['stories']) == 1 else 'ies'} | "
                 f"{n24} post(s) in the last 24 h | {mine} API post(s) in the last 24 h in the state file")
        if isinstance(q.get('used'), int) and q['used'] > mine:
            self.say(f"  ✗ the quota counts {q['used']} API posts, the state file {mine} - another tool may have "
                     f"published through the API (--apply stops; `ig-publish ack --apply` records the account's "
                     f"unknown posts, or override with --ack-unknown-posts)")
        for _kind, msg, how in found:
            self.say(f'  ✗ {msg}  (--apply stops; override with {how})')
        if not found:
            self.say('  ✓ preflight is clean')

    # ------------------------------------------------------------------ commands
    def plan(self) -> None:
        _man, items = self.load()
        st = self.load_state()
        idx = self.prep_index()
        sel, done, todo, parked = self.selection(items, st)
        g = self.gaps()
        self.say(f"Instagram publishing plan - account {self.cfg.ig_user_id}"
                 + (f" (@{self.cfg.username})" if self.cfg.username else '')
                 + f" | Graph {self.cfg.api_version} | {self.rel(self.cfg.manifest)}"
                 + (f' | --only {self.o.only}' if self.o.only else '')
                 + (f" | --keys {','.join(self.o.keys)}" if self.o.keys else ''))
        if not self.cfg.state.exists() and self.journal.publish_count():
            self.say(f'✗ the state file {self.rel(self.cfg.state)} is missing but the journal records publishes - '
                     f'--apply stops (restore the state file)')
        n_story = sum(1 for i in sel if i['kind'] == 'story')
        self.say(f"Order = publishing order | {len(sel)} item(s) ({n_story} stor{'y' if n_story == 1 else 'ies'}, "
                 f"{len(sel) - n_story} reel(s)) | to publish: {len(todo)} | spacing: stories {g['story']:g} s, "
                 f"reels {g['reel'] / 60:g} min\n")
        self.say(f" #  {'group':10} {'key':28} {'kind':5} {'secs':>6}  file\n    [state] | reel caption (in full for "
                 f"pending items)")
        todo_keys = {i['key'] for i in todo}
        for n, it in enumerate(sel, 1):
            full = it['key'] in todo_keys or bool(it.get('needs_approval'))
            self.say(self.item_line(n, it, st, idx, full_caption=full))
        self.say('')
        if st.get('hold'):
            h = st['hold']
            self.say(f"✗ HOLD: {h.get('reason')} ({h.get('key')}) - {self.hold_text(h)}")
        tray = self.tray_line(st, items)
        if tray:
            self.say(tray)
        burst = self.burst_line(st)
        if burst and todo:
            self.say(burst)
        if done:
            self.say(f'{len(done)} item(s) already live are skipped (never published again; on purpose only with '
                     f'--repost KEY).')
        for it, why in parked:
            self.say(f"Skipped: {it['key']} - {why}")
        season, early = self.season_block(todo)
        for m in season:
            self.say(f'✗ outside the season window: {m} -> --apply would stop now (exit {75 if early else 1})')
        self.online_report(st, todo, g)
        unprepared = [it['key'] for it in todo if not self.prep_record(it, idx)[0]]
        for it in todo:
            rec = self.prep_record(it, idx)[0]
            if rec and it.get('thumb_offset') is not None and it['thumb_offset'] >= rec['report']['duration'] * 1000:
                self.say(f"✗ {it['key']}: thumb_offset {it['thumb_offset']} ms is beyond the end of the video")
        if not todo:
            self.say('Nothing to publish.')
            return
        self.explain_apply(todo, st, g)
        if unprepared:
            self.say(f'\n✗ Run prep first ({len(unprepared)} item(s)): ig-publish prep')
        self.say('\nNothing was written. To publish: ig-publish publish [--only GROUP] [--canary | --limit N] --apply')

    def publish(self) -> None:
        if self.o.offline and self.o.apply:
            raise ConfigError('--offline cannot be combined with --apply (publishing needs the token and the network; '
                              'the live-account checks are never skipped).')
        if self.o.at is not None and self.o.apply and not self.endpoints.test_mode:
            raise ConfigError('--at only applies to dry runs; --apply always uses the real clock.')
        _man, items = self.load()
        by_key = {it['key']: it for it in items}
        if not self.o.apply:
            self._publish(items, by_key)
            return
        self.pre_write = True  # until the first container POST, read errors and temporary stops exit 75
        with run_lock(self.cfg.lock_path, 'publish'):
            self._publish(items, by_key)

    def _publish(self, items: list[dict], by_key: dict) -> None:
        o = self.o
        if o.apply:
            self.guard_lost_state()
        st = self.load_state()  # with --apply: read only after the lock is held
        idx = self.prep_index()
        sel, done, todo, parked = self.selection(items, st)
        g = self.gaps()
        self.say(f"({'apply' if o.apply else 'dry run'}) Instagram publish - account {self.cfg.ig_user_id} | "
                 f"selection: {len(sel)} item(s)" + (f' (--only {o.only})' if o.only else '')
                 + (f" (--keys {','.join(o.keys)})" if o.keys else '') + (' | --canary' if o.canary else '')
                 + (f' | --limit {o.limit}' if o.limit is not None and not o.canary else ''))
        for it in done:
            r = st['items'].get(it['key'], {})
            self.say(f"  = already live: {it['key']} -> {self.state_label(it, st)}"
                     + (f" | {r.get('permalink')}" if r.get('permalink') and not it.get('already_live') else ''))
        for it, why in parked:
            self.say(f"  - skipped: {it['key']} - {why}")
        if o.keys and parked:
            msg = (f"--keys: {', '.join(it['key'] for it, _ in parked)} cannot be published now (reason above); the "
                   f"selected keys are published together or not at all")
            if o.apply:
                raise Stop(f'Stopped: {msg}. Nothing was written.')
            self.say(f'✗ {msg} -> --apply would stop.')
        if not todo:
            self.say('Nothing to publish.')
            return
        preps, missing = self.preps(todo, idx)
        for n, it in enumerate(todo, 1):
            self.say(self.item_line(n, it, st, idx))
        season, early = self.season_block(todo)
        if not o.apply:
            for m in season:
                self.say(f'✗ outside the season window: {m} -> --apply would stop now (exit {75 if early else 1})')
            if st.get('hold'):
                self.say(f"✗ HOLD: {st['hold'].get('reason')} ({st['hold'].get('key')}) - {self.hold_text(st['hold'])}")
            burst = self.burst_line(st)
            if burst:
                self.say(burst)
            self.online_report(st, todo, g)
            self.explain_apply(todo, st, g)
            if missing:
                self.say(f"\n✗ Not prepared: {', '.join(missing)} -> ig-publish prep")
            self.say('\nNothing was written. Add --apply to publish.')
            return
        if missing:
            raise Stop(f"✗ Not prepared: {', '.join(missing)}\n  Run first: ig-publish prep")
        if season:
            cls = TemporaryStop if early else Stop
            raise cls('Stopped: outside the season window (nothing was written):\n'
                      + '\n'.join(f'  ✗ {m}' for m in season))
        self.client.token()  # stops here without a token, before any network call
        self.say('\n(0) preflight')
        self.check_hold(st)
        self.reconcile(st, by_key)
        sel, done, todo, parked = self.selection(items, st)  # half-finished items may be done now
        if not todo:
            self.say('Nothing to publish (half-finished items were completed).')
            return
        preps, missing = self.preps(todo, idx)
        if missing:
            raise Stop(f"✗ Not prepared: {', '.join(missing)}\n  Run first: ig-publish prep")
        snap = self.snapshot()
        found, unknown = self.issues(st, todo, g, snap)
        blocking = [(k, m, h) for k, m, h in found
                    if not ((k == 'unknown' and o.ack_unknown_posts) or (k in ('burst', 'gap') and o.ignore_hold))]
        if blocking:
            # A live reel with the same caption needs a decision (--repost): permanent. The rest passes with time.
            cls = Stop if any(k == 'duplicate' for k, _m, _h in blocking) else TemporaryStop
            raise cls('Stopped: preflight (nothing was written):\n'
                      + '\n'.join(f'  ✗ {m}\n    to override: {h}' for _k, m, h in blocking))
        for _k, m, h in found:
            self.say(f'  ! overridden on purpose ({h}): {m}')
        if unknown and o.ack_unknown_posts:
            self.acknowledge(st, unknown)
        q = self.quota()
        mine = self.api_posts_24h(st)
        self.say(f'  {self.quota_text(q)} | state file: {mine} API post(s) in the last 24 h | '
                 f'this run: at most {len(todo)}')
        if isinstance(q.get('used'), int) and q['used'] > mine and not o.ack_unknown_posts:
            raise TemporaryStop(f"Stopped: the quota counts {q['used']} API posts in the last 24 h, the state file "
                                f'only {mine}: another tool may be publishing through the API. Nothing was written. '
                                f"If that is expected, `ig-publish ack --apply` records the account's unknown posts "
                                f'(manifest items posted elsewhere: `ig-publish import`); or add --ack-unknown-posts '
                                f'to this run.')
        if q['left'] is not None and q['left'] < len(todo):
            n = max(q['left'], 0)
            only = ', '.join(i['key'] for i in todo[:n])
            raise TemporaryStop(f"Stopped: not enough quota: {q['left']} left, {len(todo)} selected. Run again when "
                                f'the quota has room, or narrow the selection'
                                + (f' (--limit {n} would publish only: {only})' if n else '') + '.')
        self.journal.write('RUN', 'publish', 0, {'only': o.only, 'want_keys': o.keys, 'limit': o.limit,
                                                 'canary': o.canary, 'repost': sorted(o.repost),
                                                 'approve': sorted(o.approve), 'ignore_hold': o.ignore_hold,
                                                 'pid': os.getpid(), 'keys': [i['key'] for i in todo]}, {'quota': q})
        st['last_run'] = {'pid': os.getpid(), 'started_at': now_iso(), 'keys': [i['key'] for i in todo]}
        self.save_state(st)
        self.pre_write = False  # live writes start now: every stop from here on is permanent (exit 1)
        self.say(f'\n(1) containers: {len(todo)}')
        ready = self.containers(todo, st, preps)
        ready = self.poll(ready, st)
        self.say(f'\n(2) publishing: {len(ready)} item(s), in manifest order')
        for i, it in enumerate(ready):
            if i:
                gap = g[it['kind']]
                if gap >= 60:
                    self.say(f"    ... waiting {gap / 60:g} min ({it['key']} at about {clock(time.time() + gap)})")
                self.sleep(gap)
            self.publish_one(it, st)
        n_ok = sum(1 for it in todo if st['items'].get(it['key'], {}).get('status') == 'published')
        self.journal.write('RUN', 'publish', 0, {'keys': [i['key'] for i in todo]}, {'published': n_ok})
        self.say(f'\n✓ {n_ok}/{len(todo)} item(s) live. State: ig-publish status')

    def delete(self) -> None:
        if len(self.o.pos) != 1:
            raise ConfigError('usage: ig-publish delete KEY [--confirm-media ID] [--apply]')
        key = self.o.pos[0]
        if not self.o.apply:
            self._delete(key)
            return
        with run_lock(self.cfg.lock_path, f'delete {key}'):
            self._delete(key)

    def _delete(self, key: str) -> None:
        self.load()
        st = self.load_state()
        rec = st['items'].get(key)
        it = next((i for i in self.items if i['key'] == key), None)
        if not rec:
            if it and it.get('already_live'):
                raise Stop(f"{key}: posted outside this tool (manifest already_live "
                           f"{it['already_live'].get('permalink')}) - delete it in the Instagram app.")
            raise Stop(f'{key}: not in the state file ({self.rel(self.cfg.state)}).')
        if rec.get('status') != 'published':
            raise Stop(f"{key}: not published (status {rec.get('status')}).")
        mid = rec.get('media_id')
        if not mid:
            cand = rec.get('media_id_candidate')
            if not cand:
                raise Stop(f"{key}: the media ID is unknown ({rec.get('media_id_note')}) - find and delete it in "
                           f'the app.')
            if self.o.confirm_media != str(cand):
                raise Stop(f"{key}: the media ID is only a CANDIDATE {cand} ({rec.get('candidate_permalink')}, "
                           f"{rec.get('candidate_timestamp')}), found by a time match - it could be another post.\n"
                           f"  Check it on Instagram, then: ig-publish delete {key} --confirm-media {cand} --apply")
            mid = str(cand)
        elif self.o.confirm_media and self.o.confirm_media != str(mid):
            raise Stop(f'{key}: --confirm-media {self.o.confirm_media} does not match the recorded media {mid}')
        self.say(f"{'✎' if self.o.apply else '(dry run)'} DELETE {key}: media {mid} | "
                 f"{rec.get('permalink') or rec.get('candidate_permalink')} | published {rec.get('published_at')}")
        if not self.o.apply:
            self.say('Nothing was deleted. Add --apply to delete (the token is first checked for '
                     'instagram_manage_contents).')
            return
        if 'instagram_manage_contents' not in self.granted():
            raise Stop("✗ Deleting needs the instagram_manage_contents permission, which the token lacks. An existing "
                       "token never gains permissions: create a new one with that permission and store it in place of "
                       "the old one. Nothing was deleted.")
        if not MEDIA_ID_RE.fullmatch(str(mid)):
            raise Stop(f'{key}: recorded media ID {mid!r} does not look like an Instagram media ID; nothing was '
                       f'deleted.')
        res = self.client.delete(str(mid), key=key)
        if not (isinstance(res, dict) and res.get('success') is True):
            raise Stop(f'✗ unexpected answer: {json.dumps(res)[:300]}')
        if res.get('deleted_id') is not None and str(res['deleted_id']) != str(mid):
            rec['delete_mismatch'] = {'asked': mid, 'deleted_id': str(res['deleted_id']), 'at': now_iso()}
            self.save_state(st)
            raise Stop(f"✗ Meta says it deleted another ID: deleted_id {res['deleted_id']} != {mid}. Check Instagram.")
        gone = False
        try:
            self.client.get(str(mid), {'fields': 'id'}, retries=1)
        except GraphError:
            gone = True
        keep = ('container_id', 'container_created_at', 'media_id', 'media_id_candidate', 'permalink',
                'published_at', 'origin')
        rec.setdefault('history', []).append({k: rec.get(k) for k in keep if rec.get(k) is not None}
                                             | {'media_id': mid, 'deleted_at': now_iso()})
        for k in TRANSIENT_FIELDS:
            rec.pop(k, None)
        rec.update(status='deleted', deleted_at=now_iso())
        self.save_state(st)
        self.say(f'  ✓ deleted {key} ({mid})' + (f" | deleted_id {res['deleted_id']}" if res.get('deleted_id') else '')
                 + (' | read-back: gone' if gone else ' | ! still visible on read-back'))
        self.say(f'  The key is never published again by itself. To publish it again on purpose: '
                 f'ig-publish publish --repost {key} --canary --apply')

    def import_record(self) -> None:
        """Record posts published outside this tool (offline; no network)."""
        if len(self.o.pos) != 1:
            raise ConfigError('usage: ig-publish import RECORD.json [--apply]')
        path = self.o.pos[0]
        if not os.path.isfile(path):
            raise ConfigError(f'record not found: {path}')
        try:
            rows = load_json(path, None)
        except ValueError as e:
            raise ConfigError(f'cannot read the record ({path}): {e}') from None
        if isinstance(rows, dict):
            rows = {k: v for k, v in rows.items() if not str(k).startswith(('_', '$'))}
        if not isinstance(rows, dict) or not rows:
            raise ConfigError('the record must be an object {"name": {"key" or "src"+"product", "media_id", "at", '
                              '"permalink"?, "container"?, "group"?}}')
        _man, items = self.load()
        by_key = {it['key']: it for it in items}
        by_src: dict[tuple[str, str], list[str]] = {}
        for it in items:
            by_src.setdefault((it['kind'], os.path.basename(it['src'])), []).append(it['key'])

        def build(st: dict) -> tuple[list[tuple], list[str], list[str]]:
            todo: list[tuple] = []
            skip: list[str] = []
            problems: list[str] = []
            seen: dict[str, str] = {}
            for name, e in rows.items():
                e = e if isinstance(e, dict) else {}
                mid = str(e.get('media_id') or '')
                at = e.get('at')
                if not MEDIA_ID_RE.fullmatch(mid):
                    problems.append(f'{name}: media_id (digits) is required')
                    continue
                if not parse_ts(at):
                    problems.append(f'{name}: "at" (ISO 8601 publish time) is required')
                    continue
                if e.get('key'):
                    key = str(e['key'])
                    if key not in by_key:
                        problems.append(f'{name}: key {key} is not in the manifest')
                        continue
                else:
                    kind = {'STORY': 'story', 'STORIES': 'story', 'REELS': 'reel', 'REEL': 'reel'}.get(
                        str(e.get('product') or '').upper())
                    src = os.path.basename(str(e.get('src') or ''))
                    if not kind or not src:
                        problems.append(f'{name}: give "key", or "src" plus "product" (STORY or REELS)')
                        continue
                    hits = by_src.get((kind, src), [])
                    if len(hits) != 1:
                        problems.append(f"{name}: {kind} {src} -> {len(hits)} match(es) in the manifest "
                                        f"({', '.join(hits) or '-'}); use \"key\" instead")
                        continue
                    key = hits[0]
                if e.get('group') and str(e['group']).upper() != by_key[key]['group'].upper():
                    problems.append(f"{name}: group {e['group']} != manifest group {by_key[key]['group']} of {key}")
                    continue
                if key in seen:
                    problems.append(f'{name}: {key} appears twice in the record ({seen[key]})')
                    continue
                seen[key] = name
                cur = st['items'].get(key) or {}
                if cur.get('status') == 'published':
                    if str(cur.get('media_id')) == mid:
                        skip.append(key)
                    else:
                        problems.append(f"{name}: {key} is already published with another media ID "
                                        f"({cur.get('media_id') or cur.get('media_id_candidate')})")
                    continue
                todo.append((key, name, e, mid, at))
            return todo, skip, problems

        todo, skip, problems = build(self.load_state())
        self.say(f'Import - {path} -> {self.rel(self.cfg.state)} ({len(rows)} record(s))')
        for key, name, e, mid, at in todo:
            mark = '✎' if self.o.apply else '-'
            self.say(f"  {mark} {key:30} <- {name:12} media {mid} | {at} | {e.get('permalink')}")
        for key in skip:
            self.say(f'  = {key}: already imported')
        if problems:
            raise Stop('✗ Import stopped, nothing was written:\n  ' + '\n  '.join(problems))
        if not self.o.apply:
            self.say(f'\nNothing was written ({len(todo)} item(s) would be recorded). Add --apply to write '
                     f'(no network, state file only).')
            return
        with run_lock(self.cfg.lock_path, 'import'):
            self.guard_lost_state()
            st = self.load_state()
            todo, skip, problems = build(st)  # again, under the lock
            if problems:
                raise Stop('✗ Import stopped, nothing was written:\n  ' + '\n  '.join(problems))
            for key, name, e, mid, at in todo:
                rec = st['items'].setdefault(key, {})
                if rec.get('container_id') or rec.get('status'):
                    self.archive(rec, f"record before the import ({rec.get('status')})")
                it = by_key[key]
                rec.update(kind=it['kind'], group=it['group'], status='published',
                           origin=f'{os.path.basename(path)}:{name}',
                           container_id=str(e.get('container') or '') or None,
                           media_id=mid, permalink=e.get('permalink'), published_at=iso(parse_ts(at)),
                           published_at_source='record', media_product_type=SPEC[it['kind']]['product'],
                           imported_at=now_iso(), publish_note='published outside ig-publish; imported')
            self.save_state(st)
            self.journal.write('IMPORT', os.path.basename(path), 0, {'keys': [t[0] for t in todo]},
                               {'media_ids': [t[3] for t in todo]})
        self.say(f"\n✓ {len(todo)} item(s) recorded as 'published' in {self.rel(self.cfg.state)}; they are never "
                 f"published again.")
        tray = self.tray_line(st, items)
        if tray:
            self.say(tray)

    def ack(self) -> None:
        """Acknowledge recent posts that the state file does not know (posted by hand or by another tool), so the
        preflight stops asking about them. Reads the account; with --apply writes the state file only."""
        self.load()  # manifest: already_live media IDs count as known
        if not self.o.apply:
            self._ack()
            return
        with run_lock(self.cfg.lock_path, 'ack'):
            self.guard_lost_state()  # never start a fresh state file next to a journal that records publishes
            self._ack()

    def _ack(self) -> None:
        st = self.load_state()
        snap = self.snapshot()
        _found, unknown = self.issues(st, [], self.gaps(), snap)
        self.say(f'Recent posts (last 24 h) on account {self.cfg.ig_user_id} that the state file does not know: '
                 f'{len(unknown)}')
        for r in unknown:
            self.say(f"  {'✎' if self.o.apply else '-'} {r.get('media_product_type') or r['_edge']} {r.get('id')} "
                     f"{str(r.get('timestamp') or '')[:16]} {r.get('permalink') or ''}")
        if not unknown:
            self.say('Nothing to acknowledge.')
            return
        if not self.o.apply:
            self.say('\nNothing was written. If you made these posts on purpose (by hand or with another tool), add '
                     '--apply: they are recorded as acknowledged in the state file and nothing is posted. Posts of '
                     'manifest items can be recorded with `ig-publish import` instead, so they are never published '
                     'again.')
            return
        self.acknowledge(st, unknown)
        self.journal.write('ACK', 'acknowledge', 0, {'media_ids': [str(r['id']) for r in unknown]}, None)
        self.say(f'\n✓ {len(unknown)} post(s) acknowledged in {self.rel(self.cfg.state)}; publish and the scheduler '
                 f'no longer stop for them.')

    def status(self) -> None:
        _man, items = self.load()
        by_key = {it['key']: it for it in items}
        online = not self.o.offline and self.token_state()[0]
        if online:
            with run_lock(self.cfg.lock_path, 'status', required=False) as got:
                if got and self.cfg.state.exists():
                    try:
                        self.reconcile(self.load_state(), by_key)
                    except (GraphError, UsageLimit) as e:
                        self.say(f'(media ID lookup skipped: {e})')
                elif not got:
                    self.say('(another ig-publish run is active: lookups skipped; the table is the current state)')
        st = self.load_state()
        self.say(f'State - {self.rel(self.cfg.state)} | account {self.cfg.ig_user_id}')
        if st.get('hold'):
            h = st['hold']
            self.say(f"✗ HOLD: {h.get('reason')} ({h.get('key')}) - {self.hold_text(h)}")
        self.say(f"{'key':28} {'group':10} {'kind':5} {'status':16} {'container':>18} {'age':>6}  {'media':>20}  "
                 f"permalink / note")
        counts: dict[str, int] = {}
        for it in items:
            r = st['items'].get(it['key'], {})
            s = r.get('status') or ('already_live' if it.get('already_live') else
                                    'needs_approval' if it.get('needs_approval') else 'pending')
            counts[s] = counts.get(s, 0) + 1
            age = ''
            if r.get('container_created_ts'):
                age = f"{(time.time() - float(r['container_created_ts'])) / 3600:.1f}h"
            al = it.get('already_live') or {}
            mid = r.get('media_id') or (f"CAND {r['media_id_candidate']}" if r.get('media_id_candidate')
                                        else al.get('media_id') or '')
            note = (r.get('permalink') or r.get('candidate_permalink') or al.get('permalink') or r.get('error')
                    or r.get('media_id_note') or '')
            self.say(f"{it['key']:28} {it['group']:10} {it['kind']:5} {s:16} {r.get('container_id') or '':>18} "
                     f"{age:>6}  {mid:>20}  {str(note)[:110]}")
        self.say('\n' + ' | '.join(f'{k}: {v}' for k, v in sorted(counts.items())))
        tray = self.tray_line(st, items)
        if tray:
            self.say(tray)
        if self.o.offline:
            self.say('Quota: --offline (not read; no token, no network)')
        elif not online:
            self.say(f'Quota: not read - no token ({self.token_state()[1]})')
        else:
            try:
                self.say(self.quota_text(self.quota()))
            except (GraphError, UsageLimit) as e:
                self.say(f'Quota: not read - {explain(e)}')

    def check(self) -> int:
        """Read-only account check. Returns the exit code (1 when something required is missing)."""
        c, cfg = self.client, self.cfg
        failures: list[str] = []
        for note in token_notes(c.token()):
            self.say(f'! {note}')
        me = c.get('me', {'fields': 'id,name'})
        self.say(f"Token owner: {me.get('name')} ({me.get('id')}) | Graph {cfg.api_version} | token from "
                 f"{'IG_PUBLISH_TEST_TOKEN (test server)' if self.endpoints.test_mode else cfg.token.describe()}")
        perms = c.get('me/permissions').get('data', [])
        granted = {p.get('permission') for p in perms if p.get('status') == 'granted'}
        self.say('Permissions: ' + ', '.join(f"{p.get('permission')}"
                                             + ('' if p.get('status') == 'granted' else f"({p.get('status')})")
                                             for p in perms))
        missing = [p for p in REQUIRED_SCOPES if p not in granted]
        if missing:
            failures.append('permissions')
            self.say(f"  ✗ missing for publishing: {', '.join(missing)}")
        else:
            self.say('  ✓ publishing permissions present (' + ', '.join(REQUIRED_SCOPES) + ')')
        if 'pages_show_list' not in granted:
            self.say('  ! pages_show_list is missing: the Page check below cannot list your Pages')
        if 'business_management' not in granted or not granted & {'ads_management', 'ads_read'}:
            self.say('  ! if your Page role comes through a Meta Business portfolio, Meta also requires '
                     'business_management and ads_management or ads_read')
        if 'instagram_manage_contents' in granted:
            self.say('  ✓ instagram_manage_contents present (needed by `delete`)')
        else:
            self.say('  - instagram_manage_contents missing: `delete --apply` will not work (an existing token never '
                     'gains permissions; create a new one if you need deletion)')
        page = None
        try:
            accts = c.get_all('me/accounts', {'fields': 'id,name,tasks,instagram_business_account'}, cap=500)
            if cfg.page_id:
                page = next((a for a in accts if str(a.get('id')) == cfg.page_id), None)
            else:
                page = next((a for a in accts if str((a.get('instagram_business_account') or {}).get('id'))
                             == cfg.ig_user_id), None)
        except GraphError as e:
            self.say(f'✗ me/accounts could not be read: {explain(e)}')
        if page:
            tasks = set(page.get('tasks') or [])
            ok = bool(tasks & PAGE_TASKS)
            self.say(f"Page: {page.get('name')} ({page.get('id')}) | tasks {', '.join(sorted(tasks)) or '-'} "
                     + ('✓' if ok else '✗ needs MANAGE or CREATE_CONTENT'))
            if not ok:
                failures.append('page tasks')
            linked = str((page.get('instagram_business_account') or {}).get('id') or '')
            if linked and linked != cfg.ig_user_id:
                failures.append('page link')
                self.say(f'  ✗ this Page is linked to Instagram account {linked}, not {cfg.ig_user_id}')
        elif cfg.page_id:
            failures.append('page')
            self.say(f'✗ Page {cfg.page_id} is not in me/accounts - the token owner has no task on it')
        else:
            self.say(f'! No Page linked to Instagram account {cfg.ig_user_id} was found in me/accounts (missing '
                     f'pages_show_list, or the role comes through a Business portfolio). Set [account] page_id to '
                     f'check a specific Page.')
        ig = c.get(cfg.ig_user_id, {'fields': 'id,username,media_count,followers_count'})
        good = not cfg.username or ig.get('username') == cfg.username
        if not good:
            failures.append('username')
        self.say(f"Instagram: @{ig.get('username')} ({ig.get('id', cfg.ig_user_id)}) | followers "
                 f"{ig.get('followers_count')} | posts {ig.get('media_count')}"
                 + ('' if good else f'  ✗ expected @{cfg.username}'))
        q = self.quota()
        self.say(self.quota_text(q))
        self.say(f'Meta usage: highest {c.usage_pct()}% (this tool stops at {cfg.safety.usage_stop_percent}%)')
        self.say('Reminder: if your Page requires Page Publishing Authorization (PPA), publishing fails until it is '
                 'completed in the Page settings; keep two-factor authentication on for the Facebook account.')
        if failures:
            self.say(f"✗ check failed: {', '.join(failures)}. `ig-publish doctor` prints the fix for each.")
            return 1
        self.say('✓ check passed')
        return 0

    # ------------------------------------------------------------------ scheduler support
    def preview(self, keys: list[str], at: float) -> Preview:
        """What would ``publish --keys KEYS --apply`` do at ``at``? Offline; never raises for content errors."""
        pv = Preview()
        saved = self.o
        self.o = Options(keys=list(keys), offline=True, at=at)
        try:
            _man, items = self.load()
            st = self.load_state()
            idx = self.prep_index()
            _sel, done, todo, parked = self.selection(items, st)
            pv.done = [i['key'] for i in done]
            pv.todo = [i['key'] for i in todo]
            pv.parked = [f"{i['key']}: {why}" for i, why in parked]
            pv.unprepared = self.preps(todo, idx)[1]
            pv.season, pv.season_early = self.season_block(todo, now=at)
        except (ConfigError, Stop) as e:
            pv.error = str(e).splitlines()[0][:300]
        finally:
            self.o = saved
        return pv
