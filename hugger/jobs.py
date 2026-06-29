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


class Busy(Exception):
    """Raised when an operation conflicts with an in-flight job (e.g. moving a
    model whose download is still running)."""
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
        self._shutting_down = False

    def shutdown(self) -> None:
        """On server shutdown, terminate in-flight transfers and persist them as
        queued (not error) so they auto-resume on the next start."""
        self._shutting_down = True
        for j in list(self._jobs.values()):
            if j.status in ("queued", "running"):
                p = j._proc
                if p and p.poll() is None:
                    try:
                        p.terminate()
                    except Exception:
                        pass
                j.status = "queued"
                try:
                    j.persist()
                except Exception:
                    pass

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
        # Record a (possibly partial) archive row up front so the model is
        # immediately visible/manageable/movable even before the download finishes.
        self._cache_archive(repo_id, meta, dest, st["id"])

        job = Job(id=uuid.uuid4().hex[:12], repo_id=repo_id, revision=revision,
                  type="download", store_id=st["id"], total_bytes=meta["total_size"],
                  done_bytes=already, sha=info["sha"])
        self._register_and_run(job)
        return job

    def _active_download_for(self, repo_id: str) -> Job | None:
        for j in self._jobs.values():
            if j.type == "download" and j.repo_id == repo_id and j.status in ("queued", "running", "paused"):
                return j
        return None

    def active_download_files(self, repo_id: str) -> set[str]:
        """Files a queued/running download for `repo_id` is fetching (from its
        metadata's selection) — used to mark them 'downloading' and lock them."""
        for j in self._jobs.values():
            if j.type == "download" and j.repo_id == repo_id and j.status in ("queued", "running"):
                st = store.get_store(j.store_id)
                if st:
                    meta = metadata.read(store_repo_path(st["path"], repo_id))
                    if meta:
                        return set(meta.get("selected", []))
        return set()

    def start_move(self, repo_id: str, dest_store_id: str) -> Job:
        # Edge case: a download for this model is in flight.
        dl = self._active_download_for(repo_id)
        if dl and dl.status in ("queued", "running"):
            raise Busy("pause the download before moving this model")
        rec = store.get_archive(repo_id)
        if not rec:
            raise KeyError(repo_id)
        st = store.get_store(dest_store_id)
        if not st:
            raise RuntimeError("destination store not found")
        if st["id"] == rec["store_id"]:
            raise Busy("model is already in that store")
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

    def retry(self, job_id: str) -> Job | None:
        """Restart a failed job (it may only exist in the DB after a restart).
        Starts a fresh job with the same repo/store/files; the old error row
        stays in history."""
        job = self._jobs.get(job_id)
        if job is not None:
            if job.status != "error":
                return None
            repo, store_id, typ, revision = job.repo_id, job.store_id, job.type, job.revision
        else:
            row = next((r for r in store.recent_jobs() if r["id"] == job_id), None)
            if not row or row["status"] != "error":
                return None
            repo, store_id, typ, revision = row["repo_id"], row["store_id"], row["type"], row["revision"] or "main"
        if typ == "move":
            new = self.start_move(repo, store_id)
        else:
            selected = None  # re-pick the originally selected files if metadata survives
            st = store.get_store(store_id)
            if st:
                meta = metadata.read(store_repo_path(st["path"], repo))
                if meta:
                    selected = meta.get("selected")
            new = self.start_download(repo, revision, store_id=store_id, selected=selected)
        # Mark the old errored job as retried, pointing at the new job.
        store.mark_retried(job_id, new.id)
        if job is not None:
            job.status = "retried"
        return new

    def clear_finished(self) -> None:
        with self._lock:
            self._jobs = {k: v for k, v in self._jobs.items()
                          if v.status in ("queued", "running", "paused")}

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
            job.total_bytes = meta["total_size"]; job.sha = meta["sha"]
            # Bytes already complete on disk before this run. hf's tqdm only counts
            # the files it (re)downloads, so we add this base to its reported bytes
            # to show the full total. Clear any stale progress file first.
            base = metadata.state(dest, meta)["downloaded_bytes"]
            metadata.progress_file(dest).unlink(missing_ok=True)
            # Seed from on-disk progress so a resumed job shows real progress now.
            job.done_bytes = metadata.read_progress(dest, meta, base=base)
            job.persist()

            def poll():
                while not poll_stop.is_set():
                    job.done_bytes = metadata.read_progress(dest, meta, base=base)
                    poll_stop.wait(1.0)

            threading.Thread(target=poll, daemon=True).start()

            env = dict(os.environ)
            env["PYTHONPATH"] = os.pathsep.join(p for p in sys.path if p)
            # NOTE: do NOT disable Xet here (see AGENTS.md). Xet is the fast default
            # transfer. Progress is synced from hf's tqdm bytes bar; on the classic
            # path .incomplete files also let read_progress reflect bytes-on-disk.
            proc = subprocess.Popen(
                [sys.executable, "-m", "hugger._dlworker", job.repo_id, job.revision, str(dest)],
                env=env,
            )
            job._proc = proc
            while proc.poll() is None:
                if self._shutting_down:
                    return  # shutdown() persisted us as queued; resume on restart
                if job._stop.is_set():
                    proc.terminate()
                    try:
                        proc.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                    job.status = "paused"; job.persist(); return
                job._stop.wait(0.5)
            if self._shutting_down:
                return

            rc = proc.returncode
            poll_stop.set()
            metadata.progress_file(dest).unlink(missing_ok=True)
            if rc == 0:
                self._cache_archive(job.repo_id, meta, dest, job.store_id)
                state = metadata.state(dest, meta)
                job.done_bytes = state["downloaded_bytes"]
                # Cache hashes of the freshly downloaded files so later update
                # verification is cheap (best-effort).
                for f in meta.get("files", []):
                    if metadata.file_downloaded(dest, f["path"], f.get("size")):
                        try:
                            self.local_hash(job.repo_id, f["path"], dest, metadata.algo_for(f))
                        except OSError:
                            pass
                job.status = "done"; job.persist()
            elif job._stop.is_set():
                job.status = "paused"; job.persist()
            elif rc is not None and rc < 0:
                # Killed by a signal (server shutdown / kill / OOM), not a real
                # download failure. Leave it queued so it auto-resumes on restart.
                job.status = "queued"; job.persist()
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
                # Same location (e.g. stores share a path) — nothing to copy, but
                # still record the new store ownership so the DB isn't left stale.
                self._cache_archive(job.repo_id, metadata.read(dest) or {}, dest,
                                    job.store_id, fallback=rec)
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
            # If a paused download for this model was moved, retarget it so an
            # unpause resumes into the destination store (its partial + metadata
            # are now there).
            dl = self._active_download_for(job.repo_id)
            if dl and dl.status == "paused":
                dl.store_id = job.store_id
                dl.persist()
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

    # --- hashing / verification ------------------------------------------
    def local_hash(self, repo_id: str, rel: str, model_dir: Path, algo: str) -> str:
        """Hash of a local file, cached in the DB keyed by (repo_id, path) and
        invalidated when size/mtime change."""
        f = Path(model_dir) / rel
        st = f.stat()
        cached = store.get_file_hash(repo_id, rel)
        if (cached and cached["algo"] == algo and cached["size"] == st.st_size
                and abs(cached["mtime"] - st.st_mtime) < 1e-6):
            return cached["hash"]
        h = util.hash_file(f, algo)
        store.set_file_hash(repo_id, rel, st.st_size, st.st_mtime, algo, h)
        return h

    def verify(self, repo_id: str, revision: str | None = None) -> dict:
        """Compare the local archive against the (latest) revision on the Hub.
        Size is checked first (cheap), then the hash (cached). Returns per-file
        status: missing | changed | unchanged."""
        rec = store.get_archive(repo_id)
        if not rec:
            raise KeyError(repo_id)
        model_dir = Path(rec["path"])
        info = hub.repo_files(repo_id, revision or rec["revision"])
        files, changed, missing = [], [], []
        for f in info["files"]:
            local = model_dir / f["path"]
            if not local.exists():
                status = "missing"; missing.append(f["path"])
            elif local.stat().st_size != f["size"]:
                status = "changed"; changed.append(f["path"])
            else:
                algo = "sha256" if f["lfs"] else "gitblob"
                same = bool(f["rhash"]) and self.local_hash(repo_id, f["path"], model_dir, algo) == f["rhash"]
                status = "unchanged" if same else "changed"
                if not same:
                    changed.append(f["path"])
            files.append({"path": f["path"], "size": f["size"], "status": status})
        # Were all of the repo's files previously downloaded? (offer "all" then)
        all_present = not missing and all(
            (model_dir / f["path"]).exists() for f in info["files"]
        )
        return {
            "repo_id": repo_id, "sha": info["sha"], "files": files,
            "changed": changed, "missing": missing, "all_present": all_present,
        }

    # --- file management --------------------------------------------------
    def update_selected(self, job_id: str, selected: list[str]) -> None:
        """Change which files a *paused* download will fetch."""
        job = self._jobs.get(job_id)
        if not job or job.type != "download" or job.status != "paused":
            return
        st = store.get_store(job.store_id)
        if not st:
            return
        dest = store_repo_path(st["path"], job.repo_id)
        info = hub.repo_files(job.repo_id, job.revision)
        meta = metadata.build(job.repo_id, job.revision, info["sha"], info["files"], selected)
        metadata.write(dest, meta)
        job.total_bytes = meta["total_size"]
        job.done_bytes = metadata.state(dest, meta)["downloaded_bytes"]
        job.persist()
        self._cache_archive(job.repo_id, meta, dest, job.store_id)

    def remove_file(self, repo_id: str, rel: str) -> None:
        rec = store.get_archive(repo_id)
        if not rec:
            return
        model_dir = Path(rec["path"])
        target = (model_dir / rel)
        if target.is_file():
            target.unlink()
        store.delete_file_hash(repo_id, rel)
        meta = metadata.read(model_dir)
        if meta:
            self._cache_archive(repo_id, meta, model_dir, rec["store_id"])


manager = JobManager()


def delete_archive(repo_id: str) -> None:
    rec = store.get_archive(repo_id)
    if rec:
        shutil.rmtree(rec["path"], ignore_errors=True)
    store.delete_archive_and_hashes(repo_id)


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
