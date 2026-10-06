"""Filesystem layout for the app. Everything lives under the project root."""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

WEB_DIR = ROOT / "app" / "web"
WORK_DIR = ROOT / "work"
SOURCE_DIR = WORK_DIR / "sources"
JOB_DIR = WORK_DIR / "jobs"
OUTPUT_DIR = ROOT / "output"
ASSET_DIR = ROOT / "assets"
MODELS_DIR = ROOT / "models"
CONFIG_FILE = ROOT / "config.json"
PRESET_FILE = ROOT / "presets.json"


def ensure_dirs() -> None:
    for d in (WORK_DIR, SOURCE_DIR, JOB_DIR, OUTPUT_DIR, ASSET_DIR):
        d.mkdir(parents=True, exist_ok=True)


def safe_name(text: str, maxlen: int = 60) -> str:
    """Turn an arbitrary title into something safe for a Windows path."""
    keep = []
    for ch in text.strip():
        if ch.isalnum() or ch in " -_.":
            keep.append(ch)
        elif ch in "/\\:":
            keep.append("-")
    out = "".join(keep).strip(" .-_")
    out = " ".join(out.split())
    if not out:
        out = "clip"
    return out[:maxlen].rstrip(" .-_") or "clip"


def rel_to_root(p: os.PathLike | str) -> str:
    try:
        return str(Path(p).resolve().relative_to(ROOT)).replace("\\", "/")
    except ValueError:
        return str(p).replace("\\", "/")
