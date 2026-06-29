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
nix build          # build the hugger package
```

### Docker

A minimal OCI image is built with nix's `dockerTools` (no Dockerfile/daemon):

```bash
nix build .#docker            # -> result (docker-archive .tar.gz)
docker load < result          # or: podman load -i result
docker run -p 7860:7860 -v hugger-data:/data hugger:latest
```

It runs the server on `0.0.0.0:7860`, stores everything under the `/data` volume
(`HUGGER_HOME=/data`), and bundles a CA bundle for HuggingFace TLS. CI builds and
pushes it to the registry via `docker-push.sh` (see `.gitlab-ci.yml`).

There's also a NixOS-in-Incus CI-runner image (`nix build .#image`) for
self-hosted GitLab runners.

### NixOS module

The flake exports `nixosModules.default`. In your system flake:

```nix
{
  inputs.hugger.url = "git+https://git.plan.ai/plan-ai/hugger";
  # ...
  outputs = { self, nixpkgs, hugger, ... }: {
    nixosConfigurations.myhost = nixpkgs.lib.nixosSystem {
      modules = [
        hugger.nixosModules.default
        {
          services.hugger = {
            enable = true;
            host = "127.0.0.1";          # behind a TLS reverse proxy
            port = 7860;
            httpsOnly = true;
            allowedHosts = [ "hugger.example.com" ];
            # Secrets (optional): EnvironmentFile with HUGGER_PASSWORD=… / HF_TOKEN=…
            # environmentFile = "/run/secrets/hugger.env";
          };
        }
      ];
    };
  };
}
```

State (DB, config, archives) lives in `/var/lib/hugger`. Without `HUGGER_PASSWORD`,
an initial password is generated and logged to the journal, and a change is forced
on first login.

On first run, if no password is set, a random one is generated and printed to
the console; you are **forced to change it on first login**. Afterwards, open
**Settings** to see the **API token** for the extension and to change your
password again. The password hash and app settings are stored in the SQLite DB.

In-progress downloads are persisted, so if the server restarts mid-download they
**resume automatically** on next start (`snapshot_download` continues partial files).

### Data stores

Models can be archived to multiple **data stores** (directories). The **default**
store lives in `~/.hugger/archives`; add more under **Stores** (each is checked
writable on creation), pick which store a download targets, set a new default, and
**move** an archived model between stores. Moves run as their own jobs (they're
large). Both download and move jobs can be **paused/resumed** and **auto-resume
after a restart** — downloads run in a subprocess that's terminated on pause
(huggingface_hub resumes the partial), and moves copy file-by-file (so partial
models move and moves resume).

Other store features:

- **Selective download** — archiving shows a file picker ("Download all" or pick
  files); the extension can archive all, archive selected, or grab a single file.
- **Disk-space check** — before a download/move, the target store must have room,
  counting other queued/running jobs so concurrent jobs can't overrun.
- **Metadata is a file** — each model dir has a `.hugger.json` (repo, sha,
  selected files + sizes) that is the source of truth; the DB is a rebuildable
  cache. **Import** a store to scan its folder and rebuild the catalog. Whether a
  file is downloaded is read from the filesystem (`GET /api/file-status`).
- **Manage files** — from the archives view, open **Manage** to download missing
  files or remove individual files. A **paused** download can also be moved, and
  its file selection edited (**Edit files**) before resuming; moving a paused
  download retargets it so resume continues in the new store. Moving a *running*
  download is refused until paused.
- **Update verification** — when an update is available, **Update…** verifies each
  file by size first, then hash (sha256 for LFS, git-blob sha1 for regular;
  cached in the DB), and offers to re-download only the changed/missing files, or
  all of them. (`GET /api/verify/{repo_id}`)

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
| `HF_TOKEN` | — | HuggingFace token for gated/private repos (or set it in **Settings**, which takes precedence). |

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
4. Browse any model page on huggingface.co. The floating control reflects server state:
   **⤓ Archive** when not stored, or **✓ Archived** with **🗑 Remove** (and **⟳ Update**
   when a newer revision exists) once it's archived.

