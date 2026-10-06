"""Title and description suggestions, built from what was actually said.

The main suggestions are extracted, not generated: for each moment we find the
sentence that is the punchline (near the loudest instant, exclaimed, the right
length) and tidy it up. That keeps titles in the streamer's own voice and
never invents something that did not happen.

A small local model (Qwen2.5-1.5B, run on the CPU) adds "<Game> but ..." ideas
- the one format it handles reliably at that size. Descriptions are assembled
from facts (timestamps, game, tags) and never written by the model, which was
found to misstate what happened.
"""
from __future__ import annotations

import gc
import math
import random
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from .transcribe import Word

TITLE_MAX = 70
SENT_END = ".!?"
FILLER_START = {
    "and", "so", "okay", "ok", "oh", "um", "uh", "like", "but", "yeah", "well",
    "alright", "right", "anyway", "anyways", "hmm", "erm", "now", "then", "just",
    "chat", "guys", "bro", "dude",
}
NOT_A_TITLE = re.compile(
    r"thank(s| you) for watching|subscribe|like and subscribe|see you (next|in the next)|"
    r"amara\.org|subtitles by", re.I)
_CAPS_WORD = re.compile(r"\b[A-Z]{2,}\b")
_GAME_STOP = {"so", "is", "and", "with", "but", "until", "while", "because", "for",
              "to", "again", "part", "ep", "episode", "day", "first", "blind", "w",
              "til", "till", "then", "after", "before", "at", "in", "on", "it"}


@dataclass
class Moment:
    """One highlight: where it is in the stream, what was said, where it lands in the output."""
    start: float
    end: float
    words: list[Word] = field(default_factory=list)
    peak: float | None = None
    score: float = 0.0
    out_at: float | None = None          # position in the finished video, for chapters
    line: str = ""                       # best line, filled by pick_lines()


# ---------------------------------------------------------------------------
# best line


def _sentences(words: Sequence[Word]) -> list[list[Word]]:
    out: list[list[Word]] = []
    cur: list[Word] = []
    for w in words:
        cur.append(w)
        t = w.text.strip()
        if t and t[-1] in SENT_END:
            out.append(cur)
            cur = []
        elif len(cur) >= 16 and t.endswith(","):
            out.append(cur)
            cur = []
    if cur:
        out.append(cur)
    return [s for s in out if s]


def _tidy(text: str) -> str:
    text = " ".join(text.split())
    # "no no no no no" -> "no no no"
    text = re.sub(r"\b(\w+)(?:[ ,]+\1\b){3,}", r"\1 \1 \1", text, flags=re.I)
    words = text.split(" ")
    while len(words) > 2 and words[0].strip(",.!?").lower() in FILLER_START:
        words = words[1:]
    text = " ".join(words).strip(" ,;:-")
    if text and text[0].islower():
        text = text[0].upper() + text[1:]
    if len(text) > TITLE_MAX:
        cut = text[:TITLE_MAX].rsplit(" ", 1)[0].rstrip(",;:-")
        text = cut + "..."
    return text


_LOW_INFO = {"yeah", "yes", "no", "okay", "ok", "oh", "wait", "what", "um", "uh", "like",
             "so", "the", "a", "and", "it", "is", "that", "this", "go", "lets", "let's",
             "chat", "guys", "bro", "dude", "man", "god", "omg", "wow", "huh", "hey",
             "was", "were", "is", "are", "be", "been", "am", "do", "did", "of", "to"}


