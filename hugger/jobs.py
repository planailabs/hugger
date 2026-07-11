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

import math
import os
import shutil
import subprocess
import sys
import traceback
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from . import _live, hub, metadata, store, util

_dir_size = util.dir_size  # kept for tests/back-compat


def _int_env(name: str, default: int, lo: int = 0) -> int:
    try:
        return max(lo, int(os.environ.get(name, str(default))))
    except ValueError:
        return default


# How many transfer jobs (download/move) may run at once; the rest wait as
# "queued" and start as slots free. 1 = serial downloads (default).
MAX_ACTIVE = _int_env("HUGGER_MAX_ACTIVE", 1, lo=1)
# Verify jobs run in a SEPARATE lane so hashing a finished model doesn't block
# the next download — one verify can run adjacent to one transfer.
MAX_VERIFY = _int_env("HUGGER_MAX_VERIFY", 1, lo=1)
# A running download making no progress for this many seconds is considered
# stalled and its worker is restarted in place (0 disables).
STALE_SECS = float(os.environ.get("HUGGER_STALE_SECS", "90") or 0)


class InsufficientSpace(Exception):
    pass


class Busy(Exception):
    """Raised when an operation conflicts with an in-flight job (e.g. moving a
    model whose download is still running)."""
    pass


def safe_repo_id(repo_id: str) -> str:
    """Return repo_id if it is safe to use as path segments, else raise.

    Blocks traversal ('..'), empty/absolute segments, and Windows path tricks so
    a crafted repo_id can't escape the archive root when joined onto a store path.
    """
    parts = repo_id.split("/")
    if not parts or any(
        p in ("", ".", "..") or "\\" in p or ":" in p for p in parts
    ):
        raise ValueError(f"unsafe repo_id: {repo_id!r}")
    return repo_id


def store_repo_path(store_path: str, repo_id: str) -> Path:
    return Path(store_path).joinpath(*safe_repo_id(repo_id).split("/"))


def _lane(job_type: str) -> str:
    """Scheduling lane: verify jobs run independently of transfers."""
    return "verify" if job_type == "verify" else "transfer"


def _terminate(proc: subprocess.Popen | None) -> None:
    """Stop a worker subprocess, escalating to kill if it ignores SIGTERM."""
    if not proc or proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
    except Exception:
        pass


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
    rate: float = 0.0               # smoothed bytes/sec on disk (runtime only)
    net_rate: float = 0.0           # smoothed wire bytes/sec from the Xet worker
    stalls: int = 0                 # auto-restarts due to staleness (runtime only)
    auto: bool = field(default=False, repr=False)  # verify auto-repairs bad files
    _stop: threading.Event = field(default_factory=threading.Event, repr=False)
    _preempt: threading.Event = field(default_factory=threading.Event, repr=False)
    _proc: subprocess.Popen | None = field(default=None, repr=False)
    _last_progress_t: float = field(default=0.0, repr=False)

    @property
    def percent(self) -> int:
        if self.total_bytes <= 0:
            return 0
        return min(100, int(self.done_bytes * 100 / self.total_bytes))

    @property
    def eta(self) -> int | None:
        """Seconds until done at the current rate, or None if not estimable."""
        if self.status != "running" or self.rate <= 0:
            return None
        remaining = self.total_bytes - self.done_bytes
        if remaining <= 0:
            return None
        secs = remaining / self.rate
        # a near-zero rate can overflow the division to float infinity
        if not math.isfinite(secs):
            return None
        return int(secs)

    def persist(self) -> None:
        store.save_job({
            "id": self.id, "repo_id": self.repo_id, "revision": self.revision,
            "type": self.type, "status": self.status, "total_bytes": self.total_bytes,
            "sha": self.sha, "error": self.error, "store_id": self.store_id,
            "src_store_id": self.src_store_id,
        })
        _live.bump()  # wake the live SSE panels on any status change

    def as_dict(self) -> dict:
        running = self.status == "running"
        return {
            "id": self.id, "repo_id": self.repo_id, "revision": self.revision,
            "type": self.type, "status": self.status, "total_bytes": self.total_bytes,
            "done_bytes": self.done_bytes, "percent": self.percent, "error": self.error,
            "store_id": self.store_id, "src_store_id": self.src_store_id,
            "rate": round(self.rate) if running else 0, "eta": self.eta, "stalls": self.stalls,
            "net_rate": round(self.net_rate) if running else 0,
        }


