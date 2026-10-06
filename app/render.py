"""Compositing a 16:9 stream into a vertical short.

Layout, top to bottom:
    [ top panel ]  the facecam / PNGTuber crop, on a background (blurred stream / colour / image)
    [ divider   ]  optional solid bar
    [ bottom    ]  the gameplay, cropped or letterboxed to fill
Captions are burned on last, over the whole frame.
"""
from __future__ import annotations

import shutil
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

from . import censor
from .ffmpeg_tools import Cancelled, list_encoders, run_ffmpeg
from .settings import hex_color


@dataclass
class Panels:
    out_w: int
    out_h: int
    top_h: int
    divider: int
    bottom_h: int


def _even(n: float, lo: int = 2) -> int:
    v = int(round(n))
    v -= v % 2
    return max(lo, v)


def panel_geometry(settings: dict[str, Any]) -> Panels:
    ow = int(settings["out_w"])
    oh = int(settings["out_h"])
    div = int(settings.get("divider", 0))
    if not settings.get("cam_enabled", True):
        return Panels(ow, oh, 0, 0, oh)
    div = _even(div, 0) if div else 0
    top = _even(oh * float(settings["top_ratio"]))
    bottom = oh - top - div
    if bottom % 2:
        bottom -= 1
        div += 1
    if bottom < 40:
        bottom = 40
        top = oh - bottom - div
    return Panels(ow, oh, top, div, bottom)


def _blur_chain(src: str, dst: str, w: int, h: int, sigma: int, dim: float) -> str:
    """Blur cheaply: shrink hard, blur the small image, grow back."""
    if sigma <= 0:
        chain = (f"[{src}]scale={w}:{h}:force_original_aspect_ratio=increase:flags=area,"
                 f"crop={w}:{h}")
    else:
        dw = _even(w / 4)
        dh = _even(h / 4)
        chain = (
            f"[{src}]scale={dw}:{dh}:force_original_aspect_ratio=increase:flags=area,"
            f"crop={dw}:{dh},gblur=sigma={max(1.0, sigma / 4):.2f}:steps=3,"
            f"scale={w}:{h}:flags=bilinear"
        )
    if dim > 0.001:
        chain += f",eq=brightness={-dim:.3f}:saturation={max(0.2, 1.0 - dim * 0.5):.3f}"
    return chain + f"[{dst}]"


def cover_crop(src_rect: tuple[int, int, int, int], panel_w: int, panel_h: int,
               zoom: float, focus_x: float, focus_y: float) -> tuple[int, int, int, int]:
    """Source-space rectangle that, scaled straight to the panel, fills it.

    Doing the crop before the scale means swscale only ever touches the pixels
    that survive, instead of resizing the whole frame and throwing half away.
    """
    gx, gy, gw, gh = src_rect
    scale = max(panel_w / gw, panel_h / gh) * max(zoom, 1e-6)
    cw = min(gw, max(2, int(round(panel_w / scale))))
    ch = min(gh, max(2, int(round(panel_h / scale))))
    ox = gx + int(round((gw - cw) * min(max(focus_x, 0.0), 1.0)))
    oy = gy + int(round((gh - ch) * min(max(focus_y, 0.0), 1.0)))
    return ox - ox % 2, oy - oy % 2, cw, ch


