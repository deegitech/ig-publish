"""A small Instagram Graph API client (Facebook Login flavour) built on ``urllib``.

Security properties:

* The token travels only in the ``Authorization`` header (``Bearer`` for graph.facebook.com, ``OAuth`` for
  rupload.facebook.com) -- never in a URL, a form body, a log line or the journal.
* Redirects are never followed (``urllib`` would otherwise forward the ``Authorization`` header).
* Uploads only go to the configured rupload host under ``/ig-api-upload/``; any other upload URL is refused
  before the token is attached.
* Meta's usage headers (``X-App-Usage``, ``X-Business-Use-Case-Usage`` ...) are tracked; once the highest value
  reaches ``usage_stop_percent`` no further request is sent.
* Every write (``POST``, upload, ``DELETE``) is journaled: an ``INTENT`` line before, the result after.

For tests, ``IG_PUBLISH_API_BASE`` and ``IG_PUBLISH_UPLOAD_BASE`` can point both endpoints at a local server
(loopback addresses only). In that mode only ``IG_PUBLISH_TEST_TOKEN`` is used, so a real token can never be sent
anywhere but Meta.
"""
from __future__ import annotations

import http.client
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from . import __version__
from .config import Timing
from .errors import ConfigError, GraphError, Stop, UsageLimit
from .redact import redact
from .state import Journal

REAL_GRAPH = 'https://graph.facebook.com'
REAL_UPLOAD = 'https://rupload.facebook.com'
API_BASE_ENV = 'IG_PUBLISH_API_BASE'
UPLOAD_BASE_ENV = 'IG_PUBLISH_UPLOAD_BASE'
USER_AGENT = f'ig-publish/{__version__}'
# Meta error codes worth an automatic retry on *reads* (never on writes).
TRANSIENT_READ_CODES = (1, 2, 4, 17, 341, 613, 80004)


@dataclass(frozen=True)
class Endpoints:
    graph: str
    upload: str
    test_mode: bool


def _is_loopback(url: str) -> bool:
    parts = urllib.parse.urlsplit(url)
    return parts.scheme in ('http', 'https') and (parts.hostname or '') in ('127.0.0.1', 'localhost', '::1')


def resolve_endpoints(env: Mapping[str, str] = os.environ) -> Endpoints:
    graph = (env.get(API_BASE_ENV) or '').rstrip('/')
    upload = (env.get(UPLOAD_BASE_ENV) or '').rstrip('/')
    if not graph and not upload:
        return Endpoints(REAL_GRAPH, REAL_UPLOAD, False)
    if not (graph and upload and _is_loopback(graph) and _is_loopback(upload)):
        raise ConfigError(f'{API_BASE_ENV} / {UPLOAD_BASE_ENV} exist only for local test servers: set both to '
                          f'http://127.0.0.1:<port> (or localhost / ::1), or unset both to talk to Meta. '
                          f'A real token is only ever sent to {REAL_GRAPH} and {REAL_UPLOAD}.')
    return Endpoints(graph, upload, True)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001 - urllib signature
        return None  # urllib then raises HTTPError for the 3xx answer


def _encode_params(params: Mapping[str, Any] | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for k, v in (params or {}).items():
        if v is None:
            continue
        out[k] = json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list, bool)) else str(v)
    return out


def _drop_field(fields: str, bad: str) -> str:
    """Remove one field (with its sub-selection, if any) from a ``fields`` expression like ``a,b{c,d},e``."""
    out = re.sub(rf'(?:(?<=^)|(?<=[{{,])){re.escape(bad)}(?:\{{[^{{}}]*\}})?(?=[,}}]|$)', '', fields)
    return re.sub(r',{2,}', ',', out).replace('{,', '{').replace(',}', '}').strip(',')


