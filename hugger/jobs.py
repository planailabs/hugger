"""Background jobs: downloads and cross-store moves, both pausable + resumable.

- Download jobs run `python -m hugger._dlworker` in a subprocess (PYTHONPATH is
  passed through so it imports correctly under a wrapped/Nix interpreter). Pause
  terminates it mid-file; huggingface_hub leaves a resumable partial, and unpause
  re-launches it. The files to fetch come from the model's .hugger.json.
- Move jobs copy a model's files between stores file-by-file (skipping
  already-copied files, so partial models move and moves resume), then flip
  ownership and remove the source.
- Disk space is checked before a job starts, counting other pending jobs targeting
  the same store so concurrent jobs can't collectively overrun.
- Jobs are persisted; queued/running jobs auto-resume on startup, paused stay paused.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from . import hub, metadata, store, util

_dir_size = util.dir_size  # kept for tests/back-compat


class InsufficientSpace(Exception):
    pass


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
    store_id: str | None = None
    src_store_id: str | None = None
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

    # --- space accounting -------------------------------------------------
    def pending_bytes(self, store_id: str, exclude: str | None = None) -> int:
        total = 0
        for j in self._jobs.values():
            if j.id == exclude or j.status not in ("queued", "running"):
                continue
            if j.store_id == store_id:
                total += max(0, j.total_bytes - j.done_bytes)
        return total

    def _ensure_space(self, st: dict, required: int) -> None:
        free = util.free_space(st["path"])
        pend = self.pending_bytes(st["id"])
        if required + pend > free:
            raise InsufficientSpace(
                f"need {util.human_size(required)} (+{util.human_size(pend)} already "
                f"queued) but only {util.human_size(free)} free in store '{st['name']}'"
            )

    # --- starting work ----------------------------------------------------
    def start_download(self, repo_id: str, revision: str = "main",
                       store_id: str | None = None, selected: list[str] | None = None) -> Job:
        st = store.get_store(store_id) if store_id else store.get_default_store()
        if not st:
            raise RuntimeError("no data store configured")
        util.check_writable(st["path"])
        info = hub.repo_files(repo_id, revision)
        meta = metadata.build(repo_id, revision, info["sha"], info["files"], selected)
        dest = store_repo_path(st["path"], repo_id)
        already = metadata.state(dest, meta)["downloaded_bytes"] if dest.exists() else 0
        self._ensure_space(st, max(0, meta["total_size"] - already))
        metadata.write(dest, meta)

        job = Job(id=uuid.uuid4().hex[:12], repo_id=repo_id, revision=revision,
                  type="download", store_id=st["id"], total_bytes=meta["total_size"],
                  done_bytes=already, sha=info["sha"])
        self._register_and_run(job)
        return job

    def start_move(self, repo_id: str, dest_store_id: str) -> Job:
        rec = store.get_archive(repo_id)
        if not rec:
            raise KeyError(repo_id)
        st = store.get_store(dest_store_id)
        if not st:
            raise RuntimeError("destination store not found")
        util.check_writable(st["path"])
        self._ensure_space(st, rec["size_bytes"] or 0)
        job = Job(id=uuid.uuid4().hex[:12], repo_id=repo_id, revision=rec["revision"],
                  type="move", store_id=dest_store_id, src_store_id=rec["store_id"],
                  sha=rec["sha"], total_bytes=rec["size_bytes"] or 0)
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
            if job.status != "paused":
                self._spawn(job)

    # --- workers ----------------------------------------------------------
    def _run_download(self, job: Job) -> None:
        poll_stop = threading.Event()
        try:
            if job._stop.is_set():
                job.status = "paused"; job.persist(); return
            job.status = "running"; job.persist()

            st = store.get_store(job.store_id)
            if not st:
                raise RuntimeError(f"store {job.store_id} not found")
            dest = store_repo_path(st["path"], job.repo_id)
            meta = metadata.read(dest)
            if not meta:  # resume with no metadata: rebuild for all files
                info = hub.repo_files(job.repo_id, job.revision)
                meta = metadata.build(job.repo_id, job.revision, info["sha"], info["files"], None)
                metadata.write(dest, meta)
            job.total_bytes = meta["total_size"]; job.sha = meta["sha"]; job.persist()

            def poll():
                while not poll_stop.is_set():
                    job.done_bytes = metadata.state(dest, meta)["downloaded_bytes"]
                    poll_stop.wait(1.0)

            threading.Thread(target=poll, daemon=True).start()

            env = dict(os.environ)
            env["PYTHONPATH"] = os.pathsep.join(p for p in sys.path if p)
            proc = subprocess.Popen(
                [sys.executable, "-m", "hugger._dlworker", job.repo_id, job.revision, str(dest)],
                env=env,
            )
            job._proc = proc
            while proc.poll() is None:
                if job._stop.is_set():
                    proc.terminate()
                    try:
                        proc.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                    job.status = "paused"; job.persist(); return
                job._stop.wait(0.5)

            rc = proc.returncode
            poll_stop.set()
            if rc == 0:
                self._cache_archive(job.repo_id, meta, dest, job.store_id)
                state = metadata.state(dest, meta)
                job.done_bytes = state["downloaded_bytes"]
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
                job.status = "done"; job.persist(); return

            files = [p for p in src.rglob("*") if p.is_file() and not p.is_symlink()]
            job.total_bytes = sum(p.stat().st_size for p in files)
            done = 0
            for p in files:
                if job._stop.is_set():
                    job.status = "paused"; job.persist(); return
                out = dest / p.relative_to(src)
                out.parent.mkdir(parents=True, exist_ok=True)
                size = p.stat().st_size
                if out.exists() and out.stat().st_size == size:
                    done += size; job.done_bytes = done; continue
                tmp = out.with_suffix(out.suffix + ".part")
                shutil.copy2(p, tmp)
                tmp.replace(out)
                done += size; job.done_bytes = done

            meta = metadata.read(dest) or {}
            self._cache_archive(job.repo_id, meta, dest, job.store_id, fallback=rec)
            shutil.rmtree(src, ignore_errors=True)
            job.status = "done"; job.persist()
        except Exception as e:
            job.status = "error"; job.error = f"{type(e).__name__}: {e}"; job.persist()

    def _cache_archive(self, repo_id, meta, dest, store_id, fallback=None) -> None:
        if meta:
            state = metadata.state(dest, meta)
            store.upsert_archive(
                repo_id, meta["revision"], meta["sha"], str(dest),
                state["downloaded_bytes"], store_id, total_bytes=meta["total_size"],
                n_files=state["n_files"], n_downloaded=state["n_downloaded"],
                complete=state["complete"],
            )
        elif fallback:  # no metadata file (legacy) — keep prior cache values
            store.upsert_archive(repo_id, fallback["revision"], fallback["sha"], str(dest),
                                 _dir_size(dest), store_id)


manager = JobManager()


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


def import_store(store_id: str) -> int:
    """Rebuild the DB cache for a store by scanning its .hugger.json files."""
    st = store.get_store(store_id)
    if not st:
        raise KeyError(store_id)
    count = 0
    for metafile in Path(st["path"]).rglob(metadata.META_NAME):
        model_dir = metafile.parent
        meta = metadata.read(model_dir)
        if not meta or "repo_id" not in meta:
            continue
        state = metadata.state(model_dir, meta)
        store.upsert_archive(
            meta["repo_id"], meta.get("revision", "main"), meta.get("sha", ""),
            str(model_dir), state["downloaded_bytes"], store_id,
            total_bytes=meta["total_size"], n_files=state["n_files"],
            n_downloaded=state["n_downloaded"], complete=state["complete"],
        )
        count += 1
    return count


def file_status(repo_id: str, rel: str) -> dict:
    rec = store.get_archive(repo_id)
    if not rec:
        return {"repo_id": repo_id, "path": rel, "archived": False, "downloaded": False}
    meta = metadata.read(rec["path"])
    size = None
    if meta:
        size = next((f["size"] for f in meta.get("files", []) if f["path"] == rel), None)
    return {
        "repo_id": repo_id, "path": rel, "archived": True,
        "downloaded": metadata.file_downloaded(rec["path"], rel, size),
    }