def build_filter(
    settings: dict[str, Any],
    src_w: int,
    src_h: int,
    *,
    subs_file: str | None = None,
    bg_input_index: int | None = None,
    duration: float | None = None,
) -> tuple[str, str]:
    """Return (filter_complex, output_label)."""
    p = panel_geometry(settings)
    fps = int(settings.get("fps", 30))
    parts: list[str] = []
    # Generated sources run forever unless told otherwise.
    span = f":d={duration + 2:.3f}" if duration else ""

    cam_on = bool(settings.get("cam_enabled", True)) and p.top_h > 0
    top_bg_mode = settings.get("top_bg", "blur")
    game_fit = settings.get("game_fit", "fill")

    needed = 1                                   # game
    if cam_on:
        needed += 1                              # cam crop
        if top_bg_mode == "blur":
            needed += 1
    if game_fit == "fit":
        needed += 1

    labels = [f"src{i}" for i in range(needed)]
    split = "".join(f"[{l}]" for l in labels)
    parts.append(f"[0:v]fps={fps},setsar=1,split={needed}{split}")
    pool = list(labels)

    def take() -> str:
        return pool.pop(0)

    stack_inputs: list[str] = []

    # ---------------- top panel ----------------------------------------
    if cam_on:
        cam = settings["cam"]
        cx = max(0, min(int(cam["x"]), max(0, src_w - 2)))
        cy = max(0, min(int(cam["y"]), max(0, src_h - 2)))
        cw = max(2, min(int(cam["w"]), src_w - cx))
        ch = max(2, min(int(cam["h"]), src_h - cy))

        if top_bg_mode == "blur":
            parts.append(_blur_chain(take(), "topbg", p.out_w, p.top_h,
                                     int(settings.get("top_blur", 26)),
                                     float(settings.get("top_dim", 0.3))))
        elif top_bg_mode == "image" and bg_input_index is not None:
            parts.append(
                f"[{bg_input_index}:v]scale={p.out_w}:{p.top_h}:"
                f"force_original_aspect_ratio=increase:flags=bicubic,"
                f"crop={p.out_w}:{p.top_h},format=yuv420p[topbg]"
            )
        else:
            col = hex_color(settings.get("top_bg_color", "#0D0D16")).lstrip("#")
            parts.append(
                f"color=c=0x{col}:s={p.out_w}x{p.top_h}:r={fps}{span},format=yuv420p[topbg]"
            )

        scale = float(settings.get("cam_scale", 1.0))
        if settings.get("cam_fit", "fit") == "fill":
            fx, fy, fw, fh = cover_crop((cx, cy, cw, ch), p.out_w, p.top_h, 1.0, 0.5, 0.5)
            tw, th = _even(p.out_w * scale), _even(p.top_h * scale)
            cam_chain = (f"[{take()}]crop={fw}:{fh}:{fx}:{fy},"
                         f"scale={tw}:{th}:flags=lanczos")
            if scale > 1.001:
                cam_chain += f",crop={p.out_w}:{p.top_h}"
        else:
            tw, th = _even(p.out_w * scale), _even(p.top_h * scale)
            cam_chain = (f"[{take()}]crop={cw}:{ch}:{cx}:{cy},"
                         f"scale={tw}:{th}:force_original_aspect_ratio=decrease:"
                         f"flags=lanczos")
        if settings.get("cam_flip"):
            cam_chain += ",hflip"
        parts.append(cam_chain + "[cam]")

        anchor = settings.get("cam_anchor", "center")
        base_x = {"left": "0", "center": "(W-w)/2", "right": "(W-w)"}.get(anchor, "(W-w)/2")
        ox = int(settings.get("cam_offset_x", 0))
        oy = int(settings.get("cam_offset_y", 0))
        parts.append(
            f"[topbg][cam]overlay=x={base_x}{ox:+d}:y=(H-h)/2{oy:+d}:"
            f"shortest=0:format=auto,format=yuv420p[top]"
        )
        stack_inputs.append("top")

    if p.divider:
        col = hex_color(settings.get("divider_color", "#000000")).lstrip("#")
        parts.append(
            f"color=c=0x{col}:s={p.out_w}x{p.divider}:r={fps}{span},format=yuv420p[div]"
        )
        stack_inputs.append("div")

    # ---------------- bottom panel -------------------------------------
    game = settings.get("game") or {}
    gw = int(game.get("w") or src_w) or src_w
    gh = int(game.get("h") or src_h) or src_h
    gx = max(0, min(int(game.get("x") or 0), max(0, src_w - 2)))
    gy = max(0, min(int(game.get("y") or 0), max(0, src_h - 2)))
    gw = max(2, min(gw, src_w - gx))
    gh = max(2, min(gh, src_h - gy))
    crop_src = (gx, gy, gw, gh) != (0, 0, src_w, src_h)

    zoom = float(settings.get("game_zoom", 1.0))
    fx = float(settings.get("game_focus_x", 0.5))
    fy = float(settings.get("game_focus_y", 0.5))

    if game_fit == "fit":
        parts.append(_blur_chain(take(), "gamebg", p.out_w, p.bottom_h,
                                 int(settings.get("top_blur", 26)), 0.35))
        chain = f"[{take()}]"
        if crop_src:
            chain += f"crop={gw}:{gh}:{gx}:{gy},"
        tw, th = _even(p.out_w * zoom), _even(p.bottom_h * zoom)
        chain += (f"scale={tw}:{th}:force_original_aspect_ratio=decrease:flags=lanczos"
                  f"[gamefg]")
        parts.append(chain)
        parts.append(
            f"[gamebg][gamefg]overlay=x=(W-w)/2:y=(H-h)/2:format=auto,"
            f"format=yuv420p[bottom]"
        )
    else:
        ox, oy, ow_, oh_ = cover_crop((gx, gy, gw, gh), p.out_w, p.bottom_h, zoom, fx, fy)
        parts.append(
            f"[{take()}]crop={ow_}:{oh_}:{ox}:{oy},"
            f"scale={p.out_w}:{p.bottom_h}:flags=lanczos,format=yuv420p[bottom]"
        )
    stack_inputs.append("bottom")

    # ---------------- stack + captions ---------------------------------
    if len(stack_inputs) == 1:
        current = stack_inputs[0]
    else:
        ins = "".join(f"[{s}]" for s in stack_inputs)
        parts.append(f"{ins}vstack=inputs={len(stack_inputs)}[stacked]")
        current = "stacked"

    if subs_file:
        safe = subs_file.replace("\\", "/").replace(":", "\\:").replace("'", "\\'")
        parts.append(f"[{current}]subtitles=filename='{safe}':alpha=1[subbed]")
        current = "subbed"

    parts.append(f"[{current}]format=yuv420p[vout]")
    return ";".join(parts), "vout"


