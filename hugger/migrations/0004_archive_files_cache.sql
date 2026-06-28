-- The .hugger.json file in each model dir is the source of truth; these columns
-- cache its summary in the DB for fast listing. size_bytes stays = bytes on disk.
ALTER TABLE archives ADD COLUMN total_bytes   INTEGER NOT NULL DEFAULT 0;
ALTER TABLE archives ADD COLUMN n_files       INTEGER NOT NULL DEFAULT 0;
ALTER TABLE archives ADD COLUMN n_downloaded  INTEGER NOT NULL DEFAULT 0;
ALTER TABLE archives ADD COLUMN complete      INTEGER NOT NULL DEFAULT 1;
