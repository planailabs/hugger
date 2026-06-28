-- Multiple data stores, store ownership for archives, and richer jobs
-- (type + pause + move source/target) for resumable move/download jobs.
CREATE TABLE stores (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL UNIQUE,
    path       TEXT NOT NULL,
    is_default INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

ALTER TABLE archives ADD COLUMN store_id TEXT;

-- jobs gains: type (download|move), the target store, and (for moves) the source.
ALTER TABLE jobs ADD COLUMN type         TEXT NOT NULL DEFAULT 'download';
ALTER TABLE jobs ADD COLUMN store_id     TEXT;
ALTER TABLE jobs ADD COLUMN src_store_id TEXT;