def pick_encoder(settings: dict[str, Any]) -> list[str]:
    choice = (settings.get("encoder") or "auto").lower()
    q = int(settings.get("quality", 20))
    if choice in ("auto", "libx264", "x264", "cpu"):
        return ["-c:v", "libx264", "-preset", "veryfast", "-crf", str(q),
                "-profile:v", "high", "-level", "4.2"]
    have = list_encoders()
    if choice in ("h264_amf", "amd", "gpu") and "h264_amf" in have:
        qp = max(10, min(40, q + 2))
        return ["-c:v", "h264_amf", "-usage", "transcoding", "-quality", "quality",
                "-rc", "cqp", "-qp_i", str(qp), "-qp_p", str(qp), "-qp_b", str(qp)]
    if choice in ("h264_nvenc", "nvidia") and "h264_nvenc" in have:
        return ["-c:v", "h264_nvenc", "-preset", "p5", "-rc", "vbr",
                "-cq", str(q), "-b:v", "0"]
    if choice in ("hevc", "libx265") and "libx265" in have:
        return ["-c:v", "libx265", "-preset", "medium", "-crf", str(q + 2),
                "-tag:v", "hvc1"]
    return ["-c:v", "libx264", "-preset", "veryfast", "-crf", str(q)]


def filter_args(graph: str, cwd: Path) -> list[str]:
    """Pass a filtergraph inline, or via a file once it gets long.

    A heavily bleeped stream produces a graph far past the 32k-character Windows
    command line. This ffmpeg reads an option's value from a file with the `-/`
    prefix (the old -filter_complex_script is gone).
    """
    if len(graph) < 6000:
        return ["-filter_complex", graph]
    path = Path(cwd) / f"graph_{uuid.uuid4().hex[:8]}.txt"
    path.write_text(graph, encoding="utf-8")
    return ["-/filter_complex", str(path)]


def _censor_settings(settings: dict[str, Any]) -> dict[str, Any]:
    return {
        "action": settings.get("censor_action", "bleep"),
        "tone_hz": int(settings.get("censor_tone", 1000)),
        "gain": float(settings.get("censor_gain", 0.25)),
    }


def render_clip(
    src: str | Path,
    dest: str | Path,
    *,
    start: float,
    duration: float,
    settings: dict[str, Any],
    src_w: int,
    src_h: int,
    subs_path: str | Path | None = None,
    has_audio: bool = True,
    bleep_spans: Sequence[tuple[float, float]] | None = None,
    workdir: str | Path | None = None,
    on_progress: Callable[[float], None] | None = None,
    cancel: threading.Event | None = None,
) -> str:
    """Cut one clip out of the source and render the vertical composite.

    `bleep_spans` are clip-relative seconds to censor.
    """
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    cwd = Path(workdir) if workdir else dest.parent
    cwd.mkdir(parents=True, exist_ok=True)

    subs_name = None
    if subs_path:
        subs = Path(subs_path)
        if subs.is_file() and subs.stat().st_size > 0:
            if subs.parent.resolve() != cwd.resolve():
                shutil.copy2(subs, cwd / subs.name)
            subs_name = subs.name

    bg_index = None
    length = max(0.2, duration)
    inputs: list[str] = ["-ss", f"{max(0.0, start):.3f}", "-t", f"{length:.3f}",
                         "-i", str(src)]
    bg_image = settings.get("top_bg_image") or ""
    if settings.get("top_bg") == "image" and bg_image and Path(bg_image).is_file():
        inputs += ["-i", str(bg_image)]
        bg_index = 1

    graph, out_label = build_filter(settings, src_w, src_h, subs_file=subs_name,
                                    bg_input_index=bg_index, duration=length)

    args = list(inputs)
    if has_audio:
        graph += ";" + censor.audio_filter(
            list(bleep_spans or []), length, in_label="0:a:0", out_label="aout",
            loudnorm=bool(settings.get("audio_norm", True)),
            **_censor_settings(settings))
        args += filter_args(graph, cwd)
        args += ["-map", f"[{out_label}]", "-map", "[aout]",
                 "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2"]
    else:
        args += filter_args(graph, cwd)
        args += ["-map", f"[{out_label}]", "-an"]
    args += pick_encoder(settings)
    # The output-side -t is what actually bounds the render: colour and divider
    # sources in the graph are endless, so without it a clip never finishes.
    args += ["-r", str(int(settings.get("fps", 30))), "-t", f"{length:.3f}",
             "-movflags", "+faststart", str(dest)]

    run_ffmpeg(args, total_seconds=duration, on_progress=on_progress,
               cancel=cancel, cwd=cwd, log_name="render")
    return str(dest)