The extension's background worker holds the token and makes the authenticated
call (so the server never needs to allow page-origin CORS). It holds http/https
host access (granted at install) so it can reach **any** server you configure —
localhost or a public HTTPS deployment — with no per-origin permission step. The manifest
ships both `background.service_worker` (Chrome) and `background.scripts` (Firefox)
so the same unpacked extension loads in either browser. It also declares
`gecko_android`, so AMO offers it for **Firefox for Android** (test on a device
with `web-ext run --target=firefox-android`).

### Publishing

`scripts/publish.sh` builds **per-store** packages (so neither store's validator
warns about the other's background key) and uploads them:

```bash
nix develop .#publish --command scripts/publish.sh all            # Chrome + Firefox + Edge
nix develop .#publish --command scripts/publish.sh all --build-only   # just build dist/extension/*.zip
nix develop .#publish --command scripts/publish.sh firefox        # one store
```

It uses Mozilla's `web-ext` (Firefox/AMO), `chrome-webstore-upload-cli` (Chrome),
and the Edge Add-ons REST API (curl). Credentials come from env:

| Store | Env vars |
|---|---|
| Chrome | `CHROME_EXTENSION_ID` `CHROME_CLIENT_ID` `CHROME_CLIENT_SECRET` `CHROME_REFRESH_TOKEN` |
| Firefox | `WEB_EXT_API_KEY` `WEB_EXT_API_SECRET` |
| Edge | `EDGE_PRODUCT_ID` `EDGE_API_KEY` `EDGE_CLIENT_ID` |

## API

All `/api/*` routes require `Authorization: Bearer <token>`.

| Method | Route | Body / returns |
|---|---|---|
| `GET` | `/api/ping` | `{ok, version}` |
| `POST` | `/api/archive` | `{repo_id, revision?}` → `{job_id, repo_id}` |
| `GET` | `/api/status/{job_id}` | job progress `{status, percent, …}` |
| `GET` | `/api/archives` | `{archives: [...]}` |
| `GET` | `/api/archive/{repo_id}` | `{archived, sha?, size_bytes?, update_available?}` |
| `DELETE` | `/api/archive/{repo_id}` | `{ok, repo_id}` |
| `GET` | `/api/files?repo_id=&revision=` | `{sha, files:[{path,size,downloaded}]}` |
| `GET` | `/api/file-status?repo_id=&path=` | `{archived, downloaded}` (FS-checked) |
| `GET` | `/api/stores` | `{stores:[...]}` |
| `POST` | `/api/stores/{id}/import` | `{ok, imported}` |
| `POST` | `/api/jobs/{id}/pause` · `/resume` | `{ok}` |

`POST /api/archive` accepts an optional `files: [paths]` (selective) and `store_id`.

## Tests

```bash
# Python unit + HTTP integration + live Hub (live tests skip when offline)
python tests/test_core.py && python tests/test_api.py && python tests/test_hub_live.py

# Everything, including the NixOS VM E2E tests (needs nix + KVM):
nix develop .#test --command ./scripts/test-all.sh   # or: nix run .#test
nix build .#checks.x86_64-linux.vm -L                # API download test
nix build .#checks.x86_64-linux.browser -L           # Chromium + Firefox E2E
```

Both VM tests boot the NixOS module and talk to a **fake HuggingFace Hub served
over self-signed HTTPS** — trusted by the VM via `security.pki.certificateFiles`
and reached through an `/etc/hosts` override of `huggingface.co`
(`nixos/fake_hf_hub.py`).

- **`vm`** (`nixos/test.nix`) — changes the autogenerated password, then downloads
  a model through the token API.
- **`browser`** (`nixos/browser_test.nix`) — loads the extension into real
  **Chromium and Firefox** (temp profiles), confirms its button appears on a
  HuggingFace model page, and drives the full archive → remove cycle via
  Selenium (`nixos/browser_check.py`).

`tests/test_hub_live.py` is the only suite that touches the **real** Hub (search
+ a tiny-model download); it skips when offline. The VM tests always use the fake
hub. The `nix develop .#test` shell provides python+selenium and both browsers for
running `nixos/browser_check.py` directly.

## Notes / known simplifications

- Download progress is measured by polling bytes-on-disk vs. the repo's total
  size (`huggingface_hub` exposes no progress callback).
- Jobs live in memory (lost on restart); completed archives are persisted in SQLite.
- The login throttle is in-process — fine for a single worker; use Redis/`slowapi`
  if you run multiple workers.
