"""Settings schema, defaults, normalisation and preset storage.

One dict drives the whole pipeline. The UI edits it, the renderer reads it.
"""
from __future__ import annotations

import copy
import json
import re
from typing import Any

from .paths import CONFIG_FILE, PRESET_FILE

# Default facecam box: a 305 px square in the bottom-left of a 1920x1080 canvas.
# It is rescaled to the real source, and the user drags it into place anyway.
REFERENCE_HEIGHT = 1080
DEFAULT_CAM_SIZE = 305

DEFAULTS: dict[str, Any] = {
    # ---- source -------------------------------------------------------
    "source": "",

    # clips = vertical highlight clips; clean = the whole stream, bleeped;
    # reel = hand-picked moments joined into one horizontal video
    "mode": "clips",

    # ---- highlight reel ----------------------------------------------
    "reel_min": 10.0,            # target length, minutes
    "reel_max": 15.0,
    "reel_moment_len": 40.0,     # typical length of one moment, seconds
    "reel_transition": "fade",   # fade | cut
    "game_name": "",             # for titles; guessed from the stream title if blank
    "hashtags": "#gaming",       # added to every description, after the game's own tag

    # ---- clean-up (monetisation) -------------------------------------
    "censor": True,
    "censor_action": "bleep",
    "censor_mild": False,
    "censor_extra": "",
    "censor_allow": "",
    "censor_captions": True,
    "censor_tone": 1000,
    "censor_gain": 0.25,
    "censor_pad": 0.06,
    "clean_model": "base",

    # ---- highlight detection -----------------------------------------
    "clip_count": 5,
    "clip_min": 18.0,
    "clip_max": 45.0,
    "target_len": 30.0,
    "sensitivity": 0.5,
    "use_audio": True,
    "use_chat": True,
    "use_speech": True,
    "skip_intro": 0.0,
    "skip_outro": 0.0,
    # Which stretch of the video to search, in seconds. range_end 0 means "to the end".
    "range_start": 0.0,
    "range_end": 0.0,
    "lead_in": 1.5,
    "whole_video": False,
    "manual_clips": [],

    # ---- layout -------------------------------------------------------
    "out_w": 1080,
    "out_h": 1920,
    "fps": 30,
    "top_ratio": 0.42,

    "cam_enabled": True,
    "cam": {"x": 0, "y": 775, "w": DEFAULT_CAM_SIZE, "h": DEFAULT_CAM_SIZE},
    "cam_fit": "fit",
    "cam_scale": 1.0,
    "cam_anchor": "center",
    "cam_offset_x": 0,
    "cam_offset_y": 0,
    "cam_flip": False,

    "top_bg": "blur",
    "top_bg_color": "#0d0d16",
    "top_bg_image": "",
    "top_blur": 26,
    "top_dim": 0.30,

    "game": {"x": 0, "y": 0, "w": 0, "h": 0},
    "game_fit": "fill",
    "game_focus_x": 0.5,
    "game_focus_y": 0.5,
    "game_zoom": 1.0,

    "divider": 0,
    "divider_color": "#000000",

    # ---- captions -----------------------------------------------------
    "captions": True,
    "whisper_model": "small",
    "language": "auto",
    "cap_style": "karaoke",
    "cap_font": "Arial Black",
    "cap_size": 76,
    "cap_color": "#FFFFFF",
    "cap_highlight": "#FFE23D",
    "cap_outline_color": "#000000",
    "cap_outline": 6,
    "cap_shadow": 3,
    "cap_pos": 0.56,
    "cap_max_words": 4,
    "cap_uppercase": True,
    "cap_margin": 0.09,
    "cap_pop": True,

    # ---- output -------------------------------------------------------
    "encoder": "auto",
    "quality": 20,
    "audio_norm": True,
    "out_folder": "",
}

CAM_FITS = ("fit", "fill")
GAME_FITS = ("fill", "fit")
ANCHORS = ("left", "center", "right")
TOP_BGS = ("blur", "color", "image")
CAP_STYLES = ("karaoke", "pop", "oneword", "plain")
WHISPER_MODELS = ("tiny", "base", "small", "medium", "large-v3")
MODES = ("clips", "clean", "reel")
TRANSITIONS = ("fade", "cut")
CENSOR_ACTIONS = ("bleep", "mute")

