"""Runnable self-checks for the non-trivial pure logic. No framework needed:

    python -m tests.test_core      (or)     python tests/test_core.py
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Point hugger at a throwaway home BEFORE importing it.
_TMP = tempfile.mkdtemp(prefix="hugger-test-")
os.environ["HUGGER_HOME"] = _TMP
os.environ["HUGGER_ARCHIVE_DIR"] = str(Path(_TMP) / "archives")

from hugger import store, jobs, hub, metadata, util  # noqa: E402
from hugger.app import human_size  # noqa: E402
from hugger import auth  # noqa: E402


def test_store_crud():
    store.run_migrations()
    sid = store.ensure_default_store(str(Path(_TMP) / "archives"))
    store.upsert_archive("org/model", "main", "abc12345", "/tmp/x", 2048, sid)
    rows = store.list_archives()
    assert len(rows) == 1 and rows[0]["repo_id"] == "org/model"
    assert rows[0]["update_available"] == 0 and rows[0]["store_id"] == sid

    store.set_update_status("org/model", "def67890", True)
    rec = store.get_archive("org/model")
    assert rec["update_available"] == 1 and rec["remote_sha"] == "def67890"

    # upsert again resets the update flag and keeps a single row.
    store.upsert_archive("org/model", "main", "def67890", "/tmp/x", 4096, sid)
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


def test_store_repo_path_cross_platform():
    p = jobs.store_repo_path("/data", "org/model")
    assert p.parts[-2:] == ("org", "model")  # uses OS separator, not literal "/"


def test_repo_id_traversal_rejected():
    """A repo_id with '..' or an absolute/empty segment must not escape the root."""
    for bad in ("org/../../../etc", "../evil", "/etc/passwd", "org//model", "a/b/.."):
        try:
            jobs.store_repo_path("/data/archives", bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"traversal not rejected: {bad!r}")
    # the well-formed id still resolves inside the root
    p = jobs.store_repo_path("/data/archives", "org/model").resolve()
    assert p.is_relative_to(Path("/data/archives").resolve())


def test_metadata_write_atomic_keeps_old_on_failure():
    """A crash during metadata.write must not truncate the existing .hugger.json."""
    d = Path(_TMP) / "atomicmeta"
    d.mkdir(parents=True, exist_ok=True)
    metadata.write(d, {"repo_id": "org/m", "selected": ["a"], "v": 1})
    assert metadata.read(d)["v"] == 1

    orig = metadata.os.replace
    metadata.os.replace = lambda *a, **k: (_ for _ in ()).throw(OSError("crash"))
    try:
        try:
            metadata.write(d, {"repo_id": "org/m", "selected": ["a", "b"], "v": 2})
        except OSError:
            pass
    finally:
        metadata.os.replace = orig
    # old content (incl. the user's file selection) survived intact
    assert metadata.read(d) == {"repo_id": "org/m", "selected": ["a"], "v": 1}
    # no half-written temp file left behind
    assert not list(d.glob(".hugger.json.tmp*"))


def test_remove_file_traversal_rejected():
    """remove_file must refuse to delete a path outside the model dir."""
    store.run_migrations()
    sid = store.ensure_default_store(str(Path(_TMP) / "archives"))
    mdir = Path(_TMP) / "rmtrav" / "org" / "model"
    mdir.mkdir(parents=True, exist_ok=True)
    outside = Path(_TMP) / "rmtrav" / "secret.txt"
    outside.write_text("keep me")
    store.upsert_archive("org/rmtrav", "main", "sha", str(mdir), 0, sid)
    try:
        jobs.manager.remove_file("org/rmtrav", "../../secret.txt")
    except ValueError:
        pass
    else:
        raise AssertionError("traversal delete not rejected")
    assert outside.exists()  # file outside the model dir survived
    store.delete_archive("org/rmtrav")


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


def test_password_stored_in_db_not_config():
    import json
    from hugger.config import CONFIG_PATH

    auth.set_password("dbpw1234", autogenerated=False)
    assert store.get_setting("password_hash") is not None
    cfg_data = json.loads(CONFIG_PATH.read_text())
    assert "password_hash" not in cfg_data  # pw is in the DB now


def test_autogenerated_flag():
    auth.set_password("temp-generated", autogenerated=True)
    assert auth.must_change_password() is True
    auth.set_password("chosen-one", autogenerated=False)
    assert auth.must_change_password() is False


def test_hf_token_setting():
    hub.set_hf_token(None)
    assert hub.hf_token_source() in ("none", "env")
    hub.set_hf_token("hf_abc123")
    assert hub.current_hf_token() == "hf_abc123"  # DB value wins
    assert hub.hf_token_source() == "ui"
    hub.set_hf_token(None)  # clear -> falls back to env (None in tests)
    assert hub.current_hf_token() == hub.cfg.hf_token
    assert hub.hf_token_source() in ("none", "env")


def test_settings_kv():
    assert store.get_setting("nope") is None
    store.set_setting("k", "v1")
    assert store.get_setting("k") == "v1"
    store.set_setting("k", "v2")  # upsert
    assert store.get_setting("k") == "v2"


def test_jobs_persistence():
    store.save_job({"id": "j1", "repo_id": "org/m", "status": "queued"})
    assert "j1" in [j["id"] for j in store.list_jobs(["queued", "running"])]
    store.save_job({"id": "j1", "repo_id": "org/m", "status": "running", "total_bytes": 100})
    store.save_job({"id": "j1", "repo_id": "org/m", "status": "done", "total_bytes": 100})
    assert "j1" not in [j["id"] for j in store.list_jobs(["queued", "running"])]
    assert "j1" in [j["id"] for j in store.list_jobs(["done"])]


def test_stores_crud():
    a = store.ensure_default_store(str(Path(_TMP) / "archives"))
    b = store.add_store("cold", str(Path(_TMP) / "cold"))
    names = {s["name"] for s in store.list_stores()}
    assert {"default", "cold"} <= names
    assert store.get_default_store()["id"] == a
    store.set_default_store(b)
    assert store.get_default_store()["id"] == b
    store.set_default_store(a)  # restore
    # empty, non-default store can be deleted
    store.delete_store(b)
    assert store.get_store(b) is None


def test_store_path_conflict_rejected():
    store.ensure_default_store(str(Path(_TMP) / "archives"))
    p = str(Path(_TMP) / "shared-store")
    store.add_store("s-a", p)
    for bad in (p, str(Path(p) / "nested"), str(Path(p).parent)):
        raised = False
        try:
            store.add_store("dup", bad)
        except ValueError:
            raised = True
        assert raised, f"expected conflict for {bad}"
    # clean up
    sid = next(s["id"] for s in store.list_stores() if s["name"] == "s-a")
    store.delete_store(sid)


def _make_archive(store_id, repo_id, files):
    st = store.get_store(store_id)
    p = jobs.store_repo_path(st["path"], repo_id)
    p.mkdir(parents=True, exist_ok=True)
    for name, data in files.items():
        (p / name).write_bytes(data)
    size = jobs._dir_size(p)
    store.upsert_archive(repo_id, "main", "sha-move", str(p), size, store_id)
    return p


def test_move_job_across_stores():
    import time
    a = store.ensure_default_store(str(Path(_TMP) / "archives"))
    b = store.add_store("dest", str(Path(_TMP) / "dest"))
    src = _make_archive(a, "org/mover", {"config.json": b"{}", "w.bin": b"x" * 2048})

    job = jobs.manager.start_move("org/mover", b)
    for _ in range(100):
        if jobs.manager.get(job.id).status in ("done", "error"):
            break
        time.sleep(0.05)
    j = jobs.manager.get(job.id)
    assert j.status == "done", j.error
    rec = store.get_archive("org/mover")
    assert rec["store_id"] == b
    dest = jobs.store_repo_path(store.get_store(b)["path"], "org/mover")
    assert (dest / "w.bin").stat().st_size == 2048
    assert not src.exists()  # source copy removed after the move
    store.delete_archive("org/mover")
    store.delete_store(b)


def test_metadata_build_and_state():
    files = [{"path": "config.json", "size": 2}, {"path": "w.bin", "size": 100}]
    meta = metadata.build("o/m", "main", "sha", files, ["config.json"])
    assert meta["total_size"] == 2 and [f["path"] for f in meta["files"]] == ["config.json"]

    d = Path(_TMP) / "metatest"
    metadata.write(d, metadata.build("o/m", "main", "sha", files, None))
    (d / "config.json").write_bytes(b"{}")  # size 2; w.bin missing
    st = metadata.state(d, metadata.read(d))
    assert st["n_files"] == 2 and st["n_downloaded"] == 1 and st["complete"] is False
    assert metadata.file_downloaded(d, "config.json", 2)
    assert not metadata.file_downloaded(d, "w.bin", 100)


def test_progress_bytes_counts_incomplete():
    d = Path(_TMP) / "prog"
    meta = metadata.build("o/p", "main", "s", [{"path": "big.bin", "size": 1000}], None)
    metadata.write(d, meta)
    assert metadata.progress_bytes(d, meta) == 0
    # in-flight staging file contributes its partial bytes
    inc = d / ".cache" / "huggingface" / "download"
    inc.mkdir(parents=True, exist_ok=True)
    (inc / "big.bin.abc123.incomplete").write_bytes(b"x" * 400)
    assert metadata.progress_bytes(d, meta) == 400
    # once final file exists, total is capped (no double count during the move)
    (d / "big.bin").write_bytes(b"y" * 1000)
    assert metadata.progress_bytes(d, meta) == 1000


def test_read_progress_from_file():
    d = Path(_TMP) / "rp"
    meta = metadata.build("o/r", "main", "s",
                          [{"path": "a.bin", "size": 400}, {"path": "big.bin", "size": 600}], None)
    metadata.write(d, meta)
    assert metadata.read_progress(d, meta) == 0  # no file -> fs fallback
    # hf reports only the missing portion (n); base (already-present) is added in.
    metadata.progress_file(d).write_text("100 600")
    assert metadata.read_progress(d, meta, base=400) == 500  # 400 done + 100 this run
    metadata.progress_file(d).write_text("600 600")
    assert metadata.read_progress(d, meta, base=400) == 1000  # complete
    metadata.progress_file(d).write_text("9999 600")  # capped at total
    assert metadata.read_progress(d, meta, base=400) == 1000


def test_job_history_and_clear():
    store.save_job({"id": "h1", "repo_id": "o/m", "status": "done"})
    store.save_job({"id": "h2", "repo_id": "o/m", "status": "error", "error": "boom"})
    ids = [j["id"] for j in store.recent_jobs()]
    assert "h1" in ids and "h2" in ids
    assert store.prune_jobs(30) == 0  # both are recent
    store.delete_finished_jobs()
    assert "h1" not in [j["id"] for j in store.recent_jobs()]


def test_shutdown_marks_running_queued():
    a = store.get_default_store()["id"]
    jobs.manager._jobs["sd1"] = jobs.Job(id="sd1", repo_id="o/m", type="download", status="running", store_id=a)
    try:
        jobs.manager.shutdown()
        assert jobs.manager.get("sd1").status == "queued"
        row = next(r for r in store.recent_jobs() if r["id"] == "sd1")
        assert row["status"] == "queued"
    finally:
        jobs.manager._shutting_down = False
        jobs.manager._jobs.pop("sd1", None)


def test_paused_job_progress_seeded_from_disk():
    """A job loaded from the DB (done_bytes isn't persisted) must show real
    on-disk progress, not 0 — the bug paused jobs hit on restart."""
    a = store.get_default_store()["id"]
    repo = "org/paused-progress"
    mdir = jobs.store_repo_path(store.get_store(a)["path"], repo)
    metadata.write(mdir, metadata.build(
        repo, "main", "s",
        [{"path": "a.bin", "size": 100}, {"path": "b.bin", "size": 50}],
        ["a.bin", "b.bin"]))
    (mdir / "a.bin").write_bytes(b"x" * 100)  # one file fully downloaded
    j = jobs.Job(id="pp1", repo_id=repo, revision="main", type="download",
                 total_bytes=150, store_id=a, status="paused")
    assert jobs.manager._on_disk_bytes(j) == 100
    j.done_bytes = jobs.manager._on_disk_bytes(j)
    assert j.percent == 66  # 100 / 150, not 0


def test_retry_error_job():
    a = store.get_default_store()["id"]
    repo = "org/retry"
    mdir = jobs.store_repo_path(store.get_store(a)["path"], repo)
    metadata.write(mdir, metadata.build(repo, "main", "s", [{"path": "a", "size": 1}], ["a"]))
    store.upsert_archive(repo, "main", "s", str(mdir), 0, a)
    store.save_job({"id": "e1", "repo_id": repo, "revision": "main", "type": "download",
                    "status": "error", "store_id": a})
    started = {}
    osd = jobs.manager.start_download
    jobs.manager.start_download = lambda r, rev="main", store_id=None, selected=None: (
        started.update(repo=r, store=store_id, sel=selected) or jobs.Job(id="new", repo_id=r))
    try:
        jobs.manager.retry("e1")  # row only in the DB
        assert started == {"repo": repo, "store": a, "sel": ["a"]}
        # the old errored row is marked retried, pointing at the new job id
        row = next(r for r in store.recent_jobs() if r["id"] == "e1")
        assert row["status"] == "retried" and row["retried_by"] == "new"
    finally:
        jobs.manager.start_download = osd
        store.delete_archive_and_hashes(repo)
        store.delete_finished_jobs()


def test_util_writable_and_free():
    d = Path(_TMP) / "wtest"
    util.check_writable(d)  # creates + verifies, no raise
    assert util.free_space(d) > 0


def test_disk_space_check_blocks_download():
    a = store.ensure_default_store(str(Path(_TMP) / "archives"))
    of, ofree = jobs.hub.repo_files, jobs.util.free_space
    jobs.hub.repo_files = lambda repo, rev="main": {"sha": "s", "files": [{"path": "big.bin", "size": 10**9}]}
    jobs.util.free_space = lambda p: 1000
    try:
        raised = False
        try:
            jobs.manager.start_download("org/toobig", store_id=a)
        except jobs.InsufficientSpace:
            raised = True
        assert raised, "expected InsufficientSpace"
    finally:
        jobs.hub.repo_files, jobs.util.free_space = of, ofree


def test_start_download_dedups_concurrent():
    """A second start_download for the same repo returns the in-flight job, not a
    duplicate worker writing the same dest."""
    a = store.ensure_default_store(str(Path(_TMP) / "archives"))
    of, ofree, osp = jobs.hub.repo_files, jobs.util.free_space, jobs.manager._spawn
    jobs.hub.repo_files = lambda repo, rev="main": {"sha": "s", "files": [{"path": "f.bin", "size": 10}]}
    jobs.util.free_space = lambda p: 10 ** 9
    jobs.manager._spawn = lambda job: None  # don't launch a real subprocess
    j1 = None
    try:
        j1 = jobs.manager.start_download("org/dedup", store_id=a)
        j2 = jobs.manager.start_download("org/dedup", store_id=a)
        assert j1.id == j2.id  # same job returned, not a second one
        actives = [j for j in jobs.manager._jobs.values()
                   if j.repo_id == "org/dedup" and j.type == "download"]
        assert len(actives) == 1
    finally:
        jobs.hub.repo_files, jobs.util.free_space, jobs.manager._spawn = of, ofree, osp
        if j1:
            jobs.manager._jobs.pop(j1.id, None)
        store.delete_archive_and_hashes("org/dedup")


def test_pending_bytes_accounting():
    a = store.get_default_store()["id"]
    j = jobs.Job(id="p1", repo_id="o/m", store_id=a, status="running", total_bytes=500, done_bytes=200)
    jobs.manager._jobs["p1"] = j
    try:
        contribution = jobs.manager.pending_bytes(a) - jobs.manager.pending_bytes(a, exclude="p1")
        assert contribution == 300
    finally:
        jobs.manager._jobs.pop("p1", None)


def test_import_store_and_file_status():
    b = store.add_store("imp", str(Path(_TMP) / "imp"))
    mdir = jobs.store_repo_path(store.get_store(b)["path"], "org/imported")
    files = [{"path": "config.json", "size": 2}, {"path": "w.bin", "size": 4}]
    metadata.write(mdir, metadata.build("org/imported", "main", "shaimp", files, None))
    (mdir / "config.json").write_bytes(b"{}")  # only one of two files present
    assert store.get_archive("org/imported") is None
    assert jobs.import_store(b) >= 1
    rec = store.get_archive("org/imported")
    assert rec and rec["store_id"] == b and rec["n_downloaded"] == 1 and rec["complete"] == 0
    assert jobs.file_status("org/imported", "config.json")["downloaded"] is True
    assert jobs.file_status("org/imported", "w.bin")["downloaded"] is False
    store.delete_archive("org/imported")
    store.delete_store(b)


def test_verify_and_hash_cache():
    a = store.ensure_default_store(str(Path(_TMP) / "archives"))
    repo = "org/verify"
    mdir = jobs.store_repo_path(store.get_store(a)["path"], repo)
    mdir.mkdir(parents=True, exist_ok=True)
    (mdir / "config.json").write_bytes(b"hello")
    (mdir / "keep.bin").write_bytes(b"abc")
    files = [
        {"path": "config.json", "size": 5, "lfs": False, "rhash": util.gitblob_sha1(mdir / "config.json")},
        {"path": "keep.bin", "size": 3, "lfs": False, "rhash": util.gitblob_sha1(mdir / "keep.bin")},
    ]
    metadata.write(mdir, metadata.build(repo, "main", "sha", files, None))
    store.upsert_archive(repo, "main", "sha", str(mdir), 8, a, total_bytes=8, n_files=2, n_downloaded=2)
    # remote: config.json changed, keep.bin same, new.bin missing locally
    new = [
        {"path": "config.json", "size": 5, "lfs": False, "rhash": "DIFFERENT"},
        {"path": "keep.bin", "size": 3, "lfs": False, "rhash": util.gitblob_sha1(mdir / "keep.bin")},
        {"path": "new.bin", "size": 10, "lfs": False, "rhash": "x"},
    ]
    orig = jobs.hub.repo_files
    jobs.hub.repo_files = lambda r, rev="main": {"sha": "newsha", "files": new}
    try:
        v = jobs.manager.verify(repo)
        statuses = {f["path"]: f["status"] for f in v["files"]}
        assert statuses == {"config.json": "changed", "keep.bin": "unchanged", "new.bin": "missing"}
        assert set(v["changed"]) == {"config.json"} and v["missing"] == ["new.bin"]
        assert v["all_present"] is False
        assert store.get_file_hash(repo, "keep.bin") is not None  # hash cached during verify
    finally:
        jobs.hub.repo_files = orig
        store.delete_archive_and_hashes(repo)


def test_start_move_busy_when_running_download():
    a = store.get_default_store()["id"]
    b = store.add_store("busy", str(Path(_TMP) / "busy"))
    store.upsert_archive("org/busy", "main", "s", str(Path(_TMP) / "x"), 1, a)
    j = jobs.Job(id="dl1", repo_id="org/busy", type="download", status="running", store_id=a)
    jobs.manager._jobs["dl1"] = j
    try:
        raised = False
        try:
            jobs.manager.start_move("org/busy", b)
        except jobs.Busy:
            raised = True
        assert raised
    finally:
        jobs.manager._jobs.pop("dl1", None)
        store.delete_archive_and_hashes("org/busy")
        store.delete_store(b)


def test_update_selected_while_paused():
    a = store.get_default_store()["id"]
    repo = "org/sel"
    mdir = jobs.store_repo_path(store.get_store(a)["path"], repo)
    metadata.write(mdir, metadata.build(repo, "main", "s", [{"path": "a", "size": 1}, {"path": "b", "size": 2}], ["a"]))
    store.upsert_archive(repo, "main", "s", str(mdir), 0, a, total_bytes=1, n_files=1, complete=False)
    j = jobs.Job(id="pj", repo_id=repo, type="download", status="paused", store_id=a, total_bytes=1)
    jobs.manager._jobs["pj"] = j
    orig = jobs.hub.repo_files
    jobs.hub.repo_files = lambda r, rev="main": {"sha": "s", "files": [{"path": "a", "size": 1}, {"path": "b", "size": 2}]}
    try:
        jobs.manager.update_selected("pj", ["a", "b"])
        meta = metadata.read(mdir)
        assert set(meta["selected"]) == {"a", "b"} and meta["total_size"] == 3
        assert jobs.manager.get("pj").total_bytes == 3
    finally:
        jobs.hub.repo_files = orig
        jobs.manager._jobs.pop("pj", None)
        store.delete_archive_and_hashes(repo)


def test_remove_file_updates_cache():
    a = store.get_default_store()["id"]
    repo = "org/rm"
    mdir = jobs.store_repo_path(store.get_store(a)["path"], repo)
    metadata.write(mdir, metadata.build(repo, "main", "s", [{"path": "f.bin", "size": 3}], None))
    (mdir / "f.bin").write_bytes(b"abc")
    store.set_file_hash(repo, "f.bin", 3, 123.0, "gitblob", "deadbeef")
    store.upsert_archive(repo, "main", "s", str(mdir), 3, a, total_bytes=3, n_files=1, n_downloaded=1)
    jobs.manager.remove_file(repo, "f.bin")
    assert not (mdir / "f.bin").exists()
    assert store.get_file_hash(repo, "f.bin") is None
    rec = store.get_archive(repo)
    assert rec["n_downloaded"] == 0 and rec["complete"] == 0
    store.delete_archive_and_hashes(repo)


def test_job_eta_and_as_dict_rate():
    j = jobs.Job(id="eta1", repo_id="o/m", status="running", total_bytes=1000, done_bytes=200)
    j.rate = 100.0
    assert j.eta == 8  # (1000-200)/100
    d = j.as_dict()
    assert d["rate"] == 100 and d["eta"] == 8 and d["stalls"] == 0
    # rate/eta only meaningful while running
    j.status = "paused"
    assert j.eta is None and j.as_dict()["rate"] == 0


def _scheduler_fixture():
    """A JobManager whose workers don't actually spawn — so scheduling decisions
    (which job runs vs. waits) can be asserted deterministically."""
    a = store.get_default_store()["id"]
    mgr = jobs.JobManager()
    mgr._spawn = lambda job: None  # claim slots without launching real workers
    return mgr, a


def test_scheduler_serializes_at_max_active_1():
    mgr, a = _scheduler_fixture()
    old = jobs.MAX_ACTIVE
    jobs.MAX_ACTIVE = 1
    try:
        j1 = jobs.Job(id="sc1", repo_id="o/a", store_id=a, total_bytes=100)
        j2 = jobs.Job(id="sc2", repo_id="o/b", store_id=a, total_bytes=100)
        mgr._register(j1); mgr._register(j2)
        assert j1.status == "running" and j2.status == "queued"  # one at a time
        # pausing a queued job just marks it; resume returns it to the queue
        mgr.pause("sc2"); assert j2.status == "paused"
        mgr.resume("sc2"); assert j2.status == "queued"  # slot still held by j1
    finally:
        jobs.MAX_ACTIVE = old
        store.delete_finished_jobs()


def test_run_now_preempts_running_job():
    mgr, a = _scheduler_fixture()
    old = jobs.MAX_ACTIVE
    jobs.MAX_ACTIVE = 1
    try:
        j1 = jobs.Job(id="rn1", repo_id="o/a", store_id=a, total_bytes=100, done_bytes=90)
        j2 = jobs.Job(id="rn2", repo_id="o/b", store_id=a, total_bytes=100)
        mgr._register(j1); mgr._register(j2)
        assert j1.status == "running" and j2.status == "queued"
        mgr.run_now("rn2")
        assert mgr._order[0] == "rn2"        # jumped the queue
        assert j1._preempt.is_set()          # running job told to step aside
        # emulate the preempted worker re-queuing itself + freeing its slot
        j1.status = "queued"; mgr._schedule()
        assert j2.status == "running" and j1.status == "queued"
    finally:
        jobs.MAX_ACTIVE = old
        store.delete_finished_jobs()


def _seed_verifiable(repo: str, good: bool = True):
    """A model dir with two files + metadata whose rhash matches (or not)."""
    a = store.get_default_store()["id"]
    mdir = jobs.store_repo_path(store.get_store(a)["path"], repo)
    (mdir).mkdir(parents=True, exist_ok=True)
    (mdir / "a.bin").write_bytes(b"alpha")
    (mdir / "b.bin").write_bytes(b"bravo")
    rh = lambda n: util.gitblob_sha1(mdir / n)
    files = [{"path": "a.bin", "size": 5, "lfs": False, "rhash": rh("a.bin")},
             {"path": "b.bin", "size": 5, "lfs": False, "rhash": "deadbeef" if not good else rh("b.bin")}]
    meta = metadata.build(repo, "main", "s", files, None)
    metadata.write(mdir, meta)
    store.upsert_archive(repo, "main", "s", str(mdir), 10, a, total_bytes=10,
                         n_files=2, n_downloaded=2, complete=1)
    return a, mdir


def test_verify_job_passes_clean_model():
    repo = "org/verify-ok"
    a, mdir = _seed_verifiable(repo, good=True)
    job = jobs.Job(id="vf-ok", repo_id=repo, type="verify", store_id=a, total_bytes=10)
    jobs.manager._jobs[job.id] = job
    try:
        jobs.manager._run_verify(job)
        assert job.status == "done", job.error
        assert job.done_bytes == 10
        assert not metadata.verify_file(mdir).exists()  # progress file cleaned up
    finally:
        jobs.manager._jobs.pop(job.id, None)
        store.delete_archive_and_hashes(repo)


def test_verify_job_flags_corrupt_file():
    repo = "org/verify-bad"
    a, mdir = _seed_verifiable(repo, good=False)
    job = jobs.Job(id="vf-bad", repo_id=repo, type="verify", store_id=a, total_bytes=10)
    jobs.manager._jobs[job.id] = job
    try:
        jobs.manager._run_verify(job)
        assert job.status == "error" and "b.bin" in (job.error or "")
    finally:
        jobs.manager._jobs.pop(job.id, None)
        store.delete_archive_and_hashes(repo)


def test_verify_resumes_skipping_done_files():
    """A pre-existing `.hugger.verify` marks a.bin done; the verify must skip it
    (so even a now-wrong a.bin passes) and only hash the rest."""
    repo = "org/verify-resume"
    a, mdir = _seed_verifiable(repo, good=True)
    metadata.write_verify(mdir, {"ok": ["a.bin"], "bad": [], "done_bytes": 5})
    (mdir / "a.bin").write_bytes(b"XXXXX")  # corrupt it — but it's already 'ok', skipped
    job = jobs.Job(id="vf-res", repo_id=repo, type="verify", store_id=a, total_bytes=10)
    jobs.manager._jobs[job.id] = job
    try:
        jobs.manager._run_verify(job)
        assert job.status == "done", job.error  # a.bin skipped, b.bin good
    finally:
        jobs.manager._jobs.pop(job.id, None)
        store.delete_archive_and_hashes(repo)


def test_verify_auto_repair_redownloads_bad():
    """A verify with auto=True deletes a failed file and re-triggers the download,
    marking it attempted so a still-bad file doesn't re-download forever."""
    repo = "org/verify-auto"
    a, mdir = _seed_verifiable(repo, good=False)  # b.bin's rhash is wrong
    started = {}
    osd = jobs.manager.start_download
    jobs.manager.start_download = lambda r, rev="main", store_id=None, selected=None: (
        started.update(repo=r, selected=selected) or jobs.Job(id="redl", repo_id=r))
    job = jobs.Job(id="va", repo_id=repo, type="verify", store_id=a, total_bytes=10, auto=True)
    jobs.manager._jobs[job.id] = job
    try:
        jobs.manager._run_verify(job)
        assert started.get("repo") == repo            # re-download triggered
        assert not (mdir / "b.bin").exists()          # bad file deleted for re-fetch
        assert (mdir / "a.bin").exists()              # good file untouched
        rec = metadata.read_bad(mdir)
        assert "b.bin" in rec["attempted"]            # won't auto-loop
    finally:
        jobs.manager.start_download = osd
        jobs.manager._jobs.pop(job.id, None)
        store.delete_archive_and_hashes(repo)


def test_verify_auto_repair_failure_surfaces_error():
    """If the auto-repair re-download can't be queued (e.g. disk full), the job
    error must say so instead of leaving a misleading 're-downloading' message."""
    import contextlib
    import io
    repo = "org/verify-repair-fail"
    a, mdir = _seed_verifiable(repo, good=False)  # b.bin is corrupt
    orig = jobs.manager.redownload_bad
    jobs.manager.redownload_bad = lambda *a, **k: (_ for _ in ()).throw(
        jobs.InsufficientSpace("no space"))
    job = jobs.Job(id="vrf", repo_id=repo, type="verify", store_id=a, total_bytes=10, auto=True)
    jobs.manager._jobs[job.id] = job
    try:
        with contextlib.redirect_stderr(io.StringIO()):  # swallow the logged traceback
            jobs.manager._run_verify(job)
        assert job.status == "error"
        assert "auto-repair could not start" in (job.error or "")
    finally:
        jobs.manager.redownload_bad = orig
        jobs.manager._jobs.pop(job.id, None)
        store.delete_archive_and_hashes(repo)


def test_redownload_bad_manual_clears_and_resumes():
    repo = "org/redl-manual"
    a, mdir = _seed_verifiable(repo, good=True)
    metadata.write_bad(mdir, ["a.bin"], ["a.bin"])   # recorded bad (already attempted)
    started = {}
    osd = jobs.manager.start_download
    jobs.manager.start_download = lambda r, rev="main", store_id=None, selected=None: (
        started.update(repo=r, selected=selected) or jobs.Job(id="m", repo_id=r))
    try:
        jobs.manager.redownload_bad(repo)             # manual: only=None, no mark_attempted
        assert started.get("repo") == repo
        assert not (mdir / "a.bin").exists()          # deleted -> will be re-fetched
        assert metadata.read_bad(mdir) == {}          # manual clears the bad record
    finally:
        jobs.manager.start_download = osd
        store.delete_archive_and_hashes(repo)


def test_verify_runs_in_separate_lane():
    """A verify job and a transfer job run at once (independent lanes)."""
    mgr, a = _scheduler_fixture()
    om, ov = jobs.MAX_ACTIVE, jobs.MAX_VERIFY
    jobs.MAX_ACTIVE = jobs.MAX_VERIFY = 1
    try:
        dl = jobs.Job(id="ln-dl", repo_id="o/a", type="download", store_id=a, total_bytes=10)
        vf = jobs.Job(id="ln-vf", repo_id="o/b", type="verify", store_id=a, total_bytes=10)
        mgr._register(dl); mgr._register(vf)
        assert dl.status == "running" and vf.status == "running"  # both lanes active
    finally:
        jobs.MAX_ACTIVE, jobs.MAX_VERIFY = om, ov
        store.delete_finished_jobs()


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\nAll {len(fns)} checks passed.")
