"""The manifest: what to publish, in which order.

The order of ``items`` is the publishing order. A JSON Schema ships with the package
(``ig_publish/schemas/manifest.schema.json``); this module performs the same checks (and a few that a schema
cannot express, such as file existence and caption rules) without third-party packages.
"""
from __future__ import annotations

import os
import re
from collections.abc import Iterable, Mapping

from .captions import caption_problems
from .config import Config
from .errors import ConfigError
from .media import SPEC, is_still
from .state import load_json

KEY_RE = re.compile(r'[a-z0-9][a-z0-9-]{1,63}')
MEDIA_ID_RE = re.compile(r'\d{6,25}')
ITEM_FIELDS = {'key', 'kind', 'group', 'src', 'caption', 'share_to_feed', 'thumb_offset', 'season', 'already_live',
               'needs_approval', 'still_seconds', 'note', 'notes'}
REEL_ONLY = ('caption', 'share_to_feed', 'thumb_offset')
TOP_FIELDS = {'items', 'ig_user_id', 'note', 'notes'}


def _user_field(name: str) -> bool:
    return name.startswith(('_', 'x-', '$'))


def load_manifest(cfg: Config, *, must_contain: Mapping[str, Iterable[str]] | None = None) -> tuple[dict, list[dict]]:
    """Load and validate the manifest. Returns ``(manifest, items)``; each item gains ``src_abs``.

    ``must_contain`` maps an option name (``--keys``) to keys that must exist in the manifest.
    """
    try:
        man = load_json(cfg.manifest, None)
    except ValueError as e:
        raise ConfigError(f'Cannot read the manifest {cfg.manifest}: {e}') from None
    if man is None:
        raise ConfigError(f'Manifest not found: {cfg.manifest} (set [paths] manifest, or run `ig-publish init`)')
    if not isinstance(man, dict) or not isinstance(man.get('items'), list):
        raise ConfigError(f'Invalid manifest {cfg.manifest}: it needs an "items" list')
    errs: list[str] = []
    extra = sorted(k for k in man if k not in TOP_FIELDS and not _user_field(k))
    if extra:
        errs.append(f'unknown top-level field(s): {", ".join(extra)}')
    if man.get('ig_user_id') is not None and str(man['ig_user_id']) != cfg.ig_user_id:
        raise ConfigError(f'The manifest is for Instagram account {man["ig_user_id"]}, the configuration for '
                          f'{cfg.ig_user_id}.')
    seen: set[str] = set()
    items: list[dict] = []
    for i, raw in enumerate(man['items'], 1):
        if not isinstance(raw, dict):
            errs.append(f'#{i}: every item must be an object')
            continue
        it = dict(raw)
        key = str(it.get('key') or '')
        where = f'#{i} {key or "?"}'
        unknown = sorted(k for k in it if k not in ITEM_FIELDS and not _user_field(k))
        if unknown:
            errs.append(f'{where}: unknown field(s) {", ".join(unknown)} (custom fields must start with "x-" or "_")')
        if not KEY_RE.fullmatch(key):
            errs.append(f'{where}: key must be 2-64 characters of a-z, 0-9 and "-", starting with a letter or digit')
        if key in seen:
            errs.append(f'{where}: duplicate key')
        seen.add(key)
        kind = it.get('kind')
        if kind not in SPEC:
            errs.append(f'{where}: kind must be "story" or "reel" (got {kind!r})')
            continue
        if not isinstance(it.get('group'), str) or not it['group'].strip():
            errs.append(f'{where}: group is required (for stories: the Highlight you plan to add it to)')
            it['group'] = str(it.get('group') or '?')
        src = it.get('src')
        if not isinstance(src, str) or not src:
            errs.append(f'{where}: src is required')
            src = ''
        it['src_abs'] = os.path.normpath(os.path.join(cfg.source_root, os.path.expanduser(src))) if src else ''
        if src and not os.path.isfile(it['src_abs']):
            errs.append(f'{where}: source file not found: {src}')
        for rx in cfg.sources.deny:
            if src and rx.search(src.replace(os.sep, '/')):
                errs.append(f'{where}: {src} matches [sources] deny pattern {rx.pattern!r} and may not be published '
                            f'through the API')
        ss = it.get('still_seconds')
        if ss is not None:
            if not is_still(src):
                errs.append(f'{where}: still_seconds only applies to image sources')
            elif isinstance(ss, bool) or not isinstance(ss, (int, float)) or not 3 <= ss <= 60:
                errs.append(f'{where}: still_seconds must be a number between 3 and 60')
        al = it.get('already_live')
        if al is not None and (not isinstance(al, dict) or not MEDIA_ID_RE.fullmatch(str(al.get('media_id') or ''))
                               or not str(al.get('permalink') or '').startswith('https://www.instagram.com/')
                               or set(al) - {'media_id', 'permalink', 'at', 'via'}):
            errs.append(f'{where}: already_live must be {{"media_id", "permalink", "at"?, "via"?}}')
        na = it.get('needs_approval')
        if na is not None and (not isinstance(na, str) or not na.strip()):
            errs.append(f'{where}: needs_approval must be a non-empty reason')
        season = it.get('season')
        if season is not None and season not in cfg.seasons:
            known = ', '.join(cfg.seasons) or 'none configured'
            errs.append(f'{where}: season {season!r} is not defined in the configuration (known: {known})')
        cap = it.get('caption')
        if kind == 'story':
            for f in REEL_ONLY:
                if it.get(f) is not None:
                    errs.append(f'{where}: "{f}" is not supported for API stories - remove it')
        else:
            if cap is None or (isinstance(cap, str) and not cap.strip()):
                if cfg.captions.required:
                    errs.append(f'{where}: reel caption missing ([captions] required = true)')
            elif not isinstance(cap, str):
                errs.append(f'{where}: caption must be a string')
            else:
                errs += [f'{where}: {p}' for p in caption_problems(cap, season, cfg.captions, cfg.seasons)]
            to = it.get('thumb_offset')
            if to is not None and (isinstance(to, bool) or not isinstance(to, int) or to < 0):
                errs.append(f'{where}: thumb_offset must be a whole number of milliseconds >= 0')
            stf = it.get('share_to_feed')
            if stf is not None and not isinstance(stf, bool):
                errs.append(f'{where}: share_to_feed must be true or false')
        items.append(it)
    if errs:
        raise ConfigError(f'Manifest errors in {cfg.manifest}:\n  ' + '\n  '.join(errs))
    for option, keys in (must_contain or {}).items():
        bad = sorted(k for k in keys if k not in seen)
        if bad:
            raise ConfigError(f'{option}: key(s) not in the manifest: {", ".join(bad)}')
    return man, items
