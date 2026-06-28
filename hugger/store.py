"""SQLite persistence. Schema is owned by yoyo-migrations (hugger/migrations).

A fresh connection is opened per call. SQLite handles file locking, and this
sidesteps cross-thread connection sharing (handlers run in a threadpool, download
jobs run in their own threads). Fine for a localhost/low-traffic tool.
ponytail: per-call connections; add a pool only if profiling shows it matters.
"""
from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from yoyo import get_backend, read_migrations

from .config import DB_PATH

_MIGRATIONS_DIR = Path(__file__).parent / "migrations"
_migrated = False


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def run_migrations() -> None:
    """Apply any pending yoyo migrations. Idempotent."""
    global _migrated
    backend = get_backend(f"sqlite:///{DB_PATH}")
    migrations = read_migrations(str(_MIGRATIONS_DIR))
    with backend.lock():
        backend.apply_migrations(backend.to_apply(migrations))
    _migrated = True


def _connect() -> sqlite3.Connection:
    if not _migrated:
        run_migrations()
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


# --- CRUD ----------------------------------------------------------------

def upsert_archive(repo_id: str, revision: str, sha: str, path: str, size_bytes: int) -> None:
    with closing(_connect()) as conn, conn:
        conn.execute(
            """INSERT INTO archives
                 (repo_id, revision, sha, path, size_bytes, archived_at, last_checked, update_available, remote_sha)
               VALUES (?,?,?,?,?,?,?,0,?)
               ON CONFLICT(repo_id) DO UPDATE SET
                 revision=excluded.revision, sha=excluded.sha, path=excluded.path,
                 size_bytes=excluded.size_bytes, archived_at=excluded.archived_at,
                 last_checked=excluded.last_checked, update_available=0, remote_sha=excluded.remote_sha""",
            (repo_id, revision, sha, path, size_bytes, _now(), _now(), sha),
        )


def list_archives() -> list[dict]:
    with closing(_connect()) as conn:
        rows = conn.execute("SELECT * FROM archives ORDER BY archived_at DESC").fetchall()
        return [dict(r) for r in rows]


def get_archive(repo_id: str) -> dict | None:
    with closing(_connect()) as conn:
        row = conn.execute("SELECT * FROM archives WHERE repo_id=?", (repo_id,)).fetchone()
        return dict(row) if row else None


def set_update_status(repo_id: str, remote_sha: str, update_available: bool) -> None:
    with closing(_connect()) as conn, conn:
        conn.execute(
            "UPDATE archives SET last_checked=?, remote_sha=?, update_available=? WHERE repo_id=?",
            (_now(), remote_sha, 1 if update_available else 0, repo_id),
        )


def delete_archive(repo_id: str) -> None:
    with closing(_connect()) as conn, conn:
        conn.execute("DELETE FROM archives WHERE repo_id=?", (repo_id,))
