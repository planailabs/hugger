"""Runnable self-checks for the non-trivial pure logic. No framework needed:

    python -m tests.test_core      (or)     python tests/test_core.py
"""
import os
import tempfile
from pathlib import Path

# Point hugger at a throwaway home BEFORE importing it.
_TMP = tempfile.mkdtemp(prefix="hugger-test-")
os.environ["HUGGER_HOME"] = _TMP
os.environ["HUGGER_ARCHIVE_DIR"] = str(Path(_TMP) / "archives")

from hugger import store, jobs, hub  # noqa: E402
from hugger.app import human_size  # noqa: E402
from hugger import auth  # noqa: E402


def test_store_crud():
    store.run_migrations()
    store.upsert_archive("org/model", "main", "abc12345", "/tmp/x", 2048)
    rows = store.list_archives()
    assert len(rows) == 1 and rows[0]["repo_id"] == "org/model"
    assert rows[0]["update_available"] == 0

    store.set_update_status("org/model", "def67890", True)
    rec = store.get_archive("org/model")
    assert rec["update_available"] == 1 and rec["remote_sha"] == "def67890"

    # upsert again resets the update flag and keeps a single row.
    store.upsert_archive("org/model", "main", "def67890", "/tmp/x", 4096)
    rec = store.get_archive("org/model")
    assert rec["update_available"] == 0 and rec["size_bytes"] == 4096
    assert len(store.list_archives()) == 1

    store.delete_archive("org/model")
    assert store.get_archive("org/model") is None


def test_human_size():
    assert human_size(0) == "0 B"
    assert human_size(512) == "512 B"
    assert human_size(1536) == "1.5 KB"
    assert human_size(5 * 1024 * 1024) == "5.0 MB"


def test_local_path_cross_platform():
    p = hub.local_path("org/model")
    assert p.parts[-2:] == ("org", "model")  # uses OS separator, not literal "/"


def test_dir_size(tmp=None):
    d = Path(_TMP) / "sz"
    d.mkdir(parents=True, exist_ok=True)
    (d / "a.bin").write_bytes(b"x" * 100)
    (d / "b.bin").write_bytes(b"y" * 23)
    assert jobs._dir_size(d) == 123


def test_token_and_csrf():
    assert auth.verify_token(auth.cfg.api_token) is True
    assert auth.verify_token("wrong") is False
    assert auth.verify_token(None) is False
    sess = {}
    tok = auth.csrf_token(sess)
    assert auth.csrf_ok(sess, tok) is True
    assert auth.csrf_ok(sess, "nope") is False


def test_password_roundtrip():
    auth.set_password("hunter2")
    assert auth.verify_password("hunter2") is True
    assert auth.verify_password("wrong") is False


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\nAll {len(fns)} checks passed.")
