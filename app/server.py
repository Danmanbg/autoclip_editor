"""Local web server. The UI is a single page talking to this JSON API."""
from __future__ import annotations

import json
import logging
import mimetypes
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any

from flask import Flask, Response, jsonify, request, send_file, send_from_directory

from . import censor, native, pipeline, reel, titles, transcribe
from . import settings as S
from . import fonts as font_mod
from .ffmpeg_tools import FFmpegMissing, ffmpeg_path, have_ffmpeg, list_encoders
from .jobs import MANAGER
from .paths import JOB_DIR, OUTPUT_DIR, ROOT, SOURCE_DIR, WEB_DIR, WORK_DIR, ensure_dirs

app = Flask(__name__, static_folder=None)
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024 * 1024      # 16 GB uploads

# Anything we are willing to serve bytes from.
_ALLOWED_ROOTS = [OUTPUT_DIR, WORK_DIR, SOURCE_DIR, JOB_DIR]


def _safe_path(raw: str) -> Path | None:
    """Only serve files that live inside our own folders."""
    if not raw:
        return None
    try:
        p = Path(raw).resolve()
    except (OSError, ValueError):
        return None
    if not p.is_file():
        return None
    for root in _ALLOWED_ROOTS:
        try:
            p.relative_to(root.resolve())
            return p
        except ValueError:
            continue
    extra = S.load_last().get("out_folder")
    if extra:
        try:
            p.relative_to(Path(extra).resolve())
            return p
        except (ValueError, OSError):
            pass
    return None


def _err(message: str, code: int = 400):
    return jsonify({"ok": False, "error": message}), code


# ---------------------------------------------------------------------------
# page + assets


@app.get("/")
def index():
    return send_from_directory(WEB_DIR, "index.html")


@app.get("/assets/<path:name>")
def assets(name: str):
    return send_from_directory(WEB_DIR, name)


@app.get("/favicon.ico")
def favicon():
    return Response(status=204)


# ---------------------------------------------------------------------------
# bootstrap


@app.get("/api/bootstrap")
def bootstrap():
    ok = have_ffmpeg()
    encoders = sorted(list_encoders() & {
        "libx264", "libx265", "h264_amf", "hevc_amf", "h264_nvenc", "h264_qsv"})
    models = [{"id": m, "cached": transcribe.is_model_cached(m)}
              for m in S.WHISPER_MODELS]
    return jsonify({
        "ok": True,
        "ffmpeg": {"ok": ok, "path": ffmpeg_path() if ok else ""},
        "defaults": S.DEFAULTS,
        "last": S.load_last(),
        "presets": S.load_presets(),
        "fonts": font_mod.available(),
        "encoders": encoders,
        "models": models,
        "sources": pipeline.list_sources(),
        "output_dir": str(OUTPUT_DIR),
        "cpu": os.cpu_count() or 4,
        "version": "1.0.0",
    })


# ---------------------------------------------------------------------------
# source


@app.post("/api/source")
def add_source():
    data = request.get_json(silent=True) or {}
    text = str(data.get("input") or "").strip()
    if not text:
        return _err("Paste a stream URL or pick a file first.")
    try:
        start = float(data.get("start") or 0)
        end = float(data.get("end") or 0)
    except (TypeError, ValueError):
        start = end = 0.0
    height = int(data.get("max_height") or 1080)

    job = MANAGER.submit(
        "source",
        lambda j: pipeline.prepare_source(j, text, start=start, end=end,
                                          max_height=height),
        lane="io",
        title=text[:120],
    )
    return jsonify({"ok": True, "job": job.to_dict()})


@app.post("/api/browse")
def browse():
    kind = (request.get_json(silent=True) or {}).get("kind", "file")
    path = native.pick_folder() if kind == "folder" else native.pick_file()
    return jsonify({"ok": True, "path": path})