def _score_sentence(sent: Sequence[Word], peak: float | None) -> float:
    text = " ".join(w.text.strip() for w in sent)
    n = len(sent)
    if NOT_A_TITLE.search(text):
        return -10.0
    tokens = [re.sub(r"[^a-z']", "", w.text.lower()) for w in sent]
    tokens = [t for t in tokens if t]
    # A starred-out swear is not content: "S***, what the f*** was that?" says nothing.
    content = {re.sub(r"[^a-z']", "", w.text.lower()) for w in sent
               if "*" not in w.text} - _LOW_INFO - {""}
    # "NO NO NO!" and "yeah yeah okay" are reactions, not titles
    if len(content) < 2:
        return -8.0
    if n < 3:
        return -5.0 + n
    score = 0.0
    score -= abs(n - 7) * 0.18                       # 4-10 words reads best
    if n > 16:
        score -= 2.0
    if len(set(tokens)) / max(len(tokens), 1) < 0.6:  # mostly repetition
        score -= 1.5
    last = sent[-1].text.strip()
    if last.endswith("!"):
        score += 1.0
    elif last.endswith("?"):
        score += 0.6
    score += min(len(_CAPS_WORD.findall(text)), 3) * 0.25
    if re.search(r"\b(I|I'm|I've|me|my|we|we're)\b", text):
        score += 0.3
    if "*" in text:                                   # starred-out swearing
        score -= 0.8
    if peak is not None:
        mid = (sent[0].start + sent[-1].end) / 2
        score += 1.6 * math.exp(-abs(mid - peak) / 6.0)
    return score


def best_line(words: Sequence[Word], peak: float | None = None) -> str:
    """The sentence most likely to be the punchline, tidied into a title.

    Two short sentences in a row are also tried together, because a lot of
    stream humour is setup + punchline: "Everyone said Xen is bad. Everyone
    was right."
    """
    sents = _sentences(words)
    candidates: list[tuple[list[Word], float]] = [(s, 0.0) for s in sents]
    for a, b in zip(sents, sents[1:]):
        if len(a) <= 7 and len(b) <= 7 and b[0].start - a[-1].end < 1.2:
            candidates.append((a + b, 0.4))
    best, best_score = "", -1e9
    for sent, bonus in candidates:
        s = _score_sentence(sent, peak) + bonus
        if s > best_score:
            best_score = s
            best = " ".join(w.text.strip() for w in sent)
    return _tidy(best) if best_score > -2.5 else ""


def pick_lines(moments: Sequence[Moment], matcher=None) -> None:
    """Fill moment.line, starring out swearing - titles get flagged too."""
    from . import censor
    for m in moments:
        words = censor.mask_words(m.words, matcher) if matcher is not None else m.words
        m.line = best_line(words, m.peak)


# ---------------------------------------------------------------------------
# game name


def guess_game(stream_title: str) -> str:
    """'Playing Half-Life so i can wait for Half-Life 3' -> 'Half-Life'."""
    t = (stream_title or "").strip()
    if not t:
        return ""
    for sep in ("|", " - ", " — ", ":"):
        if sep in t:
            parts = [p.strip() for p in t.split(sep) if p.strip()]
            # the shorter side of "Title | Game" is usually the game
            if len(parts) >= 2:
                cand = min(parts, key=len)
                if 1 <= len(cand.split()) <= 4:
                    return cand
    m = re.match(r"(?i)^\s*(?:playing|let'?s play|first time playing|trying)\s+(.+)$", t)
    if m:
        out = []
        for word in m.group(1).split():
            bare = word.strip(",.!?").lower()
            if out and (bare in _GAME_STOP or word[:1].islower() and not word[:1].isdigit()):
                break
            out.append(word.strip(",.!?"))
            if word[-1:] in ",.!?":
                break
        return " ".join(out[:5])
    return ""


def _hashtag(text: str) -> str:
    tag = re.sub(r"[^A-Za-z0-9]", "", text)
    return f"#{tag.lower()}" if tag else ""


def _tags(game: str, hashtags: str, shorts: bool = False) -> str:
    """'#shorts #halflife #gaming ...' - the game's tag, then the user's, no repeats."""
    out = ["#shorts"] if shorts else []
    out.append(_hashtag(game))
    out += [_hashtag(t) for t in re.split(r"[\s,]+", hashtags or "") if t.strip("#")]
    seen: set[str] = set()
    return " ".join(t for t in out if t and not (t in seen or seen.add(t)))


# ---------------------------------------------------------------------------
# suggestions


