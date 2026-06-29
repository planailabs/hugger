"""Live tests for resumable Xet downloads against the real Hub.

Exercises hugger._xet_download against a real Xet-backed file: a full download
matches the classic path byte-for-byte, an interrupted download resumes from the
on-disk `.xetpart`, raw byte-ranges are exact, the flag disables the path, and
non-Xet files fall through. Skips automatically when huggingface.co is
unreachable or this hf_xet predates the streaming API.

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


def _reference() -> Path:
    """The file fetched via the classic high-level path — the ground truth."""
    from huggingface_hub import hf_hub_download
    return Path(hf_hub_download(REPO, filename=XET_FILE))


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
        assert not (Path(d) / (XET_FILE + xd.PART_SUFFIX)).exists()  # part renamed away


def test_resume_from_partial():
    if not ONLINE:
        return _skip("test_resume_from_partial")
    ref = _reference()
    full = _sha(ref)
    size = ref.stat().st_size
    with tempfile.TemporaryDirectory() as d:
        # Seed a partial `.xetpart` with the real first ~1 MB, then resume.
        part = Path(d) / (XET_FILE + xd.PART_SUFFIX)
        cut = min(1 << 20, size // 2)
        with open(ref, "rb") as f, open(part, "wb") as g:
            g.write(f.read(cut))
        handled = xd.download_file(REPO, "main", XET_FILE, d, token=None)
        assert handled is True
        out = Path(d) / XET_FILE
        assert out.stat().st_size == size
        assert _sha(out) == full, "resumed file must match the reference byte-for-byte"


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


def test_non_xet_returns_false():
    if not ONLINE:
        return _skip("test_non_xet_returns_false")
    with tempfile.TemporaryDirectory() as d:
        assert xd.download_file(REPO, "main", PLAIN_FILE, d, token=None) is False
        assert not (Path(d) / PLAIN_FILE).exists()  # caller handles non-Xet files


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\nXet resume checks done ({'online' if ONLINE else 'offline/unsupported — skipped'}).")
