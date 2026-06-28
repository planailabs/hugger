"""Per-model metadata as a JSON file on disk — the source of truth.

Each archived model directory holds a `.hugger.json` describing the repo, the
selected files and their sizes, and the commit sha. The DB is only a cache of
this (rebuildable via store import). Whether a file is *downloaded* is never
stored — it's derived from the filesystem.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

META_NAME = ".hugger.json"
PROGRESS_NAME = ".hugger.progress"


def meta_path(model_dir: Path | str) -> Path:
    return Path(model_dir) / META_NAME


def progress_file(model_dir: Path | str) -> Path:
    return Path(model_dir) / PROGRESS_NAME


def read_progress(model_dir: Path | str, meta: dict) -> int:
    """Live downloaded bytes from the subprocess's tqdm progress file (works for
    both classic and Xet transfers); falls back to scanning the filesystem."""
    try:
        n = int(progress_file(model_dir).read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return progress_bytes(model_dir, meta)
    total = meta.get("total_size", 0) or n
    return max(0, min(total, n))


def build(repo_id: str, revision: str, sha: str, files: list[dict],
          selected: list[str] | None) -> dict:
    """files: [{path, size}] from the Hub. selected: subset of paths, or None=all."""
    sel = set(selected) if selected is not None else {f["path"] for f in files}
    chosen = [f for f in files if f["path"] in sel]
    return {
        "repo_id": repo_id,
        "revision": revision,
        "sha": sha,
        "archived_at": datetime.now(timezone.utc).isoformat(),
        "total_size": sum(f.get("size", 0) or 0 for f in chosen),
        "selected": [f["path"] for f in chosen],
        "files": [
            {"path": f["path"], "size": f.get("size", 0) or 0,
             "lfs": bool(f.get("lfs")), "rhash": f.get("rhash")}
            for f in chosen
        ],
    }


def algo_for(file_meta: dict) -> str:
    """Hash algorithm matching the Hub's hash for this file."""
    return "sha256" if file_meta.get("lfs") else "gitblob"


def write(model_dir: Path | str, meta: dict) -> None:
    p = meta_path(model_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(meta, indent=2), encoding="utf-8")


def read(model_dir: Path | str) -> dict | None:
    p = meta_path(model_dir)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def file_downloaded(model_dir: Path | str, rel: str, expected_size: int | None = None) -> bool:
    """FS-authoritative check: the file exists and (if known) matches its size."""
    f = Path(model_dir) / rel
    if not f.is_file():
        return False
    if expected_size is None:
        return True
    try:
        return f.stat().st_size == expected_size
    except OSError:
        return False


def progress_bytes(model_dir: Path | str, meta: dict) -> int:
    """Live downloaded bytes for a progress bar: completed files plus the bytes
    of any in-flight `*.incomplete` staging files (so a single large file shows
    byte-level progress, not a 0%->100% jump). Capped at total_size."""
    done = state(model_dir, meta)["downloaded_bytes"]
    partial = 0
    cache = Path(model_dir) / ".cache"
    if cache.exists():
        for p in cache.rglob("*.incomplete"):
            try:
                partial += p.stat().st_size
            except OSError:
                pass
    return min(meta.get("total_size", 0) or (done + partial), done + partial)


def state(model_dir: Path | str, meta: dict) -> dict:
    """Compute download progress for `meta` from the filesystem."""
    files = meta.get("files", [])
    n = len(files)
    done = 0
    done_bytes = 0
    for f in files:
        if file_downloaded(model_dir, f["path"], f.get("size")):
            done += 1
            done_bytes += f.get("size", 0) or 0
    return {
        "n_files": n,
        "n_downloaded": done,
        "downloaded_bytes": done_bytes,
        "total_bytes": meta.get("total_size", 0),
        "complete": n > 0 and done == n,
    }
