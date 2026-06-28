// Background worker: performs all authenticated calls to the hugger server.
// Running here (with host_permissions) means requests are NOT subject to page
// CORS, so the server stays locked down and reachable when exposed over HTTPS.
//
// Cross-browser: Chrome loads this as a service worker (and importScripts pulls
// in defaults.js); Firefox loads ["defaults.js", "background.js"] as event-page
// scripts (so defaults.js already ran).

if (typeof importScripts === "function") {
  try {
    importScripts("defaults.js");
  } catch (e) {
    /* already loaded as a background script (Firefox) */
  }
}

const DEFAULTS = (typeof self !== "undefined" && self.HUGGER_DEFAULTS) || {
  serverUrl: "http://localhost:7860",
  token: "",
};

async function getCfg() {
  try {
    return await chrome.storage.sync.get(DEFAULTS);
  } catch (e) {
    return DEFAULTS; // storage.sync unavailable -> fall back to baked defaults
  }
}

async function api(path, opts = {}) {
  const { serverUrl, token } = await getCfg();
  if (!serverUrl) throw new Error("Server URL not set");
  if (!token) throw new Error("API token not set — open the extension popup");
  const base = serverUrl.replace(/\/+$/, "");
  const headers = {
    Authorization: "Bearer " + token,
    "X-Hugger-Token": token,
    ...(opts.headers || {}),
  };
  // Only advertise a JSON body when we actually send one — a Content-Type on a
  // bodyless GET/DELETE makes the server try to parse an empty body.
  if (opts.body != null) headers["Content-Type"] = "application/json";
  const res = await fetch(base + path, { ...opts, headers });
  const text = await res.text();
  let data;
  try {
    data = text ? JSON.parse(text) : {};
  } catch {
    data = { raw: text };
  }
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}

async function handle(msg) {
  switch (msg.type) {
    case "ping":
      return await api("/api/ping");
    case "get_status":
      return await api("/api/archive/" + msg.repo_id);
    case "archive":
      return await api("/api/archive", {
        method: "POST",
        body: JSON.stringify({ repo_id: msg.repo_id, revision: msg.revision || "main" }),
      });
    case "status":
      return await api("/api/status/" + encodeURIComponent(msg.job_id));
    case "remove":
      return await api("/api/archive/" + msg.repo_id, { method: "DELETE" });
    default:
      throw new Error("Unknown message type: " + msg.type);
  }
}

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  handle(msg)
    .then((data) => sendResponse({ ok: true, data }))
    .catch((err) => sendResponse({ ok: false, error: String(err.message || err) }));
  return true; // keep the message channel open for the async response
});
