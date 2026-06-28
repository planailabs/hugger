-- Cache of computed local file hashes so update verification doesn't re-hash
-- unchanged files. Keyed by (repo_id, path); invalidated when size/mtime change.
CREATE TABLE file_hashes (
    repo_id TEXT NOT NULL,
    path    TEXT NOT NULL,
    size    INTEGER NOT NULL,
    mtime   REAL NOT NULL,
    algo    TEXT NOT NULL,   -- 'sha256' (LFS) | 'gitblob' (regular git blob sha1)
    hash    TEXT NOT NULL,
    PRIMARY KEY (repo_id, path)
);
