"""Manual browser probe: confirm Datastar buttons fire and capture CSP errors.
Run inside `nix develop .#test` (provides CHROME_BIN/CHROMEDRIVER)."""
import os, socket, subprocess, sys, tempfile, time

PORT = 7866
env = dict(os.environ, HUGGER_HOME=tempfile.mkdtemp(prefix="hugger-probe-"),
           HUGGER_PASSWORD="test1234", HUGGER_HOST="127.0.0.1", HUGGER_PORT=str(PORT))
srv = subprocess.Popen([sys.executable, "-m", "hugger"], env=env)
try:
    for _ in range(100):
        try:
            socket.create_connection(("127.0.0.1", PORT), timeout=0.5).close(); break
        except OSError:
            time.sleep(0.2)

    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service
    opts = Options()
    opts.add_argument("--headless=new"); opts.add_argument("--no-sandbox")
    opts.binary_location = os.environ["CHROME_BIN"]
    opts.set_capability("goog:loggingPrefs", {"browser": "ALL"})
    drv = webdriver.Chrome(service=Service(os.environ["CHROMEDRIVER"]), options=opts)
    try:
        base = f"http://127.0.0.1:{PORT}"
        drv.get(base + "/login")
        drv.find_element("name", "password").send_keys("test1234")
        drv.find_element("css selector", "form button").click()
        time.sleep(1)
        drv.get(base + "/")
        time.sleep(2)
        # Datastar loaded? (signals present)
        loaded = drv.execute_script("return !!document.querySelector('[data-signals]')")
        print("page url:", drv.current_url, "| has data-signals:", loaded)
        # click Search -> should populate #modal via SSE
        for b in drv.find_elements("css selector", "button"):
            if "Search" in b.text:
                b.click(); break
        time.sleep(2)
        modal_html = drv.find_element("id", "modal").get_attribute("innerHTML")
        print("MODAL after Search click (len %d):" % len(modal_html), modal_html[:160])
        print("RESULT:", "BUTTON WORKS" if "modal-overlay" in modal_html else "BUTTON DEAD")
        print("--- console ---")
        for e in drv.get_log("browser"):
            print(e["level"], e["message"][:240])
    finally:
        drv.quit()
finally:
    srv.terminate()
