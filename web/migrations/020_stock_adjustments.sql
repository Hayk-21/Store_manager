-- A cashier correcting a stock count from the counter.
--
-- Stock drifts. A box arrives and nobody adds it, a customer returns a vape, two
-- of something were miscounted at the last close. The count on the screen then
-- disagrees with the shelf, and the cashier is the only person standing in front
-- of both.
--
-- Which is why this is a log and not just an UPDATE. A cashier who can quietly
-- change a number can quietly cover a shortfall, so every change writes a row
-- saying who moved what and when. The owner reads it on the report beside the
-- sales and the breakage. Nothing here touches money: a count going up is not
-- income and a count going down is not an expense — a vape that broke is a
-- write-off, which costs what it cost and has its own table.
--
-- delta is signed and never zero: a correction of nothing is not a correction.

CREATE TABLE stock_adjustments (
    id               bigserial PRIMARY KEY,
    owner_id         bigint NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    store_id         bigint NOT NULL,
    item_id          bigint NOT NULL,
    -- Null when the owner did it from the website rather than a cashier from the
    -- bot. The two are told apart by which of these is filled in.
    worker_id        bigint,
    work_session_id  bigint,
    store_session_id bigint,
    delta            int NOT NULL CHECK (delta <> 0),
    -- What the count became, so the report reads without recomputing history.
    count_after      int NOT NULL CHECK (count_after >= 0),
    note             text CHECK (note IS NULL OR length(note) <= 300),
    -- The bot's idempotency key, shared by every row of one batch: the action is
    -- the tap on "confirm", not the individual product. Deliberately not unique
    -- for that reason — a cashier who corrected four counts at once wrote four
    -- rows under one key. What stops a retry applying it twice is the lock on the
    -- worker's shift, held across the check and the write.
    external_id      text CHECK (external_id IS NULL OR length(external_id) BETWEEN 8 AND 128),
    created_at       timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (store_id, owner_id) REFERENCES stores  (id, owner_id) ON DELETE CASCADE,
    FOREIGN KEY (item_id,  owner_id) REFERENCES items   (id, owner_id) ON DELETE CASCADE,
    FOREIGN KEY (worker_id, owner_id) REFERENCES workers (id, owner_id) ON DELETE SET NULL,
    FOREIGN KEY (work_session_id, owner_id)
        REFERENCES work_sessions (id, owner_id) ON DELETE SET NULL,
    FOREIGN KEY (store_session_id, owner_id)
        REFERENCES store_sessions (id, owner_id) ON DELETE SET NULL
);

CREATE INDEX stock_adjustments_by_session ON stock_adjustments (store_session_id);
CREATE INDEX stock_adjustments_by_owner_time
    ON stock_adjustments (owner_id, created_at DESC);
-- Finding "has this batch already been applied", which the service asks while
-- holding the shift lock. Not unique: one batch is several rows under one key.
CREATE INDEX stock_adjustments_idem_idx
    ON stock_adjustments (owner_id, external_id) WHERE external_id IS NOT NULL;

COMMENT ON TABLE stock_adjustments IS
    'Corrections to a stock count that are not sales and not breakage. A log '
    'rather than a bare UPDATE so a count that changes always says who changed it.';
