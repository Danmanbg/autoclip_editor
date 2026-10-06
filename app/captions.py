"""Building burned-in captions as an ASS subtitle file.

Word timings come from Whisper; this turns them into short on-screen phrases
with the active word highlighted, in the style short-form clips normally use.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from . import fonts
from . import APP_NAME
from .settings import hex_to_ass
from .transcribe import Word

MAX_ROWS = 2
GAP_BREAK = 0.75          # a pause longer than this starts a new phrase
SENTENCE_END = ".!?…"
SOFT_BREAK = ",;:—"       # break here too, once a phrase is already a decent size
# Ending a caption on one of these reads badly, so push it to the next phrase.
DANGLING = {
    "a", "an", "the", "to", "of", "in", "on", "at", "for", "with", "and", "or",
    "but", "is", "are", "was", "were", "he", "she", "they", "it", "we", "you",
    "i", "my", "his", "her", "their", "our", "your", "its", "that", "this",
    "did", "do", "does", "so", "if", "as", "by", "from", "not", "no",
}
TAIL = 0.18               # keep the last phrase up a beat after the final word
MAX_HOLD = 2.6            # never hold one highlight longer than this


@dataclass
class Phrase:
    words: list[Word]
    rows: list[list[int]]          # indices into self.words, one list per visual row

    @property
    def start(self) -> float:
        return self.words[0].start

    @property
    def end(self) -> float:
        return self.words[-1].end


def _clean(text: str, uppercase: bool) -> str:
    t = text.strip().replace("\\", "/").replace("{", "(").replace("}", ")")
    t = t.replace("\r", " ").replace("\n", " ")
    return t.upper() if uppercase else t


def _row_width(texts: Sequence[str], idxs: Sequence[int], family: str, size: int,
               space: float) -> float:
    if not idxs:
        return 0.0
    return sum(fonts.measure(texts[i], family, size) for i in idxs) + space * (len(idxs) - 1)


def _layout(texts: Sequence[str], family: str, size: int,
            max_width: float) -> list[list[int]] | None:
    """Word wrap into at most MAX_ROWS rows, or None if it will not fit.

    When it does need two rows we pick the split that makes them most even -
    a greedy fill leaves stragglers like "LOOK AT THIS / INSANE".
    """
    space = fonts.measure(" ", family, size)
    all_idx = list(range(len(texts)))
    if not all_idx:
        return []
    if _row_width(texts, all_idx, family, size, space) <= max_width:
        return [all_idx]

    best: tuple[float, list[list[int]]] | None = None
    for cut in range(1, len(all_idx)):
        top, bottom = all_idx[:cut], all_idx[cut:]
        wt = _row_width(texts, top, family, size, space)
        wb = _row_width(texts, bottom, family, size, space)
        if wt > max_width or wb > max_width:
            continue
        cost = abs(wt - wb)
        if best is None or cost < best[0]:
            best = (cost, [top, bottom])
    if best is not None:
        return best[1]

    # Falls back to greedy so a single over-long word still renders.
    rows: list[list[int]] = []
    cur: list[int] = []
    cur_w = 0.0
    for i, t in enumerate(texts):
        w = fonts.measure(t, family, size)
        add = w if not cur else cur_w + space + w
        if cur and add > max_width:
            rows.append(cur)
            cur, cur_w = [i], w
        else:
            cur, cur_w = cur + [i], add
    if cur:
        rows.append(cur)
    if len(rows) > MAX_ROWS:
        return None
    return rows


def group_words(words: Sequence[Word], settings: dict[str, Any],
                out_w: int) -> list[Phrase]:
    family = settings["cap_font"]
    size = int(settings["cap_size"])
    upper = bool(settings["cap_uppercase"])
    max_words = int(settings["cap_max_words"])
    margin = float(settings["cap_margin"])
    max_width = max(120.0, out_w * (1.0 - 2 * margin))
    if settings.get("cap_style") == "oneword":
        max_words = 1

    phrases: list[Phrase] = []
    cur: list[Word] = []

    def flush() -> None:
        nonlocal cur
        if not cur:
            return
        texts = [_clean(w.text, upper) for w in cur]
        rows = _layout(texts, family, size, max_width)
        if rows is None:
            rows = [[i] for i in range(len(texts))][:MAX_ROWS] or [[0]]
        phrases.append(Phrase(words=list(cur), rows=rows))
        cur = []

    for w in words:
        if not cur:
            cur = [w]
            continue
        gap = w.start - cur[-1].end
        prev_txt = cur[-1].text.strip()
        candidate = [_clean(x.text, upper) for x in cur + [w]]
        too_wide = _layout(candidate, family, size, max_width) is None
        hard_stop = bool(prev_txt) and prev_txt[-1] in SENTENCE_END and len(cur) >= 2
        soft_stop = bool(prev_txt) and prev_txt[-1] in SOFT_BREAK and len(cur) >= 3
        if (gap > GAP_BREAK or len(cur) >= max_words or too_wide
                or hard_stop or soft_stop):
            flush()
            cur = [w]
        else:
            cur.append(w)
    flush()
    return _undangle(phrases, family, size, max_width, upper)


def _undangle(phrases: list[Phrase], family: str, size: int, max_width: float,
              upper: bool) -> list[Phrase]:
    """Move a trailing "the"/"to"/"and" onto the next phrase where it belongs."""
    for i in range(len(phrases) - 1):
        cur, nxt = phrases[i], phrases[i + 1]
        if len(cur.words) < 2:
            continue
        tail = cur.words[-1]
        token = tail.text.strip().strip(".,;:!?").lower()
        if token not in DANGLING:
            continue
        if nxt.start - tail.end > GAP_BREAK:
            continue
        moved = [tail] + nxt.words
        texts = [_clean(w.text, upper) for w in moved]
        rows = _layout(texts, family, size, max_width)
        if rows is None:
            continue
        cur.words = cur.words[:-1]
        cur.rows = _layout([_clean(w.text, upper) for w in cur.words],
                           family, size, max_width) or [list(range(len(cur.words)))]
        nxt.words = moved
        nxt.rows = rows
    return [p for p in phrases if p.words]


def ass_time(t: float) -> str:
    t = max(0.0, t)
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = t - h * 3600 - m * 60
    return f"{h}:{m:02d}:{s:05.2f}"


def _style_block(settings: dict[str, Any], out_w: int, out_h: int) -> str:
    family = settings["cap_font"]
    size = int(settings["cap_size"])
    primary = hex_to_ass(settings["cap_color"])
    outline_c = hex_to_ass(settings["cap_outline_color"])
    back = hex_to_ass(settings["cap_outline_color"], alpha=0x60)
    bold = 0 if fonts.is_heavy(family) else -1
    margin = int(out_w * float(settings["cap_margin"]))
    outline = int(settings["cap_outline"])
    shadow = int(settings["cap_shadow"])
    return (
        "[Script Info]\n"
        f"; Generated by {APP_NAME}\n"
        "ScriptType: v4.00+\n"
        f"PlayResX: {out_w}\n"
        f"PlayResY: {out_h}\n"
        "WrapStyle: 2\n"
        "ScaledBorderAndShadow: yes\n"
        "YCbCr Matrix: TV.709\n"
        "\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour,"
        " BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle,"
        " BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Cap,{family},{size},{primary}&,{primary}&,{outline_c}&,{back}&,"
        f"{bold},0,0,0,100,100,0,0,1,{outline},{shadow},5,{margin},{margin},60,1\n"
        "\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )


def _render_row(texts: Sequence[str], rows: Sequence[Sequence[int]],
                active: int | None, normal_tag: str, active_tag: str) -> str:
    out_rows = []
    for row in rows:
        parts = []
        for i in row:
            if active is not None and i == active:
                parts.append("{" + active_tag + "}" + texts[i] + "{" + normal_tag + "}")
            else:
                parts.append(texts[i])
        out_rows.append(" ".join(parts))
    return "\\N".join(out_rows)


def build_ass(words: Sequence[Word], settings: dict[str, Any], *,
              out_w: int, out_h: int, clip_len: float) -> str:
    """Turn word timings into a complete .ass file."""
    lines = [_style_block(settings, out_w, out_h)]
    if not words:
        return lines[0]

    style = settings.get("cap_style", "karaoke")
    upper = bool(settings["cap_uppercase"])
    size = int(settings["cap_size"])
    pos_x = out_w // 2
    pos_y = int(out_h * float(settings["cap_pos"]))
    hi = hex_to_ass(settings["cap_highlight"]) + "&"
    base = hex_to_ass(settings["cap_color"]) + "&"
    pop = bool(settings.get("cap_pop", True)) and style in ("pop", "oneword")

    phrases = group_words(words, settings, out_w)

    def emit(start: float, end: float, text: str, extra: str = "") -> None:
        start = max(0.0, min(start, clip_len))
        end = max(start + 0.05, min(end, clip_len))
        if end - start < 0.04:
            return
        tags = f"\\pos({pos_x},{pos_y})" + extra
        lines.append(
            f"Dialogue: 0,{ass_time(start)},{ass_time(end)},Cap,,0,0,0,,"
            f"{{{tags}}}{text}\n"
        )

    for pi, ph in enumerate(phrases):
        texts = [_clean(w.text, upper) for w in ph.words]
        next_start = phrases[pi + 1].start if pi + 1 < len(phrases) else None
        phrase_end = ph.end + TAIL
        if next_start is not None:
            phrase_end = min(phrase_end, next_start)
        phrase_end = max(phrase_end, ph.end + 0.05)

        if style == "plain":
            # No fade-in on a phrase that starts at t=0, or the opening frame
            # (and the settings preview, which grabs exactly that frame) is blank.
            fade_in = 0 if ph.start < 0.08 else 70
            emit(ph.start, phrase_end, _render_row(texts, ph.rows, None, "", ""),
                 f"\\fad({fade_in},70)")
            continue

        if style == "oneword":
            for wi, w in enumerate(ph.words):
                nxt = (ph.words[wi + 1].start if wi + 1 < len(ph.words)
                       else (next_start if next_start is not None else w.end + TAIL))
                end = min(max(w.end + 0.08, nxt), w.start + MAX_HOLD)
                scale = "\\fscx108\\fscy108" if pop else ""
                emit(w.start, end, texts[wi], f"\\c{hi}{scale}")
            continue

        # karaoke / pop: whole phrase visible, active word highlighted
        active_tag = f"\\c{hi}" + ("\\fscx112\\fscy112" if pop else "")
        normal_tag = f"\\c{base}" + ("\\fscx100\\fscy100" if pop else "")
        for wi, w in enumerate(ph.words):
            seg_start = w.start if wi else ph.start
            if wi + 1 < len(ph.words):
                seg_end = ph.words[wi + 1].start
            else:
                seg_end = phrase_end
            seg_end = max(seg_end, seg_start + 0.05)
            seg_end = min(seg_end, seg_start + MAX_HOLD)
            text = _render_row(texts, ph.rows, wi, normal_tag, active_tag)
            emit(seg_start, seg_end, text)
        # If the highlight was capped, hold the finished phrase for the remainder.
        last_end = min(ph.words[-1].start + MAX_HOLD, phrase_end)
        if phrase_end - last_end > 0.08:
            emit(last_end, phrase_end,
                 _render_row(texts, ph.rows, len(texts) - 1, normal_tag, active_tag))

    return "".join(lines)


def preview_words(text: str = "THIS IS WHAT YOUR CAPTIONS LOOK LIKE",
                  start: float = 0.0, wps: float = 2.6) -> list[Word]:
    """Fake word timings so the UI can preview a caption style.

    Starts at t=0 so a single-frame preview grab always has a caption on it.
    """
    out: list[Word] = []
    t = start
    for token in text.split():
        dur = max(0.22, len(token) / (wps * 4.2))
        out.append(Word(start=t, end=t + dur, text=token))
        t += dur + 0.05
    return out


def _srt_stamp(t: float) -> str:
    ms_total = int(round(max(0.0, t) * 1000))
    h, rem = divmod(ms_total, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def words_to_srt(words: Iterable[Word], settings: dict[str, Any], out_w: int,
                 *, max_words: int | None = None) -> str:
    """A plain .srt alongside the video, for uploading elsewhere.

    `max_words` overrides the burned-in caption grouping: a full-length video
    wants normal subtitle lines, not 3-word punches.
    """
    st = dict(settings)
    if max_words:
        st["cap_max_words"] = max_words
        st["cap_style"] = "karaoke"
    phrases = group_words(list(words), st, out_w)
    chunks = []
    for i, ph in enumerate(phrases, 1):
        end = ph.end + TAIL
        if i < len(phrases):
            end = min(end, phrases[i].start - 0.001)     # cues must not overlap
        end = max(end, ph.start + 0.2)
        body = " ".join(w.text.strip() for w in ph.words)
        chunks.append(f"{i}\n{_srt_stamp(ph.start)} --> {_srt_stamp(end)}\n{body}\n")
    return "\n".join(chunks)