@app.post("/api/upload")
def upload():
    f = request.files.get("file")
    if f is None or not f.filename:
        return _err("No file received.")
    dest_dir = SOURCE_DIR / "uploads"
    dest_dir.mkdir(parents=True, exist_ok=True)
    from .paths import safe_name
    name = safe_name(Path(f.filename).stem, 80) + (Path(f.filename).suffix or ".mp4")
    dest = dest_dir / name
    n = 2
    while dest.exists():
        dest = dest_dir / f"{Path(name).stem} ({n}){Path(name).suffix}"
        n += 1
    f.save(str(dest))
    return jsonify({"ok": True, "path": str(dest)})


@app.get("/api/sources")
def sources():
    return jsonify({"ok": True, "sources": pipeline.list_sources()})


@app.get("/api/frame/<sid>")
def frame(sid: str):
    try:
        t = float(request.args.get("t", "0"))
        w = int(request.args.get("w", "960"))
    except ValueError:
        return _err("bad frame request")
    try:
        path = pipeline.frame_path(sid, t, width=max(160, min(w, 1920)))
    except FileNotFoundError:
        return _err("That stream is not loaded any more.", 404)
    except Exception as exc:                                      # noqa: BLE001
        return _err(f"Could not read a frame: {exc}", 500)
    return send_file(path, mimetype="image/jpeg", max_age=3600)


# ---------------------------------------------------------------------------
# preview


@app.post("/api/preview")
def preview():
    data = request.get_json(silent=True) or {}
    sid = str(data.get("source_id") or "")
    try:
        t = float(data.get("t") or 0)
    except (TypeError, ValueError):
        t = 0.0
    try:
        path = pipeline.build_preview(sid, data.get("settings") or {}, t)
    except FileNotFoundError:
        return _err("That stream is not loaded any more.", 404)
    except Exception as exc:                                      # noqa: BLE001
        return _err(f"Preview failed: {exc}", 500)
    resp = send_file(path, mimetype="image/jpeg", max_age=0)
    resp.headers["Cache-Control"] = "no-store"
    return resp


# ---------------------------------------------------------------------------
# render


@app.post("/api/render")
def start_render():
    data = request.get_json(silent=True) or {}
    sid = str(data.get("source_id") or "")
    rec = pipeline.get_source(sid)
    if rec is None:
        return _err("Load a stream first.")
    settings = data.get("settings") or {}
    mode = settings.get("mode")
    if mode == "clean":
        fn = lambda j: pipeline.make_clean(j, sid, settings)         # noqa: E731
    elif mode == "reel":
        selection = data.get("selection") or []
        if not selection:
            return _err("Pick at least one moment for the reel.")
        # Finding moments and building share the one CPU lane; building wins.
        for j in MANAGER.active():
            if j.kind == "moments":
                MANAGER.cancel(j.id)
        fn = lambda j: reel.build_reel(j, rec, settings, selection)  # noqa: E731
    else:
        fn = lambda j: pipeline.make_clips(j, sid, settings)         # noqa: E731
    job = MANAGER.submit("clips", fn, lane="cpu", title=rec.get("title") or "clips")
    return jsonify({"ok": True, "job": job.to_dict()})


# ---------------------------------------------------------------------------
# highlight reel


@app.post("/api/moments")
def find_moments():
    data = request.get_json(silent=True) or {}
    rec = pipeline.get_source(str(data.get("source_id") or ""))
    if rec is None:
        return _err("Load a stream first.")
    for j in MANAGER.active():
        if j.kind == "moments":
            MANAGER.cancel(j.id)
    settings = data.get("settings") or {}
    job = MANAGER.submit("moments", lambda j: reel.find_moments(j, rec, settings),
                         lane="cpu", title=rec.get("title") or "moments")
    return jsonify({"ok": True, "job": job.to_dict()})


@app.get("/api/playable/<sid>")
def playable(sid: str):
    rec = pipeline.get_source(sid)
    if rec is None:
        return _err("That stream is not loaded any more.", 404)
    return jsonify({"ok": True, "direct": reel.direct_playable(rec)})


