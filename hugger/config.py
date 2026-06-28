"""Configuration + secret persistence for hugger.

All secrets (session signing key, extension API token, password hash) live in
~/.hugger/config.json with 0600 perms. Env vars override at startup so the app
can be driven by a process manager / container without editing the file.
"""
from __future__ import annotations

import json
import os
import secrets
from pathlib import Path

BASE_DIR = Path(os.environ.get("HUGGER_HOME", Path.home() / ".hugger"))
ARCHIVE_DIR = Path(os.environ.get("HUGGER_ARCHIVE_DIR", BASE_DIR / "archives"))
DB_PATH = BASE_DIR / "hugger.db"
CONFIG_PATH = BASE_DIR / "config.json"


def _bool_env(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


class Config:
    """Loaded once at startup. Generates+persists secrets on first run."""

    def __init__(self) -> None:
        BASE_DIR.mkdir(parents=True, exist_ok=True)
        ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
        self._data = self._load()

        # Generate persistent secrets on first run.
        changed = False
        if not self._data.get("secret_key"):
            self._data["secret_key"] = secrets.token_urlsafe(48)
            changed = True
        if not self._data.get("api_token"):
            self._data["api_token"] = secrets.token_urlsafe(32)
            changed = True
        if changed:
            self.save()

        # Runtime-only settings (env driven, not persisted).
        self.host = os.environ.get("HUGGER_HOST", "127.0.0.1")
        self.port = int(os.environ.get("HUGGER_PORT", "7860"))
        # Comma-separated; '*' allows any Host header (fine for localhost only).
        self.allowed_hosts = [
            h.strip() for h in os.environ.get("HUGGER_ALLOWED_HOSTS", "*").split(",") if h.strip()
        ]
        # CORS origins permitted to read /api responses from a *page* context.
        # The browser extension uses a background fetch (host_permissions) and is
        # NOT subject to CORS, so this can stay empty when only the extension calls in.
        self.allowed_origins = [
            o.strip() for o in os.environ.get("HUGGER_ALLOWED_ORIGINS", "").split(",") if o.strip()
        ]
        # Set True when served over HTTPS (directly or behind a TLS proxy).
        self.https_only = _bool_env("HUGGER_HTTPS_ONLY", False)
        self.hf_token = (
            os.environ.get("HF_TOKEN")
            or os.environ.get("HUGGING_FACE_HUB_TOKEN")
            or None
        )

    # --- persisted fields -------------------------------------------------
    @property
    def secret_key(self) -> str:
        return self._data["secret_key"]

    @property
    def api_token(self) -> str:
        return self._data["api_token"]

    def set_api_token(self, token: str) -> None:
        self._data["api_token"] = token
        self.save()

    @property
    def password_hash(self) -> str | None:
        return self._data.get("password_hash")

    def set_password_hash(self, h: str) -> None:
        self._data["password_hash"] = h
        self.save()

    # --- io ---------------------------------------------------------------
    def _load(self) -> dict:
        if CONFIG_PATH.exists():
            return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        return {}

    def save(self) -> None:
        CONFIG_PATH.write_text(json.dumps(self._data, indent=2), encoding="utf-8")
        try:  # ponytail: best-effort 0600; chmod is a no-op on some Windows FS
            os.chmod(CONFIG_PATH, 0o600)
        except OSError:
            pass


cfg = Config()
