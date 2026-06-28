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


def meta_path(model_dir: Path | str) -> Path:
    return Path(model_dir) / META_NAME


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
        "files": [{"path": f["path"], "size": f.get("size", 0) or 0} for f in chosen],
    }


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
