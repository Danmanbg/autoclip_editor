"""Speech-to-text with word-level timings, for burned-in captions."""
from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

_model_lock = threading.Lock()
_models: dict[tuple, Any] = {}


@dataclass
class Word:
    start: float
    end: float
    text: str

    def to_dict(self) -> dict[str, Any]:
        return {"start": round(self.start, 3), "end": round(self.end, 3), "text": self.text}


class TranscribeError(RuntimeError):
    pass


def _cpu_threads() -> int:
    n = os.cpu_count() or 4
    return max(2, min(n - 1 if n > 4 else n, 16))


def _local_dir(size: str) -> Path | None:
    from .paths import MODELS_DIR
    folder = MODELS_DIR / size
    return folder if (folder / "model.bin").is_file() else None


def _in_hf_cache(size: str) -> bool:
    home = os.environ.get("HF_HOME") or os.path.join(
        os.environ.get("USERPROFILE") or os.path.expanduser("~"), ".cache", "huggingface"
    )
    hub = Path(home) / "hub"
    if not hub.is_dir():
        hub = Path(home)
    if not hub.is_dir():
        return False
    needle = f"faster-whisper-{size}".lower()
    try:
        for entry in hub.iterdir():
            if needle in entry.name.lower() and any(entry.rglob("model.bin")):
                return True
    except OSError:
        pass
    return False


def is_model_cached(size: str) -> bool:
    """True if the weights are already on disk, so we can warn about a download."""
    return _local_dir(size) is not None or _in_hf_cache(size)


def download_model(size: str, status: Callable[[str], None] = lambda m: None) -> Path:
    """Fetch Systran/faster-whisper-<size> into models/<size>."""
    from .downloads import hf_download
    from .paths import MODELS_DIR

    folder = MODELS_DIR / size
    if (folder / "model.bin").is_file():
        return folder
    return hf_download(f"Systran/faster-whisper-{size}", folder,
                       label=f"the {size} speech model", status=status)


def load_model(size: str = "small", compute_type: str = "int8",
               status: Callable[[str], None] = lambda m: None):
    """Load (and cache) a faster-whisper model.

    Order: our own models/ folder, then the Hugging Face cache without touching
    the network, then the parallel downloader above.
    """
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise TranscribeError(
            "faster-whisper is not installed. Run:  py -3 -m pip install faster-whisper"
        ) from exc
    key = (size, compute_type)
    with _model_lock:
        if key in _models:
            return _models[key]
        opts = dict(device="cpu", compute_type=compute_type,
                    cpu_threads=_cpu_threads(), num_workers=1)
        local = _local_dir(size)
        model = None
        if local is not None:
            model = WhisperModel(str(local), **opts)
        else:
            try:
                model = WhisperModel(size, local_files_only=True, **opts)
            except Exception:                                     # noqa: BLE001
                model = WhisperModel(str(download_model(size, status)), **opts)
        _models[key] = model
        return model


def transcribe(
    media: str | Path,
    *,
    model_size: str = "small",
    language: str = "auto",
    duration: float | None = None,
    on_progress: Callable[[float, str], None] | None = None,
    cancel: threading.Event | None = None,
) -> tuple[list[Word], str]:
    """Return (words, detected_language)."""
    if on_progress:
        if is_model_cached(model_size):
            on_progress(0.02, f"Loading {model_size} speech model")
        else:
            on_progress(0.02, f"Downloading {model_size} speech model (one time)")

    model = load_model(model_size, status=lambda m: on_progress(0.03, m) if on_progress else None)
    lang = None if (not language or language == "auto") else language

    if on_progress:
        on_progress(0.08, "Listening for speech")

    segments, info = model.transcribe(
        str(media),
        language=lang,
        word_timestamps=True,
        vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 350},
        beam_size=5,
        condition_on_previous_text=False,
        temperature=[0.0, 0.2, 0.4],
    )
    total = duration or getattr(info, "duration", 0.0) or 0.0
    words: list[Word] = []
    for seg in segments:
        if cancel is not None and cancel.is_set():
            from .ffmpeg_tools import Cancelled
            raise Cancelled("cancelled")
        for w in (getattr(seg, "words", None) or []):
            text = (w.word or "").strip()
            if not text:
                continue
            start = float(w.start)
            end = max(float(w.end), start + 0.06)
            words.append(Word(start=start, end=end, text=text))
        if on_progress and total:
            on_progress(min(0.98, 0.08 + 0.9 * (float(seg.end) / total)), "Transcribing")

    words.sort(key=lambda w: w.start)
    # Remove overlaps so caption timings stay monotonic.
    for i in range(1, len(words)):
        if words[i].start < words[i - 1].end:
            words[i].start = words[i - 1].end
        if words[i].end <= words[i].start:
            words[i].end = words[i].start + 0.06

    if on_progress:
        on_progress(1.0, f"{len(words)} words")
    detected = getattr(info, "language", "") or (lang or "")
    return words, detected


