"""Locating and driving ffmpeg / ffprobe."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Sequence

_CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


class FFmpegMissing(RuntimeError):
    pass


class Cancelled(RuntimeError):
    pass


def _winget_candidates(exe: str) -> Iterable[Path]:
    base = os.environ.get("LOCALAPPDATA")
    if not base:
        return []
    pkgs = Path(base) / "Microsoft" / "WinGet" / "Packages"
    if not pkgs.is_dir():
        return []
    found: list[Path] = []
    try:
        for entry in pkgs.iterdir():
            if "ffmpeg" not in entry.name.lower():
                continue
            found.extend(entry.glob(f"**/bin/{exe}"))
    except OSError:
        pass
    return found


def _find(exe: str, override: str | None = None) -> str:
    """Locate an ffmpeg-family binary, or raise FFmpegMissing."""
    if override:
        p = Path(override)
        if p.is_dir():
            p = p / exe
        if p.is_file():
            return str(p)
    env = os.environ.get("FFMPEG_DIR")
    if env:
        p = Path(env) / exe
        if p.is_file():
            return str(p)
    which = shutil.which(Path(exe).stem)
    if which:
        return which
    for cand in _winget_candidates(exe):
        if cand.is_file():
            return str(cand)
    extra = [
        Path(r"C:\ffmpeg\bin") / exe,
        Path(r"C:\Program Files\ffmpeg\bin") / exe,
    ]
    prog = os.environ.get("ProgramFiles")
    if prog:
        extra.append(Path(prog) / "ffmpeg" / "bin" / exe)
    for cand in extra:
        if cand.is_file():
            return str(cand)
    raise FFmpegMissing(
        f"Could not find {exe}. Install it with:  winget install Gyan.FFmpeg\n"
        f"or set the FFMPEG_DIR environment variable to the folder that contains it."
    )


_cache: dict[str, str] = {}


def ffmpeg_path(override: str | None = None) -> str:
    key = f"ffmpeg::{override or ''}"
    if key not in _cache:
        _cache[key] = _find("ffmpeg.exe" if sys.platform == "win32" else "ffmpeg", override)
    return _cache[key]


def ffprobe_path(override: str | None = None) -> str:
    key = f"ffprobe::{override or ''}"
    if key not in _cache:
        _cache[key] = _find("ffprobe.exe" if sys.platform == "win32" else "ffprobe", override)
    return _cache[key]


def have_ffmpeg(override: str | None = None) -> bool:
    try:
        ffmpeg_path(override)
        ffprobe_path(override)
        return True
    except FFmpegMissing:
        return False


@dataclass
class MediaInfo:
    path: str
    width: int = 0
    height: int = 0
    duration: float = 0.0
    fps: float = 30.0
    has_audio: bool = False
    vcodec: str = ""
    acodec: str = ""
    raw: dict = field(default_factory=dict)

    @property
    def aspect(self) -> float:
        return (self.width / self.height) if self.height else 16 / 9


def probe(path: str | os.PathLike, ffprobe: str | None = None) -> MediaInfo:
    exe = ffprobe_path(ffprobe)
    args = [exe, "-v", "quiet", "-print_format", "json",
            "-show_format", "-show_streams", str(path)]
    out = subprocess.run(
        args, capture_output=True, text=True, encoding="utf-8", errors="replace",
        creationflags=_CREATE_NO_WINDOW,
    )
    if out.returncode != 0:
        raise RuntimeError(f"ffprobe failed on {path}:\n{out.stderr[-2000:]}")
    data = json.loads(out.stdout or "{}")
    info = MediaInfo(path=str(path), raw=data)
    for s in data.get("streams", []):
        if s.get("codec_type") == "video" and not info.width:
            info.width = int(s.get("width") or 0)
            info.height = int(s.get("height") or 0)
            info.vcodec = s.get("codec_name") or ""
            rate = s.get("avg_frame_rate") or s.get("r_frame_rate") or "0/0"
            try:
                num, den = rate.split("/")
                if float(den):
                    info.fps = round(float(num) / float(den), 3)
            except (ValueError, ZeroDivisionError):
                pass
            if s.get("duration"):
                try:
                    info.duration = float(s["duration"])
                except ValueError:
                    pass
        elif s.get("codec_type") == "audio" and not info.has_audio:
            info.has_audio = True
            info.acodec = s.get("codec_name") or ""
    fmt_dur = (data.get("format") or {}).get("duration")
    if fmt_dur:
        try:
            info.duration = max(info.duration, float(fmt_dur))
        except ValueError:
            pass
    if info.fps <= 0 or info.fps > 240:
        info.fps = 30.0
    return info


# ---------------------------------------------------------------------------
# fontconfig
#
# The Windows ffmpeg builds are compiled with fontconfig but ship no config
# file, so drawtext and libass (our captions) fail - loudly, or by crashing.
# We generate a minimal config pointing at the system font folders and hand it
# to every ffmpeg process we start.

_FONTCONF_TEMPLATE = """<?xml version="1.0"?>
<!DOCTYPE fontconfig SYSTEM "urn:fontconfig:fonts.dtd">
<fontconfig>
{dirs}
  <cachedir>{cache}</cachedir>
  <match target="pattern">
    <test qual="any" name="family"><string>serif</string></test>
    <edit name="family" mode="assign" binding="same"><string>Georgia</string></edit>
  </match>
  <match target="pattern">
    <test qual="any" name="family"><string>sans-serif</string></test>
    <edit name="family" mode="assign" binding="same"><string>Arial</string></edit>
  </match>
  <match target="pattern">
    <test qual="any" name="family"><string>monospace</string></test>
    <edit name="family" mode="assign" binding="same"><string>Consolas</string></edit>
  </match>
  <config></config>
