"""Shared test helpers: synthetic media, temporary projects and running the CLI as a subprocess."""
from __future__ import annotations

import copy
import json
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), 'src')
HAVE_FFMPEG = bool(shutil.which('ffmpeg') and shutil.which('ffprobe'))
if SRC not in sys.path:
    sys.path.insert(0, SRC)
if HERE not in sys.path:
    sys.path.insert(0, HERE)


def make_clip(path: str, dur: float, src: str = 'testsrc2', freq: int = 440, size: str = '1080x1920') -> None:
    """A source Instagram would NOT accept as is: B-frames (so an edit list), moov at the end, 192 kbps audio."""
    subprocess.run(['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-y', '-threads', '2',
                    '-f', 'lavfi', '-i', f'{src}=s={size}:r=30:d={dur}',
                    '-f', 'lavfi', '-i', f'sine=frequency={freq}:sample_rate=48000:duration={dur}',
                    '-c:v', 'libx264', '-preset', 'veryfast', '-bf', '3', '-threads', '2',
                    '-c:a', 'aac', '-b:a', '192k', '-ac', '2', '-shortest', path],
                   stdin=subprocess.DEVNULL, check=True)


def make_still(path: str, size: str = '1280x720') -> None:
    subprocess.run(['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-y', '-f', 'lavfi',
                    '-i', f'testsrc2=s={size}', '-frames:v', '1', path], stdin=subprocess.DEVNULL, check=True)


def clean_env(extra: dict | None = None) -> dict:
    """The current environment minus anything ig-publish reads, plus PYTHONPATH pointing at ``src``."""
    e = {k: v for k, v in os.environ.items()
         if not k.startswith(('IG_PUBLISH_', 'IG_ACCESS_TOKEN', 'AWS_')) and k not in ('PYTHONPATH',)}
    e['PYTHONPATH'] = SRC
    e['PYTHONDONTWRITEBYTECODE'] = '1'
    e.update(extra or {})
    return e


def deep_merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def write_json(path: str, obj) -> str:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    return path


def read(path: str) -> str:
    try:
        with open(path, encoding='utf-8') as f:
            return f.read()
    except FileNotFoundError:
        return ''


def run_cli(cfg: str | None, *args: str, env: dict, timeout: float = 300,
            cwd: str | None = None) -> subprocess.CompletedProcess:
    cmd = [sys.executable, '-m', 'ig_publish'] + (['--config', cfg] if cfg else []) + list(args)
    return subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL,
                          cwd=cwd)
