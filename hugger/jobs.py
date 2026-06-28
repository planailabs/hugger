"""Background jobs: downloads and cross-store moves, both pausable + resumable.

- Download jobs run `python -m hugger._dlworker` in a subprocess so a pause can
  terminate it mid-file; huggingface_hub leaves a resumable partial, and unpause
  re-launches it.
- Move jobs copy a model's files between data stores in a worker thread, skipping
  already-copied files (so they resume), checking a stop event between files.
- Jobs are persisted; on startup queued/running jobs auto-resume and paused jobs
  are reloaded in the paused state.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from . import hub, store


def _dir_size(path: Path) -> int:
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


def store_repo_path(store_path: str, repo_id: str) -> Path:
    return Path(store_path).joinpath(*repo_id.split("/"))


@dataclass
class Job:
    id: str
    repo_id: str
    revision: str = "main"
    type: str = "download"          # download | move
    status: str = "queued"          # queued | running | paused | done | error
    total_bytes: int = 0
    done_bytes: int = 0
    error: str | None = None
    sha: str | None = None
    store_id: str | None = None      # target store
    src_store_id: str | None = None  # source store (move only)
    _stop: threading.Event = field(default_factory=threading.Event, repr=False)
    _proc: subprocess.Popen | None = field(default=None, repr=False)

    @property
    def percent(self) -> int:
        if self.total_bytes <= 0:
            return 0
        return min(100, int(self.done_bytes * 100 / self.total_bytes))

    def persist(self) -> None:
        store.save_job({
            "id": self.id, "repo_id": self.repo_id, "revision": self.revision,
            "type": self.type, "status": self.status, "total_bytes": self.total_bytes,
            "sha": self.sha, "error": self.error, "store_id": self.store_id,
            "src_store_id": self.src_store_id,
        })

    def as_dict(self) -> dict:
        return {
            "id": self.id, "repo_id": self.repo_id, "revision": self.revision,
            "type": self.type, "status": self.status, "total_bytes": self.total_bytes,
            "done_bytes": self.done_bytes, "percent": self.percent, "error": self.error,
            "store_id": self.store_id, "src_store_id": self.src_store_id,
        }


class JobManager:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()

    # --- starting work ----------------------------------------------------
    def start_download(self, repo_id: str, revision: str = "main", store_id: str | None = None) -> Job:
        if store_id is None:
            default = store.get_default_store()
            store_id = default["id"] if default else None
        job = Job(id=uuid.uuid4().hex[:12], repo_id=repo_id, revision=revision,
                  type="download", store_id=store_id)
        self._register_and_run(job)
        return job

    def start_move(self, repo_id: str, dest_store_id: str) -> Job:
        rec = store.get_archive(repo_id)
        if not rec:
            raise KeyError(repo_id)
        job = Job(id=uuid.uuid4().hex[:12], repo_id=repo_id, revision=rec["revision"],
                  type="move", store_id=dest_store_id, src_store_id=rec["store_id"], sha=rec["sha"])
        self._register_and_run(job)
        return job

    def _register_and_run(self, job: Job) -> None:
        with self._lock:
            self._jobs[job.id] = job
        job.status = "queued"
        job.persist()
        self._spawn(job)

    def _spawn(self, job: Job) -> None:
        target = self._run_download if job.type == "download" else self._run_move
        threading.Thread(target=target, args=(job,), daemon=True).start()

    # --- pause / resume ---------------------------------------------------
    def pause(self, job_id: str) -> None:
        job = self._jobs.get(job_id)
        if job and job.status in ("queued", "running"):
            job._stop.set()
            proc = job._proc
            if proc and proc.poll() is None:
                proc.terminate()

    def resume(self, job_id: str) -> None:
        job = self._jobs.get(job_id)
        if job and job.status == "paused":
            job._stop = threading.Event()
            job.status = "queued"
            job.persist()
            self._spawn(job)

    # --- queries ----------------------------------------------------------
    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def active(self) -> list[Job]:
        return [j for j in self._jobs.values() if j.status in ("queued", "running", "paused")]

    def recent(self) -> list[Job]:
        return list(self._jobs.values())

    # --- restart recovery -------------------------------------------------
    def resume_pending(self) -> None:
        for row in store.list_jobs(["queued", "running", "paused"]):
            job = Job(
                id=row["id"], repo_id=row["repo_id"], revision=row["revision"] or "main",
                type=row["type"], total_bytes=row["total_bytes"] or 0, sha=row["sha"],
                store_id=row["store_id"], src_store_id=row["src_store_id"],
                status="paused" if row["status"] == "paused" else "queued",
            )
            with self._lock:
                self._jobs[job.id] = job
            if job.status != "paused":  # paused jobs wait for an explicit resume
                self._spawn(job)

    # --- workers ----------------------------------------------------------
    def _poll_size(self, job: Job, path: Path, stop: threading.Event) -> None:
        while not stop.is_set():
            job.done_bytes = _dir_size(path)
            stop.wait(1.0)

    def _run_download(self, job: Job) -> None:
        poll_stop = threading.Event()
        try:
            if job._stop.is_set():
                job.status = "paused"; job.persist(); return
            job.status = "running"; job.persist()
            meta = hub.repo_meta(job.repo_id, job.revision)
            job.total_bytes = meta["total_size"]
            job.sha = meta["sha"]
            job.persist()

            st = store.get_store(job.store_id)
            if not st:
                raise RuntimeError(f"store {job.store_id} not found")
            dest = store_repo_path(st["path"], job.repo_id)
            dest.mkdir(parents=True, exist_ok=True)

            poller = threading.Thread(target=self._poll_size, args=(job, dest, poll_stop), daemon=True)
            poller.start()

            proc = subprocess.Popen(
                [sys.executable, "-m", "hugger._dlworker", job.repo_id, job.revision, str(dest)]
            )
            job._proc = proc
            while proc.poll() is None:
                if job._stop.is_set():
                    proc.terminate()
                    try:
                        proc.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                    job.status = "paused"; job.persist()
                    return
                job._stop.wait(0.5)

            rc = proc.returncode
            if rc == 0:
                size = _dir_size(dest)
                job.done_bytes = size or job.total_bytes
                store.upsert_archive(job.repo_id, job.revision, job.sha, str(dest), size, job.store_id)
                job.status = "done"; job.persist()
            elif job._stop.is_set():
                job.status = "paused"; job.persist()
            else:
                job.status = "error"; job.error = f"download exited with code {rc}"; job.persist()
        except Exception as e:
            job.status = "error"; job.error = f"{type(e).__name__}: {e}"; job.persist()
        finally:
            poll_stop.set()

    def _run_move(self, job: Job) -> None:
        try:
            if job._stop.is_set():
                job.status = "paused"; job.persist(); return
            job.status = "running"; job.persist()
            rec = store.get_archive(job.repo_id)
            if not rec:
                raise KeyError(job.repo_id)
            dest_store = store.get_store(job.store_id)
            if not dest_store:
                raise RuntimeError(f"store {job.store_id} not found")
            src = Path(rec["path"])
            dest = store_repo_path(dest_store["path"], job.repo_id)
            if src.resolve() == dest.resolve():
                job.status = "done"; job.persist(); return  # already there

            files = [p for p in src.rglob("*") if p.is_file() and not p.is_symlink()]
            job.total_bytes = sum(p.stat().st_size for p in files)
            done = 0
            for p in files:
                if job._stop.is_set():
                    job.status = "paused"; job.persist(); return
                rel = p.relative_to(src)
                out = dest / rel
                out.parent.mkdir(parents=True, exist_ok=True)
                size = p.stat().st_size
                if out.exists() and out.stat().st_size == size:  # resume: already copied
                    done += size
                    job.done_bytes = done
                    continue
                tmp = out.with_suffix(out.suffix + ".part")
                shutil.copy2(p, tmp)
                tmp.replace(out)
                done += size
                job.done_bytes = done

            # All files copied -> flip ownership, then remove the source copy.
            store.upsert_archive(job.repo_id, rec["revision"], rec["sha"], str(dest),
                                 _dir_size(dest), job.store_id)
            shutil.rmtree(src, ignore_errors=True)
            job.status = "done"; job.persist()
        except Exception as e:
            job.status = "error"; job.error = f"{type(e).__name__}: {e}"; job.persist()


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
