# 🤗 hugger

A self-hosted **HuggingFace model archiver**. Search models, download full
snapshots to disk, track when an update is published, and delete what you no
longer need — from a small web UI or a one-click browser extension.

- **Backend**: Python + [FastHTML](https://fastht.ml) (server-side rendered, HTMX).
- **Archiving**: [`huggingface_hub`](https://huggingface.co/docs/huggingface_hub) (`snapshot_download`, `model_info`, search).
- **Storage**: SQLite, schema managed by [yoyo-migrations](https://ollycope.com/software/yoyo/).
- **Auth**: argon2 password login + Bearer API token, designed to be exposed to the internet safely.
- **Extension**: one cross-browser (Chrome/Firefox/Edge) MV3 extension.

## Install & run

```bash
pip install -e .
hugger            # serves http://127.0.0.1:7860
```

### With uv

```bash
uv venv && uv pip install -e .
uv run hugger
```

### With Nix

```bash
nix develop        # dev shell: pinned Python + uv + build deps
nix run            # boot the server (uses uv under the hood)
```

On first run, if no password is set, a random one is generated and printed to
the console; you are **forced to change it on first login**. Afterwards, open
**Settings** to see the **API token** for the extension and to change your
password again. The password hash and app settings are stored in the SQLite DB.

In-progress downloads are persisted, so if the server restarts mid-download they
**resume automatically** on next start (`snapshot_download` continues partial files).

### Configuration (env vars)

| Var | Default | Purpose |
|---|---|---|
| `HUGGER_HOST` | `127.0.0.1` | Bind address. Set `0.0.0.0` only behind a TLS proxy. |
| `HUGGER_PORT` | `7860` | Port. |
| `HUGGER_PASSWORD` | — | Sets/updates the login password on startup. |
| `HUGGER_ARCHIVE_DIR` | `~/.hugger/archives` | Where snapshots are stored. |
| `HUGGER_HOME` | `~/.hugger` | Config + DB location. |
| `HUGGER_HTTPS_ONLY` | `false` | Send HSTS header (enable when served over HTTPS). |
| `HUGGER_ALLOWED_HOSTS` | `*` | Comma-separated Host allowlist (set your domain when public). |
| `HUGGER_ALLOWED_ORIGINS` | — | CORS origins for page-context API reads (the extension doesn't need this). |
| `HF_TOKEN` | — | HuggingFace token for gated/private repos. |

## Exposing to the internet

The app is built to be internet-facing:

- **Always** serve behind HTTPS (a reverse proxy like Caddy/nginx, or Cloudflare).
  Set `HUGGER_HTTPS_ONLY=true` and `HUGGER_ALLOWED_HOSTS=your.domain`.
- The web UI uses argon2-hashed passwords, signed session cookies, a per-IP login
  throttle, and CSRF tokens on every state-changing action.
- The JSON API (`/api/*`) is authenticated by a **Bearer token** (rotate it in Settings).
- Security headers (CSP, HSTS, X-Frame-Options, nosniff) are sent on every response.

## Browser extension

1. **Chrome/Edge**: `chrome://extensions` → enable Developer mode → *Load unpacked* → pick `extension/`.
2. **Firefox**: `about:debugging` → This Firefox → *Load Temporary Add-on* → pick `extension/manifest.json`.
3. Click the extension icon, enter your **Server URL** and **API token** (from Settings), Save & test.
4. Browse any model page on huggingface.co and click the floating **⤓ Archive** button.

The extension's background worker holds the token and makes the authenticated
call (so the server never needs to allow page-origin CORS). For a non-localhost
server it requests host permission for that origin when you save.

## API

All `/api/*` routes require `Authorization: Bearer <token>`.

| Method | Route | Body / returns |
|---|---|---|
| `GET` | `/api/ping` | `{ok, version}` |
| `POST` | `/api/archive` | `{repo_id, revision?}` → `{job_id, repo_id}` |
| `GET` | `/api/status/{job_id}` | job progress `{status, percent, …}` |
| `GET` | `/api/archives` | `{archives: [...]}` |

## Tests

```bash
python tests/test_core.py
```

## Notes / known simplifications

- Download progress is measured by polling bytes-on-disk vs. the repo's total
  size (`huggingface_hub` exposes no progress callback).
- Jobs live in memory (lost on restart); completed archives are persisted in SQLite.
- The login throttle is in-process — fine for a single worker; use Redis/`slowapi`
  if you run multiple workers.
