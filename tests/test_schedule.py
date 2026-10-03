"""Scheduler tests: in-process with a fake clock and a stub runner, plus one real loop in a subprocess.

No network and no real publisher: the runner is a stub (in-process) or a tiny script (subprocess).
"""
from __future__ import annotations

import datetime as dt
import fcntl
import io
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from helpers import clean_env, read, run_cli, write_json
from mock_graph import IG

from ig_publish.config import load_config
from ig_publish.schedule import DoneFile, Scheduler, parse_now

STUB = r'''
import os, sys, time
keys = sys.argv[sys.argv.index('--keys') + 1]
with open(os.environ['STUB_CALLS'], 'a') as f:
    f.write(keys + '\n')
time.sleep(float(os.environ.get('STUB_SLEEP', '0')))
print(f'stub published {keys} access_token=EAAexampleexampleexample1')
sys.exit(int(os.environ.get('STUB_RC', '0')))
'''


class SchedulerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.d = tempfile.mkdtemp(prefix='igp-sched-')
        with open(os.path.join(self.d, 'a.mp4'), 'wb') as f:
            f.write(b'\x00\x00\x00\x18ftypisom')  # existence is enough: the dry run only checks prep records
        keys = ('a1', 'b1', 'c1', 'd1', 'e1', 'x1', 'y2', 'z9', 'h1')
        items = [{'key': k, 'kind': 'story', 'group': 'G', 'src': 'a.mp4'} for k in keys]
        items[-1]['season'] = 'holiday'
        items.append({'key': 'p1', 'kind': 'story', 'group': 'G', 'src': 'a.mp4', 'needs_approval': 'owner decides'})
        write_json(os.path.join(self.d, 'manifest.json'), {'items': items})
        self.calls = os.path.join(self.d, 'calls.txt')
        with open(os.path.join(self.d, 'stub.py'), 'w') as f:
            f.write(STUB)
        self.cfg_path = write_json(os.path.join(self.d, 'ig-publish.json'), {
            'account': {'ig_user_id': IG},
            'seasons': {'holiday': {'start': '2030-12-01T00:00:00-08:00', 'end': '2031-01-01T00:00:00+14:00'}},
            'schedule': {'timezone': 'UTC', 'poll_seconds': 0.2,
                         'command': [sys.executable, os.path.join(self.d, 'stub.py')]}})
        self.sched_file = os.path.join(self.d, 'schedule.txt')
        self.done_path = os.path.join(self.d, 'state', 'schedule.done')
        self.log_path = os.path.join(self.d, 'state', 'schedule.log')

    def tearDown(self) -> None:
        for name in (self.log_path, self.done_path):
            self.assertNotIn('EAAexample', read(name), 'secret-looking text reached a scheduler file')
        shutil.rmtree(self.d, ignore_errors=True)

    def write_sched(self, text: str) -> None:
        with open(self.sched_file, 'w', encoding='utf-8') as f:
            f.write(text + '\n')

    def status(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for line_id, st, _detail in DoneFile(self.done_path).rows():
            out.setdefault(line_id.split(' ')[-1], []).append(st)
        return out

    # ------------------------------------------------------------------ in-process, fake clock
    def test_s1_ok_gap_retry_fail_missed_and_notifications(self) -> None:
        self.write_sched('2030-01-02 10:00 a1\n2030-01-02 10:10 b1   # comment\n2030-01-02 12:00 c1\n'
                         '2030-01-02 13:00 d1\n2030-01-03 09:00 e1')
        cfg = load_config(self.cfg_path)
        rc: dict[str, int] = {}
        applied: list[str] = []
        notes: list[str] = []

        def runner(keys: str) -> tuple[int, list[str]]:
            applied.append(keys)
            r = rc.get(keys, 0)
            out = [f'publishing {keys} access_token=EAAexampleexampleexample1']
            if r == 75:
                out += ['Stopped: preflight (nothing was written):',
                        '  ✗ 1 post(s) in the last 24 h are NOT in the state file']
            if r == 1:
                out += [f'✗ {keys}: media_publish was refused']
            return r, out

        def tick(now: str) -> str:
            out = io.StringIO()
            sch = Scheduler(cfg, fake_now=parse_now(now, 'UTC'), runner=runner, notifier=notes.append, out=out)
            self.assertEqual(sch.run(once=True), 0)
            return out.getvalue()

        self.assertEqual(tick('2030-01-02 09:59'), '', 'an idle `once` tick prints nothing (cron mail stays quiet)')
        self.assertEqual(applied, [])
        out = tick('2030-01-02 10:00')
        self.assertEqual((applied, self.status()), (['a1'], {'a1': ['ok rc=0']}))
        self.assertIn('RUN: 2030-01-02 10:00 a1', out)  # events are printed (docker logs, journald, cron mail)
        self.assertIn('DONE: 2030-01-02 10:00 a1 (ok)', out)
        self.assertNotIn('started', out)
        tick('2030-01-02 10:10')  # the gap (31 min) is not over
        self.assertEqual(applied, ['a1'])
        self.assertIn('WAITING: 2030-01-02 10:10 b1', read(self.log_path))
        rc['b1'] = 75
        tick('2030-01-02 10:31')  # temporary stop: not final, waits, one notification
        self.assertEqual(self.status()['b1'], ['retry rc=75'])
        self.assertEqual(len(notes), 1)
        self.assertIn('Waiting: b1', notes[0])
        self.assertIn('NOT in the state file', notes[0])
        buf = io.StringIO()
        Scheduler(cfg, fake_now=parse_now('2030-01-02 10:40', 'UTC'), out=buf).dry()
        self.assertIn('temporarily stopped (1 attempt(s)), next attempt after 2030-01-02 10:46', buf.getvalue())
        tick('2030-01-02 10:40')  # not time to retry yet
        self.assertEqual(applied, ['a1', 'b1'])
        tick('2030-01-02 10:47')  # retried, temporary again; no second notification
        self.assertEqual((applied, len(notes)), (['a1', 'b1', 'b1'], 1))
        rc['b1'] = 0
        tick('2030-01-02 11:03')
        self.assertEqual(self.status()['b1'], ['retry rc=75', 'retry rc=75', 'ok rc=0'])
        rc['c1'] = 1
        tick('2030-01-02 12:00')  # an error after a write: fail, notification, never retried
        self.assertEqual(self.status()['c1'], ['fail rc=1'])
        self.assertIn('FAILED: c1', notes[-1])
        self.assertIn('refused', notes[-1])
        tick('2030-01-02 13:00')
        self.assertEqual(applied, ['a1', 'b1', 'b1', 'b1', 'c1', 'd1'])
        tick('2030-01-03 16:00')  # 7 h late: missed + notification; the schedule is complete
        self.assertEqual(self.status()['e1'], ['missed'])
        self.assertIn('MISSED', notes[-1])
        self.assertEqual(len(notes), 3)
        log = read(self.log_path)
        self.assertIn('schedule complete', log)
        self.assertIn('access_token=***', log)
        self.assertEqual(applied, ['a1', 'b1', 'b1', 'b1', 'c1', 'd1'])

    def test_s2_dry_checks_every_line_offline(self) -> None:
        self.write_sched('2030-11-30 12:00 h1\n2030-12-05 12:00 h1\n2030-12-06 12:00 p1\n2030-12-07 12:00 zz\n'
                         '2099-01-01 00:00 a1')
        buf = io.StringIO()
        Scheduler(load_config(self.cfg_path), fake_now=parse_now('2030-12-06 13:00', 'UTC'), out=buf).dry()
        out = buf.getvalue().splitlines()
        row = {k: next(x for x in out if f' {k} ' in x and x.startswith('  ')) for k in ('p1', 'zz', 'a1')}
        h1 = [x for x in out if ' h1 ' in x]
        self.assertIn('outside the season window at that time', h1[0])   # line time before the window opens
        self.assertNotIn('outside the season window', h1[1])             # line time inside the window
        self.assertIn('needs prep', h1[1])
        self.assertIn('held back', row['p1'])
        self.assertIn('not in the manifest', row['zz'])
        self.assertIn('queued', row['a1'])
        self.assertIn('Not running.', buf.getvalue())
        self.assertIn('Now: publish --keys p1 --apply', buf.getvalue())
        self.assertIn('Next: 2030-12-07 12:00 (in 23 h 00 min)', buf.getvalue())
        self.assertIn('WILL BE SKIPPED: 6 d 1 h past its time', h1[0])

    # ------------------------------------------------------------------ CLI and process behaviour
    def test_s3_cli_guards_bad_lines_second_instance_and_dry_status(self) -> None:
        env = clean_env({'STUB_CALLS': self.calls})
        self.write_sched('2030-01-02 1000 a1')
        p = run_cli(self.cfg_path, 'schedule', 'once', '--apply', env=env)
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn('malformed line', p.stdout)
        self.write_sched('2030-01-02 10:00 a1')
        p = run_cli(self.cfg_path, 'schedule', 'once', '--apply', '--now', '2030-01-02 10:00', env=env)
        self.assertEqual(p.returncode, 1)
        self.assertIn('--now only works with `schedule dry`', p.stderr)
        p = run_cli(self.cfg_path, 'schedule', 'dry', '--apply', env=env)
        self.assertEqual(p.returncode, 1)
        self.assertIn('never publishes', p.stderr)
        lock = os.path.join(self.d, 'state', 'schedule.lock')
        os.makedirs(os.path.dirname(lock), exist_ok=True)
        with open(lock, 'w') as f:
            f.write(f'{os.getpid()}\n')
            f.flush()
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            p = run_cli(self.cfg_path, 'schedule', 'once', '--apply', env=env)
            self.assertEqual(p.returncode, 1)
            self.assertIn('already running', p.stdout)
            p = run_cli(self.cfg_path, 'schedule', 'dry', '--now', '2030-01-02 09:00', env=env)
            self.assertIn(f'Running: pid {os.getpid()}', p.stdout)
        p = run_cli(self.cfg_path, 'schedule', 'dry', '--now', '2030-01-02 09:00', env=env)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn('Not running.', p.stdout)
        self.assertIn('Next: 2030-01-02 10:00', p.stdout)
        self.assertFalse(os.path.exists(self.calls))

    def test_s3b_run_and_once_publish_only_with_apply(self) -> None:
        now = dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%d %H:%M')
        self.write_sched(f'{now} a1')  # due right now
        env = clean_env({'STUB_CALLS': self.calls})
        for mode in ('run', 'once'):
            p = run_cli(self.cfg_path, 'schedule', mode, env=env)
            self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
            self.assertIn(f'ig-publish schedule {mode} --apply', p.stderr)
        self.assertFalse(os.path.exists(self.calls), 'a publish started without --apply')
        self.assertFalse(os.path.exists(self.done_path))
        p = run_cli(self.cfg_path, 'schedule', 'once', '--apply', env=env)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(read(self.calls).split(), ['a1'])

    def test_s4b_loop_keeps_running_after_the_last_line_and_picks_up_new_ones(self) -> None:
        self.write_sched('2030-01-02 10:00 a1')
        cfg = load_config(write_json(os.path.join(self.d, 'loop2.json'), {
            'account': {'ig_user_id': IG}, 'schedule': {'timezone': 'UTC', 'poll_seconds': 0.05, 'gap_seconds': 0}}))
        ran: list[str] = []
        sch = Scheduler(cfg, fake_now=parse_now('2030-01-02 10:05', 'UTC'), out=io.StringIO(),
                        runner=lambda keys: (ran.append(keys), (0, []))[1])
        t = threading.Thread(target=sch.run, daemon=True)  # loop mode; no signal handlers off the main thread

        def wait_for(cond, what: str) -> None:
            t0 = time.time()
            while not cond():
                if time.time() - t0 > 20 or not t.is_alive():
                    sch.stop = True
                    self.fail(f'{what} did not happen; log:\n{read(self.log_path)}')
                time.sleep(0.05)
        t.start()
        try:
            wait_for(lambda: 'waiting for new lines' in read(self.log_path), 'completion')
            self.assertTrue(t.is_alive(), 'the loop must not exit when every line is done')
            with open(self.sched_file, 'a', encoding='utf-8') as f:
                f.write('2030-01-02 10:05 b1\n')
            wait_for(lambda: ran == ['a1', 'b1'], 'the appended line')
            wait_for(lambda: read(self.log_path).count('waiting for new lines') == 2, 'the second completion')
            time.sleep(0.4)  # several more ticks: the completion line is written once per completion, not per tick
            self.assertEqual(read(self.log_path).count('waiting for new lines'), 2)
        finally:
            sch.stop = True
            t.join(timeout=10)
        self.assertFalse(t.is_alive())

    def test_s5_notifier_gets_no_token_and_its_failure_is_logged(self) -> None:
        dump = os.path.join(self.d, 'notifier-env.txt')
        with open(os.path.join(self.d, 'notify.py'), 'w') as f:
            f.write('import os, sys\n'
                    f'with open({dump!r}, "w") as out:\n'
                    '    out.write("\\n".join(f"{k}={v}" for k, v in os.environ.items()))\n'
                    'print("webhook said no", file=sys.stderr)\n'
                    'sys.exit(3)\n')
        cfg = write_json(os.path.join(self.d, 'notify.json'), {
            'account': {'ig_user_id': IG}, 'token': {'env_var': 'MY_IG_TOKEN'},
            'schedule': {'notify_command': [sys.executable, os.path.join(self.d, 'notify.py')]}})
        secret = {'MY_IG_TOKEN': 'value-of-the-token-1', 'IG_ACCESS_TOKEN': 'value-of-the-token-2',
                  'IG_PUBLISH_TEST_TOKEN': 'value-of-the-token-3', 'NOTIFY_WEBHOOK_FILE': '/kept/for/the/notifier'}
        with mock.patch.dict(os.environ, secret):
            Scheduler(load_config(cfg), out=io.StringIO()).notify('hello')
        env = read(dump)
        self.assertNotIn('value-of-the-token', env)
        self.assertIn('NOTIFY_WEBHOOK_FILE=/kept/for/the/notifier', env)
        self.assertIn('PATH=', env)
        log = read(self.log_path)
        self.assertIn('NOTIFY: hello', log)
        self.assertIn('WARNING: notify_command exited 3: webhook said no', log)

    def test_s4_loop_rereads_the_schedule_and_sigterm_waits_for_the_running_publish(self) -> None:
        now = dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%d %H:%M')
        self.write_sched(f'{now} x1\n2099-01-01 00:00 z9')
        cfg = write_json(os.path.join(self.d, 'loop.json'), {
            'account': {'ig_user_id': IG},
            'schedule': {'timezone': 'UTC', 'poll_seconds': 0.2, 'gap_seconds': 0,
                         'command': [sys.executable, os.path.join(self.d, 'stub.py')]}})
        env = clean_env({'STUB_CALLS': self.calls, 'STUB_SLEEP': '2'})
        proc = subprocess.Popen([sys.executable, '-m', 'ig_publish', '--config', cfg, 'schedule', 'run', '--apply'],
                                env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                stdin=subprocess.DEVNULL)

        def wait_for(cond, what: str) -> None:
            t0 = time.time()
            while not cond():
                if time.time() - t0 > 30 or proc.poll() is not None:
                    proc.kill()
                    self.fail(f'{what} did not happen; log:\n{read(self.log_path)}')
                time.sleep(0.1)
        try:
            wait_for(lambda: 'x1' in self.status(), 'x1')
            with open(self.sched_file, 'a', encoding='utf-8') as f:  # a line added while running is picked up
                f.write(f'{now} y2\n')
            wait_for(lambda: 'y2' in read(self.calls), 'y2 start')
            proc.send_signal(signal.SIGTERM)  # while y2 runs: it finishes and is recorded, then the loop exits
            stdout, _stderr = proc.communicate(timeout=30)
        finally:
            if proc.poll() is None:
                proc.kill()
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(self.status(), {'x1': ['ok rc=0'], 'y2': ['ok rc=0']})
        log = read(self.log_path)
        self.assertIn('stopped (signal)', log)
        self.assertIn('access_token=***', log)
        self.assertEqual(read(self.calls).split(), ['x1', 'y2'])
        for needle in ('started (pid', f'RUN: {now} x1', 'DONE:', 'stub published x1 access_token=***',
                       'stopped (signal)'):
            self.assertIn(needle, stdout)  # the log is on stdout too (docker logs, journald)
        self.assertNotIn('EAAexample', stdout)
        for name in (self.log_path, self.log_path + '.last', self.done_path):
            self.assertEqual(stat.S_IMODE(os.stat(name).st_mode), 0o600, name)
        with open(os.path.join(self.d, 'state', 'schedule.lock')) as f:  # the instance lock was released
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


if __name__ == '__main__':
    unittest.main(verbosity=2)
