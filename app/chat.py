"""Twitch chat-replay sampling, used as a highlight signal.

Rather than paginating a whole 6-hour VOD (thousands of requests), we probe the
public replay endpoint at regular offsets. Each probe returns a page of messages;
how much wall-clock time that page spans tells us how fast chat was moving.
"""
from __future__ import annotations

import re
import threading

from . import APP_NAME, __version__
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable

GQL_URL = "https://gql.twitch.tv/gql"
# Public web client id, the same one twitch.tv itself ships to browsers.
CLIENT_ID = "kimne78kx3ncx6brgo4mv6wki5h1ko"
QUERY_HASH = "b70a3591ff0f4e0313d126c6a1502d79a1c02baebb288227c582044aa76adf6a"

HYPE_WORDS = {
    "lul", "omegalul", "kekw", "lmao", "lmfao", "pog", "poggers", "pogchamp",
    "pogu", "pepega", "monkas", "ez", "clip", "clipped", "clipit", "sheesh",
    "w", "dub", "insane", "actual", "nowayy", "noway", "wtf", "omg", "holy",
    "hypers", "pepelaugh", "icant", "sadge", "copium", "based", "gigachad",
    "goated", "cracked", "lolol", "hahaha", "jaja", "xd", "kek",
}
_WORD_RE = re.compile(r"[a-zA-Z]+")


class ChatUnavailable(RuntimeError):
    pass


def _session():
    import requests
    s = requests.Session()
    s.headers.update({
        "Client-ID": CLIENT_ID,
        "Content-Type": "application/json",
        "Accept": "*/*",
        "User-Agent": f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) {APP_NAME.replace(' ', '')}/{__version__}",
    })
    return s


def _fetch_page(sess, vod_id: str, offset: float) -> list[dict]:
    payload = [{
        "operationName": "VideoCommentsByOffsetOrCursor",
        "variables": {"videoID": str(vod_id), "contentOffsetSeconds": int(max(0, offset))},
        "extensions": {"persistedQuery": {"version": 1, "sha256Hash": QUERY_HASH}},
    }]
    resp = sess.post(GQL_URL, json=payload, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    if isinstance(data, list):
        data = data[0] if data else {}
    if data.get("errors"):
        raise ChatUnavailable(str(data["errors"])[:200])
    video = ((data.get("data") or {}).get("video") or {})
    if not video:
        raise ChatUnavailable("no chat replay for this video")
    edges = (video.get("comments") or {}).get("edges") or []
    out = []
    for e in edges:
        node = e.get("node") or {}
        try:
            t = float(node.get("contentOffsetSeconds") or 0)
        except (TypeError, ValueError):
            continue
        frags = ((node.get("message") or {}).get("fragments") or [])
        text = " ".join((f.get("text") or "") for f in frags)
        out.append({"t": t, "text": text})
    return out


def _hype_score(text: str) -> float:
    words = _WORD_RE.findall(text.lower())
    if not words:
        return 1.0
    hits = sum(1 for w in words if w in HYPE_WORDS)
    caps = 1.0
    letters = [c for c in text if c.isalpha()]
    if len(letters) >= 6 and sum(1 for c in letters if c.isupper()) / len(letters) > 0.75:
        caps = 1.3
    return (1.0 + min(hits, 4) * 0.6) * caps


def sample_density(
    vod_id: str,
    duration: float,
    *,
    step: float = 25.0,
    workers: int = 8,
    cancel: threading.Event | None = None,
    on_progress: Callable[[float, str], None] | None = None,
    max_probes: int = 1400,
):
    """Return (times, rate, hype) numpy arrays sampled across the VOD.

    rate  = messages per second around that offset
    hype  = average excitement weight of those messages
    """
    import numpy as np

    if duration <= 0:
        raise ChatUnavailable("unknown duration")
    step = max(step, duration / max_probes)
    offsets = list(np.arange(0.0, max(duration - 1.0, 1.0), step))
    if not offsets:
        raise ChatUnavailable("nothing to sample")

    sess = _session()
    # Probe once up front so an unavailable VOD fails fast and cheaply.
    first = _fetch_page(sess, vod_id, offsets[0])

    results: dict[float, tuple[float, float]] = {}

    def measure(page: list[dict], at: float) -> tuple[float, float]:
        if not page:
            return 0.0, 1.0
        window = [m for m in page if at - 1 <= m["t"] <= at + step + 30]
        use = window or page
        span = max(use[-1]["t"] - use[0]["t"], 1.0)
        rate = len(use) / span
        hype = sum(_hype_score(m["text"]) for m in use) / max(len(use), 1)
        return rate, hype

    results[offsets[0]] = measure(first, offsets[0])
    todo = offsets[1:]
    done = 1
    total = len(offsets)
    errors = 0

    def work(off: float):
        if cancel is not None and cancel.is_set():
            return off, None
        try:
            return off, _fetch_page(sess, vod_id, off)
        except Exception:
            return off, None

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = [pool.submit(work, off) for off in todo]
        for fut in as_completed(futures):
            off, page = fut.result()
            done += 1
            if page is None:
                errors += 1
            else:
                results[off] = measure(page, off)
            if on_progress and done % 8 == 0:
                on_progress(done / total, f"Reading chat replay {done}/{total}")
            if cancel is not None and cancel.is_set():
                break

    if cancel is not None and cancel.is_set():
        raise ChatUnavailable("cancelled")
    if len(results) < max(3, total * 0.25):
        raise ChatUnavailable(f"chat replay mostly unavailable ({errors} failed probes)")

    times = np.array(sorted(results), dtype=float)
    rate = np.array([results[t][0] for t in times], dtype=float)
    hype = np.array([results[t][1] for t in times], dtype=float)
    return times, rate, hype