def render_clean(
    src: str | Path,
    dest: str | Path,
    *,
    start: float,
    duration: float,
    full_length: float,
    settings: dict[str, Any],
    spans: Sequence[tuple[float, float]],
    has_audio: bool = True,
    workdir: str | Path | None = None,
    on_progress: Callable[[float], None] | None = None,
    cancel: threading.Event | None = None,
) -> str:
    """The whole stream (or the chosen part), picture untouched, swearing bleeped.

    Video is stream-copied, so a 2-hour VOD costs seconds rather than hours; only
    the audio is re-encoded. Only the first audio track is kept - copying the
    others (OBS multi-track recordings) would put the uncensored audio back in
    the file. With a trimmed range the cut snaps to the nearest keyframe.

    Falls back to .mkv if the source codecs will not go in an .mp4.
    """
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    cwd = Path(workdir) if workdir else dest.parent
    cwd.mkdir(parents=True, exist_ok=True)

    trimmed = start > 0.05 or duration < full_length - 0.05
    inputs: list[str] = []
    if start > 0.05:
        inputs += ["-ss", f"{start:.3f}"]
    if trimmed:
        inputs += ["-t", f"{duration:.3f}"]
    inputs += ["-i", str(src)]

    def attempt(target: Path) -> str:
        args = list(inputs)
        if has_audio and spans:
            graph = censor.audio_filter(list(spans), duration, in_label="0:a:0",
                                        out_label="aout", **_censor_settings(settings))
            args += filter_args(graph, cwd)
            args += ["-map", "0:v:0", "-map", "[aout]", "-c:v", "copy",
                     "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2"]
        elif has_audio:
            # Nothing to bleep: keep the original audio bit-for-bit.
            args += ["-map", "0:v:0", "-map", "0:a:0", "-c", "copy"]
        else:
            args += ["-map", "0:v:0", "-c:v", "copy", "-an"]
        args += ["-avoid_negative_ts", "make_zero"]
        if target.suffix.lower() == ".mp4":
            args += ["-movflags", "+faststart"]
        args += [str(target)]
        run_ffmpeg(args, total_seconds=duration, on_progress=on_progress,
                   cancel=cancel, cwd=cwd, log_name="clean render")
        return str(target)

    try:
        return attempt(dest.with_suffix(".mp4"))
    except Cancelled:
        raise
    except RuntimeError:
        dest.with_suffix(".mp4").unlink(missing_ok=True)
        return attempt(dest.with_suffix(".mkv"))


def render_preview(
    src: str | Path,
    dest: str | Path,
    *,
    timestamp: float,
    settings: dict[str, Any],
    src_w: int,
    src_h: int,
    subs_path: str | Path | None = None,
    workdir: str | Path | None = None,
) -> str:
    """One composited still, so the UI can show exactly what will be produced."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    cwd = Path(workdir) if workdir else dest.parent
    cwd.mkdir(parents=True, exist_ok=True)

    subs_name = None
    if subs_path:
        subs = Path(subs_path)
        if subs.is_file():
            if subs.parent.resolve() != cwd.resolve():
                shutil.copy2(subs, cwd / subs.name)
            subs_name = subs.name

    bg_index = None
    args = ["-ss", f"{max(0.0, timestamp):.3f}", "-i", str(src)]
    bg_image = settings.get("top_bg_image") or ""
    if settings.get("top_bg") == "image" and bg_image and Path(bg_image).is_file():
        args += ["-i", str(bg_image)]
        bg_index = 1

    graph, out_label = build_filter(settings, src_w, src_h,
                                    subs_file=subs_name, bg_input_index=bg_index)
    args += ["-filter_complex", graph, "-map", f"[{out_label}]",
             "-frames:v", "1", "-q:v", "3", str(dest)]
    run_ffmpeg(args, cwd=cwd, log_name="preview")
    return str(dest)


def make_thumbnail(video: str | Path, dest: str | Path, at: float = 1.0,
                   width: int = 360) -> str:
    run_ffmpeg(
        ["-ss", f"{max(0.0, at):.3f}", "-i", str(video), "-frames:v", "1",
         "-vf", f"scale={width}:-2:flags=bicubic", "-q:v", "4", str(dest)],
        log_name="thumbnail",
    )
    return str(dest)
