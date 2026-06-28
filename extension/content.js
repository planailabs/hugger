// Injects a floating "Archive" button on HuggingFace model pages and sends the
// repo id to the background worker (which holds the token and talks to hugger).

const NON_MODEL = new Set([
  "datasets", "spaces", "organizations", "settings", "docs", "models", "join",
  "login", "blog", "pricing", "tasks", "search", "notifications", "new",
  "collections", "posts", "chat", "enterprise", "", "huggingface",
]);
// Sub-pages that come *after* a repo id in the path.
const SUBPAGES = new Set([
  "tree", "blob", "resolve", "commit", "commits", "discussions", "settings",
  "raw", "blame", "edit", "upload",
]);

function repoIdFromPath(pathname) {
  const parts = pathname.replace(/^\/+|\/+$/g, "").split("/").filter(Boolean);
  if (parts.length === 0) return null;
  if (NON_MODEL.has(parts[0])) return null;
  // canonical single-segment model (e.g. /gpt2) or org/name; stop at sub-pages.
  const out = [];
  for (const p of parts.slice(0, 2)) {
    if (SUBPAGES.has(p)) break;
    out.push(p);
  }
  if (out.length === 0) return null;
  // If 2nd segment is actually a subpage, keep only the first (canonical repo).
  return out.join("/");
}

function toast(text, kind) {
  let t = document.getElementById("hugger-toast");
  if (!t) {
    t = document.createElement("div");
    t.id = "hugger-toast";
    t.style.cssText =
      "position:fixed;bottom:80px;right:20px;z-index:2147483647;padding:10px 14px;" +
      "border-radius:8px;color:#fff;font:600 13px system-ui;box-shadow:0 4px 14px rgba(0,0,0,.25);max-width:280px";
    document.body.appendChild(t);
  }
  t.style.background = kind === "error" ? "#E53935" : "#FB8C00";
  t.textContent = text;
  t.style.opacity = "1";
  clearTimeout(t._h);
  t._h = setTimeout(() => (t.style.opacity = "0"), 4000);
}

function pollStatus(jobId, repoId) {
  const iv = setInterval(() => {
    chrome.runtime.sendMessage({ type: "status", job_id: jobId }, (resp) => {
      if (!resp || !resp.ok) {
        clearInterval(iv);
        return;
      }
      const j = resp.data;
      if (j.status === "downloading") {
        toast(`${repoId}: ${j.percent}%`);
      } else if (j.status === "done") {
        clearInterval(iv);
        toast(`✓ Archived ${repoId}`);
      } else if (j.status === "error") {
        clearInterval(iv);
        toast(`✗ ${repoId}: ${j.error}`, "error");
      }
    });
  }, 1500);
}

function archive(repoId, btn) {
  btn.disabled = true;
  btn.textContent = "Archiving…";
  chrome.runtime.sendMessage({ type: "archive", repo_id: repoId }, (resp) => {
    btn.disabled = false;
    btn.textContent = "⤓ Archive";
    if (!resp) {
      toast("Extension error", "error");
      return;
    }
    if (resp.ok) {
      toast(`Started archiving ${repoId}`);
      pollStatus(resp.data.job_id, repoId);
    } else {
      toast(resp.error, "error");
    }
  });
}

function injectButton(repoId) {
  if (document.getElementById("hugger-btn")) return;
  const btn = document.createElement("button");
  btn.id = "hugger-btn";
  btn.textContent = "⤓ Archive";
  btn.title = `Archive ${repoId} to hugger`;
  btn.style.cssText =
    "position:fixed;bottom:20px;right:20px;z-index:2147483647;padding:12px 18px;" +
    "border:none;border-radius:999px;background:#FB8C00;color:#fff;font:700 14px system-ui;" +
    "cursor:pointer;box-shadow:0 4px 14px rgba(0,0,0,.3)";
  btn.onmouseenter = () => (btn.style.background = "#F4511E");
  btn.onmouseleave = () => (btn.style.background = "#FB8C00");
  btn.onclick = () => archive(repoId, btn);
  document.body.appendChild(btn);
}

function run() {
  const repoId = repoIdFromPath(location.pathname);
  const existing = document.getElementById("hugger-btn");
  if (repoId) injectButton(repoId);
  else if (existing) existing.remove();
}

// HuggingFace is an SPA — re-evaluate on navigation.
run();
let lastPath = location.pathname;
setInterval(() => {
  if (location.pathname !== lastPath) {
    lastPath = location.pathname;
    run();
  }
}, 800);