_HEX_RE = re.compile(r"^#?([0-9a-fA-F]{6})$")


def _clamp(v: Any, lo: float, hi: float, fallback: float) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return fallback
    if f != f:  # NaN
        return fallback
    return max(lo, min(hi, f))


def _clamp_int(v: Any, lo: int, hi: int, fallback: int) -> int:
    return int(round(_clamp(v, lo, hi, fallback)))


def _bool(v: Any, fallback: bool) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    if isinstance(v, (int, float)):
        return bool(v)
    return fallback


def _choice(v: Any, options: tuple[str, ...], fallback: str) -> str:
    if isinstance(v, str) and v.lower() in options:
        return v.lower()
    return fallback


def hex_color(v: Any, fallback: str = "#000000") -> str:
    if isinstance(v, str):
        m = _HEX_RE.match(v.strip())
        if m:
            return "#" + m.group(1).upper()
    return fallback.upper()


def hex_to_ass(color: str, alpha: int = 0) -> str:
    """#RRGGBB -> ASS &HAABBGGRR& (ASS stores colours byte-reversed)."""
    c = hex_color(color)
    r, g, b = c[1:3], c[3:5], c[5:7]
    return f"&H{alpha:02X}{b}{g}{r}"


def default_cam_rect(src_w: int, src_h: int) -> dict[str, int]:
    """Default facecam box: 305x305 in the bottom-left, scaled to this source."""
    scale = (src_h / REFERENCE_HEIGHT) if src_h else 1.0
    size = max(16, int(round(DEFAULT_CAM_SIZE * scale)))
    size = min(size, src_w or size, src_h or size)
    return {"x": 0, "y": max(0, (src_h or size) - size), "w": size, "h": size}


def clamp_rect(rect: dict[str, Any], src_w: int, src_h: int,
               fallback: dict[str, int] | None = None) -> dict[str, int]:
    """Keep a crop rect inside the frame and on even pixel boundaries."""
    fb = fallback or {"x": 0, "y": 0, "w": src_w, "h": src_h}
    if not isinstance(rect, dict):
        return dict(fb)
    if not src_w or not src_h:
        return {
            "x": max(0, int(_clamp(rect.get("x"), 0, 1 << 20, fb["x"]))),
            "y": max(0, int(_clamp(rect.get("y"), 0, 1 << 20, fb["y"]))),
            "w": max(2, int(_clamp(rect.get("w"), 2, 1 << 20, fb["w"]))),
            "h": max(2, int(_clamp(rect.get("h"), 2, 1 << 20, fb["h"]))),
        }
    # Width/height stay exactly as asked (a 305px overlay should crop 305px);
    # only the offsets are snapped to even pixels to keep chroma aligned.
    w = _clamp_int(rect.get("w", fb["w"]), 8, src_w, fb["w"])
    h = _clamp_int(rect.get("h", fb["h"]), 8, src_h, fb["h"])
    x = _clamp_int(rect.get("x", fb["x"]), 0, max(0, src_w - w), fb["x"])
    y = _clamp_int(rect.get("y", fb["y"]), 0, max(0, src_h - h), fb["y"])
    x -= x % 2
    y -= y % 2
    return {"x": x, "y": y, "w": max(w, 8), "h": max(h, 8)}


