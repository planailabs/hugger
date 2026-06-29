"""SQLite persistence. Schema is owned by yoyo-migrations (hugger/migrations).

A fresh connection is opened per call. SQLite handles file locking, and this
sidesteps cross-thread connection sharing (handlers run in a threadpool, jobs run
in their own threads). Fine for a localhost/low-traffic tool.
ponytail: per-call connections; add a pool only if profiling shows it matters.
"""
from __future__ import annotations

import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from yoyo import get_backend, read_migrations

from .config import DB_PATH

_MIGRATIONS_DIR = Path(__file__).parent / "migrations"
_migrated = False


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _id() -> str:
    return uuid.uuid4().hex[:12]


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


# --- archives ------------------------------------------------------------

def upsert_archive(repo_id: str, revision: str, sha: str, path: str,
                   size_bytes: int, store_id: str, total_bytes: int | None = None,
                   n_files: int = 0, n_downloaded: int = 0, complete: bool = True) -> None:
    """Cache an archive row. The .hugger.json file is the source of truth; this is
    a rebuildable cache (see import_store)."""
    if total_bytes is None:
        total_bytes = size_bytes
    with closing(_connect()) as conn, conn:
        conn.execute(
            """INSERT INTO archives
                 (repo_id, revision, sha, path, size_bytes, archived_at, last_checked,
                  update_available, remote_sha, store_id, total_bytes, n_files,
                  n_downloaded, complete)
               VALUES (?,?,?,?,?,?,?,0,?,?,?,?,?,?)
               ON CONFLICT(repo_id) DO UPDATE SET
                 revision=excluded.revision, sha=excluded.sha, path=excluded.path,
                 size_bytes=excluded.size_bytes, archived_at=excluded.archived_at,
                 last_checked=excluded.last_checked, update_available=0,
                 remote_sha=excluded.remote_sha, store_id=excluded.store_id,
                 total_bytes=excluded.total_bytes, n_files=excluded.n_files,
                 n_downloaded=excluded.n_downloaded, complete=excluded.complete""",
            (repo_id, revision, sha, path, size_bytes, _now(), _now(), sha, store_id,
             total_bytes, n_files, n_downloaded, 1 if complete else 0),
        )


def _archive_select() -> str:
    return ("SELECT a.*, s.name AS store_name FROM archives a "
            "LEFT JOIN stores s ON a.store_id = s.id ")


def list_archives() -> list[dict]:
    with closing(_connect()) as conn:
        rows = conn.execute(_archive_select() + "ORDER BY a.archived_at DESC").fetchall()
        return [dict(r) for r in rows]


def get_archive(repo_id: str) -> dict | None:
    with closing(_connect()) as conn:
        row = conn.execute(_archive_select() + "WHERE a.repo_id=?", (repo_id,)).fetchone()
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


# --- data stores ---------------------------------------------------------

def ensure_default_store(path: str) -> str:
    """Create the default store on first run and backfill archives that predate
    multi-store. Returns the default store id."""
    with closing(_connect()) as conn, conn:
        row = conn.execute("SELECT id FROM stores WHERE is_default=1").fetchone()
        if row:
            sid = row["id"]
        else:
            sid = _id()
            conn.execute(
                "INSERT INTO stores (id, name, path, is_default, created_at) VALUES (?,?,?,1,?)",
                (sid, "default", path, _now()),
            )
        conn.execute("UPDATE archives SET store_id=? WHERE store_id IS NULL", (sid,))
        return sid


def _norm(path: str) -> str:
    return str(Path(path).expanduser().resolve())


def _overlaps(a: str, b: str) -> bool:
    """True if normalized paths are equal or one contains the other."""
    pa, pb = Path(a), Path(b)
    return pa == pb or pa.is_relative_to(pb) or pb.is_relative_to(pa)


def path_conflict(path: str, exclude_id: str | None = None) -> dict | None:
    """Return an existing store whose path equals/overlaps `path`, else None.
    Two stores sharing a path break moves (src == dest), so this is enforced."""
    np = _norm(path)
    for s in list_stores():
        if s["id"] == exclude_id:
            continue
        if _overlaps(np, _norm(s["path"])):
            return s
    return None


def add_store(name: str, path: str) -> str:
    npath = _norm(path)
    conflict = path_conflict(npath)
    if conflict:
        raise ValueError(f"path overlaps existing store '{conflict['name']}' ({conflict['path']})")
    sid = _id()
    with closing(_connect()) as conn, conn:
        conn.execute(
            "INSERT INTO stores (id, name, path, is_default, created_at) VALUES (?,?,?,0,?)",
            (sid, name, npath, _now()),
        )
    return sid


def list_stores() -> list[dict]:
    with closing(_connect()) as conn:
        rows = conn.execute(
            "SELECT s.*, (SELECT COUNT(*) FROM archives a WHERE a.store_id=s.id) AS n_models "
            "FROM stores s ORDER BY s.is_default DESC, s.name"
        ).fetchall()
        return [dict(r) for r in rows]


