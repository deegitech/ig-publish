"""A careful scheduler for ``ig-publish publish --keys KEYS --apply``.

Schedule file (``[schedule] file``), one line per run, ``#`` starts a comment::

    2030-11-03 18:30 story-01,story-02
    2030-11-04 12:00 reel-launch       # times are in [schedule] timezone

Rules (the done file records every outcome, so a restart never repeats a line):

* exit 0 -> ``ok``; the line never runs again.
* exit 75 -> ``retry``: nothing was written (hold, burst guard, reel spacing, quota, a read error, season not open
  yet...). The line waits and is retried every ``retry_seconds`` until ``max_late_hours`` have passed (then
  ``missed``). The first temporary stop sends a notification. Retrying is safe: the publisher's state file
  prevents double posts.
* any other exit -> ``fail``: not retried (delete its lines from the done file to retry); a notification is sent.
* a line more than ``max_late_hours`` past its time is never run -> ``missed`` (no burst after a machine slept).
* one run at a time, at least ``gap_seconds`` between the end of one run and the start of the next, lines in file
  order (a waiting line holds back the lines behind it).
* the schedule file is re-read every tick (a broken edit keeps the last good version and is reported once);
  ``run`` keeps running when every line is done and picks up lines appended later; ``once`` runs a single tick.
* SIGTERM / Ctrl-C: the running publish is allowed to finish and is recorded, then the scheduler exits.
  A second signal terminates the publish (its state file still prevents a double post on restart).
* every line written to the log is redacted, and also printed to stdout (``docker logs``, journald); with ``once``
  (cron) the routine lines ("started", "WAITING", "schedule complete") stay in the log file only.
* ``ig-publish schedule run`` / ``once`` refuse to start without ``--apply``; ``dry`` never publishes.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import io
import os
import re
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import TextIO

from .config import Config
from .errors import EXIT_LATER, ConfigError
from .redact import redact
from .tokens import scrubbed_env

LINE_RE = re.compile(r'(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2})\s+([a-z0-9][a-z0-9-]*(?:,[a-z0-9][a-z0-9-]*)*)')


@dataclass(frozen=True)
class Line:
    epoch: float
    id: str
    keys: str


def _zone(name: str | None) -> dt.tzinfo | None:
    if not name:
        return None
    from zoneinfo import ZoneInfo
    return ZoneInfo(name)


def to_epoch(date: str, hhmm: str, tz: str | None) -> float:
    d = dt.datetime.strptime(f'{date} {hhmm}', '%Y-%m-%d %H:%M')
    z = _zone(tz)
    return (d.replace(tzinfo=z) if z else d).timestamp()


def fmt(epoch: float, tz: str | None) -> str:
    return dt.datetime.fromtimestamp(epoch, _zone(tz)).strftime('%Y-%m-%d %H:%M')


def dur(seconds: float) -> str:
    s = int(abs(seconds))
    if s < 3600:
        return f'{s // 60} min'
    if s >= 2 * 86400:
        return f'{s // 86400} d {s % 86400 // 3600} h'
    return f'{s // 3600} h {s % 3600 // 60:02d} min'


def parse_schedule(text: str, tz: str | None) -> tuple[list[Line], list[str]]:
    lines: list[Line] = []
    errs: list[str] = []
    for raw in text.splitlines():
        body = raw.split('#', 1)[0].strip()
        if not body:
            continue
        m = LINE_RE.fullmatch(body)
        if not m:
            errs.append(f'✗ malformed line: {raw.strip()}')
            continue
        d, t, keys = m.groups()
        try:
            e = to_epoch(d, t, tz)
        except ValueError:
            errs.append(f'✗ invalid date or time: {raw.strip()}')
            continue
        lines.append(Line(e, f'{d} {t} {keys}', keys))
    return lines, errs


def why_line(output: list[str]) -> str:
    """The reason to show in a notification: the first ✗ line, else the first 'Stopped' line, else the last line.
    When the publisher printed a ``fix:`` line after it, the fix is appended (`` | fix: ...``)."""
    lines = [x.strip() for x in output]
    at = next((i for i, x in enumerate(lines) if '✗' in x), None)
    if at is None:
        at = next((i for i, x in enumerate(lines) if x.startswith('Stopped')), None)
    if at is None:
        rest = [x for x in lines if x]
        return rest[-1] if rest else ''
    fix = next((x for x in lines[at + 1:] if x.startswith('fix: ')), None)
    return lines[at] + (f' | {fix}' if fix else '')


class DoneFile:
    """Append-only ``<line id>\\t<status>\\t<detail>`` records."""

    def __init__(self, path: str) -> None:
        self.path = path

    def rows(self) -> list[tuple[str, str, str]]:
        out: list[tuple[str, str, str]] = []
        try:
            with open(self.path, encoding='utf-8') as f:
                for ln in f:
                    parts = ln.rstrip('\n').split('\t')
                    if len(parts) >= 2:
                        out.append((parts[0], parts[1], parts[2] if len(parts) > 2 else ''))
        except FileNotFoundError:
            pass
        return out

    def status(self, line_id: str) -> str | None:
        """Last final status (ok / fail / missed); retry records do not count."""
        st = None
        for i, s, _d in self.rows():
            if i == line_id and not s.startswith('retry'):
                st = s
        return st

    def retry_info(self, line_id: str) -> tuple[float, int] | None:
        """``(next attempt epoch, number of temporary stops)`` or ``None``."""
        n, count = 0.0, 0
        for i, s, d in self.rows():
            if i == line_id and s.startswith('retry'):
                count += 1
                m = re.search(r'next=(\d+)', d)
                if m:
                    n = float(m.group(1))
        return (n, count) if count else None

    def last_end(self) -> float:
        best = 0.0
        for _i, _s, d in self.rows():
            m = re.search(r'end=(\d+)', d)
            if m:
                best = max(best, float(m.group(1)))
        return best

    def mark(self, line_id: str, status: str, detail: str) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, 'a', encoding='utf-8') as f:
            f.write(f'{line_id}\t{status}\t{detail}\n')
            f.flush()
            os.fsync(f.fileno())


Runner = Callable[[str], tuple[int, list[str]]]


class Scheduler:
    def __init__(self, cfg: Config, *, out: TextIO | None = None, clock: Callable[[], float] = time.time,
                 fake_now: float | None = None, runner: Runner | None = None,
                 notifier: Callable[[str], None] | None = None) -> None:
        self.cfg = cfg
        self.s = cfg.schedule
        self.tz = self.s.timezone
        self.out = out or sys.stdout
        self.clock = clock
        self.fake_now = fake_now
        self.runner = runner or self.run_publish
        self.streams_output = runner is None  # the default runner writes the child's output to the log live
        self.notifier = notifier
        self.done = DoneFile(str(self.s.done_file))
        self.lock_path = str(self.s.done_file.with_name(self.s.done_file.stem + '.lock'))
        self.last_output = str(self.s.log_file) + '.last'
        self.lines: list[Line] = []
        self.stop = False
        self.signals = 0
        self.child: subprocess.Popen[str] | None = None
        self.waited = ''
        self.reload_warned = False
        self.last_end = 0.0
        self.once = False

    # ------------------------------------------------------------------ helpers
    def now(self) -> float:
        return self.fake_now if self.fake_now is not None else self.clock()

    def say(self, msg: str) -> None:
        self.out.write(redact(msg) + '\n')
        self.out.flush()

    def fmt(self, epoch: float) -> str:
        return fmt(epoch, self.tz)

    def log(self, msg: str, *, routine: bool = False) -> None:
        """Append a redacted, timestamped line to the log file and print it to stdout (``docker logs``).
        ``routine`` lines repeat on every ``once`` tick, so in that mode (cron) they go to the file only."""
        stamp = dt.datetime.fromtimestamp(self.clock(), _zone(self.tz)).strftime('%Y-%m-%d %H:%M:%S')
        line = redact(f'{stamp} {msg}')
        path = str(self.s.log_file)
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, 'a', encoding='utf-8') as f:
            f.write(line + '\n')
        if not (routine and self.once):
            self.out.write(line + '\n')
            self.out.flush()

    def notify(self, msg: str) -> None:
        msg = redact(msg).replace('\n', ' ').replace('\t', ' ')
        self.log(f'NOTIFY: {msg}')
        if self.notifier:
            self.notifier(msg)
            return
        if not self.s.notify_command:
            return
        try:  # the notifier never gets the token variable
            r = subprocess.run([*self.s.notify_command, msg], stdin=subprocess.DEVNULL, capture_output=True,
                               text=True, timeout=30, env=scrubbed_env(self.cfg.token.env_var))
        except (OSError, subprocess.SubprocessError) as e:
            self.log(f'WARNING: notify_command did not run: {type(e).__name__}: {e}')
            return
        if r.returncode != 0:
            detail = ' '.join((r.stderr or r.stdout or '').split())[-300:]
            self.log(f'WARNING: notify_command exited {r.returncode}' + (f': {detail}' if detail else ''))

    def read_lines(self) -> tuple[list[Line], list[str]]:
        try:
            with open(self.s.file, encoding='utf-8') as f:
                text = f.read()
        except FileNotFoundError:
            return [], [f'✗ schedule file not found: {self.s.file}']
        return parse_schedule(text, self.tz)

    def reload(self) -> None:
        lines, errs = self.read_lines()
        if not errs:
            self.lines = lines
            self.reload_warned = False
        elif not self.reload_warned:
            self.log(f'WARNING: {self.s.file} is unreadable or has malformed lines - keeping the previous version '
                     f'until it is fixed: {" ".join(errs)}')
            self.reload_warned = True

    def pending(self) -> int:
        return sum(1 for ln in self.lines if not self.done.status(ln.id))

    def command(self, keys: str) -> list[str]:
        base = list(self.s.command)
        if not base:
            base = [sys.executable, '-m', 'ig_publish']
            if self.cfg.path:
                base += ['--config', str(self.cfg.path)]
        return base + ['publish', '--keys', keys, '--apply']

    def run_publish(self, keys: str) -> tuple[int, list[str]]:
        """Run the publisher as a child process (its own session, so a terminal Ctrl-C reaches only us)."""
        lines: list[str] = []
        os.makedirs(os.path.dirname(os.path.abspath(self.last_output)), exist_ok=True)
        fd = os.open(self.last_output, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as last:
            proc = subprocess.Popen(self.command(keys), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True, start_new_session=True,
                                    cwd=str(self.cfg.base_dir))
            self.child = proc
            try:
                assert proc.stdout is not None
                for raw in proc.stdout:
                    line = redact(raw.rstrip('\n'))
                    lines.append(line)
                    last.write(line + '\n')
                    self.log(f'  | {line}')
                rc = proc.wait()
            finally:
                self.child = None
        return rc, lines

    # ------------------------------------------------------------------ one tick
    def tick(self) -> None:
        """Run at most one due line."""
        n = self.now()
        s = self.s
        for ln in self.lines:
            if self.done.status(ln.id):
                continue
            if n < ln.epoch:
                continue
            if n - ln.epoch > s.max_late_seconds:
                self.done.mark(ln.id, 'missed', f'at={int(n)}')
                self.log(f'MISSED: {ln.id} - {dur(n - ln.epoch)} past its time (more than max_late_hours); not '
                         f'published. To publish it, add a line with a new time.')
                self.notify(f'MISSED, not published: {ln.keys} - {dur(n - ln.epoch)} past its time. Add a line with a '
                            f'new time to the schedule.')
                continue
            ri = self.done.retry_info(ln.id)
            if ri and n < ri[0]:
                if self.waited != f'r:{ln.id}':
                    self.log(f'WAITING: {ln.id} - next attempt after the temporary stop at {self.fmt(ri[0])}',
                             routine=True)
                self.waited = f'r:{ln.id}'
                return
            if self.last_end > 0 and n - self.last_end < s.gap_seconds:
                if self.waited != f'g:{ln.id}':
                    self.log(f'WAITING: {ln.id} - gap since the previous run not over (after '
                             f'{self.fmt(self.last_end + s.gap_seconds)})', routine=True)
                self.waited = f'g:{ln.id}'
                return
            start = n
            self.log(f'RUN: {ln.id} -> publish --keys {ln.keys} --apply')
            rc, output = self.runner(ln.keys)
            if not self.streams_output:
                for line in output:
                    self.log(f'  | {line}')
            why = why_line(output)
            if rc < 0 or (self.stop and rc > 128):
                self.log(f'INTERRUPTED: {ln.id} (signal, exit {rc}) - not recorded; it runs again after a restart '
                         f'(the state file prevents a double post)')
                return
            if rc == EXIT_LATER:
                nxt = self.now() + s.retry_seconds
                self.done.mark(ln.id, f'retry rc={rc}', f'at={int(start)} next={int(nxt)}')
                self.log(f'RETRY: {ln.id} - stopped before writing anything (exit {rc}): {why} | again after '
                         f'{self.fmt(nxt)}, at the latest {self.fmt(ln.epoch + s.max_late_seconds)}')
                if ri is None:
                    self.notify(f'Waiting: {ln.keys} - {why}. Nothing was written; retrying every '
                                f'{dur(s.retry_seconds)} until {self.fmt(ln.epoch + s.max_late_seconds)}. '
                                f'Details: schedule log')
                return
            self.last_end = self.now()
            if rc == 0:
                self.done.mark(ln.id, 'ok rc=0', f'start={int(start)} end={int(self.last_end)}')
                self.log(f'DONE: {ln.id} (ok)')
            else:
                self.done.mark(ln.id, f'fail rc={rc}', f'start={int(start)} end={int(self.last_end)}')
                self.log(f'FAILED: {ln.id} - exit {rc} (details above). Not retried (to retry, delete its lines from '
                         f'{self.s.done_file}); later lines continue.')
                self.notify(f'FAILED: {ln.keys} - exit {rc}: {why}. Not retried; details in the schedule log.')
            return

    # ------------------------------------------------------------------ instance lock and signals
    @contextlib.contextmanager
    def instance_lock(self) -> Iterator[bool]:
        import fcntl
        os.makedirs(os.path.dirname(os.path.abspath(self.lock_path)), exist_ok=True)
        fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        f = os.fdopen(fd, 'r+', encoding='utf-8')
        try:
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                yield False
                return
            f.seek(0)
            f.truncate()
            f.write(f'{os.getpid()}\n')
            f.flush()
            try:
                yield True
            finally:
                with contextlib.suppress(OSError):
                    f.seek(0)
                    f.truncate()
        finally:
            f.close()

    def running(self) -> tuple[bool, str]:
        """Is another scheduler holding the instance lock? ``(running, pid)``."""
        import fcntl
        if not os.path.exists(self.lock_path):
            return False, ''
        with open(self.lock_path, encoding='utf-8') as f:
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
            except OSError:
                return True, f.read().strip()
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        return False, ''

    def _on_signal(self, signum: int, _frame: object) -> None:
        self.signals += 1
        self.stop = True
        if self.signals > 1 and self.child and self.child.poll() is None:
            with contextlib.suppress(OSError):
                self.child.terminate()

    # ------------------------------------------------------------------ modes
    def run(self, once: bool = False) -> int:
        with self.instance_lock() as ok:
            if not ok:
                self.say(f'The scheduler is already running ({self.lock_path}).')
                return 1
            lines, errs = self.read_lines()
            if errs:
                self.say('\n'.join(errs))
                self.log(f'STOPPED - {self.s.file} has malformed lines (see `ig-publish schedule dry`)')
                return 1
            self.lines = lines
            self.once = once
            previous = {}
            if threading.current_thread() is threading.main_thread():  # signal handlers: main thread only
                previous = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}
                for sig in previous:
                    signal.signal(sig, self._on_signal)
            try:
                self.last_end = self.done.last_end()
                self.log(f"started (pid {os.getpid()}, {'once' if once else 'loop'}) | {len(self.lines)} line(s) | "
                         f"{self.s.file}", routine=True)
                complete = False
                while not self.stop:
                    self.reload()
                    if self.pending():
                        complete = False
                    self.tick()
                    with contextlib.suppress(OSError):
                        os.utime(self.lock_path)  # last tick time, shown by `schedule dry`
                    if self.stop:
                        break
                    if self.pending() == 0:
                        if once:
                            self.log('schedule complete: no pending lines', routine=True)
                            return 0
                        if not complete:  # a service keeps running: lines appended later are picked up
                            self.log('schedule complete: no pending lines; waiting for new lines')
                            complete = True
                    if once:
                        return 0
                    end = time.monotonic() + self.s.poll_seconds
                    while not self.stop and time.monotonic() < end:
                        time.sleep(min(0.25, max(0.0, end - time.monotonic())))
                self.log('stopped (signal)')
                return 0
            finally:
                for sig, handler in previous.items():
                    signal.signal(sig, handler)

    def dry(self) -> int:
        """What runs now, what is next, is a scheduler running; plus an offline check of every line."""
        from .publisher import Publisher  # local import keeps the scheduler light

        lines, errs = self.read_lines()
        if errs:
            self.say('\n'.join(errs))
            return 1
        s = self.s
        n = self.now()
        le = self.done.last_end()
        tzname = self.tz or 'local time'
        self.say(f'Scheduler (dry run) - now {self.fmt(n)} ({tzname}) | {s.file} | gap {s.gap_seconds:g} s | retry '
                 f'{s.retry_seconds:g} s | at most {dur(s.max_late_seconds)} late')
        running, pid = self.running()
        if running:
            last = os.path.getmtime(self.lock_path)
            self.say(f'Running: pid {pid or "?"} | last tick {self.fmt(last)}')
        else:
            self.say('Not running.')
        if os.path.exists(s.log_file):
            with open(s.log_file, encoding='utf-8') as f:
                tail = [x for x in f.read().splitlines() if x.strip()]
            if tail:
                self.say(f'Last log line: {tail[-1]}')
        if le > 0:
            self.say(f'Last run ended: {self.fmt(le)}')
        pub = Publisher(self.cfg, out=io.StringIO())
        first: Line | None = None
        nxt: Line | None = None
        for ln in lines:
            st = self.done.status(ln.id)
            ri = self.done.retry_info(ln.id)
            if st:
                what = f'done ({st})'
            elif n < ln.epoch:
                what = f'queued: in {dur(ln.epoch - n)}'
                nxt = nxt or ln
            elif n - ln.epoch > s.max_late_seconds:
                what = f'WILL BE SKIPPED: {dur(n - ln.epoch)} past its time (recorded as missed)'
            elif ri and n < ri[0]:
                what = f'temporarily stopped ({ri[1]} attempt(s)), next attempt after {self.fmt(ri[0])}'
                first = first or ln
            elif le > 0 and n - le < s.gap_seconds:
                what = f'due, waiting for the gap (after {self.fmt(le + s.gap_seconds)})'
                first = first or ln
            else:
                what = 'DUE: runs now' + (f' (after {ri[1]} temporary stop(s))' if ri else '')
                first = first or ln
            pv = pub.preview(ln.keys.split(','), ln.epoch)
            if pv.error:
                chk = f'✗ {pv.error}'
            elif pv.parked:
                chk = f'✗ held back: {"; ".join(pv.parked)}'
            elif pv.season:
                chk = '✗ outside the season window at that time'
            elif not pv.todo:
                chk = 'all already published'
            else:
                chk = 'check OK'
            if pv.unprepared:
                chk += f' | ✗ needs prep: {", ".join(pv.unprepared)}'
            self.say(f'  {self.fmt(ln.epoch)}  {ln.keys:50} {what} | {chk}')
        self.say(f'Now: publish --keys {first.keys} --apply' if first else 'Now: nothing')
        if nxt:
            self.say(f'Next: {self.fmt(nxt.epoch)} (in {dur(nxt.epoch - n)}) -> publish --keys {nxt.keys} --apply')
        return 0


def parse_now(value: str, tz: str | None) -> float:
    m = re.fullmatch(r'(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2})', value.strip())
    if not m:
        raise ConfigError(f'--now must look like "YYYY-MM-DD HH:MM" (got {value!r})')
    return to_epoch(m.group(1), m.group(2), tz)
