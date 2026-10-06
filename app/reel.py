"""Highlight reel: find candidate moments, let the user choose, join them.

Two jobs:
  find_moments  detection proposes about twice as much footage as the target
                length, then each candidate is transcribed in the background so
                its quote appears in the list while the user is choosing
  build_reel    renders each chosen moment (bleeped, faded at the ends) to a
                segment with identical encoder settings, then joins them losslessly
                with the concat demuxer and writes chapter markers

Transcripts are cached per source in absolute stream time, so building re-uses
what was heard while choosing, and trimming a moment never re-transcribes it.
"""
from __future__ import annotations

import json
import math
import shutil
import threading
import time
from pathlib import Path
from typing import Any

from . import censor, detect, render, titles, transcribe
from . import settings as S
from .ffmpeg_tools import extract_frame, probe, run_ffmpeg
from .jobs import Job
from .paths import JOB_DIR, OUTPUT_DIR, WORK_DIR, safe_name
from .transcribe import Word

MOMENTS_DIR = WORK_DIR / "moments"
_cache_locks: dict[str, threading.Lock] = {}
_cache_guard = threading.Lock()

PLAYABLE_CONTAINERS = {".mp4", ".m4v", ".mov", ".webm"}
PLAYABLE_VIDEO = {"h264", "vp8", "vp9", "av1"}
PLAYABLE_AUDIO = {"aac", "mp3", "opus", "vorbis", ""}


def _lock_for(sid: str) -> threading.Lock:
    with _cache_guard:
        return _cache_locks.setdefault(sid, threading.Lock())


def _folder(sid: str) -> Path:
    d = MOMENTS_DIR / sid
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------------------------------------------------------------------------
# transcript cache (absolute stream time)


class TranscriptCache:
    def __init__(self, sid: str):
        self.sid = sid
        self.path = _folder(sid) / "transcript.json"
        self.words: list[Word] = []
        self.covered: list[tuple[float, float]] = []
        self.model = ""
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.words = [Word(a, b, t) for a, b, t in data.get("words", [])]
            self.covered = [tuple(c) for c in data.get("covered", [])]
            self.model = data.get("model", "")
        except (OSError, ValueError, TypeError):
            pass

    def save(self) -> None:
        data = {"model": self.model,
                "covered": [list(c) for c in self.covered],
                "words": [[round(w.start, 3), round(w.end, 3), w.text] for w in self.words]}
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        tmp.replace(self.path)

    def has(self, a: float, b: float) -> bool:
        return any(s <= a + 0.05 and e >= b - 0.05 for s, e in self.covered)

    def get(self, a: float, b: float) -> list[Word]:
        return [w for w in self.words if w.end > a and w.start < b]

    def add(self, a: float, b: float, words: list[Word]) -> None:
        self.words = [w for w in self.words if not (w.start >= a and w.end <= b)] + words
        self.words.sort(key=lambda w: w.start)
        merged: list[tuple[float, float]] = []
        for s, e in sorted(self.covered + [(a, b)]):
            if merged and s <= merged[-1][1] + 0.05:
                merged[-1] = (merged[-1][0], max(merged[-1][1], e))
            else:
                merged.append((s, e))
        self.covered = merged


def listen(video: str, a: float, b: float, cache: TranscriptCache, *, model: str,
           language: str, workdir: Path, cancel=None) -> list[Word]:
    """Words spoken between a and b (stream seconds), transcribing only if needed."""
    if cache.has(a, b) and cache.model == model:
        return cache.get(a, b)
    wav = workdir / f"listen_{int(a * 1000)}.wav"
    run_ffmpeg(["-ss", f"{a:.3f}", "-t", f"{b - a:.3f}", "-i", video, "-vn", "-ac", "1",
                "-ar", "16000", "-c:a", "pcm_s16le", str(wav)],
               cancel=cancel, log_name="listen")
    try:
        rel, _ = transcribe.transcribe(wav, model_size=model, language=language,
                                       duration=b - a, cancel=cancel)
    finally:
        wav.unlink(missing_ok=True)
    words = [Word(a + w.start, a + w.end, w.text) for w in rel]
    if cache.model != model:
        cache.words, cache.covered, cache.model = [], [], model
    cache.add(a, b, words)
    cache.save()
    return words