def _stamp(sec: float) -> str:
    sec = int(max(0, sec))
    h, m, s = sec // 3600, (sec % 3600) // 60, sec % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def quote_titles(moments: Sequence[Moment], game: str, limit: int = 6) -> list[str]:
    """Titles from the streamer's own best lines, strongest moments first."""
    ranked = sorted((m for m in moments if m.line), key=lambda m: -m.score)
    seen: set[str] = set()
    out: list[str] = []
    for i, m in enumerate(ranked):
        key = re.sub(r"[^a-z]", "", m.line.lower())[:40]
        if not key or key in seen:
            continue
        seen.add(key)
        line = m.line.rstrip(".")
        if game and i % 2 == 0 and len(line) + len(game) + 3 <= 100:
            out.append(f"{line} | {game}")
        else:
            out.append(line)
        if len(out) >= limit:
            break
    return out


def _chapter_label(line: str, max_words: int = 7) -> str:
    words = line.rstrip(".!?").split()
    label = " ".join(words[:max_words])
    return label + ("..." if len(words) > max_words else "")


def description(
    moments: Sequence[Moment],
    *,
    kind: str,
    game: str = "",
    stream_title: str = "",
    source_url: str = "",
    duration: float = 0.0,
    hashtags: str = "#gaming",
) -> str:
    """A ready-to-paste description. Facts only - nothing invented.

    When moments carry `out_at` positions they become YouTube chapters: the list
    starts at 0:00 and every entry is at least 10 s apart, which is what YouTube
    needs to show them as chapters.
    """
    lines: list[str] = []
    timed = sorted((m for m in moments if m.out_at is not None and m.line),
                   key=lambda m: m.out_at)

    if kind == "reel":
        lead = f"The best moments from my {game} stream." if game else \
            "The best moments from my stream."
        lines.append(lead)
    elif kind == "clean":
        lead = f"Full {game} stream." if game else "Full stream."
        if stream_title:
            lead = f"{stream_title}\n\n{lead}"
        lines.append(lead)
    else:
        lines.append(timed[0].line if timed else (moments[0].line if moments else ""))

    if timed and kind in ("reel", "clean"):
        lines.append("")
        lines.append("Chapters" if kind == "reel" else "Best moments")
        last = -1e9
        if timed[0].out_at > 10 or kind == "clean":
            lines.append(f"0:00 Start")
            last = 0.0
        for m in timed:
            if m.out_at - last < 10:
                continue
            lines.append(f"{_stamp(m.out_at)} {_chapter_label(m.line)}")
            last = m.out_at

    if source_url and kind != "clips":
        lines += ["", f"Originally streamed: {source_url}"]
    tags = _tags(game, hashtags, shorts=kind == "clips")
    if tags:
        lines += ["", tags]
    return "\n".join(lines).strip() + "\n"


def short_description(title: str, game: str, hashtags: str = "#gaming") -> str:
    return f"{title}\n\n{_tags(game, hashtags, shorts=True)}\n"


# ---------------------------------------------------------------------------
# local model ideas

_llm_lock = threading.Lock()


def llm_path() -> Path | None:
    from .paths import MODELS_DIR
    folder = MODELS_DIR / "llm"
    if not folder.is_dir():
        return None
    files = sorted(folder.glob("*.gguf"), key=lambda p: p.stat().st_size)
    return files[0] if files else None


LLM_REPO = "Qwen/Qwen2.5-1.5B-Instruct-GGUF"
LLM_FILE = "qwen2.5-1.5b-instruct-q4_k_m.gguf"      # ~1.1 GB, Apache-2.0


def download_llm(status=print) -> Path:
    """Fetch the optional model behind the AI title ideas into models/llm."""
    from .downloads import hf_download
    from .paths import MODELS_DIR
    folder = MODELS_DIR / "llm"
    if (folder / LLM_FILE).is_file():
        status(f"Already have {LLM_FILE}")
        return folder
    return hf_download(LLM_REPO, folder, include=[LLM_FILE],
                       label="the AI title model", status=status)


_IDEA_SYSTEM = ("Write a YouTube title in the form '<Game> but <funny situation>'. "
                "Under 55 characters. Reply with the title only.")
_IDEA_SHOTS = [
    ("Game: Minecraft\nMoments: a creeper blew up my house; I fell in lava twice",
     "Minecraft but everything I build explodes"),
    ("Game: Elden Ring\nMoments: the boss had two health bars; I died 40 times",
     "Elden Ring but the boss refuses to die"),
]


