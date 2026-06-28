// On a HuggingFace model page, show hugger controls reflecting server state:
//   not archived        -> [⤓ Archive]
//   archived            -> ✓ Archived  [🗑 Remove]
//   archived + update    -> ✓ Archived  [⟳ Update] [🗑 Remove]
// All server calls go through the background worker (which holds the token).

const NON_MODEL = new Set([
  "datasets", "spaces", "organizations", "settings", "docs", "models", "join",
  "login", "blog", "pricing", "tasks", "search", "notifications", "new",
  "collections", "posts", "chat", "enterprise", "", "huggingface",
]);
const SUBPAGES = new Set([
  "tree", "blob", "resolve", "commit", "commits", "discussions", "settings",
  "raw", "blame", "edit", "upload",
]);

function repoIdFromPath(pathname) {
  const parts = pathname.replace(/^\/+|\/+$/g, "").split("/").filter(Boolean);
  if (parts.length === 0) return null;
  if (NON_MODEL.has(parts[0])) return null;
  const out = [];
  for (const p of parts.slice(0, 2)) {
    if (SUBPAGES.has(p)) break;
    out.push(p);
  }
  return out.length ? out.join("/") : null;
}

function send(msg) {
  return new Promise((resolve) => chrome.runtime.sendMessage(msg, resolve));
}

function toast(text, kind) {
  let t = document.getElementById("hugger-toast");
  if (!t) {
    t = document.createElement("div");
    t.id = "hugger-toast";
    t.style.cssText =
      "position:fixed;bottom:84px;right:20px;z-index:2147483647;padding:10px 14px;" +
      "border-radius:8px;color:#fff;font:600 13px system-ui;box-shadow:0 4px 14px rgba(0,0,0,.25);max-width:300px";
    document.body.appendChild(t);
  }
  t.style.background = kind === "error" ? "#E53935" : "#FB8C00";
  t.textContent = text;
  t.style.opacity = "1";
  clearTimeout(t._h);
  t._h = setTimeout(() => (t.style.opacity = "0"), 4000);
}

function box() {
  let b = document.getElementById("hugger-box");
  if (!b) {
    b = document.createElement("div");
    b.id = "hugger-box";
    b.style.cssText =
      "position:fixed;bottom:20px;right:20px;z-index:2147483647;display:flex;gap:8px;" +
      "align-items:center;font:700 14px system-ui";
    document.body.appendChild(b);
  }
  return b;
}

function button(id, text, bg) {
  const b = document.createElement("button");
  b.id = id;
  b.textContent = text;
  b.style.cssText =
    `border:none;border-radius:999px;padding:12px 18px;background:${bg};color:#fff;` +
    "font:700 14px system-ui;cursor:pointer;box-shadow:0 4px 14px rgba(0,0,0,.3)";
  return b;
}

function label(text) {
  const s = document.createElement("span");
  s.textContent = text;
  s.style.cssText =
    "background:#FFE082;color:#4E342E;padding:8px 12px;border-radius:999px;box-shadow:0 4px 14px rgba(0,0,0,.2)";
  return s;
}

let CURRENT = null; // repo id of the active page

function render(status) {
  const b = box();
  b.innerHTML = "";
  if (!status || !status.archived) {
    const a = button("hugger-btn", "⤓ Archive", "#FB8C00");
    a.onclick = () => archive(a);
    b.appendChild(a);
    return;
  }
  b.appendChild(label("✓ Archived"));
  if (status.update_available) {
    const u = button("hugger-update", "⟳ Update", "#FB8C00");
    u.onclick = () => archive(u);
    b.appendChild(u);
  }
  const r = button("hugger-remove", "🗑 Remove", "#E53935");
  r.onclick = () => remove(r);
  b.appendChild(r);
}

async function refresh() {
  if (!CURRENT) return;
  const resp = await send({ type: "get_status", repo_id: CURRENT });
  if (resp && resp.ok) render(resp.data);
  else render(null); // server unreachable -> default to offering Archive
}

function pollJob(jobId) {
  const iv = setInterval(async () => {
    const resp = await send({ type: "status", job_id: jobId });
    if (!resp || !resp.ok) {
      clearInterval(iv);
      return;
    }
    const j = resp.data;
    if (j.status === "downloading") {
      toast(`${CURRENT}: ${j.percent}%`);
    } else if (j.status === "done") {
      clearInterval(iv);
      toast(`✓ Archived ${CURRENT}`);
      refresh();
    } else if (j.status === "error") {
      clearInterval(iv);
      toast(`✗ ${CURRENT}: ${j.error}`, "error");
      refresh();
    }
  }, 1500);
}

async function archive(btn) {
  btn.disabled = true;
  btn.textContent = "Archiving…";
  const resp = await send({ type: "archive", repo_id: CURRENT });
  if (resp && resp.ok) {
    toast(`Started archiving ${CURRENT}`);
    pollJob(resp.data.job_id);
  } else {
    toast(resp ? resp.error : "Extension error", "error");
    refresh();
  }
}

async function remove(btn) {
  btn.disabled = true;
  btn.textContent = "Removing…";
  const resp = await send({ type: "remove", repo_id: CURRENT });
  if (resp && resp.ok) toast(`Removed ${CURRENT}`);
  else toast(resp ? resp.error : "Extension error", "error");
  refresh();
}

function run() {
  const repoId = repoIdFromPath(location.pathname);
  const existing = document.getElementById("hugger-box");
  if (!repoId) {
    if (existing) existing.remove();
    CURRENT = null;
    return;
  }
  CURRENT = repoId;
  refresh();
}

run();
let lastPath = location.pathname;
setInterval(() => {
  if (location.pathname !== lastPath) {
    lastPath = location.pathname;
    run();
  }
}, 800);