# ---------------------------------------------------------------------------
# playback


def direct_playable(rec: dict[str, Any]) -> bool:
    """Can the browser play the source file itself (and so seek it instantly)?"""
    path = Path(rec.get("path", ""))
    if path.suffix.lower() not in PLAYABLE_CONTAINERS:
        return False
    try:
        info = probe(path)
    except Exception:                                              # noqa: BLE001
        return False
    return info.vcodec in PLAYABLE_VIDEO and info.acodec in PLAYABLE_AUDIO


_snippet_lock = threading.Lock()


def snippet(rec: dict[str, Any], a: float, b: float) -> Path:
    """A small preview of one moment, for sources the browser cannot play."""
    sid = rec["id"]
    a, b = max(0.0, a), max(a + 0.5, b)
    dest = _folder(sid) / f"snip_{int(a * 10)}_{int(b * 10)}.mp4"
    with _snippet_lock:
        if not dest.is_file() or dest.stat().st_size < 1024:
            run_ffmpeg(["-ss", f"{a:.3f}", "-t", f"{b - a:.3f}", "-i", rec["path"],
                        "-vf", "scale=-2:360", "-c:v", "libx264", "-preset", "ultrafast",
                        "-crf", "28", "-c:a", "aac", "-b:a", "96k", "-ac", "2",
                        "-movflags", "+faststart", str(dest)], log_name="snippet")
    return dest


# ---------------------------------------------------------------------------
# job 1: find moments


