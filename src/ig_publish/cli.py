"""Command-line interface: ``ig-publish [--config PATH] COMMAND ...``."""
from __future__ import annotations

import argparse
import http.client
import math
import os
import sys
import traceback
from importlib import resources

from . import __version__
from .config import CONFIG_ENV, DEFAULT_NAMES, Config, PrepSettings, load_config
from .errors import (
    EXIT_ERROR,
    EXIT_LATER,
    EXIT_OK,
    EXIT_USAGE,
    ConfigError,
    GraphError,
    IgPublishError,
    Stop,
    TemporaryStop,
    TokenError,
    UsageLimit,
)
from .hints import explain
from .publisher import Options, Publisher, verify_report
from .redact import redact
from .timeutil import parse_when
from .tokens import scrubbed_env

DEBUG_ENV = 'IG_PUBLISH_DEBUG'
EPILOG = """\
exit codes:
  0   done
  1   error, or a stop after something was written (do not retry blindly)
  2   command-line usage error
  75  stopped BEFORE anything was written, for a reason that passes with time (lock busy, hold,
      recent posts the state file does not know, API quota/state mismatch, burst guard, reel spacing,
      quota, usage limit, read error, missing token file, season not open yet):
      running the same command later is safe; the scheduler retries it

nothing is published, deleted or imported without --apply (the scheduler too: schedule run --apply).
New here? docs/setup.md walks through the Meta setup; `ig-publish doctor` checks it and prints every fix.
Docs: https://github.com/deegitech/ig-publish
"""

TEMPLATES = (('ig-publish.toml', 'ig-publish.toml'), ('manifest.json', 'manifest.json'),
             ('schedule.txt', 'schedule.txt'))


def _keys(value: str) -> list[str]:
    keys = [k.strip() for k in value.split(',') if k.strip()]
    if not keys:
        raise argparse.ArgumentTypeError(f'invalid key list {value!r}')
    return keys


def _when(value: str) -> float:
    try:
        return parse_when(value)
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e)) from None


def _seconds(value: str) -> float:
    try:
        x = float(value)
    except ValueError:
        x = math.nan
    if not math.isfinite(x) or x < 0:
        raise argparse.ArgumentTypeError(f'must be a number of seconds >= 0 (got {value!r})')
    return x


def _count(value: str) -> int:
    try:
        n = int(value)
    except ValueError:
        n = 0
    if n < 1:
        raise argparse.ArgumentTypeError(f'must be a whole number >= 1 (got {value!r})')
    return n