class GraphClient:
    def __init__(self, *, version: str, endpoints: Endpoints, token_provider: Callable[[], str],
                 journal: Journal | None, usage_stop_percent: int, timing: Timing,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.version = version
        self.endpoints = endpoints
        self.graph = f'{endpoints.graph}/{version}'
        self.upload_base = endpoints.upload
        self._token_provider = token_provider
        self._token: str | None = None
        self.journal = journal
        self.usage_stop = usage_stop_percent
        self.timing = timing
        self.sleep = sleep
        self.usage: dict[str, Any] = {}
        handlers: list[urllib.request.BaseHandler] = [_NoRedirect()]
        if endpoints.test_mode:
            handlers.append(urllib.request.ProxyHandler({}))  # never send test traffic through a proxy
        self._opener = urllib.request.build_opener(*handlers)

    # ------------------------------------------------------------------ token
    def token(self) -> str:
        if self._token is None:
            self._token = self._token_provider()
        return self._token

    # ------------------------------------------------------------------ usage headers
    def _note_usage(self, headers: Any) -> None:
        for h in ('X-App-Usage', 'X-Ad-Account-Usage', 'X-Business-Use-Case-Usage'):
            v = headers.get(h) if headers else None
            if v:
                try:
                    self.usage[h] = json.loads(v)
                except ValueError:
                    self.usage[h] = {}

    @staticmethod
    def _top(d: Any) -> int:
        if not isinstance(d, dict):
            return 0
        best = 0
        for k in ('call_count', 'total_cputime', 'total_time', 'acc_id_util_pct'):
            try:
                best = max(best, int(float(d.get(k) or 0)))
            except (TypeError, ValueError):
                pass
        return best

    def _buc_rows(self) -> list[dict[str, Any]]:
        buc = self.usage.get('X-Business-Use-Case-Usage') or {}
        rows: list[dict[str, Any]] = []
        if isinstance(buc, dict):
            for lst in buc.values():
                if isinstance(lst, list):
                    rows += [u for u in lst if isinstance(u, dict)]
        return rows

    def usage_pct(self) -> int:
        """Highest usage percentage seen in the last answer's usage headers."""
        vals = [self._top(self.usage.get('X-App-Usage')), self._top(self.usage.get('X-Ad-Account-Usage'))]
        vals += [self._top(u) for u in self._buc_rows()]
        return max(vals)

    def regain_minutes(self) -> int:
        """``estimated_time_to_regain_access`` (minutes) from ``X-Business-Use-Case-Usage``, the largest one."""
        best = 0
        for u in self._buc_rows():
            try:
                best = max(best, int(float(u.get('estimated_time_to_regain_access') or 0)))
            except (TypeError, ValueError):
                pass
        return best

    # ------------------------------------------------------------------ transport
    def _send(self, req: urllib.request.Request, where: str, timeout: float) -> tuple[int, Any]:
        pct = self.usage_pct()
        if pct >= self.usage_stop:
            raise UsageLimit(f'Stopped: Meta API usage is at {pct}% (threshold {self.usage_stop}%). The usage window '
                             f'rolls over within about an hour; the same command continues where it stopped.')
        req.add_header('User-Agent', USER_AGENT)
        try:
            with self._opener.open(req, timeout=timeout) as r:
                self._note_usage(r.headers)
                status = r.status
                raw = r.read().decode('utf-8', 'replace')
        except urllib.error.HTTPError as e:
            self._note_usage(e.headers)
            try:
                raw = e.read().decode('utf-8', 'replace')
            except (OSError, http.client.HTTPException):
                raw = ''
            err: Any
            try:
                j = json.loads(raw)
                err = (j.get('error') or j.get('debug_info') or {'message': raw[:1500]}) if isinstance(j, dict) \
                    else {'message': raw[:1500]}
            except ValueError:
                err = {'message': raw[:1500] or f'HTTP {e.code} {e.reason}'}
            if not isinstance(err, dict):
                err = {'message': str(err)[:1500]}
            raise GraphError(e.code, json.loads(redact(json.dumps(err))), where) from None
        except urllib.error.URLError as e:
            raise GraphError(0, {'message': redact(f'network: {e.reason}')}, where) from None
        except (OSError, http.client.HTTPException) as e:
            raise GraphError(0, {'message': redact(f'network: {type(e).__name__}: {e}')}, where) from None
        try:
            body = json.loads(raw) if raw.strip() else {}
        except ValueError:
            body = {'_raw': redact(raw[:300])}
        # Meta sometimes answers HTTP 200 with an error body: observed (Oct 2026).
        if isinstance(body, dict) and isinstance(body.get('error'), dict):
            raise GraphError(status, json.loads(redact(json.dumps(body['error']))), where)
        return status, body

    def _journal(self, method: str, path: str, status: int, body: Any, result: Any) -> None:
        if self.journal:
            self.journal.write(method, path, status, body, result)

    def _intent(self, method: str, path: str, meta: dict[str, Any]) -> None:
        if self.journal:
            self.journal.intent(method, path, meta)

    # ------------------------------------------------------------------ reads
    def get(self, path: str, params: Mapping[str, Any] | None = None, retries: int = 3) -> Any:
        """``GET /{version}/{path}``. Transient errors are retried a few times (reads only)."""
        q = urllib.parse.urlencode(_encode_params(params))
        url = f'{self.graph}/{path.lstrip("/")}' + (f'?{q}' if q else '')
        for i in range(max(1, retries)):
            req = urllib.request.Request(url, headers={'Authorization': f'Bearer {self.token()}'}, method='GET')
            try:
                return self._send(req, f'GET {path}', self.timing.http_timeout_seconds)[1]
            except GraphError as e:
                transient = bool(e.err.get('is_transient')) or e.code in TRANSIENT_READ_CODES \
                    or e.status in (0, 500, 502, 503, 504)
                if not transient or i >= retries - 1:
                    raise
                self.sleep(self.timing.read_retry_backoff_seconds * (i + 1))
        raise AssertionError('unreachable')

    def get_fields(self, path: str, fields: str, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """``GET ?fields=...``. If Meta says a field does not exist in this API version, drop it and retry; the
        dropped names are returned under ``_dropped`` (read-backs survive API version drift)."""
        dropped: list[str] = []
        for _ in range(12):
            try:
                r = self.get(path, dict(params or {}, fields=fields))
                if dropped and isinstance(r, dict):
                    r['_dropped'] = dropped
                return r
            except GraphError as e:
                m = re.search(r'nonexisting field \(([^)]+)\)', str(e.err.get('message', '')))
                new = _drop_field(fields, m.group(1)) if m else fields
                if not m or new == fields:
                    raise
                dropped.append(m.group(1))
                fields = new
        raise GraphError(0, {'message': f'too many unknown fields: {dropped}'}, f'GET {path}')

    def get_all(self, path: str, params: Mapping[str, Any] | None = None, cap: int = 2000) -> list[dict[str, Any]]:
        """Paged edge. Follows the ``after`` cursor; never the ``paging.next`` URL (it can carry a token)."""
        p = dict(params or {})
        p.setdefault('limit', 100)
        out: list[dict[str, Any]] = []
        while True:
            res = self.get(path, p)
            out += res.get('data', []) if isinstance(res, dict) else []
            paging = res.get('paging', {}) if isinstance(res, dict) else {}
            after = (paging.get('cursors') or {}).get('after')
            if not paging.get('next') or not after or len(out) >= cap:
                return out
            p['after'] = after

    # ------------------------------------------------------------------ writes (journaled)
    def post(self, path: str, params: Mapping[str, Any] | None = None, *, key: str | None = None) -> Any:
        """``POST /{version}/{path}`` with a form body. Never retried here: the caller decides after reading state."""
        body = dict(params or {})
        full = f'{self.version}/{path.lstrip("/")}'
        self._intent('POST', full, {'key': key, 'body': body})
        data = urllib.parse.urlencode(_encode_params(body)).encode()
        req = urllib.request.Request(f'{self.graph}/{path.lstrip("/")}', data=data, method='POST',
                                     headers={'Authorization': f'Bearer {self.token()}',
                                              'Content-Type': 'application/x-www-form-urlencoded'})
        try:
            status, res = self._send(req, f'POST {path}', self.timing.http_timeout_seconds * 2)
        except GraphError as e:
            self._journal('POST', full, e.status, body, e.err)
            raise
        self._journal('POST', full, status, body, res)
        return res

    def upload(self, uri: str, file_path: str, *, key: str, container_id: str) -> dict[str, Any]:
        """Resumable upload of a whole file in one request (``offset: 0``), streamed from disk.

        Success is accepted only as ``{"success": true}``; any other body (``debug_info``, non-JSON) is an error.
        """
        want = urllib.parse.urlsplit(self.upload_base)
        got = urllib.parse.urlsplit(uri)
        if (got.scheme, got.netloc) != (want.scheme, want.netloc) or not got.path.startswith('/ig-api-upload/'):
            raise Stop(f'Unexpected upload address; the token was not sent: {redact(uri)} '
                       f'(expected {self.upload_base}/ig-api-upload/...)')
        size = os.path.getsize(file_path)
        jpath = got.path.lstrip('/')
        meta = {'key': key, 'container': container_id, 'file': os.path.basename(file_path), 'bytes': size}
        self._intent('UPLOAD', jpath, meta)
        try:
            with open(file_path, 'rb') as f:
                req = urllib.request.Request(uri, data=f, method='POST', headers={
                    'Authorization': f'OAuth {self.token()}', 'offset': '0', 'file_size': str(size),
                    'Content-Type': 'application/octet-stream', 'Content-Length': str(size)})
                status, res = self._send(req, f'UPLOAD {container_id}', self.timing.upload_timeout_seconds)
        except GraphError as e:
            self._journal('UPLOAD', jpath, e.status, meta, e.err)
            raise
        self._journal('UPLOAD', jpath, status, meta, res)
        if not (isinstance(res, dict) and res.get('success') is True):
            raise GraphError(status, {'message': 'upload not confirmed (no "success": true in the answer): '
                                                 + redact(json.dumps(res, ensure_ascii=False))[:400]},
                             f'UPLOAD {container_id}')
        return res

    def delete(self, object_id: str, *, key: str) -> Any:
        path = f'{self.version}/{object_id}'
        self._intent('DELETE', path, {'key': key})
        req = urllib.request.Request(f'{self.graph}/{object_id}', method='DELETE',
                                     headers={'Authorization': f'Bearer {self.token()}'})
        try:
            status, res = self._send(req, f'DELETE {object_id}', self.timing.http_timeout_seconds)
        except GraphError as e:
            self._journal('DELETE', path, e.status, {'key': key}, e.err)
            raise
        self._journal('DELETE', path, status, {'key': key}, res)
        return res