def _hms(sec: float) -> str:
    sec = int(max(0, sec))
    h, m, s = sec // 3600, (sec % 3600) // 60, sec % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def find_moments(job: Job, rec: dict[str, Any], raw: dict[str, Any]) -> dict[str, Any]:
    sid = rec["id"]
    st = S.normalize(raw, int(rec["width"]), int(rec["height"]))
    S.save_last(st)
    video = rec["path"]
    duration = float(rec.get("duration") or 0) or probe(video).duration
    has_audio = bool(rec.get("has_audio", True))
    folder = _folder(sid)

    length = st["reel_moment_len"]
    want = int(min(45, max(8, math.ceil(st["reel_max"] * 60 / length * 2))))
    det = dict(st, clip_count=want, target_len=length,
               clip_min=max(8.0, length * 0.6), clip_max=length * 1.6,
               sensitivity=max(st["sensitivity"], 0.6), manual_clips=[],
               whole_video=False, has_audio=has_audio)

    def on_stage(stage: str, frac: float, msg: str) -> None:
        job.raise_if_cancelled()
        span = {"audio": (0.0, 0.25), "chat": (0.25, 0.32), "score": (0.32, 0.35)}
        lo, hi = span.get(stage, (0.0, 0.35))
        job.set(stage="find", progress=lo + (hi - lo) * frac, message=msg or None)

    job.set(stage="find", progress=0.01, message="Listening for the loud bits")
    analysis = detect.analyse(video, folder / "audio.wav", duration=duration, settings=det,
                              vod_id=str(rec.get("vod_id") or ""),
                              time_offset=float(rec.get("time_offset") or 0.0),
                              cancel=job.cancel_event, on_stage=on_stage)
    found = sorted(analysis["clips"], key=lambda c: c.start)
    if not found:
        job.note("No moments found - widen the part of the video searched")
        return {"candidates": []}

    # Suggest a starting selection: best first, until the middle of the target.
    goal = (st["reel_min"] + st["reel_max"]) / 2 * 60
    total = 0.0
    picked: set[int] = set()
    for i in sorted(range(len(found)), key=lambda k: -found[k].score):
        if total + found[i].duration > st["reel_max"] * 60:
            continue
        picked.add(i)
        total += found[i].duration
        if total >= goal:
            break

    cands = [{
        "id": i, "start": round(c.start, 2), "end": round(c.end, 2),
        "peak": round(c.peak, 2), "score": round(c.score, 4), "tier": c.tier,
        "picked": i in picked, "thumb": "", "line": None, "text": None,
    } for i, c in enumerate(found)]
    job.update_result(candidates=cands, timeline=analysis["timeline"], duration=duration,
                      chat_note=analysis.get("chat_note", ""),
                      direct=direct_playable(rec), source=rec)
    job.note(f"Found {len(cands)} moments - {len(picked)} picked to start with "
             f"({_hms(total)})")

    job.set(stage="thumbs", progress=0.36, message="Grabbing pictures")
    for c in cands:
        job.raise_if_cancelled()
        thumb = folder / f"thumb_{int(c['peak'] * 10)}.jpg"
        if not thumb.is_file():
            try:
                extract_frame(video, c["peak"], thumb, width=320)
            except Exception:                                      # noqa: BLE001
                continue
        c["thumb"] = str(thumb)
    job.update_result(candidates=cands)

    if not has_audio:
        return {"candidates": cands}

    # Quotes, best moments first so the likely picks fill in soonest.
    model = st["clean_model"]
    cache = TranscriptCache(sid)
    matcher = censor.build_matcher(st) if st["censor"] else None
    work = JOB_DIR / job.id
    work.mkdir(parents=True, exist_ok=True)
    order = sorted(cands, key=lambda c: -c["score"])
    for n, c in enumerate(order, 1):
        job.raise_if_cancelled()
        job.set(stage="quotes", progress=0.4 + 0.6 * (n - 1) / len(order),
                message=f"Writing down what you said ({n}/{len(order)})")
        try:
            with _lock_for(sid):
                words = listen(video, c["start"], c["end"], cache, model=model,
                               language=st["language"], workdir=work,
                               cancel=job.cancel_event)
        except Exception as exc:                                   # noqa: BLE001
            if job.cancelled:
                raise
            job.note(f"Could not transcribe {_hms(c['start'])}: {exc}")
            c["line"], c["text"] = "", ""
            continue
        shown = censor.mask_words(words, matcher) if matcher else words
        c["line"] = titles.best_line(shown, c["peak"])
        c["text"] = " ".join(w.text for w in shown)[:400]
        job.update_result(candidates=cands)
    shutil.rmtree(work, ignore_errors=True)
    job.set(stage="ready", progress=1.0, message="Pick your moments")
    return {"candidates": cands}


# ---------------------------------------------------------------------------
# job 2: build the reel


def _out_geometry(rec: dict[str, Any]) -> tuple[int, int, int]:
    w, h = int(rec.get("width") or 1920), int(rec.get("height") or 1080)
    scale = min(1.0, 1920 / w, 1080 / h)
    w, h = int(w * scale) // 2 * 2, int(h * scale) // 2 * 2
    fps = 60 if float(rec.get("fps") or 30) > 45 else 30
    return w, h, fps


def _clean_selection(sel: list[dict[str, Any]], duration: float) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    for item in sel or []:
        try:
            a, b = float(item["start"]), float(item["end"])
        except (KeyError, TypeError, ValueError):
            continue
        a, b = max(0.0, a), min(duration, b)
        if b - a >= 2.0:
            out.append((a, b))
    out.sort()
    merged: list[tuple[float, float]] = []
    for a, b in out:
        if merged and a < merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
        else:
            merged.append((a, b))
    return merged