@app.get("/api/source-video/<sid>")
def source_video(sid: str):
    """The registered source file itself, so the browser can seek straight into it."""
    rec = pipeline.get_source(sid)
    if rec is None:
        return _err("That stream is not loaded any more.", 404)
    p = Path(rec["path"])
    mime = mimetypes.guess_type(p.name)[0] or "video/mp4"
    return send_file(p, mimetype=mime, conditional=True)


@app.get("/api/snippet/<sid>")
def snippet(sid: str):
    rec = pipeline.get_source(sid)
    if rec is None:
        return _err("That stream is not loaded any more.", 404)
    try:
        a, b = float(request.args.get("a", "0")), float(request.args.get("b", "0"))
        path = reel.snippet(rec, a, b)
    except ValueError:
        return _err("bad range")
    except Exception as exc:                                      # noqa: BLE001
        return _err(f"Could not make a preview: {exc}", 500)
    return send_file(path, mimetype="video/mp4", conditional=True)


# ---------------------------------------------------------------------------
# titles


def _titles_from_job(job_id: str) -> dict[str, Any] | None:
    job = MANAGER.get(job_id)
    if job is None:
        return None
    return (job.result or {}).get("titles")


@app.post("/api/titles/ai")
def titles_ai():
    """Ask the local model for '<Game> but ...' ideas. Slow-ish (~15 s)."""
    data = request.get_json(silent=True) or {}
    info = _titles_from_job(str(data.get("job_id") or "")) or {}
    lines = data.get("lines") or info.get("lines") or []
    game = str(data.get("game") or info.get("game") or "").strip()
    st = S.normalize(data.get("settings") or {})
    matcher = censor.build_matcher(st) if st["censor"] else None
    ideas, note = titles.ai_ideas(game, [str(x) for x in lines][:6], n=5, matcher=matcher)
    return jsonify({"ok": True, "ideas": ideas, "note": note})


@app.post("/api/titles/refresh")
def titles_refresh():
    """Rebuild the quote titles and description, e.g. after the game name changes."""
    data = request.get_json(silent=True) or {}
    job = MANAGER.get(str(data.get("job_id") or ""))
    if job is None:
        return _err("That job has gone - make the video again.", 404)
    info = (job.result or {}).get("titles") or {}
    rec = (job.result or {}).get("source") or {}
    game = str(data.get("game") or "").strip()
    hashtags = str(data.get("hashtags") if data.get("hashtags") is not None
                   else info.get("hashtags", "#gaming")).strip()[:200]
    moments = [titles.Moment(start=m["start"], end=m["end"], out_at=m.get("out_at"),
                             score=m.get("score", 0.0), line=m.get("line") or "")
               for m in info.get("moments") or []]
    kind = info.get("kind", "clips")
    fresh = dict(info, game=game, hashtags=hashtags,
                 quotes=titles.quote_titles(moments, game),
                 description=titles.description(moments, kind=kind, game=game,
                                                stream_title=rec.get("title") or "",
                                                source_url=rec.get("url") or "",
                                                hashtags=hashtags))
    job.update_result(titles=fresh)
    out_dir = (job.result or {}).get("out_dir")
    clips = (job.result or {}).get("clips") or []
    if out_dir and clips:
        try:
            titles.save(Path(out_dir), Path(clips[0]["file"]).stem
                        if kind != "clips" else "clips", fresh)
        except OSError:
            pass
    return jsonify({"ok": True, "titles": fresh})


@app.post("/api/censor-test")
def censor_test():
    """Show which words in a typed sentence would be bleeped, for the settings UI."""
    from . import censor
    data = request.get_json(silent=True) or {}
    text = str(data.get("text") or "")[:2000]
    matcher = censor.build_matcher(S.normalize(data.get("settings") or {}))
    tokens = text.split()
    return jsonify({"ok": True, "words": len(matcher),
                    "result": " ".join(censor.mask_word(t) if matcher.check(t) else t
                                       for t in tokens),
                    "hits": sum(1 for t in tokens if matcher.check(t))})


