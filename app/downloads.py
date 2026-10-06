"""Fetching model files from Hugging Face reliably.

On some connections Hugging Face serves each stream slowly and its xet backend
can stall at 0 bytes, so files come down as parallel byte ranges over plain
HTTPS, and every large file is checked against its published sha256.
"""
from __future__ import annotations

import hashlib
import http.client
import os
import shutil
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, Iterable


class DownloadError(RuntimeError):
    pass


def hf_download(repo: str, folder: Path, *, include: Iterable[str] | None = None,
                label: str = "", status: Callable[[str], None] = lambda m: None,
                connections: int = 8, piece: int = 16 << 20) -> Path:
    """Download `repo` (or just the `include`d files) into `folder`.

    Files land in `<folder>.partial` first and only move into place once every
    piece has arrived and the checksums match, so an interrupted download never
    leaves a half-written model that looks usable.
    """
    from huggingface_hub import HfApi, hf_hub_url

    folder = Path(folder)
    wanted = set(include) if include else None
    label = label or repo
    status(f"Looking up {label}")
    files = [s for s in HfApi().model_info(repo, files_metadata=True).siblings
             if not s.rfilename.startswith(".") and not s.rfilename.endswith(".md")
             and (wanted is None or s.rfilename in wanted)]
    if wanted is not None and {s.rfilename for s in files} != wanted:
        missing = wanted - {s.rfilename for s in files}
        raise DownloadError(f"{repo} has no {', '.join(sorted(missing))}")

    tmp = folder.with_name(folder.name + ".partial")
    tmp.mkdir(parents=True, exist_ok=True)
    total = sum(s.size or 0 for s in files) or 1
    got = [0]
    lock = threading.Lock()

    def fetch(url: str, path: Path, start: int, end: int) -> None:
        for attempt in range(10):
            try:
                req = urllib.request.Request(url, headers={"Range": f"bytes={start}-{end}"})
                with urllib.request.urlopen(req, timeout=30) as r, open(path, "r+b") as f:
                    f.seek(start)
                    while chunk := r.read(1 << 16):
                        f.write(chunk)
                        start += len(chunk)
                        with lock:
                            got[0] += len(chunk)
                if start > end:
                    return
            except (OSError, http.client.HTTPException):
                if attempt == 9:
                    raise
                time.sleep(2)

    jobs = []
    for s in files:
        path = tmp / s.rfilename
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "wb") as f:
            f.truncate(s.size or 0)
        jobs += [(hf_hub_url(repo, s.rfilename), path, a, min(a + piece, s.size) - 1)
                 for a in range(0, s.size or 0, piece)]

    with ThreadPoolExecutor(connections) as pool:
        futures = [pool.submit(fetch, *job) for job in jobs]
        last = 0
        while not all(fu.done() for fu in futures):
            time.sleep(1.5)
            speed, last = (got[0] - last) / 1.5, got[0]
            eta = (total - got[0]) / speed / 60 if speed else 0
            status(f"Downloading {label} (one time): {100 * got[0] / total:.0f}%  "
                   f"{speed / 1e6:.1f} MB/s, ~{eta:.0f} min left")
        for fu in futures:
            fu.result()

    status("Checking the download")
    for s in files:
        if s.lfs:
            h = hashlib.sha256()
            with open(tmp / s.rfilename, "rb") as f:
                while block := f.read(1 << 22):
                    h.update(block)
            if h.hexdigest() != s.lfs.sha256:
                raise DownloadError(f"{s.rfilename} downloaded corrupted - run it again")

    # Move into place. A directory cannot be renamed over an existing one on
    # Windows, so merge file by file when the folder already exists.
    if not folder.exists():
        os.replace(tmp, folder)
    else:
        for item in tmp.iterdir():
            os.replace(item, folder / item.name)
        shutil.rmtree(tmp, ignore_errors=True)
    return folder
