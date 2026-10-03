"""Configuration file: ``ig-publish.toml`` (TOML) or ``ig-publish.json`` (JSON).

The loader is strict on purpose: an unknown key is an error, because in a publishing tool a typo
(``needs_aproval``, ``reel_secnds``) must never be silently ignored. Relative paths are resolved against the
directory of the configuration file. See ``examples/ig-publish.toml`` for an annotated example and the README
for the full reference.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import ConfigError
from .timeutil import parse_aware
from .tokens import SOURCES, TokenConfig

CONFIG_ENV = 'IG_PUBLISH_CONFIG'
DEFAULT_NAMES = ('ig-publish.toml', 'ig-publish.json')
DEFAULT_API_VERSION = 'v26.0'

# Limits from Meta's IG Media reference (caption: 2,200 characters, 30 hashtags, 20 @-mentions).
CAPTION_MAX = 2200
HASHTAG_MAX = 30
MENTION_MAX = 20


@dataclass(frozen=True)
class Spacing:
    story_seconds: float = 10.0
    reel_seconds: float = 1800.0


@dataclass(frozen=True)
class Safety:
    burst_window_seconds: float = 3 * 3600.0
    burst_max_posts: int = 10
    rate_hold_seconds: float = 3600.0
    container_max_age_seconds: float = 23 * 3600.0
    usage_stop_percent: int = 85


@dataclass(frozen=True)
class Timing:
    poll_seconds: float = 15.0
    poll_timeout_seconds: float = 600.0
    guard_wait_seconds: float = 10.0
    republish_wait_seconds: float = 45.0
    rate_retry_seconds: float = 60.0
    read_retry_backoff_seconds: float = 5.0
    http_timeout_seconds: float = 60.0
    upload_timeout_seconds: float = 900.0


@dataclass(frozen=True)
class CaptionRules:
    required: bool = True
    max_length: int = CAPTION_MAX
    min_hashtags: int = 0
    max_hashtags: int = HASHTAG_MAX
    required_text: tuple[str, ...] = ()
    banned_words: tuple[str, ...] = ()
    licensed_music: tuple[str, ...] = ()
    forbid_curly_quotes: bool = False


@dataclass(frozen=True)
class SourceRules:
    deny: tuple[re.Pattern[str], ...] = ()


@dataclass(frozen=True)
class Season:
    name: str
    start: float
    end: float
    start_text: str
    end_text: str
    caption_words: tuple[str, ...] = ()


@dataclass(frozen=True)
class PrepSettings:
    width: int = 1080
    height: int = 1920
    fps: int = 30
    fit: str = 'pad'
    crf: int = 20
    preset: str = 'medium'
    max_video_kbps: int = 4500
    audio_kbps: int = 128
    threads: int = 2
    nice: int = 10
    still_seconds: float = 5.0


@dataclass(frozen=True)
class ScheduleSettings:
    file: Path
    done_file: Path
    log_file: Path
    timezone: str | None = None
    gap_seconds: float = 1860.0
    max_late_seconds: float = 6 * 3600.0
    poll_seconds: float = 60.0
    retry_seconds: float = 900.0
    notify_command: tuple[str, ...] = ()
    command: tuple[str, ...] = ()


@dataclass(frozen=True)
class Config:
    path: Path | None
    base_dir: Path
    ig_user_id: str
    username: str | None
    page_id: str | None
    api_version: str
    manifest: Path
    source_root: Path
    media_dir: Path
    state: Path
    journal: Path
    token: TokenConfig
    schedule: ScheduleSettings
    spacing: Spacing = field(default_factory=Spacing)
    safety: Safety = field(default_factory=Safety)
    timing: Timing = field(default_factory=Timing)
    captions: CaptionRules = field(default_factory=CaptionRules)
    sources: SourceRules = field(default_factory=SourceRules)
    seasons: dict[str, Season] = field(default_factory=dict)
    prep: PrepSettings = field(default_factory=PrepSettings)

    @property
    def lock_path(self) -> Path:
        return self.state.with_name(self.state.stem + '.lock')

    @property
    def prep_index(self) -> Path:
        return self.media_dir / 'prep.json'


# ---------------------------------------------------------------------------------------------- loading
def find_config(explicit: str | None = None) -> Path:
    """``--config`` wins, then ``$IG_PUBLISH_CONFIG``, then ``./ig-publish.toml`` / ``./ig-publish.json``."""
    for cand, why in ((explicit, '--config'), (os.environ.get(CONFIG_ENV), CONFIG_ENV)):
        if cand:
            p = Path(cand).expanduser()
            if not p.is_file():
                raise ConfigError(f'Configuration file not found: {p} (from {why})')
            return p.resolve()
    for name in DEFAULT_NAMES:
        p = Path(name)
        if p.is_file():
            return p.resolve()
    raise ConfigError('No configuration file. Pass --config PATH, set IG_PUBLISH_CONFIG, or run `ig-publish init` '
                      'to create ig-publish.toml in this directory.')


def read_config_file(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding='utf-8')
    except OSError as e:
        raise ConfigError(f'Cannot read {path}: {e.strerror}') from None
    suffix = path.suffix.lower()
    if suffix == '.toml':
        try:
            import tomllib  # type: ignore[import-not-found]
        except ModuleNotFoundError:  # Python 3.10
            try:
                import tomli as tomllib  # type: ignore[no-redef]
            except ModuleNotFoundError:
                raise ConfigError('Reading a TOML configuration on Python 3.10 needs the `tomli` package, which is '
                                  'normally installed with ig-publish (pip install tomli). A JSON configuration works '
                                  'everywhere.') from None
        try:
            return tomllib.loads(text)
        except tomllib.TOMLDecodeError as e:
            raise ConfigError(f'{path}: invalid TOML: {e}') from None
    if suffix == '.json':
        try:
            data = json.loads(text)
        except ValueError as e:
            raise ConfigError(f'{path}: invalid JSON: {e}') from None
        if not isinstance(data, dict):
            raise ConfigError(f'{path}: the top level must be an object')
        return data
    raise ConfigError(f'{path}: the configuration file must end in .toml or .json')


def load_config(path: str | Path | None = None) -> Config:
    p = find_config(str(path) if path else None)
    return parse_config(read_config_file(p), base_dir=p.parent, path=p)


class _Section:
    """Typed access to one table; remembers which keys were read so unknown keys can be reported."""

    def __init__(self, data: Any, name: str) -> None:
        if data is None:
            data = {}
        if not isinstance(data, dict):
            raise ConfigError(f'[{name}] must be a table (an object in JSON)')
        self.data, self.name, self.used = data, name, set()

    def has(self, key: str) -> bool:
        return self.data.get(key) is not None

    def raw(self, key: str) -> Any:
        self.used.add(key)
        return self.data.get(key)

    def text(self, key: str, default: str | None = None, *, pattern: str | None = None, hint: str = '') -> str | None:
        v = self.raw(key)
        if v is None or v == '':
            return default
        if isinstance(v, (int,)) and not isinstance(v, bool) and pattern and re.fullmatch(pattern, str(v)):
            v = str(v)  # tolerate numeric IDs written without quotes in JSON
        if not isinstance(v, str):
            raise ConfigError(f'[{self.name}] {key} must be a string')
        if pattern and not re.fullmatch(pattern, v):
            raise ConfigError(f'[{self.name}] {key} = {v!r} is not valid{": " + hint if hint else ""}')
        return v

    def number(self, key: str, default: float, *, lo: float | None = None, hi: float | None = None,
            integer: bool = False) -> Any:
        v = self.raw(key)
        if v is None:
            return default
        if isinstance(v, bool) or not isinstance(v, (int, float)) or (integer and not isinstance(v, int)):
            raise ConfigError(f'[{self.name}] {key} must be {"an integer" if integer else "a number"}')
        if (lo is not None and v < lo) or (hi is not None and v > hi):
            rng = f'{lo if lo is not None else "-inf"}..{hi if hi is not None else "inf"}'
            raise ConfigError(f'[{self.name}] {key} = {v} is out of range ({rng})')
        return v

    def flag(self, key: str, default: bool) -> bool:
        v = self.raw(key)
        if v is None:
            return default
        if not isinstance(v, bool):
            raise ConfigError(f'[{self.name}] {key} must be true or false')
        return v

    def strings(self, key: str) -> tuple[str, ...]:
        v = self.raw(key)
        if v is None:
            return ()
        if not isinstance(v, list) or not all(isinstance(x, str) and x.strip() for x in v):
            raise ConfigError(f'[{self.name}] {key} must be a list of non-empty strings')
        return tuple(v)

    def filepath(self, key: str, default: str, base: Path) -> Path:
        v = self.text(key, default)
        p = Path(os.path.expanduser(v or default))
        return p if p.is_absolute() else (base / p)

    def finish(self) -> None:
        extra = sorted(k for k in self.data if k not in self.used and not str(k).startswith(('_', '$')))
        if extra:
            raise ConfigError(f'Unknown key(s) in [{self.name}]: {", ".join(extra)}')


def _season_time(s: _Section, key: str) -> str | None:
    """A season bound: an ISO 8601 string, or a native TOML offset date-time (``2030-12-01T00:00:00-08:00``)."""
    v = s.data.get(key)
    if isinstance(v, dt.datetime):
        s.used.add(key)
        if v.tzinfo is None:
            raise ConfigError(f'[{s.name}] {key} = {v.isoformat()} has no UTC offset (write e.g. '
                              f'2030-12-01T00:00:00-08:00 or ...Z)')
        return v.isoformat()
    if isinstance(v, (dt.date, dt.time)):
        raise ConfigError(f'[{s.name}] {key} must be a date and time with a UTC offset, e.g. 2030-12-01T00:00:00-08:00')
    return s.text(key)


KNOWN_SECTIONS = ('account', 'api', 'paths', 'token', 'spacing', 'safety', 'timing', 'captions', 'sources',
                  'seasons', 'prep', 'schedule')


def parse_config(data: dict[str, Any], *, base_dir: Path, path: Path | None = None) -> Config:
    unknown = sorted(k for k in data if k not in KNOWN_SECTIONS and not str(k).startswith(('_', '$')))
    if unknown:
        raise ConfigError(f'Unknown section(s): {", ".join(unknown)} (known: {", ".join(KNOWN_SECTIONS)})')
    base = base_dir.resolve()

    acc = _Section(data.get('account'), 'account')
    ig_user_id = acc.text('ig_user_id', pattern=r'\d{5,25}', hint='the numeric Instagram professional account ID')
    if not ig_user_id:
        raise ConfigError('[account] ig_user_id is required (the numeric ID of the Instagram professional account, '
                          'not the @username)')
    username = acc.text('username')
    username = username.lstrip('@') if username else None
    page_id = acc.text('page_id', pattern=r'\d{5,25}', hint='the numeric Facebook Page ID')
    acc.finish()

    api = _Section(data.get('api'), 'api')
    version = api.text('version', DEFAULT_API_VERSION, pattern=r'v\d+\.\d+', hint='like "v26.0"') or DEFAULT_API_VERSION
    usage_stop = api.number('usage_stop_percent', 85, lo=1, hi=100, integer=True)
    api.finish()

    pth = _Section(data.get('paths'), 'paths')
    manifest = pth.filepath('manifest', 'manifest.json', base)
    source_root = pth.filepath('source_root', '.', base)
    media_dir = pth.filepath('media_dir', 'media', base)
    state = pth.filepath('state', 'state/ig-state.json', base)
    journal = pth.filepath('journal', 'state/ig-journal.jsonl', base)
    pth.finish()

    tok = _Section(data.get('token'), 'token')
    source = tok.text('source', 'env') or 'env'
    if source not in SOURCES:
        raise ConfigError(f'[token] source must be one of {", ".join(SOURCES)} (got {source!r})')
    token_path = tok.text('path')
    if token_path:
        tp = Path(os.path.expanduser(token_path))
        token_path = str(tp if tp.is_absolute() else base / tp)
    token = TokenConfig(
        source=source,
        env_var=tok.text('env_var', 'IG_ACCESS_TOKEN', pattern=r'[A-Za-z_][A-Za-z0-9_]*') or 'IG_ACCESS_TOKEN',
        path=token_path,
        keychain_service=tok.text('keychain_service'),
        keychain_account=tok.text('keychain_account'),
        ssm_parameter=tok.text('ssm_parameter', pattern=r'/?[A-Za-z0-9_.\-/]+', hint='an SSM parameter name'),
        ssm_region=tok.text('ssm_region', pattern=r'[a-z0-9-]+'),
        ssm_profile=tok.text('ssm_profile'),
        timeout_seconds=tok.number('timeout_seconds', 30.0, lo=1, hi=600),
    )
    tok.finish()
    if source == 'file' and not token.path:
        raise ConfigError('[token] path is required when source = "file"')
    if source == 'keychain' and not token.keychain_service:
        raise ConfigError('[token] keychain_service is required when source = "keychain"')
    if source == 'ssm' and not token.ssm_parameter:
        raise ConfigError('[token] ssm_parameter is required when source = "ssm"')

    sp = _Section(data.get('spacing'), 'spacing')
    spacing = Spacing(story_seconds=sp.number('story_seconds', 10.0, lo=0),
                      reel_seconds=sp.number('reel_seconds', 1800.0, lo=0))
    sp.finish()

    sf = _Section(data.get('safety'), 'safety')
    safety = Safety(burst_window_seconds=sf.number('burst_window_hours', 3.0, lo=0, hi=48) * 3600.0,
                    burst_max_posts=sf.number('burst_max_posts', 10, lo=1, hi=100, integer=True),
                    rate_hold_seconds=sf.number('rate_hold_minutes', 60.0, lo=1, hi=24 * 60) * 60.0,
                    container_max_age_seconds=sf.number('container_max_age_hours', 23.0, lo=0.01, hi=23.5) * 3600.0,
                    usage_stop_percent=usage_stop)
    sf.finish()

    tm = _Section(data.get('timing'), 'timing')
    timing = Timing(poll_seconds=tm.number('poll_seconds', 15.0, lo=0.001),
                    poll_timeout_seconds=tm.number('poll_timeout_seconds', 600.0, lo=0.01),
                    guard_wait_seconds=tm.number('guard_wait_seconds', 10.0, lo=0),
                    republish_wait_seconds=tm.number('republish_wait_seconds', 45.0, lo=0),
                    rate_retry_seconds=tm.number('rate_retry_seconds', 60.0, lo=0),
                    read_retry_backoff_seconds=tm.number('read_retry_backoff_seconds', 5.0, lo=0),
                    http_timeout_seconds=tm.number('http_timeout_seconds', 60.0, lo=1),
                    upload_timeout_seconds=tm.number('upload_timeout_seconds', 900.0, lo=1))
    tm.finish()

    cp = _Section(data.get('captions'), 'captions')
    captions = CaptionRules(required=cp.flag('required', True),
                            max_length=cp.number('max_length', CAPTION_MAX, lo=1, hi=CAPTION_MAX, integer=True),
                            min_hashtags=cp.number('min_hashtags', 0, lo=0, hi=HASHTAG_MAX, integer=True),
                            max_hashtags=cp.number('max_hashtags', HASHTAG_MAX, lo=0, hi=HASHTAG_MAX, integer=True),
                            required_text=cp.strings('required_text'),
                            banned_words=cp.strings('banned_words'),
                            licensed_music=cp.strings('licensed_music'),
                            forbid_curly_quotes=cp.flag('forbid_curly_quotes', False))
    cp.finish()
    if captions.min_hashtags > captions.max_hashtags:
        raise ConfigError('[captions] min_hashtags is larger than max_hashtags')

    src = _Section(data.get('sources'), 'sources')
    deny = []
    for pat in src.strings('deny'):
        try:
            deny.append(re.compile(pat))
        except re.error as e:
            raise ConfigError(f'[sources] deny: invalid regular expression {pat!r}: {e}') from None
    sources = SourceRules(deny=tuple(deny))
    src.finish()

    seasons: dict[str, Season] = {}
    raw_seasons = data.get('seasons') or {}
    if not isinstance(raw_seasons, dict):
        raise ConfigError('[seasons] must be a table of named seasons, e.g. [seasons.holiday]')
    for name, body in raw_seasons.items():
        if str(name).startswith(('_', '$')):
            continue
        if not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,31}', str(name)):
            raise ConfigError(f'[seasons] name {name!r} must be lower-case letters, digits, "-" or "_"')
        s = _Section(body, f'seasons.{name}')
        start_t, end_t = _season_time(s, 'start'), _season_time(s, 'end')
        if not start_t or not end_t:
            raise ConfigError(f'[seasons.{name}] needs both start and end (ISO 8601 with a UTC offset)')
        try:
            a, b = parse_aware(start_t), parse_aware(end_t)
        except ValueError as e:
            raise ConfigError(f'[seasons.{name}] {e}') from None
        if a >= b:
            raise ConfigError(f'[seasons.{name}] start must be before end')
        seasons[name] = Season(name=name, start=a, end=b, start_text=start_t, end_text=end_t,
                               caption_words=tuple(w.lower() for w in s.strings('caption_words')))
        s.finish()

    pr = _Section(data.get('prep'), 'prep')
    prep = PrepSettings(width=pr.number('width', 1080, lo=16, hi=4096, integer=True),
                        height=pr.number('height', 1920, lo=16, hi=4096, integer=True),
                        fps=pr.number('fps', 30, lo=1, hi=60, integer=True),
                        fit=pr.text('fit', 'pad', pattern=r'pad|crop|stretch', hint='pad, crop or stretch') or 'pad',
                        crf=pr.number('crf', 20, lo=0, hi=51, integer=True),
                        preset=pr.text('preset', 'medium', pattern=r'ultrafast|superfast|veryfast|faster|fast|medium|'
                                      r'slow|slower|veryslow', hint='an x264 preset') or 'medium',
                        max_video_kbps=pr.number('max_video_kbps', 4500, lo=100, hi=25000, integer=True),
                        audio_kbps=pr.number('audio_kbps', 128, lo=32, hi=128, integer=True),
                        threads=pr.number('threads', 2, lo=1, hi=64, integer=True),
                        nice=pr.number('nice', 10, lo=0, hi=19, integer=True),
                        still_seconds=pr.number('still_seconds', 5.0, lo=3, hi=60))
    pr.finish()
    if prep.width % 2 or prep.height % 2:
        raise ConfigError('[prep] width and height must be even numbers')

    sc = _Section(data.get('schedule'), 'schedule')
    tz = sc.text('timezone')
    if tz:
        try:
            from zoneinfo import ZoneInfo
            ZoneInfo(tz)
        except Exception:  # noqa: BLE001 - ZoneInfoNotFoundError, ValueError, missing tzdata
            raise ConfigError(f'[schedule] timezone {tz!r} is not a known IANA time zone (e.g. "Europe/Berlin"). '
                              f'On minimal systems install the tzdata package.') from None
    schedule = ScheduleSettings(file=sc.filepath('file', 'schedule.txt', base),
                                done_file=sc.filepath('done_file', 'state/schedule.done', base),
                                log_file=sc.filepath('log_file', 'state/schedule.log', base),
                                timezone=tz,
                                gap_seconds=sc.number('gap_seconds', 1860.0, lo=0),
                                max_late_seconds=sc.number('max_late_hours', 6.0, lo=0, hi=24 * 14) * 3600.0,
                                poll_seconds=sc.number('poll_seconds', 60.0, lo=0.05),
                                retry_seconds=sc.number('retry_seconds', 900.0, lo=0),
                                notify_command=sc.strings('notify_command'),
                                command=sc.strings('command'))
    sc.finish()

    return Config(path=path, base_dir=base, ig_user_id=ig_user_id, username=username, page_id=page_id,
                  api_version=version, manifest=manifest, source_root=source_root, media_dir=media_dir, state=state,
                  journal=journal, token=token, schedule=schedule, spacing=spacing, safety=safety, timing=timing,
                  captions=captions, sources=sources, seasons=seasons, prep=prep)
