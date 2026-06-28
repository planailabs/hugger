"""Live tests against the real HuggingFace Hub (search + tiny-model download).

These hit the network; they skip automatically when huggingface.co is
unreachable so offline runs stay green. The NixOS VM tests use the fake hub
instead — this file is the only thing that touches the real Hub.

    python tests/test_hub_live.py
"""
import os
import socket
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="hugger-live-")
os.environ["HUGGER_HOME"] = _TMP
os.environ["HUGGER_ARCHIVE_DIR"] = str(Path(_TMP) / "archives")

from hugger import hub, store, jobs  # noqa: E402

# A very small public model used for a real end-to-end download.
TINY = "56m/Dumb"


def _online() -> bool:
    try:
        socket.create_connection(("huggingface.co", 443), timeout=5).close()
        return True
    except OSError:
        return False


ONLINE = _online()


def _skip(name: str) -> None:
    print(f"skip {name} (huggingface.co unreachable)")


def test_live_search():
    if not ONLINE:
        return _skip("test_live_search")
    res = hub.search_models("bert", limit=5)
    assert res, "expected search results from the Hub"
    assert all("id" in r for r in res)
    assert any("bert" in r["id"].lower() for r in res)


def test_live_repo_meta_and_update():
    if not ONLINE:
        return _skip("test_live_repo_meta_and_update")
    meta = hub.repo_meta(TINY, "main")
    assert meta["sha"], "expected a commit sha"
    assert meta["total_size"] > 0
    # remote_sha (used for update detection) agrees with model_info's sha
    assert hub.remote_sha(TINY, "main") == meta["sha"]


def test_live_download():
    if not ONLINE:
        return _skip("test_live_download")
    dest = Path(_TMP) / "dl"
    hub.download(TINY, "main", dest)
    assert (dest / "config.json").exists(), "config.json should be downloaded"
    # something with actual weight bytes landed too
    files = [p.name for p in dest.rglob("*") if p.is_file() and not p.name.startswith(".")]
    assert len(files) >= 2, f"expected multiple files, got {files}"


def test_live_selective_download():
    if not ONLINE:
        return _skip("test_live_selective_download")
    import time
    from pathlib import Path
    sid = store.ensure_default_store(str(Path(_TMP) / "archives"))
    job = jobs.manager.start_download(TINY, store_id=sid, selected=["config.json"])
    for _ in range(180):
        if jobs.manager.get(job.id).status in ("done", "error"):
            break
        time.sleep(0.5)
    j = jobs.manager.get(job.id)
    assert j.status == "done", j.error
    dest = jobs.store_repo_path(store.get_store(sid)["path"], TINY)
    assert (dest / "config.json").exists()
    assert not (dest / "model.safetensors").exists()  # not selected
    assert jobs.file_status(TINY, "config.json")["downloaded"] is True
    assert jobs.file_status(TINY, "model.safetensors")["downloaded"] is False


def test_live_verify_matches_hub_hash():
    if not ONLINE:
        return _skip("test_live_verify_matches_hub_hash")
    import time
    from pathlib import Path
    sid = store.ensure_default_store(str(Path(_TMP) / "archives"))
    job = jobs.manager.start_download(TINY, store_id=sid, selected=["config.json"])
    for _ in range(180):
        if jobs.manager.get(job.id).status in ("done", "error"):
            break
        time.sleep(0.5)
    assert jobs.manager.get(job.id).status == "done"
    v = jobs.manager.verify(TINY)
    statuses = {f["path"]: f["status"] for f in v["files"]}
    # our git-blob sha1 of the downloaded config.json must match the Hub's blob_id
    assert statuses["config.json"] == "unchanged", statuses
    assert "model.safetensors" in v["missing"]  # not selected -> missing


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\nLive checks done ({'online' if ONLINE else 'offline — skipped'}).")
