-- Let an owner remove a worker from the list without erasing what they did.
--
-- A hard DELETE is not an option once somebody has worked: work_sessions, sales
-- and cash_movements all reference workers ON DELETE CASCADE, so deleting the
-- row would take every shift and receipt with it and quietly rewrite the
-- reports. Archiving keeps the history and takes them off the page.
--
-- A worker who never actually started is deleted outright; there is nothing to
-- preserve. The application decides which case applies.

ALTER TABLE workers ADD COLUMN archived_at timestamptz;

-- Archiving releases the @username so it can be registered again -- somebody
-- leaves, their handle gets reused, and the new person must be able to claim it.
-- That means an archived row has neither identifier, which the constraint added
-- in 003 forbade. Relax it to apply only while the worker is live.
ALTER TABLE workers DROP CONSTRAINT workers_identifiable;
ALTER TABLE workers ADD CONSTRAINT workers_identifiable CHECK (
    archived_at IS NOT NULL
    OR telegram_id IS NOT NULL
    OR telegram_username IS NOT NULL
);

CREATE INDEX workers_owner_live_idx ON workers (owner_id) WHERE archived_at IS NULL;
