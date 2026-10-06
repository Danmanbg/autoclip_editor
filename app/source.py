"""Turning whatever the user pasted into a local video file on disk."""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from .ffmpeg_tools import Cancelled, MediaInfo, ffmpeg_path, probe
from .paths import SOURCE_DIR, safe_name

URL_RE = re.compile(r"^[a-z][a-z0-9+.-]*://", re.I)
TWITCH_VOD_RE = re.compile(r"twitch\.tv/videos/(\d+)", re.I)
TWITCH_CLIP_RE = re.compile(r"(?:clips\.twitch\.tv/|twitch\.tv/\w+/clip/)([A-Za-z0-9_-]+)", re.I)

VIDEO_EXTS = {".mp4", ".mkv", ".mov", ".webm", ".flv", ".ts", ".avi", ".m4v", ".mpg", ".mpeg", ".wmv"}


@dataclass
class Source:
    path: str
    title: str = ""
    url: str = ""
    kind: str = "local"          # local | twitch_vod | twitch_clip | web
    vod_id: str = ""
    time_offset: float = 0.0     # seconds into the original stream that path[0] represents
    width: int = 0
    height: int = 0
    duration: float = 0.0
    fps: float = 30.0
    has_audio: bool = True
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def looks_like_url(text: str) -> bool:
    t = (text or "").strip()
    return bool(URL_RE.match(t)) or t.lower().startswith("www.")


def classify(url: str) -> tuple[str, str]:
    m = TWITCH_VOD_RE.search(url)
    if m:
        return "twitch_vod", m.group(1)
    m = TWITCH_CLIP_RE.search(url)
    if m:
        return "twitch_clip", m.group(1)
    return "web", ""


def _cache_dir(url: str, start: float, end: float) -> Path:
    key = hashlib.sha1(f"{url}|{start:.2f}|{end:.2f}".encode("utf-8")).hexdigest()[:16]
    return SOURCE_DIR / key


def _existing_video(folder: Path) -> Path | None:
    if not folder.is_dir():
        return None
    for p in sorted(folder.iterdir()):
        if p.suffix.lower() in VIDEO_EXTS and p.stat().st_size > 1024:
            return p
    return None


def resolve_local(text: str) -> Path | None:
    """Accept a raw path, a quoted path, or a file:// URL."""
    t = (text or "").strip().strip('"').strip("'")
    if not t:
        return None
    if t.lower().startswith("file:///"):
        from urllib.parse import unquote, urlparse
        t = unquote(urlparse(t).path).lstrip("/")
    p = Path(t)
    try:
        if p.is_file():
            return p.resolve()
    except OSError:
        return None
    return None


def download(
    url: str,
    *,
    start: float = 0.0,
    end: float = 0.0,
    on_progress: Callable[[float, str], None] | None = None,
    cancel: threading.Event | None = None,
    max_height: int = 1080,
) -> Source:
    """Fetch a video with yt-dlp into the source cache (reusing a prior download)."""
    try:
        import yt_dlp
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "yt-dlp is not installed. Run:  py -3 -m pip install yt-dlp"
        ) from exc

    kind, vod_id = classify(url)
    folder = _cache_dir(url, start, end)
    folder.mkdir(parents=True, exist_ok=True)
    meta_file = folder / "meta.json"

    cached = _existing_video(folder)
    if cached and meta_file.is_file():
        try:
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
            if on_progress:
                on_progress(1.0, "Using cached download")
            return _finish(Source(
                path=str(cached), title=meta.get("title", ""), url=url, kind=kind,
                vod_id=vod_id, time_offset=float(meta.get("time_offset", start or 0.0)),
                meta=meta,
            ))
        except (OSError, ValueError):
            pass

    state = {"pct": 0.0}

    def hook(d):
        if cancel is not None and cancel.is_set():
            raise Cancelled("cancelled")
        if d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            done = d.get("downloaded_bytes") or 0
            if total:
                state["pct"] = min(0.99, done / total)
            speed = d.get("speed") or 0
            eta = d.get("eta")
            bits = []
            if total:
                bits.append(f"{done / 1e6:.0f} / {total / 1e6:.0f} MB")
            if speed:
                bits.append(f"{speed / 1e6:.1f} MB/s")
            if eta:
                bits.append(f"ETA {int(eta)}s")
            if on_progress:
                on_progress(state["pct"], "Downloading " + "  ".join(bits))
        elif d.get("status") == "finished":
            if on_progress:
                on_progress(0.99, "Merging streams")

    fmt = (
        f"bv*[height<={max_height}][ext=mp4]+ba[ext=m4a]/"
        f"b[height<={max_height}][ext=mp4]/"
        f"bv*[height<={max_height}]+ba/b[height<={max_height}]/b"
    )
    opts: dict[str, Any] = {
        "outtmpl": str(folder / "%(title).80B.%(ext)s"),
        "format": fmt,
        "merge_output_format": "mp4",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "progress_hooks": [hook],
        "ffmpeg_location": str(Path(ffmpeg_path()).parent),
        "retries": 5,
        "fragment_retries": 10,
        "concurrent_fragment_downloads": 4,
        "windowsfilenames": True,
        "restrictfilenames": False,
        "overwrites": False,
    }
    if end and end > start:
        from yt_dlp.utils import download_range_func
        opts["download_ranges"] = download_range_func(None, [(start, end)])
        opts["force_keyframes_at_cuts"] = True

    if on_progress:
        on_progress(0.02, "Reading source info")

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)

    if cancel is not None and cancel.is_set():
        raise Cancelled("cancelled")

    path = _existing_video(folder)
    if path is None:
        raise RuntimeError("Download finished but no video file was produced.")

    title = (info or {}).get("title") or path.stem
    meta = {
        "title": title,
        "uploader": (info or {}).get("uploader") or (info or {}).get("channel") or "",
        "webpage_url": (info or {}).get("webpage_url") or url,
        "original_duration": (info or {}).get("duration") or 0,
        "time_offset": start if (end and end > start) else 0.0,
        "downloaded_at": time.time(),
    }
    meta_file.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    if on_progress:
        on_progress(1.0, "Download complete")
    return _finish(Source(
        path=str(path), title=title, url=url, kind=kind, vod_id=vod_id,
        time_offset=float(meta["time_offset"]), meta=meta,
    ))


def _finish(src: Source) -> Source:
    info: MediaInfo = probe(src.path)
    src.width, src.height = info.width, info.height
    src.duration = info.duration
    src.fps = info.fps
    src.has_audio = info.has_audio
    if not src.title:
        src.title = Path(src.path).stem
    return src


def resolve(
    text: str,
    *,
    start: float = 0.0,
    end: float = 0.0,
    on_progress: Callable[[float, str], None] | None = None,
    cancel: threading.Event | None = None,
    max_height: int = 1080,
) -> Source:
    """Main entry point: local path or URL in, ready-to-use Source out."""
    text = (text or "").strip()
    if not text:
        raise ValueError("No stream URL or file given.")

    local = resolve_local(text)
    if local is not None:
        if on_progress:
            on_progress(1.0, "Using local file")
        return _finish(Source(path=str(local), title=local.stem, kind="local"))

    if not looks_like_url(text):
        raise FileNotFoundError(
            f"That is neither a URL nor a file that exists:\n{text}"
        )
    url = text if URL_RE.match(text) else "https://" + text
    return download(url, start=start, end=end, on_progress=on_progress,
                    cancel=cancel, max_height=max_height)


def title_slug(src: Source) -> str:
    return safe_name(src.title or Path(src.path).stem)
