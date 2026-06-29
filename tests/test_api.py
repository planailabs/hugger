"""Integration tests for the HTTP layer — auth, sessions, CSRF, API, headers.

Mirrors the manual smoke checks. No network: download jobs are stubbed so no real
HuggingFace calls happen. Run standalone:

    python tests/test_api.py
"""
import os
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_TMP = tempfile.mkdtemp(prefix="hugger-apitest-")
os.environ["HUGGER_HOME"] = _TMP
os.environ["HUGGER_ARCHIVE_DIR"] = os.path.join(_TMP, "archives")

from starlette.testclient import TestClient  # noqa: E402

from hugger import app as appmod  # noqa: E402
from hugger import auth, store, jobs, hub  # noqa: E402

PASSWORD = "test1234"
auth.set_password(PASSWORD)
TOKEN = auth.cfg.api_token
STORE = store.ensure_default_store(os.path.join(_TMP, "archives"))

# Stub out real downloads: api/ui archive should not hit the network.
class _FakeJob:
    def __init__(self, repo_id):
        self.id = "fake" + repo_id.replace("/", "_")[:8]


def _setup_client():
    auth._attempts.clear()
    return TestClient(appmod.app, follow_redirects=False)


def _login(cli):
    r = cli.post("/login", data={"password": PASSWORD})
    assert r.status_code in (302, 303), r.status_code
    assert r.headers["location"] == "/"


def _ds_post(cli, url, csrf=None):
    """Simulate a Datastar @post: the runtime sends a Datastar-Request header and
    the signals as a JSON body."""
    headers = {"Datastar-Request": "true"}
    body = {"csrf": csrf} if csrf is not None else {}
    return cli.post(url, json=body, headers=headers)


def _csrf(cli):
    """Pull the per-session CSRF token out of the rendered dashboard."""
    html = cli.get("/").text
    m = re.search(r"X-CSRF-Token[^A-Za-z0-9_\-]+([A-Za-z0-9_\-]{20,})", html)
    assert m, "CSRF token not found in page"
    return m.group(1)


# --- API auth ------------------------------------------------------------

def test_api_ping_requires_token():
    cli = _setup_client()
    assert cli.get("/api/ping").status_code == 401


