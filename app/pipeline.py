"""End-to-end orchestration: stream in, finished vertical clips out."""
from __future__ import annotations

import json
import re
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from . import captions as caps
from . import censor, detect, render, source as src_mod, titles, transcribe
from . import settings as S
from .ffmpeg_tools import extract_frame, probe, run_ffmpeg
from .jobs import Job
from .paths import JOB_DIR, OUTPUT_DIR, SOURCE_DIR, WORK_DIR, safe_name

_SOURCES: dict[str, dict[str, Any]] = {}
_SOURCES_FILE = WORK_DIR / "sources.json"
_source_lock = threading.Lock()
_frame_lock = threading.Lock()
preview_lock = threading.Lock()


# ---------------------------------------------------------------------------
# source registry


def _load_registry() -> None:
    if _SOURCES:
        return
    try:
        data = json.loads(_SOURCES_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            for k, v in data.items():
                if isinstance(v, dict) and Path(v.get("path", "")).is_file():
                    _SOURCES[k] = v
    except (OSError, ValueError):
        pass


def _save_registry() -> None:
    try:
        WORK_DIR.mkdir(parents=True, exist_ok=True)
        _SOURCES_FILE.write_text(json.dumps(_SOURCES, indent=2), encoding="utf-8")
    except OSError:
        pass


def source_id_for(path: str) -> str:
    import hashlib
    return hashlib.sha1(str(Path(path).resolve()).lower().encode("utf-8")).hexdigest()[:12]


def register_source(s: src_mod.Source) -> dict[str, Any]:
    with _source_lock:
        _load_registry()
        sid = source_id_for(s.path)
        rec = s.to_dict()
        rec["id"] = sid
        rec["registered"] = time.time()
        rec["default_cam"] = S.default_cam_rect(s.width, s.height)
        _SOURCES[sid] = rec
        _save_registry()
        return rec


def get_source(sid: str) -> dict[str, Any] | None:
    with _source_lock:
        _load_registry()
        rec = _SOURCES.get(sid)
    if rec and not Path(rec.get("path", "")).is_file():
        return None
    return rec


def list_sources(limit: int = 12) -> list[dict[str, Any]]:
    with _source_lock:
        _load_registry()
        rows = [r for r in _SOURCES.values() if Path(r.get("path", "")).is_file()]
    rows.sort(key=lambda r: -float(r.get("registered", 0)))
    return rows[:limit]


# ---------------------------------------------------------------------------
# jobs


def prepare_source(job: Job, text: str, *, start: float = 0.0, end: float = 0.0,
                   max_height: int = 1080) -> dict[str, Any]:
    """Download / locate a stream and register it, ready for editing."""
    job.set(stage="source", progress=0.02, message="Looking up source")

    def prog(frac: float, msg: str) -> None:
        job.raise_if_cancelled()
        job.set(progress=0.02 + 0.9 * max(0.0, min(1.0, frac)), message=msg)

    s = src_mod.resolve(text, start=start, end=end, on_progress=prog,
                        cancel=job.cancel_event, max_height=max_height)
    job.raise_if_cancelled()
    rec = register_source(s)
    job.set(progress=0.95, message="Grabbing a frame")
    try:
        frame_path(rec["id"], max(1.0, s.duration * 0.15))
    except Exception:                                             # noqa: BLE001
        pass
    job.set(progress=1.0, stage="ready",
            message=f"{s.width}x{s.height}, {_hms(s.duration)}")
    return {"source": rec}


def frame_path(sid: str, t: float, width: int = 960) -> str:
    """Cached single frame from a source, for the crop picker and scrubber."""
    rec = get_source(sid)
    if rec is None:
        raise FileNotFoundError("source not loaded")
    t = max(0.0, min(float(t), max(0.0, float(rec.get("duration") or 0) - 0.05)))
    cache = SOURCE_DIR / "frames" / sid
    cache.mkdir(parents=True, exist_ok=True)
    dest = cache / f"{int(t * 4)}_{width}.jpg"
    if dest.is_file() and dest.stat().st_size > 512:
        return str(dest)
    with _frame_lock:
        if not dest.is_file():
            extract_frame(rec["path"], t, dest, width=width)
    return str(dest)


def _hms(sec: float) -> str:
    sec = int(max(0, sec))
    h, m, s = sec // 3600, (sec % 3600) // 60, sec % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _stamp(sec: float) -> str:
    sec = int(max(0, sec))
    h, m, s = sec // 3600, (sec % 3600) // 60, sec % 60
    return f"{h:02d}h{m:02d}m{s:02d}s" if h else f"{m:02d}m{s:02d}s"


_NAME_STRIP = re.compile(r"[^A-Za-z0-9 ']+")


def _clip_name(index: int, words: list[transcribe.Word], start: float,
               line: str = "") -> str:
    if line:
        text = " ".join(_NAME_STRIP.sub(" ", line).split())[:60].strip()
        if len(text) >= 8:
            return f"{index:02d} - {safe_name(text)}"
    if words:
        text = " ".join(w.text for w in words[:9])
        text = _NAME_STRIP.sub(" ", text)
        text = " ".join(text.split())[:52].strip()
        if len(text) >= 8:
            return f"{index:02d} - {safe_name(text)}"
    return f"{index:02d} - {_stamp(start)}"


def _unique_dir(base: Path) -> Path:
    if not base.exists():
        base.mkdir(parents=True, exist_ok=True)
        return base
    for n in range(2, 200):
        cand = base.parent / f"{base.name} ({n})"
        if not cand.exists():
            cand.mkdir(parents=True, exist_ok=True)
            return cand
    base.mkdir(parents=True, exist_ok=True)
    return base


def make_clips(job: Job, sid: str, raw_settings: dict[str, Any]) -> dict[str, Any]:
    """The main event: detect highlights, cut them, caption them, render them."""
    rec = get_source(sid)
    if rec is None:
        raise FileNotFoundError("That stream is not loaded any more - add it again.")

    src_w, src_h = int(rec["width"]), int(rec["height"])
    st = S.normalize(raw_settings, src_w, src_h)
    S.save_last(st)
    video = rec["path"]
    duration = float(rec.get("duration") or 0.0)
    has_audio = bool(rec.get("has_audio", True))
    if duration <= 0:
        duration = probe(video).duration

    work = JOB_DIR / job.id
    work.mkdir(parents=True, exist_ok=True)
    out_root = Path(st.get("out_folder") or OUTPUT_DIR)
    out_dir = _unique_dir(out_root / safe_name(rec.get("title") or "clips"))

    job.update_result(out_dir=str(out_dir), source=rec, settings=st, clips=[])
    job.set(stage="analyse", progress=0.02, message="Analysing the stream")

    # ---- detection ----------------------------------------------------
    def on_stage(stage: str, frac: float, msg: str) -> None:
        job.raise_if_cancelled()
        span = {"audio": (0.02, 0.16), "chat": (0.16, 0.24), "score": (0.24, 0.28)}
        lo, hi = span.get(stage, (0.02, 0.28))
        job.set(stage="analyse", progress=lo + (hi - lo) * frac, message=msg or None)

    if not has_audio:
        job.note("No audio track - falling back to evenly spaced clips")
    st["has_audio"] = has_audio
    analysis = detect.analyse(
        video, work / "audio.wav", duration=duration, settings=st,
        vod_id=str(rec.get("vod_id") or ""),
        time_offset=float(rec.get("time_offset") or 0.0),
        cancel=job.cancel_event, on_stage=on_stage,
    )
    clips: list[detect.Clip] = analysis["clips"]
    job.update_result(timeline=analysis["timeline"],
                      detected=[c.to_dict() for c in clips],
                      chat_note=analysis.get("chat_note", ""))
    if analysis.get("chat_note"):
        job.note(analysis["chat_note"])
    if not clips:
        job.note("No highlights found - try raising sensitivity or a longer stream")
        return {"clips": []}

    wanted = int(st.get("clip_count", 5))
    strong = sum(1 for c in clips if c.tier == "strong")
    if st.get("manual_clips") or st.get("whole_video"):
        job.note(f"Rendering {len(clips)} clip(s)")
    else:
        extra = len(clips) - strong
        note = f"Found {len(clips)} moment(s): {strong} clear highlight(s)"
        if extra:
            note += f" plus {extra} to fill your count"
        job.note(note)
        if len(clips) < wanted:
            job.note(f"Only {len(clips)} of {wanted} found - raise Sensitivity, "
                     f"shorten Clip length, or widen the part of the video used")

    want_caps = bool(st["captions"]) and has_audio
    want_censor = bool(st["censor"]) and has_audio
    # Censoring needs the words too, so transcribe if either is on.
    want_words = want_caps or want_censor
    matcher = censor.build_matcher(st) if want_censor else None
    if want_words and not transcribe.is_model_cached(st["whisper_model"]):
        job.note(f"First run: downloading the {st['whisper_model']} speech model")
    censored_total = 0
    moments: list[titles.Moment] = []
    game = st["game_name"] or titles.guess_game(rec.get("title") or "")

    results: list[dict[str, Any]] = []
    lo_all, hi_all = 0.30, 1.0
    slot = (hi_all - lo_all) / max(len(clips), 1)

    for i, clip in enumerate(clips, 1):
        job.raise_if_cancelled()
        base = lo_all + slot * (i - 1)

        def sub(frac: float, msg: str | None = None, _b: float = base) -> None:
            job.raise_if_cancelled()
            job.set(stage=f"clip {i}/{len(clips)}",
                    progress=_b + slot * max(0.0, min(1.0, frac)), message=msg)

        cdir = work / f"clip{i:02d}"
        cdir.mkdir(parents=True, exist_ok=True)
        length = max(1.0, clip.duration)
        words: list[transcribe.Word] = []
        lang = ""

        if want_words:
            sub(0.02, f"Clip {i}: pulling audio")
            wav = cdir / "clip.wav"
            run_ffmpeg(["-ss", f"{clip.start:.3f}", "-i", video, "-t", f"{length:.3f}",
                        "-vn", "-ac", "1", "-ar", str(detect.SR), "-c:a", "pcm_s16le",
                        str(wav)], cancel=job.cancel_event, log_name="clip audio")
            sub(0.08, f"Clip {i}: transcribing")
            try:
                words, lang = transcribe.transcribe(
                    wav, model_size=st["whisper_model"], language=st["language"],
                    duration=length, cancel=job.cancel_event,
                    on_progress=lambda f, m, _s=sub: _s(0.08 + 0.42 * f, None),
                )
            except Exception as exc:                              # noqa: BLE001
                job.note(f"Clip {i}: captions skipped ({type(exc).__name__}: {exc})")
                words = []

        spans: list[tuple[float, float]] = []
        hits: list[censor.Hit] = []
        shown = words
        if matcher is not None and words:
            hits = censor.find_hits(words, matcher)
            spans = censor.hits_to_spans(hits, st["censor_pad"], limit=length)
            if st["censor_captions"]:
                shown = censor.mask_words(words, matcher)
            if hits:
                censored_total += len(hits)
                job.note(f"Clip {i}: bleeped {len(hits)} word(s)")

        ass_path = None
        if words and want_caps:
            ass_path = cdir / "captions.ass"
            ass_path.write_text(
                caps.build_ass(shown, st, out_w=st["out_w"], out_h=st["out_h"],
                               clip_len=length),
                encoding="utf-8",
            )

        # Titles and names come from the starred-out transcript, so they stay
        # clean too - YouTube reads titles as well as audio.
        masked = censor.mask_words(words, matcher) if matcher else words
        peak_rel = clip.peak - clip.start if clip.start <= clip.peak <= clip.end else None
        line = titles.best_line(masked, peak_rel) if masked else ""
        name = _clip_name(i, masked, clip.start, line)
        moments.append(titles.Moment(start=clip.start, end=clip.end, words=masked,
                                     peak=peak_rel, score=clip.score))
        dest = out_dir / f"{name}.mp4"
        sub(0.52, f"Clip {i}: rendering")
        render.render_clip(
            video, dest, start=clip.start, duration=length, settings=st,
            src_w=src_w, src_h=src_h, subs_path=ass_path, has_audio=has_audio,
            bleep_spans=spans, workdir=cdir, cancel=job.cancel_event,
            on_progress=lambda f, _s=sub: _s(0.52 + 0.43 * f, None),
        )

        sub(0.96, f"Clip {i}: thumbnail")
        thumb = out_dir / f"{name}.jpg"
        try:
            render.make_thumbnail(dest, thumb, at=min(length * 0.4, max(length - 0.2, 0.1)))
        except Exception:                                          # noqa: BLE001
            thumb = None
        if words and want_caps:
            try:
                (out_dir / f"{name}.srt").write_text(
                    caps.words_to_srt(shown, st, st["out_w"]), encoding="utf-8")
            except OSError:
                pass

        entry = {
            "index": i,
            "name": name,
            "file": dest.name,
            "path": str(dest),
            "thumb": thumb.name if thumb else "",
            "start": round(clip.start, 2),
            "end": round(clip.end, 2),
            "duration": round(length, 2),
            "start_hms": _hms(clip.start),
            "score": round(clip.score, 4),
            "tier": clip.tier,
            "reasons": clip.reasons,
            "words": len(words),
            "language": lang,
            "text": " ".join(w.text for w in shown)[:600],
            "censored": len(hits),
            "title": line,
            "description": titles.short_description(line, game, st["hashtags"]) if line else "",
            "size": dest.stat().st_size if dest.is_file() else 0,
        }
        results.append(entry)
        job.update_result(clips=list(results))
        sub(1.0, f"Clip {i} done")

    manifest = {
        "created": time.time(),
        "source": {k: rec.get(k) for k in ("title", "url", "kind", "width", "height",
                                           "duration", "path")},
        "settings": st,
        "clips": results,
    }
    try:
        (out_dir / "clips.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    except OSError:
        pass

    # The scratch folder holds a wav of the whole stream, which for a long VOD is
    # hundreds of MB. Nothing needs it once the clips exist.
    shutil.rmtree(work, ignore_errors=True)

    if want_censor:
        job.note(f"Bleeped {censored_total} word(s) across all clips"
                 if censored_total else "No swearing found")
    info = _titles_info(moments, "clips", rec, matcher, game, st["hashtags"])
    if info.get("quotes"):
        try:
            titles.save(out_dir, "clips", info)
        except OSError:
            pass
    job.update_result(titles=info)
    job.set(stage="done", progress=1.0,
            message=f"{len(results)} clip(s) saved to {out_dir}")
    return {"clips": results, "out_dir": str(out_dir), "titles": info}


def _titles_info(moments: list[titles.Moment], kind: str, rec: dict[str, Any],
                 matcher, game: str, hashtags: str) -> dict[str, Any]:
    """titles.build() plus the per-moment lines the UI needs to re-run it."""
    info = titles.build(moments, kind=kind, rec=rec, matcher=matcher, game=game,
                        hashtags=hashtags)
    info["moments"] = [{"start": m.start, "end": m.end, "out_at": m.out_at,
                        "line": m.line, "score": m.score} for m in moments]
    return info


# ---------------------------------------------------------------------------
# clean full-length video


def _unique_file(path: Path) -> Path:
    if not path.exists() and not path.with_suffix(".mkv").exists():
        return path
    for n in range(2, 500):
        cand = path.with_name(f"{path.stem} ({n}){path.suffix}")
        if not cand.exists() and not cand.with_suffix(".mkv").exists():
            return cand
    return path


def make_clean(job: Job, sid: str, raw_settings: dict[str, Any]) -> dict[str, Any]:
    """The whole stream (or the chosen part), picture untouched, swearing bleeped.

    With censoring off this is just a fast lossless download/trim.
    """
    rec = get_source(sid)
    if rec is None:
        raise FileNotFoundError("That stream is not loaded any more - add it again.")

    src_w, src_h = int(rec["width"]), int(rec["height"])
    st = S.normalize(raw_settings, src_w, src_h)
    S.save_last(st)
    video = rec["path"]
    duration = float(rec.get("duration") or 0.0) or probe(video).duration
    has_audio = bool(rec.get("has_audio", True))
    lo, hi = detect.search_window(st, duration)
    length = hi - lo
    trimmed = lo > 0.5 or hi < duration - 0.5

    work = JOB_DIR / job.id
    work.mkdir(parents=True, exist_ok=True)
    stem = safe_name(rec.get("title") or "stream")
    out_dir = Path(st.get("out_folder") or OUTPUT_DIR) / stem
    out_dir.mkdir(parents=True, exist_ok=True)
    label = " (clean)" if st["censor"] and has_audio else ""
    if trimmed:
        label += f" {_stamp(lo)}-{_stamp(hi)}"
    dest = _unique_file(out_dir / f"{stem}{label}.mp4")

    job.update_result(out_dir=str(out_dir), source=rec, settings=st, mode="clean", clips=[])
    if trimmed:
        job.note(f"Using {_hms(lo)} to {_hms(hi)} ({_hms(length)})")

    words: list[transcribe.Word] = []
    hits: list[censor.Hit] = []
    spans: list[tuple[float, float]] = []
    moments: list[titles.Moment] = []
    matcher = None
    want_censor = bool(st["censor"]) and has_audio

    if want_censor:
        job.set(stage="audio", progress=0.01, message="Pulling the audio out")
        wav = work / "speech.wav"
        seek = ["-ss", f"{lo:.3f}"] if lo > 0.05 else []
        run_ffmpeg(seek + ["-t", f"{length:.3f}", "-i", video, "-vn", "-ac", "1",
                           "-ar", "16000", "-c:a", "pcm_s16le", str(wav)],
                   total_seconds=length, cancel=job.cancel_event, log_name="audio",
                   on_progress=lambda f: job.set(progress=0.01 + 0.04 * f))

        job.set(stage="transcribe", progress=0.05,
                message=f"Listening for swearing with the {st['clean_model']} model")
        words, _lang = transcribe.transcribe_long(
            wav, model_size=st["clean_model"], language=st["language"],
            cancel=job.cancel_event,
            on_progress=lambda f, m: job.set(stage="transcribe",
                                             progress=0.05 + 0.80 * f, message=m),
        )
        matcher = censor.build_matcher(st)
        hits = censor.find_hits(words, matcher)
        spans = censor.hits_to_spans(hits, st["censor_pad"], limit=length)
        verb = "bleep" if st["censor_action"] == "bleep" else "mute"
        job.note(f"Found {len(hits)} word(s) to {verb}" if hits
                 else "No swearing found - the audio is left untouched")

        job.set(stage="moments", progress=0.855, message="Picking the best moments for the description")
        try:
            peaks = detect.analyse(
                video, wav, duration=length,
                settings=dict(st, clip_count=8, target_len=30.0, clip_min=10.0,
                              clip_max=60.0, sensitivity=0.5, range_start=0.0,
                              range_end=0.0, skip_intro=0.0, skip_outro=0.0,
                              manual_clips=[], whole_video=False, use_chat=False,
                              has_audio=True),
                cancel=job.cancel_event)["clips"]
        except Exception as exc:                                   # noqa: BLE001
            if job.cancelled:
                raise
            job.note(f"Skipped the highlight timestamps ({type(exc).__name__})")
            peaks = []
        masked_all = censor.mask_words(words, matcher)
        for c in peaks:
            moments.append(titles.Moment(
                start=c.start, end=c.end, peak=c.peak, score=c.score, out_at=c.start,
                words=[w for w in masked_all if c.start <= w.start < c.end]))
    elif st["censor"] and not has_audio:
        job.note("This video has no audio - nothing to censor")

    job.set(stage="render", progress=0.86,
            message="Writing the video (picture is copied, not re-encoded)")
    out = Path(render.render_clean(
        video, dest, start=lo, duration=length, full_length=duration, settings=st,
        spans=spans, has_audio=has_audio, workdir=work, cancel=job.cancel_event,
        on_progress=lambda f: job.set(progress=0.86 + 0.11 * f),
    ))

    job.set(stage="extras", progress=0.97, message="Saving the report and captions")
    report_name = ""
    if want_censor:
        rp = out.with_name(out.stem + " - censor report.txt")
        header = (f"{out.name}\n"
                  f"Times below are positions in the ORIGINAL stream"
                  f"{' (this file starts at ' + _hms(lo) + ')' if trimmed else ''}.\n\n")
        rp.write_text(header + censor.report(hits, offset=lo), encoding="utf-8")
        report_name = rp.name
    if words:
        shown = censor.mask_words(words, matcher) if (matcher and st["censor_captions"]) \
            else words
        try:
            out.with_suffix(".srt").write_text(
                caps.words_to_srt(shown, {**st, "cap_size": 48}, 1920, max_words=10),
                encoding="utf-8")
        except OSError:
            pass

    thumb = out.with_suffix(".jpg")
    try:
        render.make_thumbnail(out, thumb, at=min(max(length * 0.1, 1.0), 120.0), width=480)
    except Exception:                                              # noqa: BLE001
        thumb = None

    entry = {
        "index": 1,
        "kind": "clean",
        "name": out.stem,
        "file": out.name,
        "path": str(out),
        "thumb": thumb.name if thumb else "",
        "start": round(lo, 2),
        "end": round(hi, 2),
        "duration": round(length, 2),
        "start_hms": _hms(lo),
        "score": 1.0,
        "tier": "full",
        "censored": len(hits),
        "words": len(words),
        "report": report_name,
        "text": (f"{len(hits)} word(s) {'bleeped' if st['censor_action'] == 'bleep' else 'muted'}"
                 if want_censor else "Lossless copy, nothing changed"),
        "size": out.stat().st_size if out.is_file() else 0,
        "width": src_w,
        "height": src_h,
    }
    game = st["game_name"] or titles.guess_game(rec.get("title") or "")
    info = _titles_info(moments, "clean", rec, matcher, game, st["hashtags"])
    try:
        titles.save(out_dir, out.stem, info)
    except OSError:
        pass
    job.update_result(clips=[entry], titles=info)
    shutil.rmtree(work, ignore_errors=True)
    job.set(stage="done", progress=1.0, message=f"Saved {out.name}")
    return {"clips": [entry], "out_dir": str(out_dir), "titles": info}


# ---------------------------------------------------------------------------
# live preview


def build_preview(sid: str, raw_settings: dict[str, Any], t: float) -> str:
    """Render one composited still exactly as the final clip will look."""
    rec = get_source(sid)
    if rec is None:
        raise FileNotFoundError("source not loaded")
    src_w, src_h = int(rec["width"]), int(rec["height"])
    st = S.normalize(raw_settings, src_w, src_h)
    work = WORK_DIR / "preview" / sid
    work.mkdir(parents=True, exist_ok=True)

    # Unique names: the browser fires previews as fast as the user drags sliders,
    # and overwriting a file Flask is still streaming fails on Windows.
    tag = uuid.uuid4().hex[:8]
    subs = None
    if st["captions"]:
        words = caps.preview_words(raw_settings.get("preview_text")
                                   or "THIS IS HOW YOUR CAPTIONS LOOK")
        subs = work / f"preview_{tag}.ass"
        subs.write_text(
            caps.build_ass(words, st, out_w=st["out_w"], out_h=st["out_h"], clip_len=8.0),
            encoding="utf-8",
        )
    dest = work / f"preview_{tag}.jpg"
    with preview_lock:
        render.render_preview(rec["path"], dest, timestamp=float(t), settings=st,
                              src_w=src_w, src_h=src_h, subs_path=subs, workdir=work)
    _sweep_previews(work, keep=tag)
    return str(dest)


def _sweep_previews(folder: Path, keep: str, max_age: float = 120.0) -> None:
    now = time.time()
    for p in folder.glob("preview_*"):
        if keep in p.name:
            continue
        try:
            if now - p.stat().st_mtime > max_age:
                p.unlink(missing_ok=True)
        except OSError:
            pass
