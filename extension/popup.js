const $ = (id) => document.getElementById(id);
const DEFAULTS = { serverUrl: "http://localhost:7860", token: "" };

function setStatus(msg, cls) {
  const el = $("status");
  el.textContent = msg;
  el.className = cls || "";
}

async function load() {
  const cfg = await chrome.storage.sync.get(DEFAULTS);
  $("serverUrl").value = cfg.serverUrl;
  $("token").value = cfg.token;
}

// The extension holds http/https host access (granted at install), so it can
// reach any server you configure — no per-origin runtime permission step.
$("save").addEventListener("click", async () => {
  const serverUrl = $("serverUrl").value.trim().replace(/\/+$/, "");
  const token = $("token").value.trim();
  if (!serverUrl || !token) {
    setStatus("Enter both a server URL and a token.", "err");
    return;
  }
  await chrome.storage.sync.set({ serverUrl, token });
  setStatus("Testing…");
  chrome.runtime.sendMessage({ type: "ping" }, (resp) => {
    if (resp && resp.ok) setStatus(`Connected ✓ (v${resp.data.version})`, "ok");
    else setStatus("Failed: " + (resp ? resp.error : "no response"), "err");
  });
});

load();