def _selection_args(p: argparse.ArgumentParser, *, publishing: bool) -> None:
    g = p.add_argument_group('selection')
    g.add_argument('--only', metavar='GROUP[,GROUP]',
                   help='only these manifest groups (STORIES and REELS select a kind)')
    g.add_argument('--keys', metavar='KEY[,KEY]', type=_keys, action='extend', default=[],
                   help='exactly these keys, in manifest order (all of them or none)')
    if not publishing:
        return
    g.add_argument('--canary', action='store_true', help='only the first item of the selection')
    g.add_argument('--limit', type=_count, metavar='N', help='at most N items')
    g.add_argument('--repost', type=_keys, action='extend', default=[], metavar='KEY[,KEY]',
                   help='publish these keys again although they are live or deleted (on purpose)')
    g.add_argument('--approve', type=_keys, action='extend', default=[], metavar='KEY[,KEY]',
                   help='release items marked "needs_approval"')
    g.add_argument('--story-gap', type=_seconds, metavar='SECONDS', help='override [spacing] story_seconds')
    g.add_argument('--reel-gap', type=_seconds, metavar='SECONDS', help='override [spacing] reel_seconds')
    g.add_argument('--offline', action='store_true',
                   help='no network and no token (skip quota and account checks); dry runs only')
    g.add_argument('--at', type=_when, metavar='TIME',
                   help='evaluate season windows at TIME (ISO 8601 with offset, or epoch seconds); dry runs only')


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog='ig-publish', formatter_class=argparse.RawDescriptionHelpFormatter,
                                description='Publish Instagram Stories and Reels through the official Graph API: '
                                            'resumable uploads, strict order, holds on spam/limit codes and no '
                                            'double posts.', epilog=EPILOG)
    p.add_argument('-c', '--config', metavar='PATH',
                   help='configuration file (default: $IG_PUBLISH_CONFIG, ./ig-publish.toml, ./ig-publish.json)')
    p.add_argument('--version', action='version', version=f'ig-publish {__version__}')
    sub = p.add_subparsers(dest='cmd', metavar='COMMAND')
    sub.required = True

    s = sub.add_parser('init', help='write an example config, manifest and schedule (never overwrites)')
    s.add_argument('directory', nargs='?', default='.')

    sub.add_parser('check', help='read-only: token owner, permissions, Page tasks, account, quota')

    s = sub.add_parser('doctor', help='read-only: check the whole setup step by step and print the fix for every '
                                      'problem (configuration, manifest, FFmpeg, state, token, permissions, Page, '
                                      'account, quota)',
                       formatter_class=argparse.RawDescriptionHelpFormatter,
                       description='Read-only checks in setup order; every failed check (✗) is followed by its fix.\n'
                                   'Nothing is written and the token is never printed. Exit 0 when nothing failed,\n'
                                   '1 otherwise.\n\n'
                                   'Setup guide:     https://github.com/deegitech/ig-publish/blob/main/docs/setup.md\n'
                                   'Every error:     https://github.com/deegitech/ig-publish/blob/main/docs/'
                                   'troubleshooting.md')
    s.add_argument('--offline', action='store_true', help='local checks only: no token and no network')

    s = sub.add_parser('prep', help='re-encode the manifest videos to the Instagram spec with ffmpeg and verify them')
    _selection_args(s, publishing=False)

    s = sub.add_parser('verify', help='check one video file against the prep profile (no network; works without a '
                                      'configuration file)')
    s.add_argument('file')
    s.add_argument('--kind', choices=('story', 'reel'), required=True)

    s = sub.add_parser('plan', help='show the plan, the live-account preflight and what --apply would do')
    _selection_args(s, publishing=True)

    s = sub.add_parser('publish', help='publish (a dry run unless --apply)')
    _selection_args(s, publishing=True)
    s.add_argument('--ack-unknown-posts', action='store_true',
                   help='continue although the account has recent posts the state file does not know')
    s.add_argument('--ignore-hold', action='store_true',
                   help='override an active hold, the burst guard and reel spacing')
    s.add_argument('--apply', action='store_true', help='actually publish')

    s = sub.add_parser('status', help='state table, hold and quota')
    s.add_argument('--offline', action='store_true', help='no network and no token')

    s = sub.add_parser('delete', help='delete a published post (a dry run unless --apply)')
    s.add_argument('key')
    s.add_argument('--confirm-media', metavar='MEDIA_ID', help='confirm a candidate media ID')
    s.add_argument('--apply', action='store_true')

    s = sub.add_parser('import', help='record posts published outside this tool (offline; a dry run unless --apply)')
    s.add_argument('record', help='JSON file {"name": {"key", "media_id", "at", "permalink"?}}')
    s.add_argument('--apply', action='store_true')

    s = sub.add_parser('ack', help='acknowledge recent posts the state file does not know, e.g. made by hand (a dry '
                                   'run unless --apply; writes the state file only, never posts)')
    s.add_argument('--apply', action='store_true', help='record them as acknowledged in the state file')

    s = sub.add_parser('schedule', help='run the scheduler: run | once (publish due lines; need --apply) | dry')
    s.add_argument('mode', choices=('run', 'once', 'dry'))
    s.add_argument('--apply', action='store_true', help='let run / once publish (each due line runs publish --apply)')
    s.add_argument('--now', metavar='"YYYY-MM-DD HH:MM"', help='pretend it is this time (dry only)')
    return p


def _options(a: argparse.Namespace) -> Options:
    return Options(apply=getattr(a, 'apply', False), canary=getattr(a, 'canary', False),
                   offline=getattr(a, 'offline', False), ack_unknown_posts=getattr(a, 'ack_unknown_posts', False),
                   ignore_hold=getattr(a, 'ignore_hold', False), only=getattr(a, 'only', None),
                   keys=list(dict.fromkeys(getattr(a, 'keys', []) or [])), limit=getattr(a, 'limit', None),
                   story_gap=getattr(a, 'story_gap', None), reel_gap=getattr(a, 'reel_gap', None),
                   repost=set(getattr(a, 'repost', []) or []), approve=set(getattr(a, 'approve', []) or []),
                   confirm_media=(getattr(a, 'confirm_media', None) or '').strip() or None,
                   at=getattr(a, 'at', None))


def _init(directory: str) -> int:
    os.makedirs(directory, exist_ok=True)
    pkg = resources.files('ig_publish') / 'templates'
    for src, dst in TEMPLATES:
        path = os.path.join(directory, dst)
        if os.path.exists(path):
            print(f'  = {path} exists, left as is')
            continue
        with open(path, 'x', encoding='utf-8') as f:
            f.write((pkg / src).read_text(encoding='utf-8'))
        print(f'  ✎ {path}')
    print('\nNext (every click: https://github.com/deegitech/ig-publish/blob/main/docs/setup.md):\n'
          '  1. edit ig-publish.toml ([account] ig_user_id, [token]) and manifest.json\n'
          '  2. store the access token (a [token] source; the default reads IG_ACCESS_TOKEN)\n'
          '  3. ig-publish doctor      (checks the setup and prints the fix for every problem)\n'
          '  4. ig-publish prep && ig-publish plan\n'
          '  5. ig-publish publish --canary --apply')
    return EXIT_OK