def normalize(raw: dict[str, Any] | None, src_w: int = 0, src_h: int = 0) -> dict[str, Any]:
    """Merge user input over the defaults and clamp everything into range."""
    s = copy.deepcopy(DEFAULTS)
    raw = raw or {}
    for k, v in raw.items():
        if k in s:
            s[k] = v

    s["source"] = str(s["source"] or "").strip()
    s["mode"] = _choice(s["mode"], MODES, "clips")

    s["censor"] = _bool(s["censor"], True)
    s["censor_action"] = _choice(s["censor_action"], CENSOR_ACTIONS, "bleep")
    s["censor_mild"] = _bool(s["censor_mild"], False)
    s["censor_extra"] = str(s["censor_extra"] or "")[:4000]
    s["censor_allow"] = str(s["censor_allow"] or "")[:4000]
    s["censor_captions"] = _bool(s["censor_captions"], True)
    s["censor_tone"] = _clamp_int(s["censor_tone"], 200, 4000, 1000)
    s["censor_gain"] = _clamp(s["censor_gain"], 0.02, 1.0, 0.25)
    s["censor_pad"] = _clamp(s["censor_pad"], 0.0, 0.5, 0.06)
    s["clean_model"] = _choice(s["clean_model"], WHISPER_MODELS, "base")

    s["reel_min"] = _clamp(s["reel_min"], 1.0, 120.0, 10.0)
    s["reel_max"] = _clamp(s["reel_max"], s["reel_min"], 180.0, max(15.0, s["reel_min"]))
    s["reel_moment_len"] = _clamp(s["reel_moment_len"], 10.0, 180.0, 40.0)
    s["reel_transition"] = _choice(s["reel_transition"], TRANSITIONS, "fade")
    s["game_name"] = str(s["game_name"] or "").strip()[:80]
    s["hashtags"] = str(s["hashtags"] if s["hashtags"] is not None else "").strip()[:200]

    s["clip_count"] = _clamp_int(s["clip_count"], 1, 30, 5)
    s["clip_min"] = _clamp(s["clip_min"], 5, 300, 18.0)
    s["clip_max"] = _clamp(s["clip_max"], s["clip_min"], 600, max(45.0, s["clip_min"]))
    s["target_len"] = _clamp(s["target_len"], s["clip_min"], s["clip_max"], 30.0)
    s["sensitivity"] = _clamp(s["sensitivity"], 0.0, 1.0, 0.5)
    for key in ("use_audio", "use_chat", "use_speech", "whole_video"):
        s[key] = _bool(s[key], DEFAULTS[key])
    s["skip_intro"] = _clamp(s["skip_intro"], 0, 86400, 0.0)
    s["skip_outro"] = _clamp(s["skip_outro"], 0, 86400, 0.0)
    s["range_start"] = _clamp(s["range_start"], 0, 172800, 0.0)
    s["range_end"] = _clamp(s["range_end"], 0, 172800, 0.0)
    if s["range_end"] and s["range_end"] <= s["range_start"]:
        s["range_end"] = 0.0
    s["lead_in"] = _clamp(s["lead_in"], 0, 15, 1.5)

    manual = []
    for item in (s["manual_clips"] or []):
        try:
            a = float(item.get("start", 0))
            b = float(item.get("end", 0))
        except (AttributeError, TypeError, ValueError):
            continue
        if b > a >= 0:
            manual.append({"start": round(a, 3), "end": round(b, 3),
                           "label": str(item.get("label") or "")[:80]})
    manual.sort(key=lambda c: c["start"])
    s["manual_clips"] = manual

    s["out_w"] = _clamp_int(s["out_w"], 240, 2160, 1080)
    s["out_h"] = _clamp_int(s["out_h"], 240, 3840, 1920)
    s["out_w"] -= s["out_w"] % 2
    s["out_h"] -= s["out_h"] % 2
    s["fps"] = _clamp_int(s["fps"], 15, 120, 30)
    s["top_ratio"] = _clamp(s["top_ratio"], 0.15, 0.85, 0.42)

    s["cam_enabled"] = _bool(s["cam_enabled"], True)
    # The stored default is measured on a 1080p canvas. If the caller did not give
    # us a box of their own, rescale it to whatever this source actually is.
    if "cam" not in raw and src_h:
        s["cam"] = default_cam_rect(src_w, src_h)
    s["cam"] = clamp_rect(s["cam"], src_w, src_h,
                          default_cam_rect(src_w, src_h) if src_h else None)
    s["cam_fit"] = _choice(s["cam_fit"], CAM_FITS, "fit")
    s["cam_scale"] = _clamp(s["cam_scale"], 0.2, 2.0, 1.0)
    s["cam_anchor"] = _choice(s["cam_anchor"], ANCHORS, "center")
    s["cam_offset_x"] = _clamp_int(s["cam_offset_x"], -2000, 2000, 0)
    s["cam_offset_y"] = _clamp_int(s["cam_offset_y"], -2000, 2000, 0)
    s["cam_flip"] = _bool(s["cam_flip"], False)

    s["top_bg"] = _choice(s["top_bg"], TOP_BGS, "blur")
    s["top_bg_color"] = hex_color(s["top_bg_color"], "#0D0D16")
    s["top_bg_image"] = str(s["top_bg_image"] or "")
    s["top_blur"] = _clamp_int(s["top_blur"], 0, 80, 26)
    s["top_dim"] = _clamp(s["top_dim"], 0.0, 0.9, 0.30)

    full = {"x": 0, "y": 0, "w": src_w, "h": src_h}
    g = s["game"] if isinstance(s["game"], dict) else {}
    if not (g.get("w") and g.get("h")) and src_w and src_h:
        s["game"] = full
    else:
        s["game"] = clamp_rect(g, src_w, src_h, full if src_w else None)
    s["game_fit"] = _choice(s["game_fit"], GAME_FITS, "fill")
    s["game_focus_x"] = _clamp(s["game_focus_x"], 0.0, 1.0, 0.5)
    s["game_focus_y"] = _clamp(s["game_focus_y"], 0.0, 1.0, 0.5)
    s["game_zoom"] = _clamp(s["game_zoom"], 1.0, 3.0, 1.0)

    s["divider"] = _clamp_int(s["divider"], 0, 40, 0)
    s["divider_color"] = hex_color(s["divider_color"], "#000000")

    s["captions"] = _bool(s["captions"], True)
    s["whisper_model"] = _choice(s["whisper_model"], WHISPER_MODELS, "small")
    s["language"] = (str(s["language"] or "auto").strip().lower() or "auto")[:12]
    s["cap_style"] = _choice(s["cap_style"], CAP_STYLES, "karaoke")
    s["cap_font"] = (str(s["cap_font"] or "Arial Black").strip() or "Arial Black")[:64]
    s["cap_size"] = _clamp_int(s["cap_size"], 20, 200, 76)
    s["cap_color"] = hex_color(s["cap_color"], "#FFFFFF")
    s["cap_highlight"] = hex_color(s["cap_highlight"], "#FFE23D")
    s["cap_outline_color"] = hex_color(s["cap_outline_color"], "#000000")
    s["cap_outline"] = _clamp_int(s["cap_outline"], 0, 20, 6)
    s["cap_shadow"] = _clamp_int(s["cap_shadow"], 0, 20, 3)
    s["cap_pos"] = _clamp(s["cap_pos"], 0.05, 0.95, 0.56)
    s["cap_max_words"] = _clamp_int(s["cap_max_words"], 1, 12, 4)
    s["cap_uppercase"] = _bool(s["cap_uppercase"], True)
    s["cap_margin"] = _clamp(s["cap_margin"], 0.0, 0.35, 0.09)
    s["cap_pop"] = _bool(s["cap_pop"], True)

    s["encoder"] = str(s["encoder"] or "auto").strip().lower()[:24] or "auto"
    s["quality"] = _clamp_int(s["quality"], 14, 32, 20)
    s["audio_norm"] = _bool(s["audio_norm"], True)
    s["out_folder"] = str(s["out_folder"] or "")

    return s


