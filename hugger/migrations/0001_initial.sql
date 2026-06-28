-- Initial schema: archived models.
CREATE TABLE archives (
    repo_id          TEXT PRIMARY KEY,
    revision         TEXT NOT NULL DEFAULT 'main',
    sha              TEXT NOT NULL,
    path             TEXT NOT NULL,
    size_bytes       INTEGER NOT NULL DEFAULT 0,
    archived_at      TEXT NOT NULL,
    last_checked     TEXT,
    update_available INTEGER NOT NULL DEFAULT 0,
    remote_sha       TEXT
);
