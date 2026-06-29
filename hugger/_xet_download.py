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

import os
from pathlib import Path

PART_SUFFIX = ".xetpart"

_available: bool | None = None


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


def download_file(repo_id: str, revision: str, rel: str, dest_dir: str | Path,
                  token: str | None) -> bool:
    """Resume-download one file via Xet. Returns True if handled (Xet file), or
    False if the file isn't Xet-backed (caller should use the classic path).
    Raises on transfer errors (the partial is left on disk for the next attempt)."""
    from hf_xet import XetFileInfo
    from huggingface_hub import get_hf_file_metadata, hf_hub_url
    from huggingface_hub.utils import build_hf_headers
    from huggingface_hub.utils._xet import get_xet_session, xet_headers_without_auth

    headers = build_hf_headers(token=token)
    meta = get_hf_file_metadata(hf_hub_url(repo_id, filename=rel, revision=revision), headers=headers)
    xfd = getattr(meta, "xet_file_data", None)
    if xfd is None:
        return False  # not a Xet file — fall back to the classic download
    expected = meta.size
    final = Path(dest_dir) / rel
    final.parent.mkdir(parents=True, exist_ok=True)
    if final.exists() and final.stat().st_size == expected:
        return True  # already complete

    part = final.with_name(final.name + PART_SUFFIX)
    have = part.stat().st_size if part.exists() else 0
    if have > expected:  # corrupt/over-long partial — start over
        part.unlink()
        have = 0

    group = get_xet_session().new_download_stream_group(
        token_refresh_url=xfd.refresh_route,
        token_refresh_headers=headers,
        custom_headers=xet_headers_without_auth(headers),
    )
    info = XetFileInfo(xfd.file_hash, expected)
    written = have
    if have < expected:
        with open(part, "ab") as f:
            for chunk in group.download_stream(info, start=have):
                f.write(chunk)
                written += len(chunk)
    if written != expected:
        raise RuntimeError(f"xet download size mismatch for {rel}: {written} != {expected}")
    part.replace(final)
    return True
