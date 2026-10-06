"""Finding profanity in a transcript and bleeping it out.

Matching is done on whole words from Whisper, as `stem + known suffix`, never as
a bare substring - that is what keeps "assassin", "assess" and "classic" out of
the results. Anything that still slips through goes in ALLOW.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from .transcribe import Word

# Words that reliably cost you monetisation.
STRONG = [
    "fuck", "motherfuck", "motherfucker", "clusterfuck", "fuckwit", "fuckboy",
    "shit", "bullshit", "horseshit", "dogshit", "shithead", "shitbag",
    "cunt", "nigger", "nigga", "bitch", "bastard", "whore", "slut",
    "dick", "dickhead", "cock", "prick", "pussy", "twat", "wanker",
    "faggot", "fag", "dyke", "tranny", "retard", "retarded",
    "asshole", "arsehole", "dumbass", "jackass",
    "bollocks", "wank", "jerkoff", "blowjob", "handjob", "cum", "jizz",
    "goddamn", "goddamnit", "chink", "spic", "wetback", "kike", "paki",
]

# Fine on most platforms; off unless the user asks for them.
MILD = [
    "damn", "damnit", "dammit", "hell", "crap", "crappy", "piss", "pissed",
    "ass", "arse", "bloody", "bugger", "sod", "bollock", "bint",
    "badass", "smartass",
    "douche", "douchebag", "screwed", "freaking", "frigging", "shag",
]

# Endings we will accept after a stem. Anything else means it is a different word.
SUFFIXES = [
    "", "s", "es", "ed", "ing", "in", "ins", "er", "ers", "y", "ies",
    "head", "heads", "face", "faces", "hole", "holes", "bag", "bags",
    "wad", "wads", "tard", "tards",
]

# Innocent words that happen to survive the stem+suffix test.
ALLOW = {
    "assess", "assessed", "assessing", "assessment", "asset", "assets",
    "assign", "assigned", "assist", "assisted", "assume", "assumed",
    "assure", "assured", "assassin", "assassins", "associate", "association",
    "cocktail", "cocktails", "cockpit", "cockpits", "peacock", "peacocks",
    "shiitake", "titan", "titans", "analysis", "analyst", "analytics",
    "bassline", "classic", "classics", "scunthorpe", "penistone",
    "dickens", "dickinson", "cumulative", "cumbersome", "cucumber",
    "document", "documents", "circumstance", "circumstances", "accumulate",
    "hello", "shell", "shelled", "shelter", "crape", "crapper",
    "grass", "glasses", "passing", "massive", "canada", "cannot",
    "cocky", "cocker", "cockers", "cumin", "cumming", "dicky",
}

_CLEAN = re.compile(r"[^a-z]+")
# Endings that double a final consonant first: shit -> shitty, crap -> crapped.
_DOUBLING = ("y", "ier", "iest", "ed", "ing", "in", "er", "ers")
_VOWELS = set("aeiou")
# Whisper sometimes censors its own output ("f***ing"). A word with letters and
# asterisks in it is treated as a hit.
_REDACTED = re.compile(r"^[a-z]{0,2}\*{2,}[a-z]*$", re.I)
_EDGE_PUNCT = re.compile(r"^[^\w*]+|[^\w*]+$")


@dataclass
class Hit:
    start: float
    end: float
    word: str
    matched: str

    def to_dict(self) -> dict[str, Any]:
        return {"start": round(self.start, 3), "end": round(self.end, 3),
                "word": self.word, "matched": self.matched}


class Matcher:
    """Expands stems into the full set of word forms, once."""

    def __init__(self, stems: Iterable[str], allow: Iterable[str] = ()):
        self.allow = {_CLEAN.sub("", a.lower()) for a in allow if a.strip()}
        self.forms: dict[str, str] = {}
        for stem in stems:
            stem = _CLEAN.sub("", stem.strip().lower())
            if len(stem) < 2:
                continue
            for suf in SUFFIXES:
                self.forms.setdefault(_CLEAN.sub("", stem + suf), stem)
            last = stem[-1]
            if last not in _VOWELS and len(stem) >= 3 and stem[-2] in _VOWELS:
                for suf in _DOUBLING:
                    self.forms.setdefault(stem + last + suf, stem)

    def check(self, raw: str) -> str | None:
        """Return the stem this word matches, or None."""
        text = (raw or "").strip()
        if _REDACTED.match(_EDGE_PUNCT.sub("", text)):
            return "redacted"
        word = _CLEAN.sub("", text.lower())
        if not word or word in self.allow or word in ALLOW:
            return None
        return self.forms.get(word)

    def __len__(self) -> int:
        return len(self.forms)


def build_matcher(settings: dict[str, Any]) -> Matcher:
    stems: list[str] = list(STRONG)
    if settings.get("censor_mild"):
        stems += MILD
    extra = settings.get("censor_extra") or ""
    if isinstance(extra, str):
        extra = re.split(r"[,\n;]+", extra)
    stems += [str(w) for w in (extra or [])]
    allow = settings.get("censor_allow") or ""
    if isinstance(allow, str):
        allow = re.split(r"[,\n;]+", allow)
    return Matcher(stems, [str(a) for a in (allow or [])])


def find_hits(words: Sequence[Word], matcher: Matcher) -> list[Hit]:
    hits: list[Hit] = []
    for w in words:
        stem = matcher.check(w.text)
        if stem:
            hits.append(Hit(start=float(w.start), end=float(w.end),
                            word=w.text.strip(), matched=stem))
    return hits


def hits_to_spans(hits: Sequence[Hit], pad: float = 0.06,
                  limit: float | None = None) -> list[tuple[float, float]]:
    """Padded, merged, non-overlapping time spans to silence."""
    spans: list[tuple[float, float]] = []
    for h in hits:
        a = max(0.0, h.start - pad)
        b = h.end + pad
        if limit is not None:
            b = min(b, limit)
        if b <= a:
            continue
        if spans and a <= spans[-1][1] + 0.02:
            spans[-1] = (spans[-1][0], max(spans[-1][1], b))
        else:
            spans.append((a, b))
    return spans


def mask_word(text: str) -> str:
    """fuck -> f***, keeping any punctuation that was attached.

    Idempotent: '*' counts as part of the word, so masking 's***' again gives
    's***' rather than '****' - transcripts get masked more than once on the
    way to a title.
    """
    def is_word(ch: str) -> bool:
        return ch.isalnum() or ch == "*"
    core = text.strip()
    lead = ""
    trail = ""
    while core and not is_word(core[0]):
        lead, core = lead + core[0], core[1:]
    while core and not is_word(core[-1]):
        trail, core = core[-1] + trail, core[:-1]
    if not core:
        return text
    if len(core) == 1:
        return lead + "*" + trail
    return lead + core[0] + "*" * (len(core) - 1) + trail


def mask_words(words: Sequence[Word], matcher: Matcher) -> list[Word]:
    """A copy of the transcript with profanity starred out, for the captions."""
    out: list[Word] = []
    for w in words:
        text = mask_word(w.text) if matcher.check(w.text) else w.text
        out.append(Word(start=w.start, end=w.end, text=text))
    return out


# ffmpeg's expression evaluator gives up somewhere between 50 and 100 chained
# between() terms, so spans are split into chunks with one volume filter each.
CHUNK = 40


def _duck_chain(spans: Sequence[tuple[float, float]]) -> str:
    """Chained volume filters that silence every span.

    Each filter carries an `enable` window covering only its own chunk, so its
    40-term expression is evaluated during that stretch and bypassed elsewhere -
    which is what keeps a 2-hour, 900-bleep stream at a few seconds of work.
    Spans are merged before this, so chunks never overlap and the product of the
    per-chunk gains equals the gain for all spans together.
    """
    parts = []
    for i in range(0, len(spans), CHUNK):
        chunk = spans[i:i + CHUNK]
        terms = "+".join(f"between(t,{a:.3f},{b:.3f})" for a, b in chunk)
        lo, hi = max(0.0, chunk[0][0] - 0.05), chunk[-1][1] + 0.05
        parts.append(f"volume=volume='1-min(1,{terms})':eval=frame:"
                     f"enable='between(t,{lo:.3f},{hi:.3f})'")
    return ",".join(parts)


def audio_filter(
    spans: Sequence[tuple[float, float]],
    duration: float,
    *,
    action: str = "bleep",
    tone_hz: int = 1000,
    gain: float = 0.25,
    sample_rate: int = 48000,
    in_label: str = "0:a",
    out_label: str = "acen",
    loudnorm: bool = False,
) -> str:
    """filter_complex fragment that mutes - and optionally bleeps over - `spans`.

    The bleep is `tone - (tone run through the same duck chain)`: exactly zero
    outside the spans and the full tone inside them, with no second set of
    expressions. Verified sample-exact: residual outside spans is 0.0.
    Long graphs must go through `-/filter_complex <file>`; see render.filter_args.
    """
    layout = f"aformat=sample_fmts=fltp:sample_rates={sample_rate}:channel_layouts=stereo"
    # eval=frame re-evaluates once per audio frame, so pin frames at 10 ms:
    # otherwise timing precision depends on the input (a WAV can arrive in
    # 100 ms+ frames and let the start of a word through).
    frames = f"asetnsamples=n={sample_rate // 100}:p=0"
    norm = ",loudnorm=I=-14:TP=-1.5:LRA=11" if loudnorm else ""
    if not spans:
        return f"[{in_label}]{layout}{norm}[{out_label}]"

    duck = _duck_chain(spans)
    main = f"[{in_label}]{layout},{frames},{duck}[cen_duck]"
    if action == "mute":
        return f"{main};[cen_duck]anull{norm}[{out_label}]"

    level = max(0.0, min(float(gain), 1.0))
    return ";".join([
        main,
        f"sine=frequency={int(tone_hz)}:sample_rate={sample_rate}:"
        f"duration={max(duration, 0.1) + 1:.3f},{layout},{frames},asplit=2[cen_t1][cen_t2]",
        # Invert with volume=-1: amix silently ignores a negative weight and
        # would ADD the two tones, leaving a constant beep over everything.
        f"[cen_t2]{duck},volume=-1[cen_tinv]",
        f"[cen_t1][cen_tinv]amix=inputs=2:normalize=0:"
        f"duration=first,volume={level:.3f}[cen_beep]",
        f"[cen_duck][cen_beep]amix=inputs=2:duration=first:normalize=0{norm}[{out_label}]",
    ])


def report(hits: Sequence[Hit], offset: float = 0.0) -> str:
    """A human-readable list of everything that was bleeped."""
    if not hits:
        return "Nothing was censored.\n"
    lines = [f"{len(hits)} word(s) censored", ""]
    counts: dict[str, int] = {}
    for h in hits:
        counts[h.matched] = counts.get(h.matched, 0) + 1
        t = h.start + offset
        stamp = f"{int(t // 3600):d}:{int(t % 3600 // 60):02d}:{t % 60:05.2f}"
        lines.append(f"  {stamp}   {mask_word(h.word)}")
    lines.append("")
    lines.append("Totals:")
    for stem, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        lines.append(f"  {mask_word(stem)} x{n}")
    return "\n".join(lines) + "\n"
