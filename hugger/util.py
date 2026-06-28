"""Small filesystem helpers: directory size, free space, writability."""
from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path


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
