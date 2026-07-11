"""Self-checks for the `.xetpart` ranges sidecar (torrent-style resume). Pure
local, no network:

    python -m tests.test_xet_ranges      (or)     python tests/test_xet_ranges.py
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hugger import _xet_ranges as xr  # noqa: E402
from hugger import metadata  # noqa: E402


def _part(name: str = "m.bin.xetpart") -> Path:
    return Path(tempfile.mkdtemp(prefix="hugger-xr-")) / name


def test_merge_missing_covered():
    part = _part()
    db = xr.RangeDB.open(part, "h1", 100)
    try:
        assert db.missing(100) == [(0, 100)] and db.covered() == 0
        db.add(10, 20)
        db.add(30, 40)
        db.add(20, 30)                      # adjacent on both sides -> one range
        assert db.missing(100) == [(0, 10), (40, 100)]
        db.add(5, 12)                       # overlaps the left edge
        db.add(60, 70)
        assert db.missing(100) == [(0, 5), (40, 60), (70, 100)]
        assert db.covered() == 45
        db.add(0, 100)                      # swallow everything
        assert db.missing(100) == [] and db.covered() == 100
        db.add(50, 60)                      # fully-contained no-op
        assert db.covered() == 100
    finally:
        db.close()


def test_persistence_across_reopen():
    part = _part()
    db = xr.RangeDB.open(part, "h1", 100)
    db.add(0, 45)
    part.write_bytes(b"x" * 45)             # data on disk backs the claim
    db.commit()
    db.close()

    assert xr.covered_bytes(part) == 45     # read-only helper sees committed state
    db = xr.RangeDB.open(part, "h1", 100)
    try:
        assert db.covered() == 45 and db.missing(100) == [(45, 100)]
    finally:
        db.close()


def test_uncommitted_ranges_not_persisted():
    part = _part()
    db = xr.RangeDB.open(part, "h1", 100)
    db.add(0, 45)                           # no commit — like a crash mid-window
    db.close()
    db = xr.RangeDB.open(part, "h1", 100)
    try:
        assert db.covered() == 0            # under-claims; bytes get re-fetched
    finally:
        db.close()


def test_hash_or_size_change_wipes_partial():
    for reopen_args in (("h2", 100), ("h1", 200)):
        part = _part()
        db = xr.RangeDB.open(part, "h1", 100)
        db.add(0, 45)
        part.write_bytes(b"x" * 45)
        db.commit()
        db.close()
        db = xr.RangeDB.open(part, *reopen_args)
        try:
            assert db.covered() == 0 and not part.exists()  # stale data dropped
        finally:
            db.close()


def test_legacy_contiguous_part_is_seeded():
    part = _part()
    part.write_bytes(b"y" * 30)             # old append-only scheme: no sidecar
    db = xr.RangeDB.open(part, "h1", 100)
    try:
        assert db.covered() == 30 and db.missing(100) == [(30, 100)]
    finally:
        db.close()


def test_overlong_legacy_part_is_wiped():
    part = _part()
    part.write_bytes(b"z" * 150)
    db = xr.RangeDB.open(part, "h1", 100)
    try:
        assert not part.exists() and db.covered() == 0
    finally:
        db.close()


def test_overlong_part_with_valid_sidecar_is_wiped():
    """External corruption growing the part past its expected size must not
    survive even when the sidecar itself validates."""
    part = _part()
    db = xr.RangeDB.open(part, "h1", 100)
    db.add(0, 100)
    part.write_bytes(b"x" * 100)
    db.commit()
    db.close()
    part.write_bytes(b"x" * 150)
    db = xr.RangeDB.open(part, "h1", 100)
    try:
        assert db.covered() == 0 and not part.exists()
    finally:
        db.close()


def test_sidecar_overclaiming_part_is_wiped():
    """A sidecar claiming bytes past the data on disk (e.g. data file truncated
    behind our back) must not be trusted."""
    part = _part()
    db = xr.RangeDB.open(part, "h1", 100)
    db.add(0, 50)
    db.commit()                             # claims 50 but the part holds 10
    db.close()
    part.write_bytes(b"a" * 10)
    db = xr.RangeDB.open(part, "h1", 100)
    try:
        assert db.covered() == 0 and not part.exists()
    finally:
        db.close()


def test_finalize_removes_sidecar():
    part = _part()
    db = xr.RangeDB.open(part, "h1", 10)
    db.finalize()
    assert not xr.db_path(part).exists()
    assert xr.covered_bytes(part) is None   # no sidecar -> caller uses st_size


def test_progress_bytes_prefers_sidecar():
    """A sparse part's st_size overstates its content; progress must use the
    sidecar's byte count when one exists, st_size otherwise (legacy)."""
    dest = Path(tempfile.mkdtemp(prefix="hugger-xr-"))
    meta = {"files": [{"path": "big.bin", "size": 100}], "total_size": 100}
    part = dest / "big.bin.xetpart"
    part.write_bytes(b"x" * 60)             # legacy contiguous part, no sidecar
    assert metadata.progress_bytes(dest, meta) == 60   # fallback: st_size
    db = xr.RangeDB.open(part, "h1", 100)   # sidecar exists before writes (real flow)
    with open(part, "r+b") as f:            # 20 more bytes at the tail: st_size = 100
        f.seek(80)
        f.write(b"x" * 20)
    db.add(80, 100)
    db.commit()
    db.close()
    assert metadata.progress_bytes(dest, meta) == 80   # sidecar, not st_size (100)


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\nAll {len(fns)} checks passed.")
