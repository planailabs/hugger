// Background service worker: performs all authenticated calls to the hugger
// server. Running here (with host_permissions) means requests are NOT subject to
// page CORS, so the server can stay locked down and still be reachable when
// exposed over the internet via HTTPS.

const DEFAULTS = { serverUrl: "http://localhost:7860", token: "" };

async function getCfg() {
  return chrome.storage.sync.get(DEFAULTS);
}

async function api(path, opts = {}) {
  const { serverUrl, token } = await getCfg();
  if (!serverUrl) throw new Error("Server URL not set");
  if (!token) throw new Error("API token not set — open the extension popup");
  const base = serverUrl.replace(/\/+$/, "");
  const res = await fetch(base + path, {
    ...opts,
    headers: {
      "Content-Type": "application/json",
      Authorization: "Bearer " + token,
      "X-Hugger-Token": token,
      ...(opts.headers || {}),
    },
  });
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
    case "archive":
      return await api("/api/archive", {
        method: "POST",
        body: JSON.stringify({ repo_id: msg.repo_id, revision: msg.revision || "main" }),
      });
    case "status":
      return await api("/api/status/" + encodeURIComponent(msg.job_id));
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
