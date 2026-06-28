// On a HuggingFace model page, show hugger controls reflecting server state:
//   not archived  -> [⤓ Archive] [Files…]
//   archived       -> ✓ Archived  [⟳ Update] [🗑 Remove] [Files…]
// "Files…" opens a panel to archive only selected files (or per-file ⤓).
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

function fmtSize(n) {
  let f = n;
  for (const u of ["B", "KB", "MB", "GB", "TB"]) {
    if (f < 1024 || u === "TB") return (u === "B" ? f : f.toFixed(1)) + " " + u;
    f /= 1024;
  }
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
      "align-items:flex-end;flex-direction:column;font:700 14px system-ui";
    document.body.appendChild(b);
  }
  return b;
}

function button(id, text, bg) {
  const b = document.createElement("button");
  if (id) b.id = id;
  b.textContent = text;
  b.style.cssText =
    `border:none;border-radius:999px;padding:10px 16px;background:${bg};color:#fff;` +
    "font:700 13px system-ui;cursor:pointer;box-shadow:0 4px 14px rgba(0,0,0,.3)";
  return b;
}

function label(text) {
  const s = document.createElement("span");
  s.textContent = text;
  s.style.cssText =
    "background:#FFE082;color:#4E342E;padding:8px 12px;border-radius:999px;box-shadow:0 4px 14px rgba(0,0,0,.2)";
  return s;
}

let CURRENT = null;

function render(status) {
  const b = box();
  b.innerHTML = "";
  const ctrls = document.createElement("div");
  ctrls.style.cssText = "display:flex;gap:8px;align-items:center";
  if (!status || !status.archived) {
    const a = button("hugger-btn", "⤓ Archive", "#FB8C00");
    a.onclick = () => archive(null, a);
    ctrls.appendChild(a);
  } else {
    ctrls.appendChild(label("✓ Archived"));
    if (status.update_available) {
      const u = button("hugger-update", "⟳ Update", "#FB8C00");
      u.onclick = () => archive(null, u);
      ctrls.appendChild(u);
    }
    const r = button("hugger-remove", "🗑 Remove", "#E53935");
    r.onclick = () => remove(r);
    ctrls.appendChild(r);
  }
  const f = button("hugger-files", "Files…", "#6D4C41");
  f.onclick = toggleFiles;
  ctrls.appendChild(f);
  b.appendChild(ctrls);
}

async function toggleFiles() {
  const existing = document.getElementById("hugger-panel");
  if (existing) {
    existing.remove();
    return;
  }
  const panel = document.createElement("div");
  panel.id = "hugger-panel";
  panel.style.cssText =
    "background:#FFF8E1;color:#4E342E;border:1px solid #FFE082;border-radius:10px;" +
    "padding:10px;max-width:420px;max-height:50vh;overflow:auto;box-shadow:0 6px 18px rgba(0,0,0,.25);" +
    "font:400 13px system-ui;order:-1";
  panel.textContent = "Loading files…";
  box().appendChild(panel);

  const resp = await send({ type: "get_files", repo_id: CURRENT });
  if (!resp || !resp.ok) {
    panel.textContent = "Failed to list files: " + (resp ? resp.error : "no response");
    return;
  }
  panel.innerHTML = "";
  const list = document.createElement("div");
  for (const file of resp.data.files) {
    const row = document.createElement("label");
    row.style.cssText = "display:flex;align-items:center;gap:8px;padding:3px 0";
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.checked = !file.downloaded;
    cb.dataset.path = file.path;
    const name = document.createElement("span");
    name.style.cssText = "flex:1;font-family:ui-monospace,monospace";
    name.textContent = file.path + (file.downloaded ? " ✓" : "");
    const sz = document.createElement("span");
    sz.style.cssText = "color:#8D6E63";
    sz.textContent = fmtSize(file.size);
    const one = button(null, "⤓", "#FB8C00");
    one.style.padding = "4px 9px";
    one.onclick = (e) => { e.preventDefault(); archive([file.path], one); };
    row.append(cb, name, sz, one);
    list.appendChild(row);
  }
  const footer = document.createElement("div");
  footer.style.cssText = "display:flex;gap:8px;margin-top:8px";
  const all = button(null, "⤓ Download all", "#FB8C00");
  all.onclick = () => archive(null, all);
  const sel = button(null, "⤓ Download selected", "#6D4C41");
  sel.onclick = () => {
    const paths = [...list.querySelectorAll("input:checked")].map((c) => c.dataset.path);
    if (paths.length) archive(paths, sel);
    else toast("No files selected", "error");
  };
  footer.append(all, sel);
  panel.append(list, footer);
}

function pollJob(jobId) {
  const iv = setInterval(async () => {
    const resp = await send({ type: "status", job_id: jobId });
    if (!resp || !resp.ok) { clearInterval(iv); return; }
    const j = resp.data;
    if (["queued", "running", "downloading"].includes(j.status)) {
      toast(`${CURRENT}: ${j.percent}%`);
    } else if (j.status === "paused") {
      clearInterval(iv); toast(`⏸ ${CURRENT} paused`);
    } else if (j.status === "done") {
      clearInterval(iv); toast(`✓ Archived ${CURRENT}`); refresh();
    } else if (j.status === "error") {
      clearInterval(iv); toast(`✗ ${CURRENT}: ${j.error}`, "error"); refresh();
    }
  }, 1500);
}

async function archive(files, btn) {
  if (btn) { btn.disabled = true; btn.textContent = "Archiving…"; }
  const resp = await send({ type: "archive", repo_id: CURRENT, files });
  if (resp && resp.ok) {
    toast(`Started archiving ${CURRENT}`);
    pollJob(resp.data.job_id);
  } else {
    toast(resp ? resp.error : "Extension error", "error");
    refresh();
  }
}

async function remove(btn) {
  btn.disabled = true; btn.textContent = "Removing…";
  const resp = await send({ type: "remove", repo_id: CURRENT });
  toast(resp && resp.ok ? `Removed ${CURRENT}` : (resp ? resp.error : "Extension error"),
        resp && resp.ok ? undefined : "error");
  refresh();
}

async function refresh() {
  if (!CURRENT) return;
  const resp = await send({ type: "get_status", repo_id: CURRENT });
  render(resp && resp.ok ? resp.data : null);
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
