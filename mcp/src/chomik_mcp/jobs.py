"""Background jobs: downloads, uploads and clones can take hours."""
from __future__ import annotations

import collections
import os
import subprocess
import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field
from typing import Callable


@dataclass
class Job:
    id: str
    kind: str
    title: str
    status: str = "running"  # running | done | failed
    started: float = field(default_factory=time.time)
    finished: float | None = None
    progress: dict = field(default_factory=dict)
    log: collections.deque = field(default_factory=lambda: collections.deque(maxlen=200))
    error: str | None = None
    log_path: str | None = None
    proc: subprocess.Popen | None = None

    def say(self, line: str) -> None:
        self.log.append(f"{time.strftime('%H:%M:%S')} {line}")

    def summary(self, tail: int = 15) -> dict:
        if self.proc is not None and self.status == "running":
            code = self.proc.poll()
            if code is not None:
                self.status = "done" if code in (0, 1) else "failed"  # cloner exits 1 when some files are missing
                self.finished = time.time()
                if code not in (0, 1):
                    self.error = f"exit code {code}"
        lines = list(self.log)
        if self.log_path and os.path.exists(self.log_path):
            with open(self.log_path, encoding="utf-8", errors="replace") as fh:
                lines = list(collections.deque(fh, maxlen=tail))
        return {
            "id": self.id,
            "kind": self.kind,
            "title": self.title,
            "status": self.status,
            "started": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.started)),
            "seconds": round((self.finished or time.time()) - self.started),
            "progress": self.progress,
            "error": self.error,
            "log_tail": [line.rstrip("\n") for line in lines[-tail:]],
        }


class Jobs:
    def __init__(self, log_dir: str):
        self.log_dir = log_dir
        self._jobs: dict[str, Job] = {}

    def get(self, job_id: str) -> Job:
        if job_id not in self._jobs:
            raise ValueError(f"no job {job_id!r}")
        return self._jobs[job_id]

    def all(self) -> list[Job]:
        return sorted(self._jobs.values(), key=lambda j: j.started, reverse=True)

    def thread(self, kind: str, title: str, fn: Callable[[Job], None]) -> Job:
        job = Job(uuid.uuid4().hex[:8], kind, title)
        self._jobs[job.id] = job

        def runner():
            try:
                fn(job)
                job.status = "done"
            except Exception as e:  # noqa: BLE001 - reported through job_status
                job.status, job.error = "failed", f"{type(e).__name__}: {e}"
                job.say(traceback.format_exc(limit=3))
            finally:
                job.finished = time.time()

        threading.Thread(target=runner, name=f"job-{job.id}", daemon=True).start()
        return job

    def process(self, kind: str, title: str, argv: list[str]) -> Job:
        job = Job(uuid.uuid4().hex[:8], kind, title)
        os.makedirs(self.log_dir, exist_ok=True)
        job.log_path = os.path.join(self.log_dir, f"{kind}-{job.id}.log")
        out = open(job.log_path, "w", encoding="utf-8")  # noqa: SIM115 - owned by the child
        job.proc = subprocess.Popen(argv, stdout=out, stderr=subprocess.STDOUT, env={**os.environ, "PYTHONUNBUFFERED": "1"})
        self._jobs[job.id] = job
        return job
