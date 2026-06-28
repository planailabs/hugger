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

// Request host permission for non-localhost servers so the background worker can
// fetch cross-origin (needed when hugger is exposed over the internet).
async function ensurePermission(serverUrl) {
  try {
    const u = new URL(serverUrl);
    const origin = `${u.protocol}//${u.hostname}/*`;
    const has = await chrome.permissions.contains({ origins: [origin] });
    if (has) return true;
    return await chrome.permissions.request({ origins: [origin] });
  } catch {
    return false;
  }
}

$("save").addEventListener("click", async () => {
  const serverUrl = $("serverUrl").value.trim().replace(/\/+$/, "");
  const token = $("token").value.trim();
  if (!serverUrl || !token) {
    setStatus("Enter both a server URL and a token.", "err");
    return;
  }
  setStatus("Requesting permission…");
  const granted = await ensurePermission(serverUrl);
  if (!granted) {
    setStatus("Permission for that server was denied.", "err");
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
