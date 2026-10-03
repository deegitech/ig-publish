"""Video preparation and verification (``ffmpeg`` / ``ffprobe``; no network).

``prep`` re-encodes every source to one conservative profile that Instagram accepts for Stories and Reels:

* H.264 High, 4:2:0, progressive, constant frame rate, **no B-frames**, closed GOP;
* AAC-LC, at most 128 kbps, 48 kHz, stereo (a silent track is added when the source has none);
* MP4 with the ``moov`` box first (fast start), **no edit list** (``elst``), metadata stripped;
* scaled to the configured size (default 1080x1920) with padding, cropping or stretching.

``verify`` checks the result independently of how it was made (``ffprobe`` plus a small MP4 box parser and the
x264 settings string), so a broken encoder or a hand-made file cannot slip through.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
from collections.abc import Mapping
from typing import Any

from .config import PrepSettings
from .errors import ConfigError, Stop

# Instagram media limits (Meta's IG Media / Reels specifications). Sizes in decimal megabytes.
SPEC: dict[str, dict[str, Any]] = {
    'story': {'media_type': 'STORIES', 'product': 'STORY', 'min_s': 3.0, 'max_s': 60.0, 'max_mb': 100},
    'reel': {'media_type': 'REELS', 'product': 'REELS', 'min_s': 3.0, 'max_s': 900.0, 'max_mb': 300},
}
MAX_VIDEO_BITRATE = 25_000_000
# A 128k AAC encode can measure slightly above 128 kbps; above this the audio is re-encoded at a lower rate.
MAX_AUDIO_BITRATE = 130_000
STILL_EXTENSIONS = ('.png', '.jpg', '.jpeg', '.webp', '.bmp')


# ``-fps_mode`` (constant frame rate output) exists since FFmpeg 5.1.
MIN_FFMPEG = (5, 1)


def ffmpeg_version(first_line: str) -> tuple[int, int] | None:
    """``(major, minor)`` from the first line of ``ffmpeg -version``; ``None`` when it has no release number
    (git and nightly builds), in which case the version is not checked."""
    m = re.match(r'ffmpeg version n?(\d+)\.(\d+)', (first_line or '').strip())
    return (int(m.group(1)), int(m.group(2))) if m else None


def require_tools(*, encoder: bool = True, env: Mapping[str, str] | None = None) -> None:
    """``ffprobe`` (verification) and, for ``encoder=True``, an ``ffmpeg`` new enough for ``prep``."""
    missing = [t for t in (('ffmpeg', 'ffprobe') if encoder else ('ffprobe',)) if not shutil.which(t)]
    if missing:
        raise ConfigError(f'{" and ".join(missing)} not found on PATH. Install FFmpeg (e.g. `brew install ffmpeg`, '
                          f'`apt-get install ffmpeg`).')
    if not encoder:
        return
    try:
        r = subprocess.run(['ffmpeg', '-version'], stdin=subprocess.DEVNULL, capture_output=True, text=True,
                           timeout=30, env=env)
    except (OSError, subprocess.SubprocessError):
        return  # a broken ffmpeg is reported by the encode itself
    ver = ffmpeg_version((r.stdout or '').split('\n', 1)[0])
    if ver is not None and ver < MIN_FFMPEG:
        raise ConfigError(f'FFmpeg {MIN_FFMPEG[0]}.{MIN_FFMPEG[1]} or newer is needed for `prep` (found '
                          f'{ver[0]}.{ver[1]}). On Ubuntu 22.04 and other systems with an older FFmpeg, use the Docker '
                          f'image or a static FFmpeg build.')


def is_still(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in STILL_EXTENSIONS


def video_filter(p: PrepSettings) -> str:
    w, h = p.width, p.height
    if p.fit == 'crop':
        geo = f'scale={w}:{h}:force_original_aspect_ratio=increase:flags=lanczos,crop={w}:{h}'
    elif p.fit == 'stretch':
        geo = f'scale={w}:{h}:flags=lanczos'
    else:
        geo = (f'scale={w}:{h}:force_original_aspect_ratio=decrease:force_divisible_by=2:flags=lanczos,'
               f'pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black')
    return f'{geo},setsar=1,format=yuv420p'


def encoder_args(p: PrepSettings) -> tuple[list[str], list[str], list[str]]:
    v = ['-c:v', 'libx264', '-profile:v', 'high', '-pix_fmt', 'yuv420p', '-preset', p.preset, '-crf', str(p.crf),
         '-maxrate', f'{p.max_video_kbps}k', '-bufsize', f'{p.max_video_kbps * 2}k', '-bf', '0',
         '-g', str(p.fps * 2), '-flags', '+cgop', '-r', str(p.fps), '-fps_mode', 'cfr']
    a = ['-c:a', 'aac', '-b:a', f'{p.audio_kbps}k', '-ar', '48000', '-ac', '2']
    mux = ['-use_editlist', '0', '-movflags', '+faststart', '-map_metadata', '-1']
    return v, a, mux


def recipe(p: PrepSettings) -> str:
    """Short hash of everything that shapes the output; a change triggers a re-encode."""
    v, a, mux = encoder_args(p)
    return hashlib.sha256(json.dumps([video_filter(p), v, a, mux, 'r1']).encode()).hexdigest()[:12]


def sha256(path: str, _cache: dict[tuple[str, int, int], str] = {}) -> str:  # noqa: B006 - deliberate memo
    st = os.stat(path)
    k = (os.path.abspath(path), st.st_size, st.st_mtime_ns)
    if k not in _cache:
        h = hashlib.sha256()
        with open(path, 'rb') as f:
            for b in iter(lambda: f.read(1 << 20), b''):
                h.update(b)
        _cache[k] = h.hexdigest()
    return _cache[k]


# ---------------------------------------------------------------------------------------------- probing
def _ratio(s: Any) -> float:
    try:
        a, b = str(s).split('/')
        return float(a) / float(b) if float(b) else 0.0
    except (ValueError, TypeError):
        return 0.0


def probe(path: str, env: Mapping[str, str] | None = None) -> dict[str, Any]:
    r = subprocess.run(['ffprobe', '-v', 'error', '-print_format', 'json', '-show_format', '-show_streams', path],
                       stdin=subprocess.DEVNULL, capture_output=True, text=True, env=env)
    if r.returncode != 0:
        raise Stop(f'ffprobe could not read {path}: {r.stderr.strip()[:300]}')
    return json.loads(r.stdout or '{}')


_CONTAINER_BOXES = {b'moov', b'trak', b'mdia', b'minf', b'stbl', b'edts', b'dinf', b'mvex'}


def _boxes(buf: bytes) -> list[str]:
    """Box types inside ``moov``, descending into container boxes."""
    out: list[str] = []
    pos, n = 0, len(buf)
    while pos + 8 <= n:
        size, typ = struct.unpack('>I4s', buf[pos:pos + 8])
        h = 8
        if size == 1:
            if pos + 16 > n:
                break
            size, h = struct.unpack('>Q', buf[pos + 8:pos + 16])[0], 16
        elif size == 0:
            size = n - pos
        if size < h or pos + size > n:
            break
        out.append(typ.decode('latin-1'))
        if typ in _CONTAINER_BOXES:
            out += _boxes(buf[pos + h:pos + size])
        pos += size
    return out


def atoms(path: str) -> dict[str, Any]:
    """Top-level MP4 box order (is ``moov`` before ``mdat``?) and whether ``moov`` holds an edit list."""
    n = os.path.getsize(path)
    top: list[str] = []
    moov = b''
    with open(path, 'rb') as f:
        pos = 0
        while pos + 8 <= n:
            f.seek(pos)
            head = f.read(8)
            if len(head) < 8:
                break
            size, typ = struct.unpack('>I4s', head)
            h = 8
            if size == 1:
                ext = f.read(8)
                if len(ext) < 8:
                    break
                size, h = struct.unpack('>Q', ext)[0], 16
            elif size == 0:
                size = n - pos
            if size < h:
                break
            top.append(typ.decode('latin-1'))
            if typ == b'moov' and size <= 256 << 20:
                f.seek(pos + h)
                moov = f.read(size - h)
            pos += size
    inner = _boxes(moov)
    first = top.index('moov') < top.index('mdat') if ('moov' in top and 'mdat' in top) else False
    return {'top': top, 'moov_first': first, 'elst': 'elst' in inner or b'elst' in moov, 'edts': 'edts' in inner}


def x264_settings(path: str) -> dict[str, int | None] | None:
    """The settings string x264 writes into the stream (first 4 MB): ``bframes``, ``open_gop``, ``keyint``."""
    with open(path, 'rb') as f:
        head = f.read(4 << 20)
    m = re.search(rb'x264 - core \d+[^\x00]{0,4000}', head)
    if not m:
        return None
    s = m.group(0).decode('latin-1')
    out: dict[str, int | None] = {}
    for k in ('bframes', 'open_gop', 'keyint'):
        mm = re.search(rf'\b{k}=(\d+)', s)
        out[k] = int(mm.group(1)) if mm else None
    return out


def verify(path: str, kind: str, p: PrepSettings, env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Check one file against Instagram's limits and the prep profile. ``report['ok']`` / ``report['problems']``.

    The prep profile (exact size, constant frame rate, no B-frames ...) is stricter than Instagram's own limits."""
    sp = SPEC[kind]
    j = probe(path, env)
    fmt = j.get('format', {})
    vs = [s for s in j.get('streams', []) if s.get('codec_type') == 'video']
    au = [s for s in j.get('streams', []) if s.get('codec_type') == 'audio']
    v, a = (vs[0] if vs else {}), (au[0] if au else {})
    at = atoms(path)
    x = x264_settings(path)
    rep: dict[str, Any] = {
        'duration': round(float(fmt.get('duration') or 0), 3), 'bytes': os.path.getsize(path),
        'vcodec': v.get('codec_name'), 'profile': v.get('profile'), 'width': v.get('width'), 'height': v.get('height'),
        'pix_fmt': v.get('pix_fmt'), 'field_order': v.get('field_order'),
        'fps': round(_ratio(v.get('r_frame_rate')), 3),
        'avg_fps': round(_ratio(v.get('avg_frame_rate')), 3), 'has_b_frames': v.get('has_b_frames'),
        'vbitrate': int(v.get('bit_rate') or 0), 'acodec': a.get('codec_name'),
        'sample_rate': int(a.get('sample_rate') or 0), 'channels': a.get('channels'),
        'abitrate': int(a.get('bit_rate') or 0), 'top_boxes': at['top'], 'moov_first': at['moov_first'],
        'elst': at['elst'], 'x264': x}
    probs: list[str] = []
    d = rep['duration']
    if len(vs) != 1 or len(au) != 1:
        probs.append(f'{len(vs)} video / {len(au)} audio streams (needs exactly 1 / 1)')
    if not sp['min_s'] <= d <= sp['max_s']:
        probs.append(f"duration {d:.2f} s (outside {sp['min_s']:g}-{sp['max_s']:g} s for a {kind})")
    if rep['bytes'] > sp['max_mb'] * 1_000_000:
        probs.append(f"size {rep['bytes'] / 1e6:.1f} MB (> {sp['max_mb']} MB)")
    if rep['vcodec'] != 'h264' or rep['profile'] != 'High':
        probs.append(f"video {rep['vcodec']}/{rep['profile']} (needs h264/High)")
    if rep['pix_fmt'] != 'yuv420p':
        probs.append(f"pixel format {rep['pix_fmt']} (needs yuv420p)")
    if (rep['width'], rep['height']) != (p.width, p.height):
        probs.append(f"resolution {rep['width']}x{rep['height']} (needs {p.width}x{p.height})")
    if rep['field_order'] not in (None, 'progressive'):
        probs.append(f"interlaced video ({rep['field_order']})")
    if rep['fps'] != p.fps or not p.fps - 0.5 <= rep['avg_fps'] <= p.fps + 0.5:
        probs.append(f"frame rate {rep['fps']}/{rep['avg_fps']} (needs a constant {p.fps})")
    if rep['has_b_frames'] != 0:
        probs.append(f"B-frames present (has_b_frames={rep['has_b_frames']})")
    if rep['vbitrate'] > MAX_VIDEO_BITRATE:
        probs.append(f"video bitrate {rep['vbitrate'] / 1e6:.1f} Mbps (> {MAX_VIDEO_BITRATE // 1_000_000})")
    if rep['acodec'] != 'aac' or rep['sample_rate'] > 48000 or rep['channels'] not in (1, 2):
        probs.append(f"audio {rep['acodec']} {rep['sample_rate']} Hz {rep['channels']} ch (needs AAC, <= 48 kHz, "
                     f"mono or stereo)")
    if rep['abitrate'] > MAX_AUDIO_BITRATE:
        probs.append(f"audio bitrate {rep['abitrate'] / 1000:.0f} kbps (> {MAX_AUDIO_BITRATE // 1000})")
    if not rep['moov_first']:
        probs.append(f"moov box is not first (boxes: {' '.join(at['top'])})")
    if rep['elst']:
        probs.append('edit list (elst) present')
    if x is None:
        probs.append('no x264 settings string (B-frames/GOP cannot be verified; run `ig-publish prep`)')
    else:
        if x.get('bframes') != 0:
            probs.append(f"x264 bframes={x.get('bframes')}")
        if x.get('open_gop') not in (0, None):
            probs.append(f"open GOP (open_gop={x.get('open_gop')})")
    rep['problems'] = probs
    rep['ok'] = not probs
    return rep


