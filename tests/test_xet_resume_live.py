"""Live tests for resumable Xet downloads against the real Hub.

Exercises hugger._xet_download against real Xet-backed files: a full download
matches the classic path byte-for-byte, an interrupted download RESUMES from the
on-disk `.xetpart` (a legacy contiguous partial is migrated into the ranges
sidecar; a holey partial fetches only its missing ranges), an over-long/garbage
partial restarts cleanly, raw byte-ranges are exact on both the ordered and
unordered streams, the flag disables the path, and non-Xet files fall through.
Token refresh (token_refresh_url on the stream group) and range-resume work
together: a fresh group on retry refreshes the token, then fetches only the
holes. Skips automatically when huggingface.co is unreachable or this hf_xet
predates the unordered streaming API.

    python tests/test_xet_resume_live.py
"""
import hashlib
import os
import socket
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("HUGGER_HOME", tempfile.mkdtemp(prefix="hugger-xet-"))

from hugger import _xet_download as xd  # noqa: E402
from hugger import _xet_ranges as xr  # noqa: E402

REPO = "hf-internal-testing/tiny-random-gpt2"
XET_FILE = "pytorch_model.bin"      # ~3.5 MB, Xet-backed
PLAIN_FILE = "config.json"          # not Xet-backed


def _online() -> bool:
    try:
        socket.create_connection(("huggingface.co", 443), timeout=5).close()
        return True
    except OSError:
        return False


ONLINE = _online() and xd.available()


def _skip(name: str) -> None:
    why = "huggingface.co unreachable" if not _online() else "hf_xet lacks streaming API"
    print(f"skip {name} ({why})")