# ---------------------------------------------------------------------------
# persistence


def _read_json(path, fallback):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return fallback


def _write_json(path, data) -> None:
    tmp = str(path) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    try:
        import os
        os.replace(tmp, path)
    except OSError:
        pass


def load_last() -> dict[str, Any]:
    return _read_json(CONFIG_FILE, {}) or {}


# Settings that only make sense for the video they were set on, so they are not
# carried over to the next one.
_PER_VIDEO = ("source", "manual_clips", "range_start", "range_end", "has_audio", "game_name")


def save_last(settings: dict[str, Any]) -> None:
    keep = {k: v for k, v in settings.items() if k not in _PER_VIDEO}
    _write_json(CONFIG_FILE, keep)


def load_presets() -> dict[str, Any]:
    data = _read_json(PRESET_FILE, {})
    return data if isinstance(data, dict) else {}


def save_preset(name: str, settings: dict[str, Any]) -> dict[str, Any]:
    name = (name or "").strip()[:60]
    if not name:
        raise ValueError("preset needs a name")
    presets = load_presets()
    presets[name] = {k: v for k, v in settings.items() if k not in _PER_VIDEO}
    _write_json(PRESET_FILE, presets)
    return presets


def delete_preset(name: str) -> dict[str, Any]:
    presets = load_presets()
    presets.pop(name, None)
    _write_json(PRESET_FILE, presets)
    return presets
