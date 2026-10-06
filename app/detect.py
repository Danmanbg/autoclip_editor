"""Highlight detection.

Signals we combine, each normalised to roughly 0..1:
  loudness   - how loud the stream is (shouting, explosions, music stings)
  burst      - how much louder than the last ~90 seconds (novelty, not just a loud game)
  bright     - high-frequency energy (laughter, screams, cheering)
  voice      - speech-band activity (the streamer is actually talking, not idle)
  chat       - Twitch chat replay message rate x excitement (when available)

The combined curve is integrated over a sliding window; the best non-overlapping
windows become clips, with edges nudged onto quiet moments so they start and end
cleanly.
"""
from __future__ import annotations

import math
import threading
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import numpy as np

from .ffmpeg_tools import Cancelled, run_ffmpeg

SR = 16000
N_FFT = 2048
HOP = 1600            # 0.1 s per analysis frame
FPS_ANALYSIS = SR / HOP


@dataclass
class Clip:
    start: float
    end: float
    score: float = 0.0
    peak: float = 0.0
    reasons: dict[str, float] = field(default_factory=dict)
    label: str = ""
    tier: str = "strong"          # strong | filler - how confident the pick is

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)

    def to_dict(self) -> dict[str, Any]:
        return {
            "start": round(self.start, 3),
            "end": round(self.end, 3),
            "duration": round(self.duration, 3),
            "score": round(self.score, 4),
            "peak": round(self.peak, 3),
            "reasons": {k: round(v, 3) for k, v in self.reasons.items()},
            "label": self.label,
            "tier": self.tier,
        }


# ---------------------------------------------------------------------------
# audio


def extract_audio(video: str | Path, wav_path: str | Path, *,
                  duration: float | None = None,
                  on_progress: Callable[[float], None] | None = None,
                  cancel: threading.Event | None = None) -> str:
    run_ffmpeg(
        ["-i", str(video), "-vn", "-ac", "1", "-ar", str(SR),
         "-c:a", "pcm_s16le", "-f", "wav", str(wav_path)],
        total_seconds=duration, on_progress=on_progress, cancel=cancel,
        log_name="audio extract",
    )
    return str(wav_path)