</fontconfig>
"""

_font_env: dict[str, str] | None = None


def _system_font_dirs() -> list[Path]:
    dirs: list[Path] = []
    if sys.platform == "win32":
        win = os.environ.get("SystemRoot") or r"C:\Windows"
        dirs.append(Path(win) / "Fonts")
        local = os.environ.get("LOCALAPPDATA")
        if local:
            p = Path(local) / "Microsoft" / "Windows" / "Fonts"
            if p.is_dir():
                dirs.append(p)
    else:
        for p in (Path("/usr/share/fonts"), Path("/usr/local/share/fonts"),
                  Path.home() / ".fonts"):
            if p.is_dir():
                dirs.append(p)
    return [d for d in dirs if d.is_dir()]


def ensure_fontconfig() -> dict[str, str]:
    """Write a minimal fonts.conf and return the env ffmpeg should run with."""
    global _font_env
    if _font_env is not None:
        return _font_env
    env = dict(os.environ)
    if env.get("FONTCONFIG_FILE") and Path(env["FONTCONFIG_FILE"]).is_file():
        _font_env = env
        return env
    try:
        from .paths import WORK_DIR
        conf_dir = WORK_DIR / "fontconfig"
        cache_dir = conf_dir / "cache"
        conf_dir.mkdir(parents=True, exist_ok=True)
        cache_dir.mkdir(parents=True, exist_ok=True)
        dirs = "\n".join(f"  <dir>{d}</dir>" for d in _system_font_dirs())
        conf = conf_dir / "fonts.conf"
        text = _FONTCONF_TEMPLATE.format(dirs=dirs, cache=cache_dir)
        if not conf.is_file() or conf.read_text(encoding="utf-8") != text:
            conf.write_text(text, encoding="utf-8")
        env["FONTCONFIG_FILE"] = str(conf)
        env["FONTCONFIG_PATH"] = str(conf_dir)
    except OSError:
        pass
    _font_env = env
    return env


_TIME_RE = re.compile(r"out_time=(-?)(\d+):(\d\d):(\d\d(?:\.\d+)?)")
_PROGRESS_PREFIXES = (
    "frame=", "fps=", "out_time", "total_size", "bitrate=", "speed=",
    "stream_", "dup_frames", "drop_frames", "progress=",
)


def run_ffmpeg(
    args: Sequence[str],
    *,
    total_seconds: float | None = None,
    on_progress: Callable[[float], None] | None = None,
    cancel: threading.Event | None = None,
    cwd: str | os.PathLike | None = None,
    ffmpeg: str | None = None,
    log_name: str = "ffmpeg",
) -> str:
    """Run ffmpeg, streaming progress to `on_progress`. Returns the tail of stderr."""
    exe = ffmpeg_path(ffmpeg)
    cmd = [exe, "-hide_banner", "-nostdin", "-y"]
    track = bool(on_progress and total_seconds)
    if track:
        cmd += ["-progress", "pipe:2", "-nostats"]
    cmd += [str(a) for a in args]

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        cwd=str(cwd) if cwd else None,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        env=ensure_fontconfig(),
        creationflags=_CREATE_NO_WINDOW,
    )
    tail: list[str] = []
    killed = False
    try:
        assert proc.stderr is not None
        for line in proc.stderr:
            line = line.rstrip("\r\n")
            if cancel is not None and cancel.is_set():
                killed = True
                proc.kill()
                break
            if track and line.startswith(_PROGRESS_PREFIXES):
                m = _TIME_RE.match(line)
                if m and on_progress:
                    sign, hh, mm, ss = m.groups()
                    if sign != "-":
                        secs = int(hh) * 3600 + int(mm) * 60 + float(ss)
                        on_progress(max(0.0, min(1.0, secs / max(total_seconds or 1.0, 0.001))))
                continue
            if line:
                tail.append(line)
                if len(tail) > 400:
                    del tail[:200]
        proc.wait()
    finally:
        if proc.poll() is None:
            proc.kill()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
    if killed or (cancel is not None and cancel.is_set()):
        raise Cancelled("cancelled")
    text = "\n".join(tail)
    if proc.returncode not in (0, None):
        raise RuntimeError(f"{log_name} failed (exit {proc.returncode}):\n{text[-2500:]}")
    if on_progress:
        on_progress(1.0)
    return text


def list_encoders(ffmpeg: str | None = None) -> set[str]:
    try:
        exe = ffmpeg_path(ffmpeg)
    except FFmpegMissing:
        return set()
    out = subprocess.run(
        [exe, "-hide_banner", "-encoders"], capture_output=True, text=True,
        encoding="utf-8", errors="replace", creationflags=_CREATE_NO_WINDOW,
    )
    names: set[str] = set()
    for line in out.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and len(parts[0]) == 6 and parts[0][0] in "VAS":
            names.add(parts[1])
    return names


def extract_frame(src: str | os.PathLike, timestamp: float, dest: str | os.PathLike,
                  width: int = 960, ffmpeg: str | None = None) -> str:
    """Grab a single JPEG frame at `timestamp` seconds."""
    run_ffmpeg(
        ["-ss", f"{max(timestamp, 0.0):.3f}", "-i", str(src), "-frames:v", "1",
         "-vf", f"scale={width}:-2:flags=bicubic", "-q:v", "3", str(dest)],
        log_name="frame grab", ffmpeg=ffmpeg,
    )
    return str(dest)