def _chapters_file(path: Path, title: str, marks: list[tuple[float, float, str]]) -> None:
    def esc(s: str) -> str:
        for ch in ("\\", "=", ";", "#", "\n"):
            s = s.replace(ch, "\\" + ch)
        return s
    lines = [";FFMETADATA1", f"title={esc(title)}"]
    for start, end, name in marks:
        lines += ["[CHAPTER]", "TIMEBASE=1/1000", f"START={int(start * 1000)}",
                  f"END={int(end * 1000)}", f"title={esc(name)}"]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def build_reel(job: Job, rec: dict[str, Any], raw: dict[str, Any],
               selection: list[dict[str, Any]]) -> dict[str, Any]:
    sid = rec["id"]
    st = S.normalize(raw, int(rec["width"]), int(rec["height"]))
    S.save_last(st)
    video = rec["path"]
    duration = float(rec.get("duration") or 0) or probe(video).duration
    has_audio = bool(rec.get("has_audio", True))
    parts = _clean_selection(selection, duration)
    if not parts:
        raise ValueError("Pick at least one moment for the reel.")

    w, h, fps = _out_geometry(rec)
    fade = 0.35 if st["reel_transition"] == "fade" else 0.0
    work = JOB_DIR / job.id
    work.mkdir(parents=True, exist_ok=True)
    stem = safe_name(rec.get("title") or "stream")
    out_dir = Path(st.get("out_folder") or OUTPUT_DIR) / stem
    out_dir.mkdir(parents=True, exist_ok=True)
    total_len = sum(b - a for a, b in parts)
    job.update_result(out_dir=str(out_dir), source=rec, settings=st, mode="reel", clips=[])
    job.note(f"Building a {_hms(total_len)} reel from {len(parts)} moment(s), "
             f"{w}x{h} at {fps} fps")

    cache = TranscriptCache(sid)
    matcher = censor.build_matcher(st) if st["censor"] else None
    want_words = has_audio          # needed for bleeping and for the titles
    segs: list[Path] = []
    moments: list[titles.Moment] = []
    bleeped = 0
    slot = 0.92 / len(parts)
    vf_base = (f"fps={fps},scale={w}:{h}:force_original_aspect_ratio=decrease:flags=lanczos,"
               f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1,format=yuv420p")

    for i, (a, b) in enumerate(parts):
        job.raise_if_cancelled()
        base = 0.02 + slot * i
        d = b - a
        words: list[Word] = []
        if want_words:
            job.set(stage=f"moment {i + 1}/{len(parts)}", progress=base,
                    message=f"Moment {i + 1}: checking what was said")
            with _lock_for(sid):
                words = listen(video, a, b, cache, model=st["clean_model"],
                               language=st["language"], workdir=work,
                               cancel=job.cancel_event)
        rel = [Word(x.start - a, x.end - a, x.text) for x in words]
        spans: list[tuple[float, float]] = []
        if matcher is not None and rel:
            hits = censor.find_hits(rel, matcher)
            spans = censor.hits_to_spans(hits, st["censor_pad"], limit=d)
            bleeped += len(hits)

        vf = vf_base
        if fade:
            vf += f",fade=t=in:st=0:d={fade},fade=t=out:st={max(0.0, d - fade):.3f}:d={fade}"
        graph = f"[0:v]{vf}[v]"
        maps = ["-map", "[v]"]
        if has_audio:
            graph += ";" + censor.audio_filter(
                spans, d, in_label="0:a:0", out_label="ac",
                action=st["censor_action"], tone_hz=st["censor_tone"],
                gain=st["censor_gain"])
            af = "anull"
            if fade:
                af = (f"afade=t=in:st=0:d={fade},"
                      f"afade=t=out:st={max(0.0, d - fade):.3f}:d={fade}")
            graph += f";[ac]{af}[a]"
            maps += ["-map", "[a]", "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2"]
        seg = work / f"seg_{i:03d}.mp4"
        job.set(message=f"Moment {i + 1}: rendering")
        run_ffmpeg(["-ss", f"{a:.3f}", "-t", f"{d:.3f}", "-i", video]
                   + render.filter_args(graph, work) + maps
                   + render.pick_encoder(st)
                   + ["-r", str(fps), "-g", str(fps * 2), "-t", f"{d:.3f}", str(seg)],
                   total_seconds=d, cancel=job.cancel_event, cwd=work,
                   log_name="segment",
                   on_progress=lambda f, _b=base: job.set(progress=_b + slot * (0.15 + 0.85 * f)))
        segs.append(seg)
        moments.append(titles.Moment(start=a, end=b, words=words,
                                     peak=_peak_in(a, b, selection), score=_score_in(a, b, selection)))

    job.set(stage="join", progress=0.94, message="Joining the moments")
    actual = [probe(s).duration for s in segs]
    cursor = 0.0
    marks: list[tuple[float, float, str]] = []
    titles.pick_lines(moments, matcher)
    for m, dur in zip(moments, actual):
        m.out_at = cursor
        marks.append((cursor, cursor + dur, m.line or f"Moment at {_hms(m.start)}"))
        cursor += dur
    meta = work / "chapters.txt"
    _chapters_file(meta, f"{rec.get('title') or stem} - highlights", marks)
    listing = work / "list.txt"
    listing.write_text("".join(f"file '{s.name}'\n" for s in segs), encoding="utf-8")

    dest = _unique(out_dir / f"{stem} - highlights.mp4")
    run_ffmpeg(["-f", "concat", "-safe", "0", "-i", listing.name, "-i", meta.name,
                "-map", "0", "-map_metadata", "1", "-map_chapters", "1",
                "-c", "copy", "-movflags", "+faststart", str(dest)],
               cwd=work, cancel=job.cancel_event, log_name="join")

    job.set(stage="extras", progress=0.97, message="Writing titles and a description")
    info = titles.build(moments, kind="reel", rec=rec, matcher=matcher, game=st["game_name"],
                        hashtags=st["hashtags"])
    titles.save(out_dir, dest.stem, info)
    thumb = dest.with_suffix(".jpg")
    try:
        render.make_thumbnail(dest, thumb, at=min(max(actual[0] * 0.5, 1.0), 20.0), width=480)
    except Exception:                                              # noqa: BLE001
        thumb = None

    entry = {
        "index": 1, "kind": "reel", "name": dest.stem, "file": dest.name, "path": str(dest),
        "thumb": thumb.name if thumb else "", "start": 0.0, "end": cursor,
        "duration": round(cursor, 2), "start_hms": "0:00", "score": 1.0, "tier": "reel",
        "censored": bleeped, "words": sum(len(m.words) for m in moments),
        "report": "", "moments": len(parts),
        "text": f"{len(parts)} moments" + (f" · {bleeped} word(s) bleeped" if bleeped else ""),
        "size": dest.stat().st_size if dest.is_file() else 0,
    }
    info["moments"] = [{"start": m.start, "end": m.end, "out_at": m.out_at, "line": m.line,
                        "score": m.score} for m in moments]
    job.update_result(clips=[entry], titles=info)
    shutil.rmtree(work, ignore_errors=True)
    job.set(stage="done", progress=1.0, message=f"Saved {dest.name}")
    return {"clips": [entry], "out_dir": str(out_dir), "titles": info}


def _match(a: float, b: float, selection: list[dict[str, Any]]) -> dict[str, Any] | None:
    best, overlap = None, 0.0
    for item in selection or []:
        try:
            s, e = float(item["start"]), float(item["end"])
        except (KeyError, TypeError, ValueError):
            continue
        ov = min(b, e) - max(a, s)
        if ov > overlap:
            best, overlap = item, ov
    return best


def _peak_in(a: float, b: float, selection: list[dict[str, Any]]) -> float | None:
    item = _match(a, b, selection)
    try:
        p = float(item["peak"]) if item and item.get("peak") is not None else None
    except (TypeError, ValueError):
        p = None
    return p if p is not None and a <= p <= b else None


def _score_in(a: float, b: float, selection: list[dict[str, Any]]) -> float:
    item = _match(a, b, selection)
    try:
        return float(item.get("score") or 0) if item else 0.0
    except (TypeError, ValueError):
        return 0.0


def _unique(path: Path) -> Path:
    if not path.exists():
        return path
    for n in range(2, 500):
        cand = path.with_name(f"{path.stem} ({n}){path.suffix}")
        if not cand.exists():
            return cand
    return path