def _sha(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _reference(name: str = XET_FILE) -> Path:
    """The file fetched via the classic high-level path — the ground truth."""
    from huggingface_hub import hf_hub_download
    return Path(hf_hub_download(REPO, filename=name))


def test_flag_disables():
    os.environ["HUGGER_XET_RESUME"] = "0"
    try:
        assert xd.enabled() is False
    finally:
        os.environ.pop("HUGGER_XET_RESUME", None)


def test_full_download_matches_classic():
    if not ONLINE:
        return _skip("test_full_download_matches_classic")
    ref = _reference()
    with tempfile.TemporaryDirectory() as d:
        handled = xd.download_file(REPO, "main", XET_FILE, d, token=None)
        assert handled is True
        out = Path(d) / XET_FILE
        assert out.exists() and out.stat().st_size == ref.stat().st_size
        assert _sha(out) == _sha(ref)
        part = Path(d) / (XET_FILE + xd.PART_SUFFIX)
        assert not part.exists()                  # part renamed away
        assert not xr.db_path(part).exists()      # ranges sidecar cleaned up


def test_resume_from_partial():
    """Seed a real legacy (contiguous, no sidecar) partial `.xetpart`, then
    resume: it is migrated into the ranges sidecar, only the missing tail is
    fetched and the result matches the reference byte-for-byte."""
    if not ONLINE:
        return _skip("test_resume_from_partial")
    ref = _reference()
    full = _sha(ref)
    size = ref.stat().st_size
    with tempfile.TemporaryDirectory() as d:
        part = Path(d) / (XET_FILE + xd.PART_SUFFIX)
        cut = min(1 << 20, size // 2)
        with open(ref, "rb") as f, open(part, "wb") as g:
            g.write(f.read(cut))  # real first ~1 MB already on disk
        handled = xd.download_file(REPO, "main", XET_FILE, d, token=None)
        assert handled is True
        out = Path(d) / XET_FILE
        assert out.stat().st_size == size
        assert _sha(out) == full, "resumed file must match the reference byte-for-byte"
        assert not xr.db_path(part).exists()


def test_resume_from_holey_partial():
    """Seed a partial with a HOLE in the middle (sidecar records two ranges);
    resume fetches only the missing ranges and the result is byte-exact."""
    if not ONLINE:
        return _skip("test_resume_from_holey_partial")
    from huggingface_hub import get_hf_file_metadata, hf_hub_url
    from huggingface_hub.utils import build_hf_headers

    ref = _reference()
    data = ref.read_bytes()
    size = len(data)
    meta = get_hf_file_metadata(hf_hub_url(REPO, filename=XET_FILE),
                                headers=build_hf_headers())
    with tempfile.TemporaryDirectory() as d:
        part = Path(d) / (XET_FILE + xd.PART_SUFFIX)
        a, b, c = size // 8, size // 4, size // 2  # have [0,a) and [b,c); rest missing
        db = xr.RangeDB.open(part, meta.xet_file_data.file_hash, size)
        with open(part, "wb") as f:
            f.write(data[:a])
            f.seek(b)
            f.write(data[b:c])
        db.add(0, a)
        db.add(b, c)
        db.commit()
        db.close()
        assert xd.download_file(REPO, "main", XET_FILE, d, token=None) is True
        out = Path(d) / XET_FILE
        assert out.stat().st_size == size
        assert _sha(out) == _sha(ref), "holey resume must match the reference byte-for-byte"
        assert not part.exists() and not xr.db_path(part).exists()


def test_corrupt_overlong_partial_restarts():
    if not ONLINE:
        return _skip("test_corrupt_overlong_partial_restarts")
    ref = _reference()
    size = ref.stat().st_size
    with tempfile.TemporaryDirectory() as d:
        part = Path(d) / (XET_FILE + xd.PART_SUFFIX)
        part.write_bytes(b"\x00" * (size + 1024))  # longer than the real file -> garbage
        assert xd.download_file(REPO, "main", XET_FILE, d, token=None) is True
        out = Path(d) / XET_FILE
        assert out.stat().st_size == size
        assert _sha(out) == _sha(ref)


def test_range_correctness():
    if not ONLINE:
        return _skip("test_range_correctness")
    from hf_xet import XetFileInfo
    from huggingface_hub import get_hf_file_metadata, hf_hub_url
    from huggingface_hub.utils import build_hf_headers
    from huggingface_hub.utils._xet import get_xet_session, xet_headers_without_auth

    ref = _reference()
    headers = build_hf_headers()
    meta = get_hf_file_metadata(hf_hub_url(REPO, filename=XET_FILE), headers=headers)
    xfd = meta.xet_file_data
    grp = get_xet_session().new_download_stream_group(
        token_refresh_url=xfd.refresh_route, token_refresh_headers=headers,
        custom_headers=xet_headers_without_auth(headers))
    info = XetFileInfo(xfd.file_hash, meta.size)
    start, end = 1000, 1000 + (1 << 18)
    got = b"".join(grp.download_stream(info, start=start, end=end))
    assert len(got) == end - start, f"range returned {len(got)} bytes, want {end - start}"
    assert got == ref.read_bytes()[start:end], "ranged bytes must match the reference slice"

    # Unordered stream: offsets are RELATIVE to the range start (what the
    # torrent-style writer relies on); reassembled they must be byte-exact.
    buf = bytearray(end - start)
    total = 0
    for off, chunk in grp.download_unordered_stream(info, start=start, end=end):
        buf[off:off + len(chunk)] = chunk
        total += len(chunk)
    assert total == end - start, f"unordered range returned {total} bytes, want {end - start}"
    assert bytes(buf) == ref.read_bytes()[start:end], "unordered ranged bytes must match"


def test_non_xet_returns_false():
    if not ONLINE:
        return _skip("test_non_xet_returns_false")
    with tempfile.TemporaryDirectory() as d:
        assert xd.download_file(REPO, "main", PLAIN_FILE, d, token=None) is False
        assert not (Path(d) / PLAIN_FILE).exists()  # caller handles non-Xet files


XET_FILES = ["model.safetensors", "pytorch_model.bin", "tf_model.h5"]  # all Xet-backed


def test_download_all_resumes_one():
    """Several Xet files download concurrently (each its own short-lived group); a
    pre-seeded partial proves resume on this path; non-Xet files are returned."""
    if not ONLINE:
        return _skip("test_download_all_resumes_one")
    with tempfile.TemporaryDirectory() as d:
        refs = {n: _reference(n) for n in XET_FILES}
        first = XET_FILES[0]
        (Path(d) / (first + xd.PART_SUFFIX)).write_bytes(
            refs[first].read_bytes()[: refs[first].stat().st_size // 2])  # real first half
        classic = xd.download_all(REPO, "main", XET_FILES + [PLAIN_FILE], d, token=None)
        assert classic == [PLAIN_FILE], classic
        assert not (Path(d) / PLAIN_FILE).exists()  # non-Xet not handled here
        for n in XET_FILES:
            assert _sha(Path(d) / n) == _sha(refs[n]), n


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\nXet resume checks done ({'online' if ONLINE else 'offline/unsupported — skipped'}).")
