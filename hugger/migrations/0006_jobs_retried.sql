-- When an errored job is retried, the old row is marked status='retried' and
-- points at the new job's id.
ALTER TABLE jobs ADD COLUMN retried_by TEXT;
