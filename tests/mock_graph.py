"""A local fake of the Graph API and the rupload endpoint (``http.server`` in a thread).

Tests never touch the network: ``IG_PUBLISH_API_BASE`` and ``IG_PUBLISH_UPLOAD_BASE`` point at this server
and the token comes from ``IG_PUBLISH_TEST_TOKEN``. The mock records every request and flags protocol violations
(token in a URL, wrong ``Authorization`` scheme, a publish before FINISHED, a double publish ...).
All IDs here are made-up placeholders.
"""
from __future__ import annotations

import http.server
import json
import threading
import time
import urllib.parse

VERSION = 'v26.0'
IG = '17841401234567890'           # placeholder Instagram user ID
PAGE = '1234567890'                # placeholder Facebook Page ID
USERNAME = 'example.brand'
TOKEN = 'test-token-for-the-local-mock-only'  # gitleaks:allow - not a secret, never valid anywhere
SCOPES = ['instagram_basic', 'instagram_content_publish', 'pages_show_list', 'pages_read_engagement',
          'business_management', 'ads_read']


def iso(ts: float) -> str:
    return time.strftime('%Y-%m-%dT%H:%M:%S+0000', time.gmtime(ts))


class Mock:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.token = TOKEN
        self.base = ''
        self.reset()

    def reset(self) -> None:
        with self.lock:
            self.seq = 0
            self.log: list[dict] = []
            self.containers: dict[str, dict] = {}
            self.created: list[str] = []
            self.media: dict[str, dict] = {}
            self.publish_calls: list[tuple[str, float]] = []
            self.published: list[str] = []
            self.quota_used, self.quota_total = 0, 50
            self.usage = 3
            self.buc_regain: int | None = None        # estimated_time_to_regain_access (minutes)
            self.polls_to_finish = 2
            self.never_finish = False
            self.container_error_at: set[int] = set()  # n-th container (from 1) ends in ERROR
            self.publish_fail: dict[int, object] = {}   # n-th media_publish call fails like this
            self.status_fail: dict[int, tuple] = {}     # n-th container status read fails (http, code)
            self.upload_reply: dict[int, tuple] = {}    # n-th upload gets this answer (http, body)
            self.echo_token_on: str | None = None       # error text on this path echoes the Authorization header
            self.me_echo = False                        # /me name echoes the Authorization header (HTTP 200)
            self.echo_raw_on: str | None = None         # error text on this path contains the bare token
            self.error_200_on: str | None = None        # answer HTTP 200 with an error body on this path
            self.redirect_on: str | None = None         # answer 302 (to /stolen) on this path
            self.upload_uri: str | None = None          # container answers carry this upload URI instead
            self.create_without_id = False              # container answers (HTTP 200) carry no "id"
            self.quota_duration_echo = False            # quota_duration echoes the Authorization header (malformed)
            self.scopes = list(SCOPES)
            self.declined: list[str] = []               # permissions with status "declined"
            self.page_tasks = ['ADVERTISE', 'ANALYZE', 'CREATE_CONTENT', 'MESSAGING', 'MODERATE', 'MANAGE']
            self.pages: list[dict] | None = None        # me/accounts rows instead of the default Page
            self.me_error: tuple | None = None          # GET me fails with (http, code, subcode, message)
            self.ig_error: tuple | None = None          # GET {IG} fails with (http, code, subcode, message)
            self.account_type: str | None = None        # None: the field is not reported (a "nonexisting field")
            self.extra_live: list[dict] = []            # posts made elsewhere (by hand, another script)
            self.stories_fail: tuple | None = None      # /stories read fails with (http, code)
            self.latency = 0.0
            self.violations: list[str] = []
            self.n_create = self.n_publish = self.n_status = self.n_upload = 0

    @staticmethod
    def public(m: dict) -> dict:
        return {k: m[k] for k in ('id', 'permalink', 'media_type', 'media_product_type', 'timestamp', 'caption')
                if k in m}

    def make_live(self, cid: str) -> str:
        """The container goes live: a post appears on the account. Also used for "Meta published it after all"."""
        with self.lock:
            c = self.containers[cid]
            n = len(self.media) + 1
            mid = f'535353{n:06d}'
            story = c['media_type'] == 'STORIES'
            self.media[mid] = {'id': mid, 'cid': cid, 'media_type': 'VIDEO',
                               'media_product_type': 'STORY' if story else 'REELS',
                               'permalink': (f'https://www.instagram.com/stories/{USERNAME}/{mid}/' if story
                                             else f'https://www.instagram.com/reel/MOCK{n}/'),
                               'timestamp': iso(time.time()),
                               **({} if story else {'caption': c['form'].get('caption')})}
            c['status_code'] = 'PUBLISHED'
            self.published.append(cid)
            self.quota_used += 1
            return mid