def _err(msg: str) -> None:
    print(redact(msg), file=sys.stderr)


def _say(msg: str) -> None:
    print(redact(msg), flush=True)


def _verify(args: argparse.Namespace) -> int:
    """``verify`` works without a configuration file: then the default prep profile is used."""
    if args.config or os.environ.get(CONFIG_ENV) or any(os.path.isfile(n) for n in DEFAULT_NAMES):
        cfg = load_config(args.config)
        prep, env_var, where = cfg.prep, cfg.token.env_var, f'[prep] of {cfg.path}'
    else:
        prep, env_var, where = PrepSettings(), None, 'the defaults; no configuration file found'
    return verify_report(args.file, args.kind, prep, _say, profile=where, env=scrubbed_env(env_var))


def _later(msg: str) -> int:
    _err(msg)
    _err(f'(exit {EXIT_LATER}: nothing was written; the same command can be retried later - the scheduler does)')
    return EXIT_LATER


def run(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == 'init':
        return _init(args.directory)
    if args.cmd == 'schedule' and args.mode != 'dry' and not args.apply:
        _err(f'✗ `schedule {args.mode}` publishes the due lines of the schedule (each one as `publish --keys KEYS '
             f'--apply`). Add --apply to let it: ig-publish schedule {args.mode} --apply. To preview: ig-publish '
             f'schedule dry')
        return EXIT_USAGE
    pub: Publisher | None = None
    try:
        if args.cmd == 'verify':
            return _verify(args)
        if args.cmd == 'doctor':
            from .doctor import run_doctor
            return run_doctor(args.config, offline=args.offline)
        cfg: Config = load_config(args.config)
        if args.cmd == 'schedule':
            from .schedule import Scheduler, parse_now
            if args.now and args.mode != 'dry':
                raise ConfigError('--now only works with `schedule dry`')
            if args.apply and args.mode == 'dry':
                raise ConfigError('`schedule dry` never publishes; drop --apply')
            fake = parse_now(args.now, cfg.schedule.timezone) if args.now else None
            sch = Scheduler(cfg, fake_now=fake)
            return sch.dry() if args.mode == 'dry' else sch.run(once=args.mode == 'once')
        opts = _options(args)
        if args.cmd in ('delete', 'import'):
            opts.pos = [args.key if args.cmd == 'delete' else args.record]
        pub = Publisher(cfg, opts)
        if args.cmd == 'check':
            return pub.check()
        {'prep': pub.prep, 'plan': pub.plan, 'publish': pub.publish, 'status': pub.status, 'delete': pub.delete,
         'import': pub.import_record, 'ack': pub.ack}[args.cmd]()
        return EXIT_OK
    except TemporaryStop as e:
        return _later(str(e))
    except TokenError as e:
        msg = f'✗ token: {e}' + (f'\n  fix: {e.fix}' if e.fix else '')
        if e.temporary and pub is not None and pub.pre_write:
            return _later(msg)
        _err(msg)
        return EXIT_ERROR
    except (GraphError, UsageLimit) as e:
        if pub is not None and pub.pre_write:
            return _later(f'✗ Meta API (read, before any write): {explain(e)}')
        _err(f'✗ Meta API: {explain(e)}')
        return EXIT_ERROR
    except (TimeoutError, ConnectionError, http.client.HTTPException) as e:
        if pub is not None and pub.pre_write:
            return _later(f'✗ network (read, before any write): {type(e).__name__}: {e}')
        _err(f'✗ network: {type(e).__name__}: {e}')
        return EXIT_ERROR
    except (ConfigError, Stop, IgPublishError) as e:
        _err(str(e))
        return EXIT_ERROR
    except KeyboardInterrupt:
        _err('interrupted')
        return 130
    except Exception as e:  # noqa: BLE001 - a bug must still end redacted, with exit 1 (never "retry later")
        _err(f'✗ unexpected error ({type(e).__name__}): {e}')
        if os.environ.get(DEBUG_ENV):
            _err(traceback.format_exc())
        else:
            _err(f'  This is probably a bug in ig-publish. Run the command again with {DEBUG_ENV}=1 to see the '
                 f'(redacted) traceback, and please report it.')
        return EXIT_ERROR


def main(argv: list[str] | None = None) -> int:
    return run(argv)


if __name__ == '__main__':  # pragma: no cover
    sys.exit(main())