class JobManager:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._order: list[str] = []  # scheduling priority (front = highest)
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
            if j.store_id != store_id:
                continue
            # Reserve only the *remaining* bytes: subtract what's already on disk
            # (completed files + in-flight partials). A running job's done_bytes is
            # live; for a queued/paused one, recompute from disk so a stale seed
            # doesn't over-reserve space that's already used.
            on_disk = j.done_bytes if j.status == "running" else self._on_disk_bytes(j)
            total += max(0, j.total_bytes - on_disk)
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
        # Deduplicate: a second click (or UI + extension firing together) must not
        # spawn a second worker writing the same dest dir. Mirrors start_verify.
        existing = self._active_download_for(repo_id)
        if existing:
            return existing
        st = store.get_store(store_id) if store_id else store.get_default_store()
        if not st:
            raise RuntimeError("no data store configured")
        util.check_writable(st["path"])
        info = hub.repo_files(repo_id, revision)
        meta = metadata.build(repo_id, revision, info["sha"], info["files"], selected)
        dest = store_repo_path(st["path"], repo_id)
        # Count in-flight partials (.incomplete/.xetpart), not just completed files,
        # so retrying a half-finished download only requires its *remaining* bytes.
        already = metadata.progress_bytes(dest, meta) if dest.exists() else 0
        self._ensure_space(st, max(0, meta["total_size"] - already))
        metadata.write(dest, meta)
        # Record a (possibly partial) archive row up front so the model is
        # immediately visible/manageable/movable even before the download finishes.
        self._cache_archive(repo_id, meta, dest, st["id"])

        job = Job(id=uuid.uuid4().hex[:12], repo_id=repo_id, revision=revision,
                  type="download", store_id=st["id"], total_bytes=meta["total_size"],
                  done_bytes=already, sha=info["sha"])
        self._register(job)
        return job

    def _active_download_for(self, repo_id: str) -> Job | None:
        for j in self._jobs.values():
            if j.type == "download" and j.repo_id == repo_id and j.status in ("queued", "running", "paused"):
                return j
        return None

    def _active_verify_for(self, repo_id: str) -> Job | None:
        for j in self._jobs.values():
            if j.type == "verify" and j.repo_id == repo_id and j.status in ("queued", "running", "paused"):
                return j
        return None

    def start_verify(self, repo_id: str, auto: bool = False) -> Job:
        """Queue a job that hashes the model's downloaded files and checks them
        against the expected hashes. Runs in the verify lane (adjacent to a
        transfer). With auto=True (post-download), a failed file is deleted and
        re-downloaded automatically. Returns an existing active verify if any."""
        existing = self._active_verify_for(repo_id)
        if existing:
            return existing
        rec = store.get_archive(repo_id)
        if not rec:
            raise KeyError(repo_id)
        model_dir = Path(rec["path"])
        meta = metadata.read(model_dir)
        total = 0
        if meta:
            for f in meta.get("files", []):
                if metadata.file_downloaded(model_dir, f["path"], f.get("size")):
                    total += f.get("size", 0) or 0
        job = Job(id=uuid.uuid4().hex[:12], repo_id=repo_id, revision=rec["revision"],
                  type="verify", store_id=rec["store_id"], total_bytes=total, sha=rec["sha"],
                  auto=auto)
        self._register(job)
        return job

    def redownload_bad(self, repo_id: str, only: list[str] | None = None,
                       mark_attempted: bool = False) -> Job | None:
        """Delete bad files (those in `only`, or all recorded bad files) and resume
        the download so they're re-fetched — the original file selection is kept,
        so deleting a file just makes it 'missing' and only those re-download.

        `mark_attempted` (auto-repair) records the retried files so a file that's
        still bad after one auto re-download isn't re-fetched forever. A manual
        re-download clears that history, giving the files a fresh auto chance."""
        rec = store.get_archive(repo_id)
        if not rec:
            return None
        model_dir = Path(rec["path"])
        badrec = metadata.read_bad(model_dir)
        bad = badrec.get("files", [])
        targets = [b for b in only if b in bad] if only is not None else list(bad)
        if not targets:
            return None
        meta = metadata.read(model_dir)
        selected = meta.get("selected") if meta else None  # keep the original selection
        for rel in targets:
            (model_dir / rel).unlink(missing_ok=True)
            store.delete_file_hash(repo_id, rel)
        remaining = [b for b in bad if b not in targets]
        attempted = (set(badrec.get("attempted", [])) | set(targets)) if mark_attempted else set()
        if remaining or attempted:
            metadata.write_bad(model_dir, remaining, sorted(attempted))
        else:
            metadata.clear_bad(model_dir)
        return self.start_download(repo_id, rec["revision"], store_id=rec["store_id"],
                                   selected=selected)

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
        self._register(job)
        return job

    def _register(self, job: Job) -> None:
        with self._lock:
            self._jobs[job.id] = job
            if job.id not in self._order:
                self._order.append(job.id)
        job.status = "queued"
        job.persist()
        self._schedule()

    def _schedule(self) -> None:
        """Start queued jobs (in priority order) until each lane is full: up to
        MAX_ACTIVE transfers and MAX_VERIFY verifies, counted independently so a
        verify can run adjacent to a transfer. Called whenever a slot may free."""
        if self._shutting_down:
            return
        to_start: list[Job] = []
        with self._lock:
            slots = {"transfer": MAX_ACTIVE, "verify": MAX_VERIFY}
            for j in self._jobs.values():
                if j.status == "running":
                    slots[_lane(j.type)] -= 1
            for jid in self._order:
                if all(s <= 0 for s in slots.values()):
                    break
                j = self._jobs.get(jid)
                if j and j.status == "queued" and slots[_lane(j.type)] > 0:
                    j.status = "running"  # claim the slot now so we don't double-pick
                    j._stop = threading.Event()
                    j._preempt = threading.Event()
                    to_start.append(j)
                    slots[_lane(j.type)] -= 1
        for j in to_start:
            j.persist()
            self._spawn(j)

    def _spawn(self, job: Job) -> None:
        target = {"download": self._run_download, "move": self._run_move,
                  "verify": self._run_verify}[job.type]
        threading.Thread(target=target, args=(job,), daemon=True).start()

    # --- pause / resume / prioritise -------------------------------------
    def pause(self, job_id: str) -> None:
        job = self._jobs.get(job_id)
        if not job:
            return
        if job.status == "running":
            # Running: signal the worker to stop; it persists `paused` and frees
            # the slot (its scheduler call then starts the next queued job).
            job._stop.set()
            proc = job._proc
            if proc and proc.poll() is None:
                proc.terminate()
        elif job.status == "queued":
            # Queued: no worker yet — just mark it paused so the scheduler skips it.
            job.status = "paused"
            job.persist()

    def resume(self, job_id: str) -> None:
        job = self._jobs.get(job_id)
        if job and job.status == "paused":
            job._stop = threading.Event()
            job.status = "queued"
            job.persist()
            self._schedule()  # start now if a slot is free, else wait in the queue

    def run_now(self, job_id: str) -> None:
        """Prioritise a queued job: move it to the front and, if all slots are
        busy, preempt a running job (which returns to 'queued' and auto-resumes
        when a slot frees) so the chosen job starts immediately."""
        job = self._jobs.get(job_id)
        if not job or job.status != "queued":
            return
        victim: Job | None = None
        lane = _lane(job.type)
        cap = MAX_VERIFY if lane == "verify" else MAX_ACTIVE
        with self._lock:
            if job_id in self._order:
                self._order.remove(job_id)
            self._order.insert(0, job_id)
            # only the SAME lane competes for the slot we want
            running = [j for j in self._jobs.values()
                       if j.status == "running" and _lane(j.type) == lane]
            if len(running) >= cap and running:
                # demote the running job that's furthest from done (least lost work)
                victim = min(running, key=lambda j: j.percent)
        if victim is not None:
            victim._preempt.set()
            proc = victim._proc
            if proc and proc.poll() is None:
                proc.terminate()
            # the preempted worker re-queues itself and calls _schedule, which
            # then picks our now-front job.
        else:
            self._schedule()

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
            self._order = [jid for jid in self._order if jid in self._jobs]

    # --- queries ----------------------------------------------------------
    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def active(self) -> list[Job]:
        return [j for j in self._jobs.values() if j.status in ("queued", "running", "paused")]

    def recent(self) -> list[Job]:
        return list(self._jobs.values())

    # --- restart recovery -------------------------------------------------
    def _on_disk_bytes(self, job: Job) -> int:
        """Bytes already downloaded for `job`, computed from disk — so a job loaded
        from the DB (which doesn't persist done_bytes) shows real progress, not 0.
        Matters most for paused jobs, which never spawn the poll that would seed it."""
        if job.type != "download":
            return 0
        try:
            st = store.get_store(job.store_id)
            if not st:
                return 0
            dest = store_repo_path(st["path"], job.repo_id)
            meta = metadata.read(dest)
            return metadata.read_progress(dest, meta) if meta else 0
        except Exception:
            return 0

    def resume_pending(self) -> None:
        for row in store.list_jobs(["queued", "running", "paused"]):
            job = Job(
                id=row["id"], repo_id=row["repo_id"], revision=row["revision"] or "main",
                type=row["type"], total_bytes=row["total_bytes"] or 0, sha=row["sha"],
                store_id=row["store_id"], src_store_id=row["src_store_id"],
                status="paused" if row["status"] == "paused" else "queued",
            )
            job.done_bytes = self._on_disk_bytes(job)  # seed progress from disk
            with self._lock:
                self._jobs[job.id] = job
                self._order.append(job.id)
        # Start up to MAX_ACTIVE of the non-paused jobs; the rest stay queued.
        self._schedule()

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
            metadata.xfer_file(dest).unlink(missing_ok=True)
            # Seed from on-disk progress so a resumed job shows real progress now.
            job.done_bytes = metadata.read_progress(dest, meta, base=base)
            job.persist()

            def poll():
                # Track on-disk progress, a smoothed transfer rate (EWMA over ~1s
                # samples), and the time of the last byte gained (for stall detection).
                # `.hugger.xfer` (cumulative wire bytes from the Xet worker, when the
                # patched hf_xet exposes them) feeds a separate network-rate EWMA.
                last_t = time.monotonic(); last_b = job.done_bytes
                last_x = metadata.read_xfer(dest)
                while not poll_stop.is_set():
                    prev = job.done_bytes
                    job.done_bytes = metadata.read_progress(dest, meta, base=base)
                    now = time.monotonic()
                    if job.done_bytes != prev:
                        job._last_progress_t = now
                        _live.bump()  # push progress to the live panels as it changes
                    dt = now - last_t
                    if dt >= 1.0:
                        inst = max(0, job.done_bytes - last_b) / dt
                        job.rate = inst if job.rate <= 0 else 0.4 * inst + 0.6 * job.rate
                        x = metadata.read_xfer(dest)
                        if x is not None:
                            xinst = max(0, x - (last_x or 0)) / dt
                            job.net_rate = (xinst if job.net_rate <= 0
                                            else 0.4 * xinst + 0.6 * job.net_rate)
                            last_x = x
                        last_t = now; last_b = job.done_bytes
                    poll_stop.wait(1.0)

            threading.Thread(target=poll, daemon=True).start()

            def launch():
                env = dict(os.environ)
                env["PYTHONPATH"] = os.pathsep.join(p for p in sys.path if p)
                # Keep hf's working cache (Xet chunk cache HF_XET_CACHE=$HF_HOME/xet,
                # etc.) on the target store's volume — not the process home — so it
                # doesn't bloat / fill another filesystem and stays with the data.
                # (We pass the token explicitly, so HF_HOME isn't used for auth.)
                env["HF_HOME"] = str(Path(st["path"]) / ".hf")
                # NOTE: do NOT disable Xet here (see AGENTS.md). Xet is the fast default
                # transfer. Progress is synced from hf's tqdm bytes bar; on the classic
                # path .incomplete files also let read_progress reflect bytes-on-disk.
                return subprocess.Popen(
                    [sys.executable, "-m", "hugger._dlworker", job.repo_id, job.revision, str(dest)],
                    env=env,
                )

            proc = launch()
            job._proc = proc
            job._last_progress_t = time.monotonic()
            while proc.poll() is None:
                if self._shutting_down:
                    return  # shutdown() persisted us as queued; resume on restart
                if job._preempt.is_set():
                    # "Run now" demoted us so another job can take the slot — stop and
                    # go back to 'queued' (the resumable partial is on disk).
                    _terminate(proc)
                    job.status = "queued"; job.persist(); return
                if job._stop.is_set():
                    _terminate(proc)
                    job.status = "paused"; job.persist(); return
                if STALE_SECS and time.monotonic() - job._last_progress_t > STALE_SECS:
                    # No bytes for too long: the transfer is wedged. Restart the
                    # worker in place — it resumes from the bytes already on disk.
                    job.stalls += 1
                    _terminate(proc)
                    proc = launch(); job._proc = proc
                    job._last_progress_t = time.monotonic()
                    job.persist()  # surface the stall count to the live panels
                    continue
                job._stop.wait(0.5)
            if self._shutting_down:
                return

            rc = proc.returncode
            poll_stop.set()
            metadata.progress_file(dest).unlink(missing_ok=True)
            metadata.xfer_file(dest).unlink(missing_ok=True)
            if rc == 0:
                self._cache_archive(job.repo_id, meta, dest, job.store_id)
                state = metadata.state(dest, meta)
                job.done_bytes = state["downloaded_bytes"]
                job.status = "done"; job.persist()
                # Hash the model in a separate verify job (adjacent lane) so this
                # download's slot frees immediately for the next one and the
                # integrity hashing doesn't block it. auto=True so any file that
                # fails the hash is deleted and re-downloaded automatically.
                try:
                    self.start_verify(job.repo_id, auto=True)
                except Exception:
                    # download is done but integrity check couldn't start — log it
                    # so a silent skip doesn't hide possibly-corrupt files.
                    traceback.print_exc()
            elif job._preempt.is_set():
                job.status = "queued"; job.persist()
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
            if not self._shutting_down:
                self._schedule()  # a slot freed — start the next queued job

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
                if job._preempt.is_set():
                    job.status = "queued"; job.persist(); return
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
        finally:
            if not self._shutting_down:
                self._schedule()  # a slot freed — start the next queued job

    def _run_verify(self, job: Job) -> None:
        """Hash each downloaded file and check it against the expected hash. Runs
        in the verify lane. Resumable: `.hugger.verify` records which files have
        passed/failed so an aborted run skips them and continues."""
        try:
            if job._stop.is_set():
                job.status = "paused"; job.persist(); return
            job.status = "running"; job.persist()
            rec = store.get_archive(job.repo_id)
            if not rec:
                raise KeyError(job.repo_id)
            model_dir = Path(rec["path"])
            meta = metadata.read(model_dir)
            if not meta:
                raise RuntimeError("no metadata to verify against")

            st = metadata.read_verify(model_dir)
            ok_set = set(st.get("ok", []))
            bad = list(st.get("bad", []))
            base = int(st.get("done_bytes", 0) or 0)
            job.auto = job.auto or bool(st.get("auto"))  # survive an interrupted auto verify
            job.total_bytes = job.total_bytes or meta.get("total_size", 0)
            job.done_bytes = base
            job.persist()

            last_save = [time.monotonic()]

            def save(force=False):
                now = time.monotonic()
                if force or now - last_save[0] >= 1.0:
                    last_save[0] = now
                    metadata.write_verify(model_dir, {"ok": sorted(ok_set), "bad": bad,
                                                      "done_bytes": base, "auto": job.auto})
                    _live.bump()

            for f in meta.get("files", []):
                rel = f["path"]
                if rel in ok_set or rel in bad:
                    continue
                if self._shutting_down:
                    save(force=True); return
                if job._preempt.is_set():
                    save(force=True); job.status = "queued"; job.persist(); return
                if job._stop.is_set():
                    save(force=True); job.status = "paused"; job.persist(); return
                size = f.get("size", 0) or 0
                if not metadata.file_downloaded(model_dir, rel, f.get("size")):
                    continue  # only verify what's actually downloaded
                algo = metadata.algo_for(f)
                acc = [base]  # base + bytes hashed so far in this file

                def on_bytes(n):
                    acc[0] += n
                    job.done_bytes = acc[0]

                try:
                    h = util.hash_file(model_dir / rel, algo, on_bytes=on_bytes,
                                       stop=lambda: job._stop.is_set() or job._preempt.is_set())
                except util.HashAborted:
                    save(force=True)
                    job.status = "queued" if job._preempt.is_set() else "paused"
                    job.persist(); return
                except OSError:
                    bad.append(rel); save(force=True); continue
                store.set_file_hash(job.repo_id, rel, size,
                                    (model_dir / rel).stat().st_mtime, algo, h)
                if f.get("rhash") and h != f["rhash"]:
                    bad.append(rel)
                else:
                    ok_set.add(rel)
                base += size
                job.done_bytes = base
                save()

            metadata.verify_file(model_dir).unlink(missing_ok=True)
            if not bad:
                metadata.clear_bad(model_dir)  # all good now
                job.status = "done"; job.persist()
                return
            # Some files failed the hash. Persist them (for the UI re-download) and
            # track which we've already auto-retried so a still-bad file isn't
            # re-fetched forever.
            prev_attempted = set(metadata.read_bad(model_dir).get("attempted", []))
            metadata.write_bad(model_dir, bad, prev_attempted)
            fresh = [b for b in bad if b not in prev_attempted]
            summary = ", ".join(bad[:3]) + (" …" if len(bad) > 3 else "")
            if job.auto and fresh:
                # auto-repair: delete the freshly-bad files and re-download them.
                job.status = "error"
                job.error = f"{len(bad)} file(s) failed; re-downloading {len(fresh)}"
                job.persist()
                try:
                    self.redownload_bad(job.repo_id, only=fresh, mark_attempted=True)
                except Exception as e:
                    # the repair couldn't be queued — don't leave the user waiting
                    # on a "re-downloading" message for a job that never starts.
                    traceback.print_exc()
                    job.error = f"{len(bad)} file(s) failed; auto-repair could not start: {e}"
                    job.persist()
            else:
                job.status = "error"
                job.error = f"{len(bad)} file(s) failed verification: {summary}"
                job.persist()
        except Exception as e:
            job.status = "error"; job.error = f"{type(e).__name__}: {e}"; job.persist()
        finally:
            if not self._shutting_down:
                self._schedule()  # verify lane freed — start the next queued job

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
        model_dir = Path(rec["path"]).resolve()
        target = (model_dir / rel).resolve()
        if not target.is_relative_to(model_dir):
            raise ValueError(f"path escapes model dir: {rel!r}")
        if target.is_file():
            target.unlink()
        store.delete_file_hash(repo_id, rel)
        meta = metadata.read(model_dir)
        if meta:
            self._cache_archive(repo_id, meta, model_dir, rec["store_id"])
        _live.bump()  # archive size/files changed -> refresh live panels


manager = JobManager()


def delete_archive(repo_id: str) -> None:
    rec = store.get_archive(repo_id)
    if rec:
        shutil.rmtree(rec["path"], ignore_errors=True)
    store.delete_archive_and_hashes(repo_id)
    _live.bump()


def check_update(repo_id: str) -> dict:
    rec = store.get_archive(repo_id)
    if not rec:
        raise KeyError(repo_id)
    rsha = hub.remote_sha(repo_id, rec["revision"])
    available = rsha != rec["sha"]
    store.set_update_status(repo_id, rsha, available)
    _live.bump()
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
    if count:
        _live.bump()
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