def test_api_ping_with_token():
    cli = _setup_client()
    r = cli.get("/api/ping", headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and "version" in body


def test_api_ping_custom_header_token():
    cli = _setup_client()
    r = cli.get("/api/ping", headers={"X-Hugger-Token": TOKEN})
    assert r.status_code == 200


def test_api_archives_listing():
    cli = _setup_client()
    r = cli.get("/api/archives", headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 200 and "archives" in r.json()


def test_api_archive_validation_and_start():
    cli = _setup_client()
    orig = jobs.manager.start_download
    jobs.manager.start_download = lambda repo_id, revision="main", store_id=None, selected=None: _FakeJob(repo_id)
    try:
        h = {"Authorization": f"Bearer {TOKEN}"}
        assert cli.post("/api/archive", json={}, headers=h).status_code == 400
        r = cli.post("/api/archive", json={"repo_id": "org/m"}, headers=h)
        assert r.status_code == 200 and r.json()["repo_id"] == "org/m"
        assert r.json()["job_id"].startswith("fake")
    finally:
        jobs.manager.start_download = orig


def test_api_status_not_found():
    cli = _setup_client()
    r = cli.get("/api/status/nope", headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 404


def test_api_archive_status_and_delete():
    cli = _setup_client()
    h = {"Authorization": f"Bearer {TOKEN}"}
    # unknown repo -> archived: false
    r = cli.get("/api/archive/org/unknown-model", headers=h)
    assert r.status_code == 200 and r.json()["archived"] is False
    # seed one, then it reports archived
    store.upsert_archive("org/known-model", "main", "sha1", os.path.join(_TMP, "ka"), 99, STORE)
    r = cli.get("/api/archive/org/known-model", headers=h)
    body = r.json()
    assert body["archived"] is True and body["size_bytes"] == 99
    # delete via API removes it
    d = cli.request("DELETE", "/api/archive/org/known-model", headers=h)
    assert d.status_code == 200 and d.json()["ok"] is True
    assert cli.get("/api/archive/org/known-model", headers=h).json()["archived"] is False


def test_api_archive_status_requires_token():
    cli = _setup_client()
    assert cli.get("/api/archive/org/x").status_code == 401


def test_api_stores_listing():
    cli = _setup_client()
    r = cli.get("/api/stores", headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 200
    names = {s["name"] for s in r.json()["stores"]}
    assert "default" in names


def test_api_job_pause_resume_ok():
    cli = _setup_client()
    h = {"Authorization": f"Bearer {TOKEN}"}
    assert cli.post("/api/jobs/none/pause", headers=h).json()["ok"] is True
    assert cli.post("/api/jobs/none/resume", headers=h).json()["ok"] is True


def test_stores_page_renders():
    cli = _setup_client()
    _login(cli)
    assert "Data stores" in cli.get("/stores").text


def test_archive_space_error_shows_in_modal():
    cli = _setup_client()
    _login(cli)
    of = hub.repo_files
    hub.repo_files = lambda r, rev="main": {"sha": "s", "files": [{"path": "a", "size": 1}]}
    appmod.hub.repo_files = hub.repo_files
    orig = jobs.manager.start_download

    def boom(*a, **k):
        raise jobs.InsufficientSpace("need 805 GB but only 86 GB free")

    jobs.manager.start_download = boom
    try:
        r = cli.post("/ui/archive", data={"repo_id": "org/x", "mode": "all"},
                     headers={"X-CSRF-Token": _csrf(cli)}).text
        assert "hx-swap-oob" in r and "need 805 GB" in r and "modal-overlay" in r
    finally:
        jobs.manager.start_download = orig
        hub.repo_files = of
        appmod.hub.repo_files = of


def test_search_returns_modal():
    cli = _setup_client()
    _login(cli)
    of = appmod.search_models
    appmod.search_models = lambda q, limit=25: [{"id": "o/m", "downloads": 1, "likes": 0, "last_modified": ""}]
    try:
        r = cli.post("/ui/search", data={"q": "x"}, headers={"X-CSRF-Token": _csrf(cli)}).text
        assert "modal-overlay" in r and "o/m" in r and "Archive" in r
    finally:
        appmod.search_models = of


def test_ui_move_starts_job():
    cli = _setup_client()
    _login(cli)
    called = {}
    orig = jobs.manager.start_move
    jobs.manager.start_move = lambda repo_id, dest: called.update(repo=repo_id, dest=dest) or _FakeJob(repo_id)
    try:
        cli.post("/ui/move/org/mv", data={"store_id": "s2"})  # no csrf -> ignored
        assert not called
        cli.post("/ui/move/org/mv", data={"store_id": "s2"}, headers={"X-CSRF-Token": _csrf(cli)})
        assert called == {"repo": "org/mv", "dest": "s2"}
    finally:
        jobs.manager.start_move = orig


# --- web auth / sessions -------------------------------------------------

def test_root_redirects_without_session():
    cli = _setup_client()
    r = cli.get("/")
    assert r.status_code == 303 and r.headers["location"] == "/login"


def test_login_page_renders():
    cli = _setup_client()
    assert cli.get("/login").status_code == 200


def test_login_wrong_password():
    cli = _setup_client()
    r = cli.post("/login", data={"password": "nope"})
    assert r.status_code == 303 and "error" in r.headers["location"]
    # still unauthenticated
    assert cli.get("/").headers["location"] == "/login"


def test_login_success_and_access():
    cli = _setup_client()
    _login(cli)
    r = cli.get("/")
    assert r.status_code == 200 and "Recent activity" in r.text


def test_archives_page():
    cli = _setup_client()
    _login(cli)
    r = cli.get("/archives")
    assert r.status_code == 200 and "Archived models" in r.text


def test_no_token_warning_on_dashboard():
    cli = _setup_client()
    _login(cli)
    hub.set_hf_token(None)
    assert "No HuggingFace token set" in cli.get("/").text
    hub.set_hf_token("hf_x")
    assert "No HuggingFace token set" not in cli.get("/").text
    hub.set_hf_token(None)


def test_archive_status_get_with_content_type_header():
    # Regression: a Content-Type on a bodyless GET must not 500 the endpoint.
    cli = _setup_client()
    r = cli.get(
        "/api/archive/org/whatever",
        headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"},
    )
    assert r.status_code == 200 and r.json()["archived"] is False


def test_logout_clears_session():
    cli = _setup_client()
    _login(cli)
    assert cli.get("/").status_code == 200
    cli.get("/logout")
    assert cli.get("/").headers["location"] == "/login"


def test_login_throttle_locks_out():
    cli = _setup_client()
    for _ in range(5):
        cli.post("/login", data={"password": "wrong"})
    r = cli.post("/login", data={"password": "wrong"})
    assert "Too+many" in r.headers["location"]


# --- CSRF on state-changing UI routes ------------------------------------

def test_delete_requires_csrf():
    cli = _setup_client()
    _login(cli)
    store.upsert_archive("org/csrf", "main", "sha123", os.path.join(_TMP, "fake-archive"), 10, STORE)
    # Datastar action: CSRF rides as the `csrf` signal (JSON body). None -> no-op.
    cli.post("/ui/delete/org/csrf")
    assert store.get_archive("org/csrf") is not None
    # With the valid CSRF signal -> deleted.
    _ds_post(cli, "/ui/delete/org/csrf", csrf=_csrf(cli))
    assert store.get_archive("org/csrf") is None


def test_archive_ui_requires_csrf():
    cli = _setup_client()
    _login(cli)
    started = {"n": 0}
    orig = jobs.manager.start_download
    jobs.manager.start_download = lambda repo_id, revision="main", store_id=None, selected=None: started.__setitem__("n", started["n"] + 1) or _FakeJob(repo_id)
    try:
        cli.post("/ui/archive", data={"repo_id": "org/x"})  # no csrf
        assert started["n"] == 0
        cli.post("/ui/archive", data={"repo_id": "org/x"}, headers={"X-CSRF-Token": _csrf(cli)})
        assert started["n"] == 1
    finally:
        jobs.manager.start_download = orig


# --- settings ------------------------------------------------------------

def test_change_password_flow():
    cli = _setup_client()
    _login(cli)
    # wrong current password is rejected
    r = cli.post("/settings/password", data={"current": "bad", "new": "newpass1"})
    assert "incorrect" in r.headers["location"]
    assert auth.verify_password(PASSWORD)
    # correct current updates it
    r = cli.post("/settings/password", data={"current": PASSWORD, "new": "newpass1"})
    assert "updated" in r.headers["location"]
    assert auth.verify_password("newpass1")
    auth.set_password(PASSWORD)  # restore for other tests


def test_force_password_change_when_autogenerated():
    cli = _setup_client()
    auth.set_password("genpw1x", autogenerated=True)
    try:
        # login lands on the forced change page, not the dashboard
        r = cli.post("/login", data={"password": "genpw1x"})
        assert r.headers["location"] == "/change-password"
        # any other page bounces back to it
        assert cli.get("/").headers["location"] == "/change-password"
        # mismatch / too-short are rejected
        assert "match" in cli.post("/change-password", data={"new": "abcdef1", "confirm": "zzz"}).headers["location"]
        assert "short" in cli.post("/change-password", data={"new": "ab", "confirm": "ab"}).headers["location"]
        # valid change clears the flag and unlocks the app
        r = cli.post("/change-password", data={"new": "brandnew1", "confirm": "brandnew1"})
        assert r.headers["location"] == "/"
        assert auth.must_change_password() is False
        assert auth.verify_password("brandnew1") is True
        assert cli.get("/").status_code == 200
    finally:
        auth.set_password(PASSWORD, autogenerated=False)


def test_hf_token_set_and_clear():
    cli = _setup_client()
    _login(cli)
    r = cli.post("/settings/hf-token", data={"token": "hf_test123"})
    assert "saved" in r.headers["location"]
    assert hub.current_hf_token() == "hf_test123"
    assert hub.hf_token_source() == "ui"
    cli.post("/settings/hf-token/clear")
    assert hub.hf_token_source() in ("none", "env")


def test_rotate_token_changes_token():
    cli = _setup_client()
    _login(cli)
    before = auth.cfg.api_token
    cli.post("/settings/rotate-token")
    assert auth.cfg.api_token != before


# --- security headers ----------------------------------------------------

def test_security_headers_present():
    cli = _setup_client()
    _login(cli)
    h = cli.get("/").headers
    assert h["x-content-type-options"] == "nosniff"
    assert h["x-frame-options"] == "DENY"
    assert "content-security-policy" in h
    # Datastar's expression evaluator needs unsafe-eval in script-src.
    assert "'unsafe-eval'" in h["content-security-policy"]
    assert h["referrer-policy"] == "no-referrer"


def test_datastar_runtime_self_hosted():
    cli = _setup_client()
    _login(cli)
    # the runtime is referenced and actually served from /static (no CDN)
    assert "/static/datastar.js" in cli.get("/").text
    r = cli.get("/static/datastar.js")
    assert r.status_code == 200
    assert "Datastar" in r.text


def test_jobs_panel_is_datastar_stream():
    from fasthtml.common import to_xml
    panel = to_xml(appmod.jobs_panel())
    assert 'data-on-load="@get(' in panel  # opens the SSE stream on load
    assert 'id="jobs-body"' in panel        # inner target the stream morphs


def test_jobs_pause_button_is_datastar():
    """A running job renders a Datastar Pause action (not htmx)."""
    from fasthtml.common import to_xml

    class _J:
        id = "abc-123"; type = "download"; status = "running"; repo_id = "o/m"
        percent = 10; done_bytes = 1; total_bytes = 10
    orig = jobs.manager.active
    jobs.manager.active = lambda: [_J()]
    try:
        body = to_xml(appmod.jobs_body())
    finally:
        jobs.manager.active = orig
    assert "data-on-click=\"@post('/ui/jobs/abc-123/pause')\"" in body
    assert "hx-post" not in body  # Pause/Resume no longer htmx


def test_jobs_pause_returns_patch():
    cli = _setup_client()
    _login(cli)
    # Datastar @post carries csrf as a signal (JSON body); route returns a patch.
    r = _ds_post(cli, "/ui/jobs/none/pause", csrf=_csrf(cli))
    assert r.status_code == 200
    assert "datastar-patch-elements" in r.text
    assert "jobs-body" in r.text


def test_jobs_sse_stream_emits_patch():
    import asyncio

    async def first_frame():
        resp = await appmod.ui_jobs()
        chunk = await resp.body_iterator.__anext__()
        return chunk if isinstance(chunk, str) else chunk.decode()
    ev = asyncio.run(first_frame())
    assert "datastar-patch-elements" in ev
    assert "jobs-body" in ev


def test_archives_panel_is_datastar_stream():
    from fasthtml.common import to_xml
    panel = to_xml(appmod.archives_panel())
    assert 'data-on-load="@get(' in panel
    assert 'id="archives-body"' in panel
    # Check-all is a Datastar action
    assert "@post('/ui/check-all')" in panel


def test_archives_check_all_returns_patch():
    cli = _setup_client()
    _login(cli)
    r = _ds_post(cli, "/ui/check-all", csrf=_csrf(cli))
    assert r.status_code == 200
    assert "datastar-patch-elements" in r.text and "archives-body" in r.text


def test_summary_sse_stream():
    """The dashboard summary is a Datastar SSE stream patching #summary-body."""
    import asyncio
    from fasthtml.common import to_xml
    assert 'data-on-load="@get(' in to_xml(appmod.summary_panel())

    async def first_frame():
        resp = await appmod.ui_summary()
        chunk = await resp.body_iterator.__anext__()
        return chunk if isinstance(chunk, str) else chunk.decode()
    ev = asyncio.run(first_frame())
    assert "datastar-patch-elements" in ev
    assert "summary-body" in ev


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\nAll {len(fns)} checks passed.")
