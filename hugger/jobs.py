"""Background download jobs with progress tracking.

snapshot_download exposes no progress callback, so we run it in a worker thread
and a poller thread measures bytes-on-disk against the known total size. Jobs are
in-memory only (lost on restart); completed archives are persisted in SQLite.
"""
from __future__ import annotations

import shutil
import threading
import uuid
from dataclasses import dataclass, field

from . import hub, store


def _dir_size(path) -> int:
    total = 0
    if not path.exists():
        return 0
    for p in path.rglob("*"):
        try:
            if p.is_file() and not p.is_symlink():
                total += p.stat().st_size
        except OSError:
            pass
    return total


@dataclass
class Job:
    id: str
    repo_id: str
    revision: str
    status: str = "queued"  # queued | downloading | done | error
    total_bytes: int = 0
    done_bytes: int = 0
    error: str | None = None
    sha: str | None = None

    @property
    def percent(self) -> int:
        if self.total_bytes <= 0:
            return 0
        return min(100, int(self.done_bytes * 100 / self.total_bytes))

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "repo_id": self.repo_id,
            "revision": self.revision,
            "status": self.status,
            "total_bytes": self.total_bytes,
            "done_bytes": self.done_bytes,
            "percent": self.percent,
            "error": self.error,
        }


class JobManager:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()

    def start(self, repo_id: str, revision: str = "main") -> Job:
        job = Job(id=uuid.uuid4().hex[:12], repo_id=repo_id, revision=revision)
        with self._lock:
            self._jobs[job.id] = job
        store.upsert_job(job.id, repo_id, revision, "queued")
        threading.Thread(target=self._run, args=(job,), daemon=True).start()
        return job

    def resume_pending(self) -> None:
        """Re-launch jobs that were queued/downloading when the process stopped.

        snapshot_download resumes partial downloads (it skips complete files and
        continues incomplete ones), so this just re-runs them."""
        for row in store.list_active_jobs():
            job = Job(
                id=row["id"], repo_id=row["repo_id"], revision=row["revision"],
                status="queued", total_bytes=row["total_bytes"] or 0, sha=row["sha"],
            )
            with self._lock:
                self._jobs[job.id] = job
            threading.Thread(target=self._run, args=(job,), daemon=True).start()

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def active(self) -> list[Job]:
        # Active = anything not yet acknowledged-as-finished by the UI poller.
        return [j for j in self._jobs.values() if j.status in ("queued", "downloading")]

    def recent(self) -> list[Job]:
        return list(self._jobs.values())

    def clear_finished(self) -> None:
        with self._lock:
            self._jobs = {k: v for k, v in self._jobs.items() if v.status in ("queued", "downloading")}

    def _run(self, job: Job) -> None:
        dest = hub.local_path(job.repo_id)
        stop = threading.Event()

        def poll():
            while not stop.is_set():
                job.done_bytes = _dir_size(dest)
                stop.wait(1.0)

        try:
            job.status = "downloading"
            meta = hub.repo_meta(job.repo_id, job.revision)
            job.total_bytes = meta["total_size"]
            job.sha = meta["sha"]
            store.upsert_job(job.id, job.repo_id, job.revision, "downloading", job.total_bytes, job.sha)

            poller = threading.Thread(target=poll, daemon=True)
            poller.start()
            try:
                hub.download(job.repo_id, job.revision, dest)
            finally:
                stop.set()
                poller.join(timeout=2)

            size = _dir_size(dest)
            job.done_bytes = size or job.total_bytes
            store.upsert_archive(job.repo_id, job.revision, job.sha, str(dest), size)
            job.status = "done"
            store.upsert_job(job.id, job.repo_id, job.revision, "done", job.total_bytes, job.sha)
        except Exception as e:  # surface the real error to the UI/API
            stop.set()
            job.status = "error"
            job.error = f"{type(e).__name__}: {e}"
            store.upsert_job(job.id, job.repo_id, job.revision, "error", job.total_bytes, job.sha, job.error)


def delete_archive(repo_id: str) -> None:
    rec = store.get_archive(repo_id)
    if rec:
        shutil.rmtree(rec["path"], ignore_errors=True)
    store.delete_archive(repo_id)


def check_update(repo_id: str) -> dict:
    rec = store.get_archive(repo_id)
    if not rec:
        raise KeyError(repo_id)
    rsha = hub.remote_sha(repo_id, rec["revision"])
    available = rsha != rec["sha"]
    store.set_update_status(repo_id, rsha, available)
    return {"repo_id": repo_id, "local": rec["sha"], "remote": rsha, "update_available": available}


manager = JobManager()