# ---------------------------------------------------------------------------------------------- encoding
def _lower_priority(niceness: int):
    def fn() -> None:
        try:
            os.nice(niceness)
        except (OSError, AttributeError):
            pass
    return fn


def encode(src: str, out: str, p: PrepSettings, still_seconds: float | None = None,
           env: Mapping[str, str] | None = None) -> None:
    """Encode ``src`` (a video, or a still image when ``still_seconds`` is given) to ``out``."""
    v_args, a_args, mux = encoder_args(p)
    threads = str(p.threads)
    cmd = ['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-y', '-threads', threads]
    if still_seconds is not None:
        cmd += ['-loop', '1', '-framerate', str(p.fps), '-t', f'{still_seconds:g}', '-i', src]
        has_audio = False
    else:
        cmd += ['-i', src]
        has_audio = any(s.get('codec_type') == 'audio' for s in probe(src, env).get('streams', []))
    if not has_audio:  # a silent AAC track keeps every output in the same shape
        cmd += ['-f', 'lavfi', '-i', 'anullsrc=r=48000:cl=stereo']
    cmd += ['-map', '0:v:0', '-map', '0:a:0' if has_audio else '1:a:0', '-vf', video_filter(p),
            '-filter_threads', '1', *v_args, *a_args]
    if not has_audio:
        cmd += ['-shortest']
    cmd += ['-threads', threads, *mux, out]
    pre = _lower_priority(p.nice) if p.nice else None
    r = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True, preexec_fn=pre,  # noqa: PLW1509
                       env=env)
    if r.returncode != 0:
        raise Stop(f'ffmpeg failed for {src}: {r.stderr.strip()[-600:]}')
    a = next((s for s in probe(out, env).get('streams', []) if s.get('codec_type') == 'audio'), {})
    if int(a.get('bit_rate') or 0) > MAX_AUDIO_BITRATE:
        # Re-encode only the audio at a lower bitrate; the video stream is copied unchanged.
        tmp = out + '.a.mp4'
        cmd2 = ['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-y', '-i', out, '-map', '0:v:0',
                '-map', '0:a:0', '-c:v', 'copy', '-c:a', 'aac', '-b:a', f'{max(32, p.audio_kbps - 16)}k',
                '-ar', '48000', '-ac', '2', *mux, tmp]
        r = subprocess.run(cmd2, stdin=subprocess.DEVNULL, capture_output=True, text=True, preexec_fn=pre,  # noqa: PLW1509
                           env=env)
        if r.returncode != 0:
            raise Stop(f'ffmpeg (audio re-encode) failed for {src}: {r.stderr.strip()[-600:]}')
        os.replace(tmp, out)