@app.get("/api/jobs")
def jobs():
    return jsonify({"ok": True, "jobs": MANAGER.list(limit=25)})


@app.get("/api/job/<job_id>")
def job_detail(job_id: str):
    job = MANAGER.get(job_id)
    if job is None:
        return _err("Unknown job.", 404)
    return jsonify({"ok": True, "job": job.to_dict()})


@app.post("/api/job/<job_id>/cancel")
def job_cancel(job_id: str):
    return jsonify({"ok": MANAGER.cancel(job_id)})


# ---------------------------------------------------------------------------
# files


@app.get("/api/file")
def serve_file():
    p = _safe_path(request.args.get("path", ""))
    if p is None:
        return _err("Not found.", 404)
    mime = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
    if p.suffix.lower() in (".txt", ".srt"):
        mime = "text/plain"            # Flask appends the utf-8 charset to text/*
    resp = send_file(p, mimetype=mime, conditional=True)
    if request.args.get("download"):
        resp.headers["Content-Disposition"] = f'attachment; filename="{p.name}"'
    return resp


@app.post("/api/reveal")
def reveal():
    path = (request.get_json(silent=True) or {}).get("path") or str(OUTPUT_DIR)
    return jsonify({"ok": native.reveal(path)})


# ---------------------------------------------------------------------------
# presets


@app.post("/api/presets")
def presets():
    data = request.get_json(silent=True) or {}
    action = data.get("action")
    try:
        if action == "save":
            out = S.save_preset(str(data.get("name") or ""), data.get("settings") or {})
        elif action == "delete":
            out = S.delete_preset(str(data.get("name") or ""))
        else:
            out = S.load_presets()
    except ValueError as exc:
        return _err(str(exc))
    return jsonify({"ok": True, "presets": out})


@app.post("/api/normalize")
def normalize():
    data = request.get_json(silent=True) or {}
    rec = pipeline.get_source(str(data.get("source_id") or "")) or {}
    st = S.normalize(data.get("settings") or {},
                     int(rec.get("width") or 0), int(rec.get("height") or 0))
    from .render import panel_geometry
    p = panel_geometry(st)
    return jsonify({"ok": True, "settings": st,
                    "panels": {"top": p.top_h, "divider": p.divider,
                               "bottom": p.bottom_h}})


@app.post("/api/cleanup")
def cleanup():
    """Delete cached downloads and intermediate files (not the finished clips)."""
    freed = 0
    for folder in (SOURCE_DIR, JOB_DIR, WORK_DIR / "preview", WORK_DIR / "moments"):
        if not folder.is_dir():
            continue
        for entry in list(folder.iterdir()):
            try:
                size = sum(f.stat().st_size for f in entry.rglob("*") if f.is_file()) \
                    if entry.is_dir() else entry.stat().st_size
                if entry.is_dir():
                    shutil.rmtree(entry, ignore_errors=True)
                else:
                    entry.unlink(missing_ok=True)
                freed += size
            except OSError:
                pass
    pipeline._SOURCES.clear()
    return jsonify({"ok": True, "freed_mb": round(freed / 1e6, 1)})


def create_app() -> Flask:
    ensure_dirs()
    return app


def run(host: str = "127.0.0.1", port: int = 8420, open_browser: bool = True,
        debug: bool = False) -> None:
    ensure_dirs()
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    url = f"http://{host}:{port}/"
    if open_browser:
        import threading
        import webbrowser
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    from . import APP_NAME
    print(f"\n  {APP_NAME} is running at  {url}\n  Press Ctrl+C to stop.\n")
    app.run(host=host, port=port, debug=debug, threaded=True, use_reloader=False)
