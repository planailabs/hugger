"""Seed representative data, boot the server, screenshot every page for design
review. Run in `nix develop .#test` (or .#default). Not part of the test suite."""
import os, socket, subprocess, sys, tempfile, time
from pathlib import Path

HOME = tempfile.mkdtemp(prefix="hugger-shots-")
os.environ["HUGGER_HOME"] = HOME
os.environ["HUGGER_ARCHIVE_DIR"] = str(Path(HOME) / "archives")
PORT = 7884
SHOTS = Path("/tmp/shots"); SHOTS.mkdir(exist_ok=True)

# --- seed data ------------------------------------------------------------
from hugger import store, jobs, metadata  # noqa: E402
store.run_migrations()
a = store.ensure_default_store(str(Path(HOME) / "archives"))
b = store.add_store("second", str(Path(HOME) / "archives2"))
REPO1 = "DavidAU/Llama-3.2-8X4B-MOE-V2-Dark-Champion-Instruct-uncensored-abliterated-21B-GGUF"
REPO2 = "56m/Dumb"
for repo, sz, sha in [(REPO1, 0, "bd6f48af11"), (REPO2, 33_400_000, "188e98fb22")]:
    d = jobs.store_repo_path(store.get_store(a)["path"], repo)
    metadata.write(d, metadata.build(repo, "main", sha, [{"path": "config.json", "size": sz or 1}], ["config.json"]))
    store.upsert_archive(repo, "main", sha, str(d), sz, a)
store.set_update_status(REPO2, "newsha99", True)  # one shows "update available"

# a paused download with on-disk partial -> ~17% progress
pdest = jobs.store_repo_path(store.get_store(a)["path"], REPO1)
metadata.write(pdest, metadata.build(REPO1, "main", "bd6f48af11",
               [{"path": f"shard{i}.bin", "size": 100} for i in range(4)],
               [f"shard{i}.bin" for i in range(4)]))
(pdest / "shard0.bin").write_bytes(b"x" * 100)  # 2 of 4 shards complete -> 50%
(pdest / "shard1.bin").write_bytes(b"x" * 100)
store.save_job({"id": "pausedjob", "repo_id": REPO1, "revision": "main", "type": "download",
                "status": "paused", "total_bytes": 400, "store_id": a, "sha": "bd6f48af11"})
store.save_job({"id": "donejob", "repo_id": REPO2, "revision": "main", "type": "download",
                "status": "done", "total_bytes": 33_400_000, "store_id": a})
store.save_job({"id": "errjob", "repo_id": "org/broken", "revision": "main", "type": "download",
                "status": "error", "error": "404 Not Found", "store_id": a})

# --- boot server ----------------------------------------------------------
env = dict(os.environ, HUGGER_PASSWORD="test1234", HUGGER_HOST="127.0.0.1", HUGGER_PORT=str(PORT))
srv = subprocess.Popen([sys.executable, "-m", "hugger"], env=env)
for _ in range(150):
    try: socket.create_connection(("127.0.0.1", PORT), 0.4).close(); break
    except OSError: time.sleep(0.2)

# --- screenshot -----------------------------------------------------------
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
o = Options(); o.add_argument("--headless=new"); o.add_argument("--no-sandbox")
o.add_argument("--force-device-scale-factor=1"); o.add_argument("--hide-scrollbars")
o.binary_location = os.environ["CHROME_BIN"]
d = webdriver.Chrome(service=Service(os.environ["CHROMEDRIVER"]), options=o)
d.set_window_size(1320, 1400)
try:
    base = f"http://127.0.0.1:{PORT}"
    d.get(base + "/login")
    d.find_element("name", "password").send_keys("test1234")
    d.find_element("css selector", "form button").click(); time.sleep(1)
    pages = [("home", "/"), ("archives", "/archives"), ("jobs", "/jobs"),
             ("stores", "/stores"), ("settings", "/settings"), ("manage", f"/manage/{REPO2}")]
    for name, path in pages:
        d.get(base + path); time.sleep(2.0)  # let SSE panels render
        # size window to full content height for a full-page shot
        h = d.execute_script("return Math.max(document.body.scrollHeight, 900)")
        d.set_window_size(1320, min(h + 40, 4000)); time.sleep(0.4)
        d.save_screenshot(str(SHOTS / f"{name}.png"))
        print("shot", name, path)
    # also a modal: search results
    d.get(base + "/"); time.sleep(1.5)
    for btn in d.find_elements("css selector", "button"):
        if "Search" in btn.text: btn.click(); break
    time.sleep(1.5); d.save_screenshot(str(SHOTS / "modal-search.png")); print("shot modal-search")
finally:
    d.quit(); srv.terminate()
print("done ->", SHOTS)