def _quiet_cut(samples, target: int, search: int, sr: int) -> int:
    """Index of the quietest 50 ms near `target`, looking back up to `search`."""
    import numpy as np
    lo = max(0, target - search)
    seg = samples[lo:target]
    hop = max(1, sr // 20)
    if seg.size < hop * 2:
        return target
    frames = seg[: (seg.size // hop) * hop].reshape(-1, hop).astype(np.float32)
    energy = (frames * frames).mean(axis=1)
    return lo + int(np.argmin(energy)) * hop + hop // 2


def transcribe_long(
    wav_path: str | Path,
    *,
    model_size: str = "base",
    language: str = "auto",
    chunk_seconds: float = 900.0,
    on_progress: Callable[[float, str], None] | None = None,
    cancel: threading.Event | None = None,
) -> tuple[list[Word], str]:
    """Transcribe hours of audio without holding all of it in memory.

    Works through a 16 kHz mono wav in ~15 minute pieces, cutting each at the
    quietest moment before the boundary so a word is not sliced in half.
    """
    import wave

    import numpy as np

    from .ffmpeg_tools import Cancelled

    if on_progress:
        on_progress(0.0, f"Loading {model_size} speech model"
                         + ("" if is_model_cached(model_size) else " (one-time download)"))
    model = load_model(model_size, status=lambda m: on_progress(0.03, m) if on_progress else None)
    lang = None if (not language or language == "auto") else language

    words: list[Word] = []
    detected = lang or ""
    with wave.open(str(wav_path), "rb") as wf:
        if wf.getnchannels() != 1 or wf.getsampwidth() != 2:
            raise TranscribeError("expected a 16-bit mono wav")
        sr = wf.getframerate()
        total = wf.getnframes()
        chunk = int(chunk_seconds * sr)
        pos = 0
        carry = np.zeros(0, dtype=np.int16)
        while pos < total or carry.size:
            if cancel is not None and cancel.is_set():
                raise Cancelled("cancelled")
            want = chunk - carry.size
            raw = wf.readframes(max(0, want)) if pos < total else b""
            fresh = np.frombuffer(raw, dtype="<i2")
            pos += fresh.size
            buf = np.concatenate([carry, fresh]) if carry.size else fresh
            if buf.size == 0:
                break
            if pos < total:
                cut = _quiet_cut(buf, buf.size, int(8 * sr), sr)
                piece, carry = buf[:cut], buf[cut:].copy()
            else:
                piece, carry = buf, np.zeros(0, dtype=np.int16)
            offset = (pos - fresh.size - (buf.size - fresh.size)) / sr
            audio = piece.astype(np.float32) / 32768.0

            segments, info = model.transcribe(
                audio, language=lang, word_timestamps=True, vad_filter=True,
                vad_parameters={"min_silence_duration_ms": 350}, beam_size=5,
                condition_on_previous_text=False, temperature=[0.0, 0.2, 0.4],
            )
            if not detected:
                detected = getattr(info, "language", "") or ""
            piece_len = piece.size / sr
            for seg in segments:
                if cancel is not None and cancel.is_set():
                    raise Cancelled("cancelled")
                for w in (getattr(seg, "words", None) or []):
                    text = (w.word or "").strip()
                    if text:
                        s = offset + float(w.start)
                        words.append(Word(start=s, end=max(offset + float(w.end), s + 0.06),
                                          text=text))
                if on_progress and total:
                    done = (offset + min(float(seg.end), piece_len)) * sr
                    on_progress(min(0.99, done / total),
                                f"Transcribing  {_hhmm(offset + float(seg.end))} / "
                                f"{_hhmm(total / sr)}")

    words.sort(key=lambda w: w.start)
    if on_progress:
        on_progress(1.0, f"{len(words)} words transcribed")
    return words, detected


def _hhmm(sec: float) -> str:
    sec = int(max(0, sec))
    return f"{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}"