def get_store(store_id: str) -> dict | None:
    with closing(_connect()) as conn:
        row = conn.execute("SELECT * FROM stores WHERE id=?", (store_id,)).fetchone()
        return dict(row) if row else None


def get_default_store() -> dict | None:
    with closing(_connect()) as conn:
        row = conn.execute("SELECT * FROM stores WHERE is_default=1").fetchone()
        return dict(row) if row else None


def set_default_store(store_id: str) -> None:
    with closing(_connect()) as conn, conn:
        conn.execute("UPDATE stores SET is_default=0")
        conn.execute("UPDATE stores SET is_default=1 WHERE id=?", (store_id,))


def store_model_count(store_id: str) -> int:
    with closing(_connect()) as conn:
        return conn.execute("SELECT COUNT(*) FROM archives WHERE store_id=?", (store_id,)).fetchone()[0]


def delete_store(store_id: str) -> None:
    with closing(_connect()) as conn, conn:
        conn.execute("DELETE FROM stores WHERE id=? AND is_default=0", (store_id,))


# --- settings (key/value) ------------------------------------------------

def get_setting(key: str) -> str | None:
    with closing(_connect()) as conn:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None


def set_setting(key: str, value: str) -> None:
    with closing(_connect()) as conn, conn:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )


def delete_setting(key: str) -> None:
    with closing(_connect()) as conn, conn:
        conn.execute("DELETE FROM settings WHERE key=?", (key,))


# --- jobs (persisted for restart resume) ---------------------------------

def save_job(d: dict) -> None:
    with closing(_connect()) as conn, conn:
        conn.execute(
            """INSERT INTO jobs
                 (id, repo_id, revision, type, status, total_bytes, sha, error,
                  store_id, src_store_id, created_at, updated_at)
               VALUES (:id,:repo_id,:revision,:type,:status,:total_bytes,:sha,:error,
                       :store_id,:src_store_id,:now,:now)
               ON CONFLICT(id) DO UPDATE SET
                 status=excluded.status, total_bytes=excluded.total_bytes, sha=excluded.sha,
                 error=excluded.error, store_id=excluded.store_id,
                 src_store_id=excluded.src_store_id, updated_at=excluded.updated_at""",
            {
                "id": d["id"], "repo_id": d["repo_id"], "revision": d.get("revision", "main"),
                "type": d.get("type", "download"), "status": d["status"],
                "total_bytes": d.get("total_bytes", 0), "sha": d.get("sha"),
                "error": d.get("error"), "store_id": d.get("store_id"),
                "src_store_id": d.get("src_store_id"), "now": _now(),
            },
        )


def delete_archive_and_hashes(repo_id: str) -> None:
    with closing(_connect()) as conn, conn:
        conn.execute("DELETE FROM archives WHERE repo_id=?", (repo_id,))
        conn.execute("DELETE FROM file_hashes WHERE repo_id=?", (repo_id,))


# --- file hash cache -----------------------------------------------------

def get_file_hash(repo_id: str, path: str) -> dict | None:
    with closing(_connect()) as conn:
        row = conn.execute(
            "SELECT * FROM file_hashes WHERE repo_id=? AND path=?", (repo_id, path)
        ).fetchone()
        return dict(row) if row else None


def set_file_hash(repo_id: str, path: str, size: int, mtime: float, algo: str, h: str) -> None:
    with closing(_connect()) as conn, conn:
        conn.execute(
            """INSERT INTO file_hashes (repo_id, path, size, mtime, algo, hash)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(repo_id, path) DO UPDATE SET
                 size=excluded.size, mtime=excluded.mtime, algo=excluded.algo, hash=excluded.hash""",
            (repo_id, path, size, mtime, algo, h),
        )


def delete_file_hash(repo_id: str, path: str) -> None:
    with closing(_connect()) as conn, conn:
        conn.execute("DELETE FROM file_hashes WHERE repo_id=? AND path=?", (repo_id, path))


def list_jobs(statuses: list[str]) -> list[dict]:
    placeholders = ",".join("?" * len(statuses))
    with closing(_connect()) as conn:
        rows = conn.execute(
            f"SELECT * FROM jobs WHERE status IN ({placeholders}) ORDER BY created_at", statuses
        ).fetchall()
        return [dict(r) for r in rows]


def recent_jobs(limit: int = 200) -> list[dict]:
    with closing(_connect()) as conn:
        rows = conn.execute(
            "SELECT * FROM jobs ORDER BY updated_at DESC, created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


def prune_jobs(max_age_days: int = 30) -> int:
    """Delete finished (done/error) jobs older than max_age_days. Returns count."""
    from datetime import timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(days=max_age_days)).isoformat()
    with closing(_connect()) as conn, conn:
        cur = conn.execute(
            "DELETE FROM jobs WHERE status IN ('done','error') AND updated_at < ?", (cutoff,)
        )
        return cur.rowcount


def delete_finished_jobs() -> int:
    with closing(_connect()) as conn, conn:
        cur = conn.execute("DELETE FROM jobs WHERE status IN ('done','error')")
        return cur.rowcount
