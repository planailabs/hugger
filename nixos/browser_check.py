#!/usr/bin/env python3
"""Drive a real browser with the hugger extension loaded from a temp profile.

Verifies the extension injects its UI on a HuggingFace model page and that the
full archive -> remove cycle works through the server.

Usage:  browser_check.py {chrome|firefox}

Env:
  HUGGER_EXT_DIR   unpacked extension dir (with token baked into defaults.js)
  HUGGER_XPI       zipped extension (firefox temporary add-on)
  HUGGER_SERVER    hugger base url (default http://127.0.0.1:7860)
  HUGGER_TOKEN     api token (for the script's own server checks)
  CHROME_BIN / CHROMEDRIVER / FIREFOX_BIN / GECKODRIVER   binary paths
"""
import json
import os
import sys
import time
import urllib.request

from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

HF = "https://huggingface.co"
SERVER = os.environ.get("HUGGER_SERVER", "http://127.0.0.1:7860")
TOKEN = os.environ["HUGGER_TOKEN"]
EXAMPLE = "HuggingFaceTB/SmolLM2-135M-Instruct"  # from the user's example URL
MODEL = "test-org/tiny-model"                     # the one our fake hub can serve


def server_call(path, method="GET"):
    req = urllib.request.Request(
        SERVER + path, method=method, headers={"Authorization": "Bearer " + TOKEN}
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.load(r)


def server_archived(repo):
    return server_call(f"/api/archive/{repo}").get("archived", False)


def make_chrome():
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service

    ext = os.environ["HUGGER_EXT_DIR"]
    o = Options()
    o.binary_location = os.environ["CHROME_BIN"]
    o.add_argument("--headless=new")
    o.add_argument("--no-sandbox")
    o.add_argument("--disable-gpu")
    o.add_argument("--disable-dev-shm-usage")
    o.add_argument(f"--user-data-dir=/tmp/chrome-profile-{os.getpid()}")
    o.add_argument("--ignore-certificate-errors")
    o.add_argument(f"--disable-extensions-except={ext}")
    o.add_argument(f"--load-extension={ext}")
    return webdriver.Chrome(service=Service(executable_path=os.environ["CHROMEDRIVER"]), options=o)


def make_firefox():
    from selenium import webdriver
    from selenium.webdriver.firefox.options import Options
    from selenium.webdriver.firefox.service import Service

    o = Options()
    o.binary_location = os.environ["FIREFOX_BIN"]
    o.add_argument("-headless")
    o.set_capability("acceptInsecureCerts", True)
    d = webdriver.Firefox(service=Service(executable_path=os.environ["GECKODRIVER"]), options=o)
    d.install_addon(os.environ["HUGGER_XPI"], temporary=True)
    return d


def main():
    which = sys.argv[1]
    # Start each run from a clean server state so the flow is deterministic.
    try:
        server_call(f"/api/archive/{MODEL}", method="DELETE")
    except Exception:
        pass

    driver = make_chrome() if which == "chrome" else make_firefox()
    driver.set_page_load_timeout(60)
    wait = WebDriverWait(driver, 90)
    try:
        # 1. The button shows up on the example model page from the prompt.
        driver.get(f"{HF}/{EXAMPLE}")
        wait.until(EC.presence_of_element_located((By.ID, "hugger-btn")))
        print(f"[{which}] extension button present on {EXAMPLE}")

        # 2. Archive -> Remove cycle on a model the fake hub can actually serve.
        driver.get(f"{HF}/{MODEL}")
        btn = wait.until(EC.presence_of_element_located((By.ID, "hugger-btn")))
        assert "Archive" in btn.text, f"expected Archive button, got {btn.text!r}"
        btn.click()

        # download completes -> extension flips to a Remove button
        wait.until(EC.presence_of_element_located((By.ID, "hugger-remove")))
        assert server_archived(MODEL), "server should report the model archived"
        print(f"[{which}] archived {MODEL} via extension")

        # 3. Remove -> back to an Archive button + gone on the server.
        driver.find_element(By.ID, "hugger-remove").click()
        wait.until(EC.presence_of_element_located((By.ID, "hugger-btn")))
        for _ in range(20):
            if not server_archived(MODEL):
                break
            time.sleep(0.5)
        assert not server_archived(MODEL), "server should report the model removed"
        print(f"[{which}] removed {MODEL} via extension")
        print(f"[{which}] OK")
    finally:
        driver.quit()


if __name__ == "__main__":
    main()
