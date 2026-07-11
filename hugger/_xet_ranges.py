"""Byte-range bookkeeping for out-of-order `.xetpart` writes.

A `<file>.xetpart` is filled torrent-style: chunks land at their byte offsets in
whatever order the network completes them, so the part is no longer a contiguous
prefix and its st_size can't tell us what's present. A SQLite sidecar
(`<file>.xetpart.db`) records the merged, end-exclusive byte ranges actually
written, letting a retry fetch only the holes and letting progress polls count
real bytes. The sidecar lives next to the part, so model moves (dir copy) and
deletes (rmtree) carry or drop it automatically.

Crash-safety contract with the writer: the data file is flushed+fsynced BEFORE
ranges are committed here, so the DB may under-claim (lost tail is re-fetched)
but never over-claim.
"""
from __future__ import annotations

import bisect
import sqlite3
from pathlib import Path

DB_SUFFIX = ".db"
_VERSION = "1"


def db_path(part: Path) -> Path:
    return part.with_name(part.name + DB_SUFFIX)


def covered_bytes(part: Path) -> int | None:
    """Bytes present in `part` per its sidecar, or None if there is no (usable)
    sidecar — the caller should fall back to st_size (legacy contiguous parts).
    Read-only and safe to call from another process while the writer commits."""
    p = db_path(part)
    if not p.exists():
        return None
    try:
        con = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=0.5)
        try:
            n = con.execute("SELECT COALESCE(SUM(end - start), 0) FROM ranges").fetchone()[0]
            return int(n)
        finally:
            con.close()
    except sqlite3.Error:
        return None


class RangeDB:
    """Owns the sidecar for one part file. Single writer (one worker thread per
    file); ranges are kept merged in memory and mirrored to SQLite on commit()."""

    def __init__(self, part: Path, con: sqlite3.Connection, ranges: list[tuple[int, int]]):
        self.part = part
        self._con = con
        self._starts = [r[0] for r in ranges]  # kept in lockstep with _ranges for bisect
        self._ranges = ranges

    # -- lifecycle -----------------------------------------------------------

    @classmethod
    def open(cls, part: Path, file_hash: str, expected: int) -> "RangeDB":
        """Open/create the sidecar for `part`, wiping stale state:
        * sidecar for a different file_hash/expected size (revision changed —
          same-size content changes used to resume wrongly on st_size alone),
        * sidecar claiming bytes the part can't hold (inconsistent), or a
          sidecar without its part,
        * legacy over-long part (bigger than expected).
        A legacy part WITHOUT a sidecar is a contiguous prefix from the old
        append-only scheme and is seeded as the range [0, st_size)."""
        dbp = db_path(part)
        have = part.stat().st_size if part.exists() else None
        if have is not None and have > expected:  # corrupt/over-long partial
            part.unlink()
            dbp.unlink(missing_ok=True)
            have = None

        ranges: list[tuple[int, int]] | None = None
        if dbp.exists():
            ranges = cls._load(dbp, file_hash, expected, have)
            if ranges is None:  # unreadable / mismatched / inconsistent
                dbp.unlink(missing_ok=True)
                part.unlink(missing_ok=True)
                have = None
        if ranges is None:
            ranges = [(0, have)] if have else []  # legacy contiguous prefix

        con = sqlite3.connect(dbp)
        con.executescript(
            "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);"
            "CREATE TABLE IF NOT EXISTS ranges (start INTEGER NOT NULL, end INTEGER NOT NULL);"
        )
        con.executemany("INSERT OR REPLACE INTO meta VALUES (?, ?)",
                        [("version", _VERSION), ("file_hash", file_hash),
                         ("expected_size", str(expected))])
        db = cls(part, con, ranges)
        db.commit()
        return db

    @staticmethod
    def _load(dbp: Path, file_hash: str, expected: int,
              part_size: int | None) -> list[tuple[int, int]] | None:
        """Validated ranges from an existing sidecar, or None if it must be wiped."""
        try:
            con = sqlite3.connect(dbp)
            try:
                meta = dict(con.execute("SELECT key, value FROM meta"))
                rows = con.execute("SELECT start, end FROM ranges ORDER BY start").fetchall()
            finally:
                con.close()
        except sqlite3.Error:
            return None
        if (meta.get("version") != _VERSION or meta.get("file_hash") != file_hash
                or meta.get("expected_size") != str(expected)):
            return None
        ranges = [(int(a), int(b)) for a, b in rows]
        last = -1
        for a, b in ranges:
            if a < 0 or b <= a or b > expected or a <= last:  # unsorted/overlap/corrupt
                return None
            last = b
        if part_size is None:
            return None if ranges else []  # claims bytes but the part is gone
        if ranges and ranges[-1][1] > part_size:
            return None  # claims bytes past the data actually on disk
        return ranges

    def close(self) -> None:
        self._con.close()

    def finalize(self) -> None:
        """Close and remove the sidecar (the part was renamed to its final name)."""
        self.close()
        db_path(self.part).unlink(missing_ok=True)

    # -- interval bookkeeping --------------------------------------------------

    def add(self, start: int, end: int) -> None:
        """Record [start, end) as written, merging overlapping/adjacent ranges.
        In-memory only until commit()."""
        if end <= start:
            return
        rs = self._ranges
        i = bisect.bisect_left(self._starts, start)
        if i and rs[i - 1][1] >= start:  # previous range touches/overlaps us
            i -= 1
        j = i
        while j < len(rs) and rs[j][0] <= end:  # all ranges touching [start, end)
            start = min(start, rs[j][0])
            end = max(end, rs[j][1])
            j += 1
        rs[i:j] = [(start, end)]
        self._starts[i:j] = [start]

    def covered(self) -> int:
        return sum(b - a for a, b in self._ranges)

    def missing(self, expected: int) -> list[tuple[int, int]]:
        """Holes: the complement of the recorded ranges over [0, expected)."""
        out = []
        pos = 0
        for a, b in self._ranges:
            if a > pos:
                out.append((pos, a))
            pos = max(pos, b)
        if pos < expected:
            out.append((pos, expected))
        return out

    def commit(self) -> None:
        """Mirror the in-memory ranges to disk atomically. The caller must have
        flushed+fsynced the part file first (see module docstring)."""
        with self._con:
            self._con.execute("DELETE FROM ranges")
            self._con.executemany("INSERT INTO ranges VALUES (?, ?)", self._ranges)
