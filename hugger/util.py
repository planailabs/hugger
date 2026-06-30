"""Small filesystem helpers: directory size, free space, writability."""
from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from pathlib import Path

_CHUNK = 1024 * 1024


class HashAborted(Exception):
    """Raised by hash_file when its `stop()` callback returns True mid-file."""


def hash_file(path: Path | str, algo: str, on_bytes=None, stop=None) -> str:
    """Hash a file. `algo`: 'sha256' (LFS) or 'gitblob' (git blob SHA-1, matches
    the Hub's blob_id for non-LFS files: sha1(b"blob <size>\\0" + content)).

    `on_bytes(n)` is called with each chunk's length for byte-level progress;
    `stop()` is polled between chunks and raises HashAborted if it returns True,
    so a long hash can be cancelled (pause/preempt) promptly."""
    if algo == "sha256":
        h = hashlib.sha256()
    else:
        h = hashlib.sha1()
        h.update(f"blob {os.path.getsize(path)}\0".encode())
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(_CHUNK), b""):
            if stop is not None and stop():
                raise HashAborted()
            h.update(chunk)
            if on_bytes is not None:
                on_bytes(len(chunk))
    return h.hexdigest()


def sha256_file(path: Path | str) -> str:
    return hash_file(path, "sha256")


def gitblob_sha1(path: Path | str) -> str:
    return hash_file(path, "gitblob")


def dir_size(path: Path | str) -> int:
    total = 0
    p = Path(path)
    if not p.exists():
        return 0
    for f in p.rglob("*"):
        try:
            if f.is_file() and not f.is_symlink():
                total += f.stat().st_size
        except OSError:
            pass
    return total


def free_space(path: Path | str) -> int:
    """Bytes free on the filesystem holding `path` (walks up to an existing parent)."""
    p = Path(path)
    while not p.exists():
        if p.parent == p:
            break
        p = p.parent
    return shutil.disk_usage(str(p)).free


def check_writable(path: Path | str) -> None:
    """Ensure `path` exists as a dir and is writable, else raise OSError."""
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=str(p), prefix=".hugger-write-test-"):
        pass


def human_size(n: int) -> str:
    f = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if f < 1024 or unit == "TB":
            return f"{f:.0f} {unit}" if unit == "B" else f"{f:.1f} {unit}"
        f /= 1024
    return f"{f:.1f} TB"
