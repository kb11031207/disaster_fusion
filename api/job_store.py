"""
In-memory job store. Each job tracks status, progress messages, and results.
Keyed by UUID job_id. No persistence — restarts clear all jobs (fine for demo).
"""

from __future__ import annotations

import threading
import uuid
from typing import Any, Optional


class Job:
    def __init__(self, job_id: str):
        self.job_id    = job_id
        self.status    = "queued"       # queued | running | done | failed
        self.progress  = "Queued"
        self.error:    Optional[str] = None
        self.results:  Optional[dict] = None
        self.overture: Optional[dict] = None

    def to_dict(self) -> dict:
        return {
            "job_id":   self.job_id,
            "status":   self.status,
            "progress": self.progress,
            "error":    self.error,
        }


class JobStore:
    def __init__(self):
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()

    def create(self) -> Job:
        job_id = str(uuid.uuid4())[:8]
        job = Job(job_id)
        with self._lock:
            self._jobs[job_id] = job
        return job

    def get(self, job_id: str) -> Optional[Job]:
        with self._lock:
            return self._jobs.get(job_id)

    def all(self) -> list[dict]:
        with self._lock:
            return [j.to_dict() for j in self._jobs.values()]


store = JobStore()
