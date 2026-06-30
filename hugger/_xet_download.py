"""Resumable Xet downloads.

huggingface_hub's high-level download writes a whole Xet file in one shot (no
partial-on-disk, no resume). hf_xet does expose a lower-level byte-range stream
(`XetDownloadStreamGroup.download_stream(file_info, start=N)`); we drive it
directly so a Xet file resumes from the bytes already on disk.

Bytes are written to `<file>.xetpart` and renamed to the final name on
completion, so an interrupted/paused download continues where it stopped. Files
that aren't Xet-backed return False so the caller falls back to the classic path.

Disable with HUGGER_XET_RESUME=0 (then the classic snapshot_download path is used,
which resumes LFS files but not Xet).
"""
from __future__ import annotations

import concurrent.futures
import os
from pathlib import Path

PART_SUFFIX = ".xetpart"

_available: bool | None = None


def _concurrency() -> int:
    """How many files to stream through the group at once (HUGGER_XET_CONCURRENCY,
    default 8). The shared chunk cache still dedups across the concurrent files."""
    try:
        return max(1, int(os.environ.get("HUGGER_XET_CONCURRENCY", "8")))
    except ValueError:
        return 8


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
            _available = hasattr(sess, "new_download_stream_group") and hasattr(hf_xet, "XetFileInfo")
        except Exception:
            _available = False
    return _available


def enabled() -> bool:
    return os.environ.get("HUGGER_XET_RESUME", "1") != "0" and available()


def _stream_one(group, rel: str, file_hash: str, expected: int, dest_dir: str | Path) -> None:
    """Stream one Xet file through an existing group into `<rel>.xetpart`,
    resuming from its current size, then rename to the final name. Raises on a
    size mismatch (the partial is left on disk for the next attempt)."""
    from hf_xet import XetFileInfo

    final = Path(dest_dir) / rel
    final.parent.mkdir(parents=True, exist_ok=True)
    if final.exists() and final.stat().st_size == expected:
        return  # already complete

    part = final.with_name(final.name + PART_SUFFIX)
    have = part.stat().st_size if part.exists() else 0
    if have > expected:  # corrupt/over-long partial — start over
        part.unlink()
        have = 0

    written = have
    if have < expected:
        # Flush to the OS every ~1 MB so the parent's progress poll (which reads
        # this file's on-disk size) keeps advancing — an unflushed BufferedWriter
        # makes an actively-transferring file look stalled for long stretches.
        flushed = have
        with open(part, "ab") as f:
            for chunk in group.download_stream(XetFileInfo(file_hash, expected), start=have):
                f.write(chunk)
                written += len(chunk)
                if written - flushed >= 1 << 20:
                    f.flush()
                    flushed = written
    if written != expected:
        raise RuntimeError(f"xet download size mismatch for {rel}: {written} != {expected}")
    part.replace(final)


def _new_group(refresh_route: str, headers: dict):
    from huggingface_hub.utils._xet import get_xet_session, xet_headers_without_auth
    return get_xet_session().new_download_stream_group(
        token_refresh_url=refresh_route,
        token_refresh_headers=headers,
        custom_headers=xet_headers_without_auth(headers),
    )


def download_all(repo_id: str, revision: str, rels: list[str], dest_dir: str | Path,
                 token: str | None) -> list[str]:
    """Download `rels` into `dest_dir`. Xet-backed files all stream through ONE
    shared group, so the CAS connection, token lifecycle, and content-addressed
    chunk cache are reused across files (chunks shared between files — e.g. across
    shards — are fetched once); each file still resumes from its `.xetpart`.
    Returns the rels that are NOT Xet-backed, for the caller to fetch classically."""
    from huggingface_hub import get_hf_file_metadata, hf_hub_url
    from huggingface_hub.utils import build_hf_headers

    headers = build_hf_headers(token=token)
    xet_items: list[tuple[str, str, int]] = []  # (rel, file_hash, size)
    classic: list[str] = []
    refresh_route: str | None = None
    for rel in rels:
        meta = get_hf_file_metadata(hf_hub_url(repo_id, filename=rel, revision=revision), headers=headers)
        xfd = getattr(meta, "xet_file_data", None)
        if xfd is None:
            classic.append(rel)
        else:
            xet_items.append((rel, xfd.file_hash, meta.size))
            refresh_route = xfd.refresh_route  # shared per repo+revision

    if xet_items:
        group = _new_group(refresh_route, headers)
        workers = min(_concurrency(), len(xet_items))
        if workers <= 1:
            for rel, file_hash, size in xet_items:
                _stream_one(group, rel, file_hash, size, dest_dir)
        else:
            # Stream files concurrently through the one group; download_stream
            # releases the GIL for the network/CAS work, so this overlaps real
            # transfer. A failed file leaves its `.xetpart` for the next attempt;
            # we let the others finish, then surface the first error.
            with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
                futs = {ex.submit(_stream_one, group, rel, file_hash, size, dest_dir): rel
                        for rel, file_hash, size in xet_items}
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
    _stream_one(_new_group(xfd.refresh_route, headers), rel, xfd.file_hash, meta.size, dest_dir)
    return True