def ai_ideas(game: str, lines: Sequence[str], n: int = 5,
             matcher=None) -> tuple[list[str], str]:
    """'<Game> but ...' titles from the local model. Returns (titles, note).

    The model is loaded for the call and released afterwards: this laptop has
    little free RAM, and Whisper may already be holding some.
    """
    path = llm_path()
    if path is None:
        return [], "Optional: run  py -3 run.py --get-ai-model  once (1.1 GB) to turn these on."
    lines = [l for l in lines if l][:6]
    if not lines:
        return [], "Nothing was said clearly enough to base ideas on."
    try:
        from llama_cpp import Llama
    except ImportError:
        return [], "Install llama-cpp-python to turn these on - see the README."

    from . import censor
    game = game or "This game"
    base = [{"role": "system", "content": _IDEA_SYSTEM}]
    for u, a in _IDEA_SHOTS:
        base += [{"role": "user", "content": u}, {"role": "assistant", "content": a}]

    def messages(attempt: int) -> list[dict[str, str]]:
        # A model this small latches onto whatever it read last, so each
        # attempt puts a different moment at the end.
        k = attempt % len(lines)
        order = lines[k + 1:] + lines[:k + 1]
        return base + [{"role": "user",
                        "content": f"Game: {game}\nMoments: " + "; ".join(order)}]

    out: list[str] = []
    seen: set[str] = set()
    with _llm_lock:
        # A fresh seed each call, or "suggest again" returns the same list.
        llm = Llama(model_path=str(path), n_ctx=2048, n_threads=4, n_batch=256,
                    seed=random.randint(1, 2**31 - 1), verbose=False)
        try:
            for attempt in range(n * 2):
                if len(out) >= n:
                    break
                res = llm.create_chat_completion(messages=messages(attempt), temperature=0.9,
                                                 max_tokens=32, stop=["\n"])
                t = res["choices"][0]["message"]["content"].strip().strip('"').strip()
                key = re.sub(r"[^a-z]", "", t.lower())
                if not t or key in seen or len(t) > 80 or "#" in t:
                    continue
                if not t.lower().startswith(game.lower()[:4]):
                    continue
                seen.add(key)
                if matcher is not None:
                    t = " ".join(censor.mask_word(w) if matcher.check(w) else w
                                 for w in t.split())
                out.append(t)
        finally:
            del llm
            gc.collect()
    return out, "Written by a small local AI - some will miss, pick what fits."


# ---------------------------------------------------------------------------
# one call for the pipelines


def build(moments: Sequence[Moment], *, kind: str, rec: dict[str, Any],
          matcher=None, game: str = "", hashtags: str = "#gaming") -> dict[str, Any]:
    """Everything the UI shows in the titles panel, minus the slow AI ideas."""
    pick_lines(moments, matcher)
    game = game or guess_game(rec.get("title") or "")
    titles = quote_titles(moments, game)
    desc = description(moments, kind=kind, game=game,
                       stream_title=rec.get("title") or "",
                       source_url=rec.get("url") or "",
                       duration=float(rec.get("duration") or 0),
                       hashtags=hashtags)
    return {
        "kind": kind,
        "game": game,
        "hashtags": hashtags,
        "quotes": titles,
        "lines": [m.line for m in sorted(moments, key=lambda m: -m.score) if m.line][:6],
        "description": desc,
        "ai_available": llm_path() is not None,
    }


def save(folder: Path, stem: str, data: dict[str, Any]) -> Path:
    path = folder / f"{stem} - titles and description.txt"
    parts = ["TITLE IDEAS (from what you said)", ""]
    parts += [f"  {t}" for t in data.get("quotes") or ["(nothing quotable was said)"]]
    if data.get("ai"):
        parts += ["", "AI IDEAS", ""] + [f"  {t}" for t in data["ai"]]
    parts += ["", "DESCRIPTION", "", data.get("description") or ""]
    path.write_text("\n".join(parts), encoding="utf-8")
    return path
