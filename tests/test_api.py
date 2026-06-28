"""Integration tests for the HTTP layer — auth, sessions, CSRF, API, headers.

Mirrors the manual smoke checks. No network: download jobs are stubbed so no real
HuggingFace calls happen. Run standalone:

    python tests/test_api.py
"""
import os
import re
import tempfile

_TMP = tempfile.mkdtemp(prefix="hugger-apitest-")
os.environ["HUGGER_HOME"] = _TMP
os.environ["HUGGER_ARCHIVE_DIR"] = os.path.join(_TMP, "archives")

from starlette.testclient import TestClient  # noqa: E402

from hugger import app as appmod  # noqa: E402
from hugger import auth, store, jobs, hub  # noqa: E402

PASSWORD = "test1234"
auth.set_password(PASSWORD)
TOKEN = auth.cfg.api_token

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
    orig = jobs.manager.start
    jobs.manager.start = lambda repo_id, revision="main": _FakeJob(repo_id)
    try:
        h = {"Authorization": f"Bearer {TOKEN}"}
        assert cli.post("/api/archive", json={}, headers=h).status_code == 400
        r = cli.post("/api/archive", json={"repo_id": "org/m"}, headers=h)
        assert r.status_code == 200 and r.json()["repo_id"] == "org/m"
        assert r.json()["job_id"].startswith("fake")
    finally:
        jobs.manager.start = orig


def test_api_status_not_found():
    cli = _setup_client()
    r = cli.get("/api/status/nope", headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 404


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
    assert r.status_code == 200 and "Archived models" in r.text


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
    store.upsert_archive("org/csrf", "main", "sha123", os.path.join(_TMP, "fake-archive"), 10)
    # Without CSRF header -> no deletion.
    cli.post("/ui/delete/org/csrf")
    assert store.get_archive("org/csrf") is not None
    # With valid CSRF -> deleted.
    cli.post("/ui/delete/org/csrf", headers={"X-CSRF-Token": _csrf(cli)})
    assert store.get_archive("org/csrf") is None


def test_archive_ui_requires_csrf():
    cli = _setup_client()
    _login(cli)
    started = {"n": 0}
    orig = jobs.manager.start
    jobs.manager.start = lambda repo_id, revision="main": started.__setitem__("n", started["n"] + 1) or _FakeJob(repo_id)
    try:
        cli.post("/ui/archive", data={"repo_id": "org/x"})  # no csrf
        assert started["n"] == 0
        cli.post("/ui/archive", data={"repo_id": "org/x"}, headers={"X-CSRF-Token": _csrf(cli)})
        assert started["n"] == 1
    finally:
        jobs.manager.start = orig


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
    assert h["referrer-policy"] == "no-referrer"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\nAll {len(fns)} checks passed.")
