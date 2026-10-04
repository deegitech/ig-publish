"""Durable local records: the state file, the write journal and the run lock.

* **State** (JSON) is the single source of truth for what was published. It is written atomically
  (temporary file + ``os.replace``) and created with mode ``0600``.
* **Journal** (JSON lines, ``0600``) is append-only: every API write is recorded as an ``INTENT`` line *before* the
  request and a result line *after* it. If a result line is missing, the process died mid-request.
* **Run lock** (``fcntl.flock``) makes sure only one writing command runs at a time per state file.

Everything written here passes through :data:`ig_publish.redact.redact`.
"""
from __future__ import annotations

import contextlib
import json
import os
import tempfile
from collections.abc import Iterator
from typing import Any

from .errors import TemporaryStop
from .redact import redact
from .timeutil import now_iso


def load_json(path: str | os.PathLike[str], default: Any) -> Any:
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def save_json(path: str | os.PathLike[str], obj: Any) -> None:
    """Atomic, private (0600) JSON write. The text is redacted before it touches the disk."""
    path = os.fspath(path)
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=os.path.basename(path) + '.', suffix='.tmp', dir=d)  # mode 0600
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(redact(json.dumps(obj, indent=1, ensure_ascii=False, default=str)) + '\n')
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


class Journal:
    """Append-only JSON-lines log of every API write (intent first, result after)."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = os.fspath(path)

    def write(self, method: str, path: str, status: int, body: Any, result: Any) -> None:
        line = redact(json.dumps({'t': now_iso(), 'method': method, 'path': path, 'status': status, 'body': body,
                                  'result': result}, ensure_ascii=False, default=str))
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, 'a', encoding='utf-8') as f:
            f.write(line + '\n')
            f.flush()
            os.fsync(f.fileno())

    def intent(self, method: str, path: str, meta: dict[str, Any]) -> None:
        """Record what is about to be written. A result line with the same method and path follows it."""
        self.write('INTENT', path, 0, dict(meta, method=method), None)

    def entries(self) -> Iterator[dict[str, Any]]:
        try:
            with open(self.path, encoding='utf-8') as f:
                for line in f:
                    try:
                        j = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(j, dict):
                        yield j
        except FileNotFoundError:
            return

    def publish_count(self) -> int:
        """Successful ``media_publish`` and ``IMPORT`` lines; used to notice a lost state file."""
        return sum(1 for j in self.entries()
                   if j.get('method') == 'IMPORT'
                   or (str(j.get('path', '')).endswith('/media_publish') and j.get('status') == 200))


@contextlib.contextmanager
def run_lock(path: str | os.PathLike[str], what: str, required: bool = True) -> Iterator[bool]:
    """Exclusive, non-blocking ``flock`` on ``path``.

    ``required=True``: a busy lock raises :class:`TemporaryStop` (exit 75). ``required=False``: yields ``False``.
    The lock is released when the file is closed, including when the process dies.
    """
    import fcntl  # POSIX only; imported here so the rest of the package still imports elsewhere

    path = os.fspath(path)
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(fd, 'r+', encoding='utf-8') as f:
        got = False
        try:
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                got = True
            except OSError:
                if required:
                    f.seek(0)
                    who = f.read().strip()[:200]
                    raise TemporaryStop(f'Stopped: another ig-publish run holds the lock ({who or "lock held"}). '
                                        f'Stopped so nothing is posted twice; run the command again when it has '
                                        f'finished. Lock file: {path}') from None
            if got:
                f.seek(0)
                f.truncate()
                f.write(f'pid {os.getpid()} | {what} | {now_iso()}\n')
                f.flush()
            yield got
        finally:
            if got:
                with contextlib.suppress(OSError):
                    f.seek(0)
                    f.truncate()
