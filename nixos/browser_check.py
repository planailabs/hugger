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

from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

HF = "https://huggingface.co"
SERVER = os.environ.get("HUGGER_SERVER", "http://127.0.0.1:7860")
TOKEN = os.environ["HUGGER_TOKEN"]
EXAMPLE = "HuggingFaceTB/SmolLM2-135M-Instruct"  # from the user's example URL
MODEL = "test-org/tiny-model"                     # the one our fake hub can serve
PAGE_LOAD_TIMEOUT = 60                            # seconds, per navigation attempt
DRIVER_LOG = "/tmp/chromedriver-hugger.log"       # echoed into the test log on failure


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
    # Keep chromedriver's own log: when a renderer stalls, that log holds the
    # DevTools error that Selenium only surfaces as an opaque timeout.
    return webdriver.Chrome(
        service=Service(
            executable_path=os.environ["CHROMEDRIVER"],
            log_output=DRIVER_LOG,
            service_args=["--verbose"],
        ),
        options=o,
    )


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


def _print_driver_tail(which):
    """Echo chromedriver's verbose log — it names the DevTools command that hung."""
    try:
        with open(DRIVER_LOG, "r", errors="replace") as fh:
            tail = fh.read()[-4000:]
    except OSError as e:
        print(f"[{which}] {DRIVER_LOG}: <unavailable: {e}>", flush=True)
        return
    print(f"[{which}] --- {DRIVER_LOG} (tail) ---\n{tail}", flush=True)


def diagnose(driver, which, label):
    """Best-effort page state, printed to the test log so a timeout is triageable.

    Every probe is guarded and printed as it runs: when the renderer is wedged a
    probe can block, and the subtest timeout then kills us — everything printed
    before that point is what the CI trace keeps. Screenshots are useless here
    (the guest /tmp is discarded), so the state is reported as text.
    """
    print(f"[{which}] --- diagnostics for {label} ---", flush=True)
    probes = (
        ("current_url", lambda: driver.current_url),
        ("title", lambda: driver.title),
        ("readyState", lambda: driver.execute_script("return document.readyState")),
        ("hugger-btn count", lambda: len(driver.find_elements(By.ID, "hugger-btn"))),
        (
            "hugger-box html",
            lambda: (driver.execute_script(
                "var b = document.getElementById('hugger-box');"
                "return b ? b.outerHTML : null;"
            ) or "")[:600],
        ),
    )
    for name, probe in probes:
        try:
            print(f"[{which}] {name}: {probe()!r}", flush=True)
        except Exception as e:  # noqa: BLE001 - diagnostics must never mask the real error
            print(f"[{which}] {name}: <unavailable: {type(e).__name__}: {e}>", flush=True)
    _print_driver_tail(which)


def navigate(driver, url, which, label):
    """Load `url`, retrying once if the renderer stalls past the page-load budget.

    The browser VM shares a CI runner with the other VM checks, so a heavily
    loaded runner can starve the renderer and make `driver.get` time out even
    though the (local, tiny) page is fine. That is a transient: retry once and
    keep the assertions themselves strict. A real product failure still fails --
    the retry only covers the navigation step, never the extension behaviour.
    """
    for attempt in (1, 2):
        try:
            driver.get(url)
            return
        except TimeoutException as e:
            print(
                f"[{which}] navigation to {url} timed out "
                f"(attempt {attempt}/2, {PAGE_LOAD_TIMEOUT}s): {e}",
                flush=True,
            )
            if attempt == 2:
                diagnose(driver, which, label)
                raise
            time.sleep(2)


def wait_for_button(driver, wait, which, label):
    """Wait for the extension UI, reporting page state if it never shows up."""
    try:
        return wait.until(EC.presence_of_element_located((By.ID, "hugger-btn")))
    except TimeoutException:
        diagnose(driver, which, label)
        raise


def main():
    which = sys.argv[1]
    # Start each run from a clean server state so the flow is deterministic.
    try:
        server_call(f"/api/archive/{MODEL}", method="DELETE")
    except Exception:
        pass

    driver = make_chrome() if which == "chrome" else make_firefox()
    driver.set_page_load_timeout(PAGE_LOAD_TIMEOUT)
    wait = WebDriverWait(driver, 90)
    try:
        # 1. The button shows up on the example model page from the prompt.
        navigate(driver, f"{HF}/{EXAMPLE}", which, "example model page")
        wait_for_button(driver, wait, which, "example model page")
        print(f"[{which}] extension button present on {EXAMPLE}")

        # 2. Archive -> Remove cycle on a model the fake hub can actually serve.
        navigate(driver, f"{HF}/{MODEL}", which, "fake-hub model page")
        btn = wait_for_button(driver, wait, which, "fake-hub model page")
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
