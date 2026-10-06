"""System font discovery and text measurement.

Used for two things: offering a real font list in the UI, and measuring caption
text so we can wrap lines the same way libass will.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

from .paths import WORK_DIR

_CACHE_FILE = WORK_DIR / "fonts.json"
_CACHE_VERSION = 2
_index: dict[str, str] | None = None
_measure_cache: dict[tuple, float] = {}

# Sensible defaults to show first in the UI, if they exist on this machine.
PREFERRED = [
    "Arial Black", "Impact", "Segoe UI Black", "Bahnschrift", "Verdana",
    "Arial", "Tahoma", "Trebuchet MS", "Comic Sans MS", "Georgia",
    "Franklin Gothic Heavy", "Gill Sans MT", "Segoe UI", "Calibri",
]

# Fallback width factors (fraction of font size per character) when PIL cannot help.
_WIDTH_HINTS = {
    "impact": 0.46, "arial black": 0.63, "segoe ui black": 0.60,
    "bahnschrift": 0.50, "verdana": 0.62, "arial": 0.55, "tahoma": 0.54,
}


def _font_dirs() -> list[Path]:
    dirs: list[Path] = []
    if sys.platform == "win32":
        win = os.environ.get("SystemRoot") or r"C:\Windows"
        dirs.append(Path(win) / "Fonts")
        local = os.environ.get("LOCALAPPDATA")
        if local:
            dirs.append(Path(local) / "Microsoft" / "Windows" / "Fonts")
    else:
        dirs += [Path("/usr/share/fonts"), Path.home() / ".fonts"]
    return [d for d in dirs if d.is_dir()]


# Windows reports e.g. family="Arial", style="Black" for ariblk.ttf, but everyone
# (including fontconfig and libass) calls that font "Arial Black". These styles get
# their own entry; Bold/Italic variants do not, or the list becomes unusable.
_STANDALONE_STYLES = {
    "black", "heavy", "ultra", "ultrabold", "extrabold", "semibold", "demibold",
    "light", "extralight", "thin", "semilight", "medium", "narrow", "condensed",
    "narrow bold", "condensed bold", "condensed black",
}


def _scan() -> dict[str, str]:
    """Display name -> best file path (regular preferred, else anything)."""
    try:
        from PIL import ImageFont
    except ImportError:
        return {}
    found: dict[str, tuple[int, str]] = {}

    def offer(name: str, rank: int, path: str) -> None:
        cur = found.get(name)
        if cur is None or rank > cur[0]:
            found[name] = (rank, path)

    for d in _font_dirs():
        try:
            entries = list(d.glob("*.ttf")) + list(d.glob("*.otf")) + list(d.glob("*.ttc"))
        except OSError:
            continue
        for path in entries:
            try:
                family, style = ImageFont.truetype(str(path), 24).getname()
            except Exception:                                   # noqa: BLE001
                continue
            if not family:
                continue
            style_l = (style or "").strip().lower()
            if style_l in ("", "regular", "book"):
                rank = 3
            elif style_l == "bold":
                rank = 2
            elif "italic" in style_l or "oblique" in style_l:
                rank = 0
            else:
                rank = 1
            offer(family, rank, str(path))
            if style_l in _STANDALONE_STYLES:
                offer(f"{family} {style.strip()}", 4, str(path))
    return {k: v[1] for k, v in sorted(found.items())}


def index(refresh: bool = False) -> dict[str, str]:
    global _index
    if _index is not None and not refresh:
        return _index
    if not refresh and _CACHE_FILE.is_file():
        try:
            data = json.loads(_CACHE_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("fonts") \
                    and data.get("v") == _CACHE_VERSION \
                    and time.time() - float(data.get("at", 0)) < 30 * 86400:
                _index = {k: v for k, v in data["fonts"].items() if Path(v).is_file()}
                if _index:
                    return _index
        except (OSError, ValueError, TypeError):
            pass
    _index = _scan()
    try:
        WORK_DIR.mkdir(parents=True, exist_ok=True)
        _CACHE_FILE.write_text(
            json.dumps({"v": _CACHE_VERSION, "at": time.time(), "fonts": _index}),
            encoding="utf-8")
    except OSError:
        pass
    return _index


def available() -> list[str]:
    """Font families, with the caption-friendly ones floated to the top."""
    fams = index()
    top = [f for f in PREFERRED if f in fams]
    rest = [f for f in sorted(fams) if f not in top]
    return top + rest


def font_file(family: str) -> str | None:
    fams = index()
    if family in fams:
        return fams[family]
    low = family.strip().lower()
    for name, path in fams.items():
        if name.lower() == low:
            return path
    return None


def measure(text: str, family: str, size: int) -> float:
    """Width of `text` in pixels at `size`, as closely as we can predict."""
    if not text:
        return 0.0
    key = (text, family, size)
    hit = _measure_cache.get(key)
    if hit is not None:
        return hit
    width: float | None = None
    path = font_file(family)
    if path:
        try:
            from PIL import ImageFont
            font = ImageFont.truetype(path, size)
            width = float(font.getlength(text))
        except Exception:                                       # noqa: BLE001
            width = None
    if width is None:
        factor = _WIDTH_HINTS.get(family.strip().lower(), 0.58)
        width = len(text) * size * factor
    if len(_measure_cache) > 20000:
        _measure_cache.clear()
    _measure_cache[key] = width
    return width


def is_heavy(family: str) -> bool:
    """Does this family already look bold, so we should not fake-bold it?"""
    low = family.lower()
    return any(tok in low for tok in ("black", "impact", "heavy", "bold", "ultra", "fat"))
