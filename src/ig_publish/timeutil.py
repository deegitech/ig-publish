"""Small time helpers (stdlib only, Python 3.10 compatible)."""
from __future__ import annotations

import datetime as dt
import math
import time


def now_iso() -> str:
    """Local time with UTC offset, second precision."""
    return dt.datetime.now().astimezone().isoformat(timespec='seconds')


def iso(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts).astimezone().isoformat(timespec='seconds')


def clock(ts: float) -> str:
    """Human-readable local time plus UTC, e.g. ``2030-01-15 12:00 +01:00 (11:00 UTC)``."""
    loc = dt.datetime.fromtimestamp(ts).astimezone()
    utc = dt.datetime.fromtimestamp(ts, dt.timezone.utc)
    off = loc.strftime('%z')
    if len(off) == 5:
        off = f'{off[:3]}:{off[3:]}'
    u = f'{utc:%H:%M}' if loc.date() == utc.date() else f'{utc:%Y-%m-%d %H:%M}'
    return f'{loc:%Y-%m-%d %H:%M} {off} ({u} UTC)'


def parse_ts(value: object) -> float:
    """Lenient timestamp parser for API answers and state files. Returns 0.0 when it cannot parse.

    Accepts Meta's ``2030-01-02T12:00:00+0000``, ISO 8601 with ``Z`` or ``+00:00``, and epoch numbers.
    Naive ISO strings are taken as local time.
    """
    if value is None or value == '':
        return 0.0
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    s = str(value).strip()
    try:
        return dt.datetime.strptime(s, '%Y-%m-%dT%H:%M:%S%z').timestamp()
    except ValueError:
        pass
    try:
        d = dt.datetime.fromisoformat(s.replace('Z', '+00:00'))
        return (d if d.tzinfo else d.astimezone()).timestamp()
    except ValueError:
        pass
    try:
        return float(s)
    except ValueError:
        return 0.0


def parse_aware(value: object) -> float:
    """Strict parser for configuration times: ISO 8601 *with* an explicit UTC offset (or ``Z``).

    Raises ``ValueError`` for anything else, so a season window can never silently use the machine's time zone.
    """
    s = str(value).strip()
    try:
        d = dt.datetime.fromisoformat(s.replace('Z', '+00:00'))
    except ValueError:
        try:
            d = dt.datetime.strptime(s, '%Y-%m-%dT%H:%M:%S%z')
        except ValueError:
            raise ValueError(f'not an ISO 8601 time: {s!r}') from None
    if d.tzinfo is None:
        raise ValueError(f'{s!r} has no UTC offset (write e.g. 2030-12-01T00:00:00-08:00 or ...Z)')
    return d.timestamp()


def parse_when(value: str) -> float:
    """``--at`` values: epoch seconds or an ISO 8601 time with offset."""
    try:
        x = float(value)
    except ValueError:
        return parse_aware(value)
    if not math.isfinite(x):
        raise ValueError(f'not a time: {value!r}')
    return x


def monotonic() -> float:
    return time.monotonic()
