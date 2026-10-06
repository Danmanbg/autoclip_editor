"""Background job queue with progress reporting.

Two lanes: `io` for downloads (a couple can run at once) and `cpu` for renders
(strictly one at a time, since ffmpeg and Whisper will happily eat every core).
"""
from __future__ import annotations

import json
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable

from .ffmpeg_tools import Cancelled
from .paths import JOB_DIR

MAX_LOG = 300


@dataclass
class Job:
    id: str
    kind: str
    title: str = ""
    status: str = "queued"           # queued | running | done | error | cancelled
    stage: str = ""
    message: str = ""
    progress: float = 0.0
    created: float = field(default_factory=time.time)
    started: float = 0.0
    finished: float = 0.0
    error: str = ""
    result: dict[str, Any] = field(default_factory=dict)
    log: list[str] = field(default_factory=list)
    cancel_event: threading.Event = field(default_factory=threading.Event, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    # -- reporting used by the pipeline ---------------------------------
    def set(self, *, stage: str | None = None, message: str | None = None,
            progress: float | None = None) -> None:
        with self._lock:
            if stage is not None:
                self.stage = stage
            if message is not None and message != self.message:
                self.message = message
                stamp = time.strftime("%H:%M:%S")
                self.log.append(f"{stamp}  {message}")
                if len(self.log) > MAX_LOG:
                    del self.log[:len(self.log) - MAX_LOG]
            if progress is not None:
                self.progress = max(0.0, min(1.0, float(progress)))

    def note(self, message: str) -> None:
        self.set(message=message)

    def update_result(self, **kw: Any) -> None:
        with self._lock:
            self.result.update(kw)

    @property
    def cancelled(self) -> bool:
        return self.cancel_event.is_set()

    def raise_if_cancelled(self) -> None:
        if self.cancel_event.is_set():
            raise Cancelled("cancelled")

    def to_dict(self, with_log: bool = True) -> dict[str, Any]:
        with self._lock:
            d = {
                "id": self.id,
                "kind": self.kind,
                "title": self.title,
                "status": self.status,
                "stage": self.stage,
                "message": self.message,
                "progress": round(self.progress, 4),
                "created": self.created,
                "started": self.started,
                "finished": self.finished,
                "elapsed": round((self.finished or time.time()) - (self.started or self.created), 1),
                "error": self.error,
                "result": self.result,
            }
            if with_log:
                d["log"] = list(self.log)
            return d


class JobManager:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._order: list[str] = []
        self._lock = threading.Lock()
        self._pools = {
            "io": ThreadPoolExecutor(max_workers=2, thread_name_prefix="io"),
            "cpu": ThreadPoolExecutor(max_workers=1, thread_name_prefix="cpu"),
        }
        JOB_DIR.mkdir(parents=True, exist_ok=True)

    def submit(self, kind: str, fn: Callable[[Job], dict[str, Any] | None], *,
               lane: str = "cpu", title: str = "") -> Job:
        job = Job(id=uuid.uuid4().hex[:12], kind=kind, title=title)
        with self._lock:
            self._jobs[job.id] = job
            self._order.append(job.id)
            self._prune_locked()
        self._pools.get(lane, self._pools["cpu"]).submit(self._run, job, fn)
        return job

    def _run(self, job: Job, fn: Callable[[Job], dict[str, Any] | None]) -> None:
        job.status = "running"
        job.started = time.time()
        try:
            out = fn(job)
            if job.cancelled:
                job.status = "cancelled"
                job.set(message="Cancelled")
            else:
                if isinstance(out, dict):
                    job.update_result(**out)
                job.status = "done"
                job.set(progress=1.0, message="Finished")
        except Cancelled:
            job.status = "cancelled"
            job.set(message="Cancelled")
        except Exception as exc:                                  # noqa: BLE001
            job.status = "error"
            job.error = f"{type(exc).__name__}: {exc}"
            job.set(message=job.error)
            job.log.append(traceback.format_exc()[-2000:])
        finally:
            job.finished = time.time()
            self._persist(job)

    def _persist(self, job: Job) -> None:
        try:
            path = JOB_DIR / f"{job.id}.json"
            path.write_text(json.dumps(job.to_dict(), indent=2, default=str),
                            encoding="utf-8")
        except (OSError, TypeError, ValueError):
            pass

    def _prune_locked(self, keep: int = 60) -> None:
        while len(self._order) > keep:
            old = self._order.pop(0)
            self._jobs.pop(old, None)

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def cancel(self, job_id: str) -> bool:
        job = self.get(job_id)
        if job is None or job.status in ("done", "error", "cancelled"):
            return False
        job.cancel_event.set()
        job.set(message="Cancelling...")
        return True

    def list(self, kind: str | None = None, limit: int = 30) -> list[dict[str, Any]]:
        with self._lock:
            ids = list(reversed(self._order))
            jobs = [self._jobs[i] for i in ids if i in self._jobs]
        if kind:
            jobs = [j for j in jobs if j.kind == kind]
        return [j.to_dict(with_log=False) for j in jobs[:limit]]

    def active(self) -> list[Job]:
        with self._lock:
            return [j for j in self._jobs.values() if j.status in ("queued", "running")]


MANAGER = JobManager()