def _band_slice(lo: float, hi: float) -> slice:
    bin_hz = SR / N_FFT
    return slice(max(1, int(lo / bin_hz)), min(N_FFT // 2, int(hi / bin_hz)) + 1)


def audio_features(wav_path: str | Path, *,
                   on_progress: Callable[[float], None] | None = None,
                   cancel: threading.Event | None = None) -> dict[str, np.ndarray]:
    """Stream the wav and return per-frame energy bands (0.1 s resolution)."""
    total_e: list[np.ndarray] = []
    voice_e: list[np.ndarray] = []
    high_e: list[np.ndarray] = []

    window = np.hanning(N_FFT).astype(np.float32)
    sl_voice = _band_slice(250, 3500)
    sl_high = _band_slice(2500, 7600)
    chunk_frames = 600                       # ~60 s of audio per pass
    need = N_FFT + HOP * (chunk_frames - 1)

    with wave.open(str(wav_path), "rb") as wf:
        if wf.getnchannels() != 1 or wf.getsampwidth() != 2:
            raise RuntimeError("expected 16-bit mono wav")
        nframes = wf.getnframes() or 1
        carry = np.zeros(0, dtype=np.float32)
        read_total = 0
        while True:
            if cancel is not None and cancel.is_set():
                raise Cancelled("cancelled")
            raw = wf.readframes(need - len(carry))
            if not raw and len(carry) < N_FFT:
                break
            read_total += len(raw) // 2
            block = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
            buf = np.concatenate([carry, block]) if carry.size else block
            if buf.size < N_FFT:
                carry = buf
                if not raw:
                    break
                continue
            n = 1 + (buf.size - N_FFT) // HOP
            frames = np.lib.stride_tricks.sliding_window_view(buf, N_FFT)[::HOP][:n]
            spec = np.abs(np.fft.rfft(frames * window, axis=1)).astype(np.float32)
            power = spec * spec
            total_e.append(power.sum(axis=1))
            voice_e.append(power[:, sl_voice].sum(axis=1))
            high_e.append(power[:, sl_high].sum(axis=1))
            carry = buf[n * HOP:].copy()
            if on_progress:
                on_progress(min(0.99, read_total / max(nframes, 1)))
            if not raw:
                break

    if not total_e:
        empty = np.zeros(1, dtype=np.float32)
        return {"total": empty, "voice": empty.copy(), "high": empty.copy()}
    if on_progress:
        on_progress(1.0)
    return {
        "total": np.concatenate(total_e),
        "voice": np.concatenate(voice_e),
        "high": np.concatenate(high_e),
    }


# ---------------------------------------------------------------------------
# maths helpers


def _smooth(x: np.ndarray, seconds: float) -> np.ndarray:
    n = max(1, int(round(seconds * FPS_ANALYSIS)))
    if n <= 1 or x.size <= 2:
        return x.astype(np.float32)
    kernel = np.ones(n, dtype=np.float32) / n
    pad = n // 2
    padded = np.pad(x.astype(np.float32), (pad, pad), mode="edge")
    return np.convolve(padded, kernel, mode="same")[pad:pad + x.size]


def _rolling_baseline(x: np.ndarray, seconds: float) -> np.ndarray:
    """Cheap long-window baseline: heavy box smoothing."""
    return _smooth(x, seconds)


def _norm(x: np.ndarray, lo_pct: float = 5.0, hi_pct: float = 97.0) -> np.ndarray:
    if x.size == 0:
        return x
    lo = float(np.percentile(x, lo_pct))
    hi = float(np.percentile(x, hi_pct))
    if hi - lo < 1e-9:
        return np.zeros_like(x, dtype=np.float32)
    return np.clip((x - lo) / (hi - lo), 0.0, 1.0).astype(np.float32)


def _db(power: np.ndarray) -> np.ndarray:
    return 10.0 * np.log10(np.maximum(power, 1e-12))


def _resample_to(times_src: np.ndarray, values: np.ndarray, n_target: int,
                 duration: float) -> np.ndarray:
    if times_src.size == 0 or n_target <= 0:
        return np.zeros(max(n_target, 0), dtype=np.float32)
    grid = np.arange(n_target, dtype=np.float32) / FPS_ANALYSIS
    return np.interp(grid, times_src, values, left=values[0], right=values[-1]).astype(np.float32)


# ---------------------------------------------------------------------------
# scoring


def build_score(
    feats: dict[str, np.ndarray],
    *,
    duration: float,
    chat: tuple[np.ndarray, np.ndarray, np.ndarray] | None = None,
    use_audio: bool = True,
    use_speech: bool = True,
) -> tuple[np.ndarray, dict[str, np.ndarray]]:
    total = feats["total"]
    n = total.size
    loud_db = _db(total)
    loud = _smooth(loud_db, 0.6)

    base = _rolling_baseline(loud, 90.0)
    burst = _norm(_smooth(loud - base, 0.8), 20, 98)

    hf_ratio = feats["high"] / np.maximum(total, 1e-12)
    bright = _norm(_smooth(hf_ratio, 1.0), 20, 97)

    voice_ratio = feats["voice"] / np.maximum(total, 1e-12)
    voice_on = (voice_ratio > 0.25) & (loud > np.percentile(loud, 45))
    voice = _norm(_smooth(voice_on.astype(np.float32), 3.0), 5, 95)

    loudness = _norm(loud, 25, 98)

    parts: dict[str, np.ndarray] = {}
    weights: dict[str, float] = {}
    if use_audio:
        parts["loudness"] = loudness
        weights["loudness"] = 0.8
        parts["burst"] = burst
        weights["burst"] = 1.35
        parts["bright"] = bright
        weights["bright"] = 0.7
    if use_speech:
        parts["voice"] = voice
        weights["voice"] = 0.45

    if chat is not None:
        times, rate, hype = chat
        chat_signal = _norm(rate, 10, 97) * _norm(hype, 5, 95) ** 0.5
        chat_res = _resample_to(times, chat_signal, n, duration)
        chat_res = _smooth(chat_res, 4.0)
        # Chat reacts a beat after the moment; shift it earlier so windows line up.
        shift = int(round(2.5 * FPS_ANALYSIS))
        if shift and chat_res.size > shift:
            chat_res = np.concatenate([chat_res[shift:], np.repeat(chat_res[-1], shift)])
        parts["chat"] = _norm(chat_res, 5, 97)
        weights["chat"] = 1.9

    if not parts:
        parts["loudness"] = loudness
        weights["loudness"] = 1.0

    wsum = sum(weights.values()) or 1.0
    score = np.zeros(n, dtype=np.float32)
    for key, arr in parts.items():
        score += arr * (weights[key] / wsum)
    return _smooth(score, 1.5), parts


def _resolve_clip(
    best: int,
    score: np.ndarray,
    *,
    win: int,
    lo_i: int,
    hi_i: int,
    duration: float,
    min_len: float,
    max_len: float,
    lead_in: float,
    quiet: np.ndarray | None,
) -> tuple[float, float, int]:
    """Turn a winning window start into real clip bounds (start, end, peak index)."""
    peak_i = best + int(np.argmax(score[best:best + win]))

    # Put the payoff about 45% in, so there is a little build-up first.
    want_s = peak_i - int(win * 0.45) - int(lead_in * FPS_ANALYSIS)
    want_s = max(lo_i, min(want_s, max(lo_i, hi_i - win)))
    want_e = want_s + win

    radius = int(2.8 * FPS_ANALYSIS)
    s_sec = _snap_quiet(quiet, want_s, radius, backwards=True, lo=lo_i, hi=hi_i) / FPS_ANALYSIS
    e_sec = _snap_quiet(quiet, want_e, radius, backwards=False, lo=lo_i, hi=hi_i) / FPS_ANALYSIS

    if e_sec - s_sec < min_len:
        e_sec = s_sec + min_len
    if e_sec - s_sec > max_len:
        e_sec = s_sec + max_len
    s_sec = max(0.0, min(s_sec, max(0.0, duration - min_len)))
    e_sec = min(duration, max(e_sec, s_sec + min_len))
    return s_sec, e_sec, peak_i


def pick_clips(
    score: np.ndarray,
    parts: dict[str, np.ndarray],
    *,
    duration: float,
    count: int,
    min_len: float,
    max_len: float,
    target_len: float,
    sensitivity: float = 0.5,
    skip_intro: float = 0.0,
    skip_outro: float = 0.0,
    lead_in: float = 1.5,
    quiet: np.ndarray | None = None,
) -> list[Clip]:
    """Pick the best non-overlapping windows.

    Three passes, each looser than the last, stopping as soon as we have enough:
      1. high bar, spread widely across the searched span
      2. high bar, only normal spacing between clips
      3. modest bar, normal spacing - so a long stream still fills the quota

    Every iteration retires the candidate it looked at, whether or not it became
    a clip. Without that the same peak wins argmax forever and the search stalls.
    """
    n = score.size
    if n == 0 or duration <= 0:
        return []

    def to_idx(sec: float) -> int:
        return int(max(0, min(n - 1, round(sec * FPS_ANALYSIS))))

    lo_sec = max(0.0, skip_intro)
    hi_sec = max(lo_sec + min_len, duration - max(0.0, skip_outro))
    lo_i, hi_i = to_idx(lo_sec), to_idx(hi_sec)
    win = max(1, int(round(target_len * FPS_ANALYSIS)))

    # Searched span too short to slide a window through: take it whole.
    if hi_i - lo_i <= win or win >= n:
        start = max(0.0, min(lo_sec, max(0.0, duration - min_len)))
        end = min(duration, max(start + min_len, min(hi_sec, start + max_len)))
        a_i, b_i = to_idx(start), to_idx(end)
        reasons = {k: float(np.mean(v[a_i:b_i + 1])) for k, v in parts.items()}
        return [Clip(start=round(start, 3), end=round(end, 3), score=1.0,
                     peak=round(start, 3), reasons=reasons, label="whole span")]

    # float64: over a 6-hour stream a float32 running sum drifts enough to
    # reorder near-equal windows.
    csum = np.concatenate([[0.0], np.cumsum(score.astype(np.float64))])
    starts = np.arange(0, n - win, dtype=np.int64)
    window_score = (csum[starts + win] - csum[starts]) / win

    avail = (starts >= lo_i) & ((starts + win) <= hi_i)
    if not avail.any():
        avail = np.ones(starts.size, dtype=bool)

    vals = window_score[avail]
    median = float(np.median(vals))
    peak = float(np.percentile(vals, 99.5))
    spread = max(peak - median, 1e-9)
    # sensitivity 0 = only clearly outstanding moments, 1 = take what you can get.
    # The strict bar is relative to how big the peaks are; the top-up bar is a
    # plain rank, because a stream with two enormous spikes has a huge `spread`
    # and any fraction of it would still exclude everything else.
    strict_floor = median + spread * (0.62 - 0.52 * sensitivity)
    loose_floor = float(np.percentile(vals, max(5.0, 60.0 - 45.0 * sensitivity)))

    tight_gap = max(target_len * 1.15, 6.0)
    searched = max(hi_sec - lo_sec, 1.0)
    wide_gap = min(max(tight_gap, searched / (count * 2.2)), 420.0)

    clips: list[Clip] = []
    taken: list[tuple[float, float]] = []

    def harvest(min_score: float, gap_sec: float, tier: str) -> None:
        gap_i = max(1, int(gap_sec * FPS_ANALYSIS))
        guard = 0
        limit = count * 60 + 60
        while len(clips) < count and guard < limit:
            guard += 1
            if not avail.any():
                return
            masked = np.where(avail, window_score, -np.inf)
            best = int(np.argmax(masked))
            best_val = float(masked[best])
            if not np.isfinite(best_val) or best_val < min_score:
                return

            # Retire this candidate up front: whatever happens below, argmax must
            # not hand us the same window again.
            avail[max(0, best - gap_i // 2):best + gap_i // 2 + 1] = False

            s_sec, e_sec, peak_i = _resolve_clip(
                best, score, win=win, lo_i=lo_i, hi_i=hi_i, duration=duration,
                min_len=min_len, max_len=max_len, lead_in=lead_in, quiet=quiet)

            a = max(0, int((s_sec - gap_sec) * FPS_ANALYSIS))
            b = min(avail.size, int((e_sec + gap_sec) * FPS_ANALYSIS))
            avail[a:b] = False

            if any(not (e_sec <= t0 or s_sec >= t1) for t0, t1 in taken):
                continue
            if e_sec - s_sec < min_len * 0.75:
                continue

            a_i, b_i = to_idx(s_sec), to_idx(e_sec)
            reasons = {k: float(np.mean(v[a_i:b_i + 1])) for k, v in parts.items()}
            clips.append(Clip(
                start=round(s_sec, 3), end=round(e_sec, 3),
                score=round(best_val, 5), peak=round(peak_i / FPS_ANALYSIS, 3),
                reasons=reasons, tier=tier,
            ))
            taken.append((s_sec, e_sec))

    harvest(strict_floor, wide_gap, "strong")
    if len(clips) < count:
        harvest(strict_floor, tight_gap, "strong")
    # Topping up with weaker moments is what "generous" means. At the picky end
    # we would rather hand back three great clips than ten padded ones - but
    # never come back empty-handed.
    if len(clips) < count and (sensitivity >= 0.3 or len(clips) < 2):
        harvest(loose_floor, tight_gap, "filler")

    clips.sort(key=lambda c: -c.score)
    return clips[:count]


def _snap_quiet(quiet: np.ndarray | None, idx: int, radius: int, *,
                backwards: bool, lo: int, hi: int) -> int:
    """Nudge a cut point onto the quietest instant nearby."""
    if quiet is None or quiet.size == 0 or radius <= 0:
        return int(max(lo, min(idx, hi)))
    if backwards:
        a, b = max(lo, idx - radius), min(hi, idx + radius // 3)
    else:
        a, b = max(lo, idx - radius // 3), min(hi, idx + radius)
    if b <= a:
        return int(max(lo, min(idx, hi)))
    seg = quiet[a:b]
    if seg.size == 0:
        return int(max(lo, min(idx, hi)))
    return int(a + np.argmin(seg))


def timeline_for_ui(score: np.ndarray, duration: float, points: int = 720) -> list[float]:
    if score.size == 0:
        return []
    points = max(32, min(points, 2000))
    idx = np.linspace(0, score.size - 1, points).astype(int)
    vals = score[idx]
    hi = float(vals.max()) or 1.0
    return [round(float(v / hi), 3) for v in vals]


def _hms(sec: float) -> str:
    sec = int(max(0, sec))
    h, m, s = sec // 3600, (sec % 3600) // 60, sec % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def search_window(settings: dict[str, Any], duration: float) -> tuple[float, float]:
    """The stretch of the video to look in, as (start, end) seconds.

    "Search this part only" (range_start/range_end) and the skip-intro/outro
    trims are both honoured; whichever is tighter wins.
    """
    lo = max(float(settings.get("skip_intro", 0.0) or 0.0),
             float(settings.get("range_start", 0.0) or 0.0))
    hi = duration - float(settings.get("skip_outro", 0.0) or 0.0)
    r_end = float(settings.get("range_end", 0.0) or 0.0)
    if r_end > 0:
        hi = min(hi, r_end)
    lo = max(0.0, min(lo, max(0.0, duration - 1.0)))
    hi = max(lo + 1.0, min(hi, duration))
    return lo, hi


def _silent_plan(settings: dict[str, Any], duration: float) -> dict[str, Any]:
    """No audio track: there is nothing to score, so spread clips evenly."""
    manual = settings.get("manual_clips") or []
    if manual:
        clips = [Clip(start=float(m["start"]), end=float(m["end"]), score=1.0,
                      label=m.get("label", "")) for m in manual]
    elif settings.get("whole_video"):
        clips = [Clip(start=0.0, end=duration, score=1.0, label="Full video")]
    else:
        want = max(1, int(settings.get("clip_count", 5)))
        length = float(settings.get("target_len", 30.0))
        lo, hi = search_window(settings, duration)
        hi = max(lo + length, hi)
        span = max(hi - lo, length)
        step = span / want
        clips = []
        for i in range(want):
            start = lo + step * i
            end = min(hi, start + length)
            if end - start >= min(length, 3.0):
                clips.append(Clip(start=round(start, 3), end=round(end, 3), score=1.0))
    return {"clips": clips, "timeline": [], "signals": [],
            "chat_note": "no audio track - clips spaced evenly", "duration": duration}


def analyse(
    video: str | Path,
    wav_path: str | Path,
    *,
    duration: float,
    settings: dict[str, Any],
    vod_id: str = "",
    time_offset: float = 0.0,
    cancel: threading.Event | None = None,
    on_stage: Callable[[str, float, str], None] | None = None,
) -> dict[str, Any]:
    """Full detection run. Returns clips + a timeline for the UI."""

    def report(stage: str, frac: float, msg: str = "") -> None:
        if on_stage:
            on_stage(stage, frac, msg)

    if not settings.get("has_audio", True):
        return _silent_plan(settings, duration)

    report("audio", 0.0, "Extracting audio")
    if not Path(wav_path).is_file():
        extract_audio(video, wav_path, duration=duration,
                      on_progress=lambda f: report("audio", f * 0.55, "Extracting audio"),
                      cancel=cancel)
    report("audio", 0.55, "Measuring loudness")
    feats = audio_features(wav_path,
                           on_progress=lambda f: report("audio", 0.55 + f * 0.45, "Measuring loudness"),
                           cancel=cancel)

    chat_data = None
    chat_note = ""
    if settings.get("use_chat") and vod_id:
        try:
            from .chat import sample_density
            report("chat", 0.0, "Reading chat replay")
            times, rate, hype = sample_density(
                vod_id, duration + time_offset,
                cancel=cancel,
                on_progress=lambda f, m: report("chat", f, m),
            )
            if time_offset:
                times = times - time_offset
            chat_data = (times, rate, hype)
            chat_note = f"chat replay: {int(times.size)} probes"
        except Exception as exc:                              # noqa: BLE001
            chat_note = f"chat unavailable ({type(exc).__name__})"
            chat_data = None
    elif settings.get("use_chat"):
        chat_note = "chat only works on Twitch VODs"

    report("score", 0.2, "Scoring highlights")
    score, parts = build_score(
        feats, duration=duration, chat=chat_data,
        use_audio=bool(settings.get("use_audio", True)),
        use_speech=bool(settings.get("use_speech", True)),
    )
    quiet = _smooth(_db(feats["total"]), 0.4)

    win_lo, win_hi = search_window(settings, duration)

    manual = settings.get("manual_clips") or []
    if manual:
        clips = [Clip(start=float(m["start"]), end=float(m["end"]),
                      score=1.0, peak=float(m["start"]), label=m.get("label", ""))
                 for m in manual]
    elif settings.get("whole_video"):
        clips = [Clip(start=win_lo, end=win_hi, score=1.0, peak=win_lo,
                      label="Full video")]
    else:
        if win_lo > 0.5 or win_hi < duration - 0.5:
            report("score", 0.55,
                   f"Searching {_hms(win_lo)} to {_hms(win_hi)} only")
        report("score", 0.6, "Choosing the best moments")
        clips = pick_clips(
            score, parts,
            duration=duration,
            count=int(settings.get("clip_count", 5)),
            min_len=float(settings.get("clip_min", 18.0)),
            max_len=float(settings.get("clip_max", 45.0)),
            target_len=float(settings.get("target_len", 30.0)),
            sensitivity=float(settings.get("sensitivity", 0.5)),
            skip_intro=win_lo,
            skip_outro=max(0.0, duration - win_hi),
            lead_in=float(settings.get("lead_in", 1.5)),
            quiet=quiet,
        )

    report("score", 1.0, "Detection done")
    return {
        "clips": clips,
        "timeline": timeline_for_ui(score, duration),
        "signals": sorted(parts.keys()),
        "chat_note": chat_note,
        "duration": duration,
    }