MOCK = Mock()


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *a) -> None:  # quiet
        pass

    def reply(self, code: int, obj, headers: dict | None = None) -> None:
        body = obj if isinstance(obj, bytes) else json.dumps(obj).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('X-App-Usage', json.dumps({'call_count': MOCK.usage, 'total_cputime': 1, 'total_time': 1}))
        if MOCK.buc_regain is not None:
            self.send_header('X-Business-Use-Case-Usage', json.dumps({PAGE: [{
                'type': 'instagram', 'call_count': 10, 'total_cputime': 1, 'total_time': 1,
                'estimated_time_to_regain_access': MOCK.buc_regain}]}))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def err(self, http_code: int, code: int, msg: str, sub: int | None = None, transient: bool | None = None) -> None:
        e: dict = {'message': msg, 'type': 'OAuthException', 'code': code, 'fbtrace_id': 'Amock'}
        if sub:
            e['error_subcode'] = sub
        if transient is not None:
            e['is_transient'] = transient
        self.reply(http_code, {'error': e})

    def do_GET(self) -> None:  # noqa: N802
        self.route('GET')

    def do_POST(self) -> None:  # noqa: N802
        self.route('POST')

    def do_DELETE(self) -> None:  # noqa: N802
        self.route('DELETE')

    def route(self, method: str) -> None:
        u = urllib.parse.urlsplit(self.path)
        n = int(self.headers.get('Content-Length') or 0)
        body = self.rfile.read(n) if n else b''
        auth = self.headers.get('Authorization') or ''
        if MOCK.latency:
            time.sleep(MOCK.latency)
        with MOCK.lock:
            MOCK.seq += 1
            entry = {'seq': MOCK.seq, 't': time.time(), 'method': method, 'path': u.path,
                     'query': dict(urllib.parse.parse_qsl(u.query)), 'auth': bool(auth)}
            MOCK.log.append(entry)
            if 'access_token' in u.query or b'access_token' in body[:20000] or MOCK.token in u.query:
                MOCK.violations.append(f'token in the URL or body: {method} {u.path}')
            if u.path.startswith('/stolen'):
                MOCK.violations.append(f'redirect followed with Authorization={bool(auth)}')
                return self.reply(200, {'stolen': True})
            if u.path.startswith(f'/ig-api-upload/{VERSION}/'):
                return self.rupload(u.path, auth, body, entry)
            if not u.path.startswith(f'/{VERSION}/'):
                return self.err(404, 803, f'unknown path {u.path}')
            if auth != f'Bearer {MOCK.token}':
                MOCK.violations.append(f'bad graph auth on {method} {u.path}')
                return self.err(401, 190, 'Invalid OAuth access token.')
            if MOCK.redirect_on and MOCK.redirect_on in u.path:
                return self.reply(302, {}, {'Location': f'{MOCK.base}/stolen'})
            if MOCK.error_200_on and MOCK.error_200_on in u.path:
                return self.reply(200, {'error': {'message': 'error inside a 200 answer', 'type': 'OAuthException',
                                                  'code': 31, 'error_subcode': 3858385}})
            form = dict(urllib.parse.parse_qsl(body.decode())) if method == 'POST' else {}
            entry['form'] = form
            self.query = entry['query']
            return self.graph(method, u.path[len(f'/{VERSION}/'):].strip('/').split('/'), form)

    # ---- rupload: raw bytes, "OAuth" scheme, offset / file_size headers
    def rupload(self, path: str, auth: str, body: bytes, entry: dict) -> None:
        cid = path.rsplit('/', 1)[-1]
        c = MOCK.containers.get(cid)
        if not c:
            return self.err(400, 100, f'no container {cid}')
        problems = []
        if auth != f'OAuth {MOCK.token}':
            problems.append('Authorization must be "OAuth <token>"')
        if self.headers.get('offset') != '0':
            problems.append(f"offset {self.headers.get('offset')!r}")
        if self.headers.get('file_size') != str(len(body)):
            problems.append(f"file_size {self.headers.get('file_size')!r} vs {len(body)} bytes")
        if body[4:8] != b'ftyp':
            problems.append('body is not an MP4')
        if problems:
            MOCK.violations.append(f'rupload {cid}: ' + '; '.join(problems))
            return self.err(400, 100, 'bad upload: ' + '; '.join(problems))
        MOCK.n_upload += 1
        entry['bytes'] = len(body)
        forced = MOCK.upload_reply.get(MOCK.n_upload)
        if forced:
            return self.reply(*forced)  # the bytes count as not received: the container never finishes
        c['uploaded'] = len(body)
        return self.reply(200, {'success': True, 'message': 'Upload successful.'})

    def listing(self, stories: bool) -> list[dict]:
        rows = [MOCK.public(m) for m in MOCK.media.values()
                if (m['media_product_type'] == 'STORY') == stories and not m.get('deleted')]
        rows += [dict(x) for x in MOCK.extra_live if (x.get('media_product_type') == 'STORY') == stories]
        return sorted(rows, key=lambda r: r.get('timestamp') or '', reverse=True)

    def graph(self, method: str, parts: list[str], form: dict) -> None:
        if MOCK.echo_token_on and MOCK.echo_token_on in '/'.join(parts):
            return self.err(400, 190, f"Invalid OAuth access token: {self.headers.get('Authorization')}")
        if MOCK.echo_raw_on and MOCK.echo_raw_on in '/'.join(parts):
            raw = (self.headers.get('Authorization') or '').split(' ', 1)[-1]
            return self.err(400, 190, f'token was {raw}; please check it')
        if method == 'GET' and parts == ['me']:
            if MOCK.me_error:
                http, code, sub, msg = MOCK.me_error
                return self.err(http, code, msg, sub=sub)
            name = f"Mock Owner {self.headers.get('Authorization')}" if MOCK.me_echo else 'Mock Owner'
            return self.reply(200, {'id': '1000001', 'name': name})
        if method == 'GET' and parts == ['me', 'permissions']:
            return self.reply(200, {'data': [{'permission': p, 'status': 'granted'} for p in MOCK.scopes]
                                    + [{'permission': p, 'status': 'declined'} for p in MOCK.declined]})
        if method == 'GET' and parts == ['me', 'accounts']:
            if MOCK.pages is not None:
                return self.reply(200, {'data': MOCK.pages})
            return self.reply(200, {'data': [{'id': PAGE, 'name': 'Example Page', 'tasks': MOCK.page_tasks,
                                              'instagram_business_account': {'id': IG}}]})
        if method == 'GET' and parts == [IG]:
            if MOCK.ig_error:
                http, code, sub, msg = MOCK.ig_error
                return self.err(http, code, msg, sub=sub)
            fields = self.query.get('fields', '').split(',')
            if 'account_type' in fields:
                if MOCK.account_type is None:
                    return self.err(400, 100, '(#100) Tried accessing nonexisting field (account_type) on node type '
                                              '(IGUser)')
                return self.reply(200, {'id': IG, 'account_type': MOCK.account_type})
            return self.reply(200, {'id': IG, 'username': USERNAME, 'followers_count': 42,
                                    'media_count': sum(1 for m in MOCK.media.values() if not m.get('deleted'))})
        if method == 'GET' and parts == [IG, 'content_publishing_limit']:
            duration = self.headers.get('Authorization') if MOCK.quota_duration_echo else 86400
            return self.reply(200, {'data': [{'config': {'quota_total': MOCK.quota_total, 'quota_duration': duration},
                                              'quota_usage': MOCK.quota_used}]})
        if method == 'POST' and parts == [IG, 'media']:
            return self.create(form)
        if method == 'POST' and parts == [IG, 'media_publish']:
            return self.publish(form)
        if method == 'GET' and parts == [IG, 'stories']:
            if MOCK.stories_fail:
                return self.err(MOCK.stories_fail[0], MOCK.stories_fail[1], 'mock stories read failure')
            return self.reply(200, {'data': self.listing(True)})
        if method == 'GET' and parts == [IG, 'media']:
            return self.reply(200, {'data': self.listing(False)})
        if len(parts) == 1 and parts[0] in MOCK.containers and method == 'GET':
            return self.status(parts[0])
        if len(parts) == 1 and parts[0] in MOCK.media:
            m = MOCK.media[parts[0]]
            if method == 'DELETE':
                if m.get('deleted'):
                    return self.err(400, 100, 'Object does not exist')
                m['deleted'] = True
                return self.reply(200, {'success': True, 'deleted_id': m['id']})
            if method == 'GET':
                if m.get('deleted'):
                    return self.err(400, 100, 'Unsupported get request. Object does not exist')
                return self.reply(200, MOCK.public(m))
        return self.err(400, 100, f"Unsupported {method} request: {'/'.join(parts)}")

    def create(self, form: dict) -> None:
        mt = form.get('media_type')
        if form.get('upload_type') != 'resumable':
            MOCK.violations.append(f'container without upload_type=resumable: {form}')
        if mt == 'STORIES':
            if set(form) - {'media_type', 'upload_type'}:
                MOCK.violations.append(f'story container with extra fields: {sorted(form)}')
        elif mt == 'REELS':
            if form.get('share_to_feed') not in ('true', 'false') or \
                    ('thumb_offset' in form and not form['thumb_offset'].isdigit()):
                MOCK.violations.append(f'reel container with bad share_to_feed/thumb_offset: {sorted(form)}')
        else:
            return self.err(400, 100, f'bad media_type {mt}')
        MOCK.n_create += 1
        cid = f'424242{MOCK.n_create:06d}'
        MOCK.containers[cid] = {'n': MOCK.n_create, 'media_type': mt, 'form': form, 'uploaded': 0, 'polls': 0,
                                'status_code': 'IN_PROGRESS', 'error': MOCK.n_create in MOCK.container_error_at}
        MOCK.created.append(cid)
        if MOCK.create_without_id:
            return self.reply(200, {'uri': f'{MOCK.base}/ig-api-upload/{VERSION}/{cid}'})
        return self.reply(200, {'id': cid, 'uri': MOCK.upload_uri or f'{MOCK.base}/ig-api-upload/{VERSION}/{cid}'})

    def status(self, cid: str) -> None:
        MOCK.n_status += 1
        forced = MOCK.status_fail.get(MOCK.n_status)
        if forced:
            return self.err(forced[0], forced[1], f'mock status failure {forced[1]}')
        c = MOCK.containers[cid]
        sc = c['status_code']
        if sc not in ('FINISHED', 'ERROR', 'PUBLISHED'):
            if c['uploaded']:
                c['polls'] += 1
                if c['error']:
                    sc = 'ERROR'
                elif not MOCK.never_finish and c['polls'] >= MOCK.polls_to_finish:
                    sc = 'FINISHED'
            c['status_code'] = sc
        text = {'ERROR': 'Error: Media upload has failed with error code 2207026'}.get(sc, sc)
        phase = {'status': 'complete' if c['uploaded'] else 'not_started', 'bytes_transferred': c['uploaded']}
        return self.reply(200, {'id': cid, 'status_code': sc, 'status': text,
                                'video_status': {'uploading_phase': phase}})

    def publish(self, form: dict) -> None:
        cid = form.get('creation_id') or ''
        MOCK.n_publish += 1
        MOCK.publish_calls.append((cid, time.time()))
        c = MOCK.containers.get(cid)
        if not c:
            return self.err(400, 100, 'no such container')
        if c['status_code'] == 'PUBLISHED':
            MOCK.violations.append(f'DOUBLE media_publish of {cid}')
            return self.err(400, 9, 'The media has already been published', sub=2207008)
        if c['status_code'] != 'FINISHED':
            MOCK.violations.append(f"media_publish before FINISHED: {cid} {c['status_code']}")
            return self.err(400, 9, 'Media ID is not available', sub=2207027)
        fail = MOCK.publish_fail.get(MOCK.n_publish)
        if isinstance(fail, tuple):
            fail = {'http': fail[0], 'code': fail[1], 'sub': fail[2]}
        if fail == '500':  # NOT published
            return self.err(500, 2, 'Service temporarily unavailable', transient=True)
        if fail == '200_no_id':  # NOT published, and the answer is HTTP 200 without an "id"
            return self.reply(200, {})
        if isinstance(fail, dict) and not fail.get('published'):
            return self.err(fail['http'], fail['code'], f"mock failure {fail['code']}/{fail.get('sub')}",
                            sub=fail.get('sub'), transient=fail.get('transient'))
        mid = MOCK.make_live(cid)
        if fail == '500_published':  # the post WENT LIVE but the answer is a 5xx
            return self.err(500, 1, 'An unknown error occurred')
        if fail == '200_no_id_published':  # the post WENT LIVE but the answer (HTTP 200) has no "id"
            return self.reply(200, {})
        if isinstance(fail, dict):  # the post went live but the answer is an error (e.g. 403 code 4 / 2207051)
            return self.err(fail['http'], fail['code'], f"mock failure {fail['code']}/{fail.get('sub')}",
                            sub=fail.get('sub'), transient=fail.get('transient'))
        return self.reply(200, {'id': mid})


def start() -> http.server.ThreadingHTTPServer:
    srv = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    MOCK.base = f'http://127.0.0.1:{srv.server_address[1]}'
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv
