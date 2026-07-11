"""Resumable Xet downloads, torrent-style.

huggingface_hub's high-level download has no Xet resume: an interrupted file is
re-fetched from scratch, and its on-disk chunk cache does NOT retain data chunks
across runs (it's ~tens of KB even after a multi-MB download — shard/CAS metadata,
not file data). So we drive hf_xet's lower-level UNORDERED byte-range stream
(`XetDownloadStreamGroup.download_unordered_stream`) ourselves: chunks arrive in
completion order as `(offset, bytes)` and are written at their offsets into
`<file>.xetpart`, with a SQLite sidecar (see `_xet_ranges`) recording which byte
ranges are present. On completion the part is renamed and the sidecar removed.
A retry — or a whole new run — fetches only the missing ranges.

Why the unordered stream (vs the ordered one used before): the ordered iterator
only yields the NEXT in-file chunk, while xet fetches many terms concurrently —
so a slow first term looks identical to a wedged connection and defeats stall
detection. With completion-order delivery, chunk arrival IS network liveness:
the `_iter_timeout` watchdog only fires when nothing is actually moving, and
nothing xet fetched ahead is thrown away on retry.

Why it used to "go stale" on big repos, and how this avoids it:
  * The old code created ONE stream group and reused it for the whole (multi-hour)
    download; once its short-lived CAS token expired mid-stream the iterator
    blocked forever. We now use a SHORT-LIVED group per file (recreated on each
    retry), so a token only has to outlive a single file/attempt.
  * A read-timeout watchdog (`_iter_timeout`) turns a wedged stream into an error
    instead of an infinite hang, and `_stream_resilient` retries with a fresh
    group, resuming from the ranges already on disk — so even a single huge file
    that outlives a token recovers on its own.

Files that aren't Xet-backed return to the caller for the classic path.
Disable with HUGGER_XET_RESUME=0 (classic snapshot_download path; resumes LFS but
not Xet).
"""
from __future__ import annotations

import concurrent.futures
import os
import queue
import sys
import threading
import time
from pathlib import Path

from . import _xet_ranges

PART_SUFFIX = ".xetpart"

_available: bool | None = None


def _int_env(name: str, default: int, lo: int = 0) -> int:
    try:
        return max(lo, int(os.environ.get(name, str(default))))
    except ValueError:
        return default


def _concurrency() -> int:
    """How many files to stream concurrently (HUGGER_XET_CONCURRENCY, default 8)."""
    return _int_env("HUGGER_XET_CONCURRENCY", 8, lo=1)


def _read_timeout() -> int:
    """Abort a file's stream if no chunk arrives for this long (seconds), so a
    wedged CAS connection — the usual cause of a download going 'stale' on a big
    repo once its token expires mid-stream — fails fast and is retried instead of
    blocking forever. The unordered stream yields chunks in completion order, so
    'no chunk' really means 'no network progress' (no head-of-line false
    positives). HUGGER_XET_READ_TIMEOUT, default 60; 0 disables."""
    return _int_env("HUGGER_XET_READ_TIMEOUT", 60, lo=0)


def _retries() -> int:
    """Per-file retry budget on a transient failure (timeout / dropped connection
    / expired token). HUGGER_XET_RETRIES, default 6."""
    return _int_env("HUGGER_XET_RETRIES", 6, lo=0)


_SENTINEL = object()


def _iter_timeout(gen, timeout: int):
    """Yield from a blocking generator, raising TimeoutError if no item arrives
    within `timeout` seconds. The producer runs in a daemon thread; on timeout we
    abandon it (it's blocked on a dead socket and dies with the process) and let
    the caller retry with a fresh group, resuming from bytes already on disk."""
    if not timeout:
        yield from gen
        return
    q: queue.Queue = queue.Queue(maxsize=16)  # bounded -> backpressure, no runaway readahead

    def produce():
        try:
            for item in gen:
                q.put(item)
            q.put(_SENTINEL)
        except BaseException as e:  # surface producer errors to the consumer
            q.put(e)

    threading.Thread(target=produce, daemon=True).start()
    while True:
        try:
            item = q.get(timeout=timeout)
        except queue.Empty:
            raise TimeoutError(f"no data for {timeout}s (stream wedged)")
        if item is _SENTINEL:
            return
        if isinstance(item, BaseException):
            raise item
        yield item


def available() -> bool:
    """Whether this hf_xet / huggingface_hub exposes the streaming range API."""
    global _available
    if _available is None:
        try:
            import hf_xet  # noqa: F401
            from huggingface_hub import get_hf_file_metadata, hf_hub_url  # noqa: F401
            from huggingface_hub.utils._xet import (  # noqa: F401
                get_xet_session, xet_headers_without_auth,
            )
            sess = get_xet_session()
            _available = (hasattr(sess, "new_download_stream_group")
                          and hasattr(hf_xet, "XetFileInfo")
                          and hasattr(hf_xet.XetDownloadStreamGroup, "download_unordered_stream"))
        except Exception:
            _available = False
    return _available


def enabled() -> bool:
    return os.environ.get("HUGGER_XET_RESUME", "1") != "0" and available()


def _new_group(refresh_route: str, headers: dict):
    from huggingface_hub.utils._xet import get_xet_session, xet_headers_without_auth
    return get_xet_session().new_download_stream_group(
        token_refresh_url=refresh_route,
        token_refresh_headers=headers,
        custom_headers=xet_headers_without_auth(headers),
    )


# Fsync the part and commit its ranges after this many new bytes — or after
# _COMMIT_SECS with any pending bytes, so the parent's progress poll (and its
# 90s stall watchdog) keeps advancing even on slow links.
_COMMIT_BYTES = 8 << 20
_COMMIT_SECS = 5.0


def _stream_one(group, rel: str, file_hash: str, expected: int, dest_dir: str | Path) -> None:
    """Stream one Xet file through `group` into `<rel>.xetpart`, fetching only
    the byte ranges its sidecar doesn't already record, then rename to the final
    name. Raises on incomplete coverage (partial + sidecar are left on disk for
    the next attempt)."""
    from hf_xet import XetFileInfo

    final = Path(dest_dir) / rel
    final.parent.mkdir(parents=True, exist_ok=True)
    part = final.with_name(final.name + PART_SUFFIX)
    if final.exists() and final.stat().st_size == expected:
        # Already complete; drop leftovers (e.g. a crash between rename and
        # sidecar removal on a previous run).
        part.unlink(missing_ok=True)
        _xet_ranges.db_path(part).unlink(missing_ok=True)
        return

    db = _xet_ranges.RangeDB.open(part, file_hash, expected)
    try:
        part.touch()  # r+b needs the file to exist (also covers expected == 0)
        holes = db.missing(expected)
        if holes:
            # ponytail: no preallocation/sparse flags — offset writes leave holes
            # sparse on POSIX; NTFS zero-fills gaps (wasted writes, still correct:
            # the sidecar, not st_size, says which bytes are real).
            with open(part, "r+b") as f:
                pending = 0
                last_commit = time.monotonic()

                def checkpoint():
                    # Data must be durable BEFORE the sidecar claims it, so a
                    # crash can only under-claim (lost tail is re-fetched).
                    nonlocal pending, last_commit
                    f.flush()
                    os.fsync(f.fileno())
                    db.commit()
                    pending, last_commit = 0, time.monotonic()

                for a, b in holes:
                    stream = group.download_unordered_stream(
                        XetFileInfo(file_hash, expected), start=a, end=b)
                    # Offsets are relative to the requested range start.
                    for off, data in _iter_timeout(stream, _read_timeout()):
                        f.seek(a + off)
                        f.write(data)
                        db.add(a + off, a + off + len(data))
                        pending += len(data)
                        if pending >= _COMMIT_BYTES or (
                                pending and time.monotonic() - last_commit >= _COMMIT_SECS):
                            checkpoint()
                if pending:
                    checkpoint()
        covered = db.covered()
        if covered != expected:
            raise RuntimeError(f"xet download incomplete for {rel}: {covered} != {expected}")
        part.replace(final)
        db.finalize()
    finally:
        db.close()


def _stream_resilient(make_group, rel: str, file_hash: str, expected: int,
                      dest_dir: str | Path) -> None:
    """Stream one file, retrying on transient failure (timeout / dropped CAS
    connection / expired token) with a FRESH group each attempt and resuming from
    the ranges already recorded for `.xetpart`. A short-lived per-file group
    avoids the multi-hour token expiry that wedges a single long-lived shared
    group."""
    attempts = _retries() + 1
    for i in range(attempts):
        try:
            _stream_one(make_group(), rel, file_hash, expected, dest_dir)
            return
        except Exception as e:
            if i == attempts - 1:
                raise
            wait = min(2 ** i, 30)
            print(f"[xet] {rel}: {type(e).__name__}: {e} — retry {i + 1}/{attempts - 1} "
                  f"in {wait}s (resuming from disk)", file=sys.stderr, flush=True)
            time.sleep(wait)


def download_all(repo_id: str, revision: str, rels: list[str], dest_dir: str | Path,
                 token: str | None) -> list[str]:
    """Download `rels` into `dest_dir`. Each Xet-backed file streams through its
    OWN short-lived group (recreated per attempt), concurrently across files. Each
    file resumes from its `.xetpart`. Returns the rels that are NOT Xet-backed,
    for the caller to fetch classically."""
    from huggingface_hub import get_hf_file_metadata, hf_hub_url
    from huggingface_hub.utils import build_hf_headers

    headers = build_hf_headers(token=token)
    xet_items: list[tuple[str, str, int, str]] = []  # (rel, file_hash, size, refresh_route)
    classic: list[str] = []
    for rel in rels:
        meta = get_hf_file_metadata(hf_hub_url(repo_id, filename=rel, revision=revision), headers=headers)
        xfd = getattr(meta, "xet_file_data", None)
        if xfd is None:
            classic.append(rel)
        else:
            xet_items.append((rel, xfd.file_hash, meta.size, xfd.refresh_route))

    if xet_items:
        def task(rel, file_hash, size, refresh_route):
            _stream_resilient(lambda: _new_group(refresh_route, headers),
                              rel, file_hash, size, dest_dir)

        workers = min(_concurrency(), len(xet_items))
        if workers <= 1:
            for item in xet_items:
                task(*item)
        else:
            # download_stream releases the GIL for the network/CAS work, so files
            # overlap real transfer. A failed file leaves its `.xetpart` for the
            # next run; we let the others finish, then surface the first error.
            with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
                futs = {ex.submit(task, *item): item[0] for item in xet_items}
                errors = [(futs[f], f.exception())
                          for f in concurrent.futures.as_completed(futs) if f.exception()]
            if errors:
                rel, err = errors[0]
                raise RuntimeError(f"xet download failed for {rel}: {err}") from err
    return classic


def download_file(repo_id: str, revision: str, rel: str, dest_dir: str | Path,
                  token: str | None) -> bool:
    """Resume-download a single file via Xet. Returns True if handled (Xet file),
    or False if it isn't Xet-backed (caller should use the classic path)."""
    from huggingface_hub import get_hf_file_metadata, hf_hub_url
    from huggingface_hub.utils import build_hf_headers

    headers = build_hf_headers(token=token)
    meta = get_hf_file_metadata(hf_hub_url(repo_id, filename=rel, revision=revision), headers=headers)
    xfd = getattr(meta, "xet_file_data", None)
    if xfd is None:
        return False  # not a Xet file — fall back to the classic download
    _stream_resilient(lambda: _new_group(xfd.refresh_route, headers),
                      rel, xfd.file_hash, meta.size, dest_dir)
    return True
