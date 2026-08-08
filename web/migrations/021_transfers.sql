-- Moving stock from one shop to another.
--
-- A shop runs out of something a sister shop has a box of, and somebody drives it
-- across. Until now that could only be recorded as two lies: a write-off at one
-- end and a correction at the other, which reads as breakage in the statistics and
-- as a miscount in the log.
--
-- The row is a request first and a movement second, because the two shops are two
-- people. The cashier who wants the stock cannot reach into a shelf they are not
-- standing at and take it — somebody there has to agree that the box exists and
-- that it left. So a transfer asked for from the bot waits at 'pending' until a
-- worker at the source store approves it, and only then does any count change.
--
-- The owner has no such problem: they can see both shelves at once and answer to
-- nobody, so a transfer made on the website applies immediately and is recorded as
-- already approved by them.
--
-- Nothing here touches money. Stock moving between two shops the same person owns
-- earns nothing and costs nothing; it is the same box on a different shelf. Which
-- is also why the cost price travels with it: valuing the stock at the receiving
-- shop's guess would quietly change what the business is worth.

CREATE TABLE transfers (
    id                bigserial PRIMARY KEY,
    owner_id          bigint NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    item_id           bigint NOT NULL,
    from_store_id     bigint NOT NULL,
    to_store_id       bigint NOT NULL,
    quantity          int NOT NULL CHECK (quantity > 0),
    -- 'pending'  — asked for, nobody at the source has answered yet.
    -- 'approved' — agreed and applied. The two are one step: an approval that did
    --              not move the stock would be a promise, and the shelf would
    --              disagree with the screen until somebody noticed.
    -- 'rejected' — the box is not there, or is not going.
    --
    -- Three, not four: there is deliberately no 'cancelled'. Withdrawing a request
    -- is a reasonable thing to want and nothing offers it yet, and a status no code
    -- can produce is a claim the schema cannot back up. Adding it later is one line.
    status            text NOT NULL DEFAULT 'pending'
                      CHECK (status IN ('pending', 'approved', 'rejected')),
    -- Who asked. Null when the owner moved it themselves from the website.
    requested_by_worker_id bigint,
    -- Who answered, and when. Null while pending, and null forever on a transfer
    -- the owner made — nobody was asked.
    decided_by_worker_id   bigint,
    decided_at        timestamptz,
    -- Set when the owner did it, so the two paths are told apart in the history
    -- rather than inferred from which worker column is empty.
    decided_by_owner  boolean NOT NULL DEFAULT false,
    -- What it was worth as it left, frozen like every other stock snapshot here.
    unit_cost         numeric(12,2) NOT NULL DEFAULT 0 CHECK (unit_cost >= 0),
    note              text CHECK (note IS NULL OR length(note) <= 300),
    external_id       text CHECK (external_id IS NULL OR length(external_id) BETWEEN 8 AND 128),
    created_at        timestamptz NOT NULL DEFAULT now(),
    -- The two shops must be different. A transfer to itself is a no-op that would
    -- still print in the history and still confuse whoever read it.
    CHECK (from_store_id <> to_store_id),
    -- A decision has a time, and a time has a decision.
    CHECK ((status = 'pending') = (decided_at IS NULL)),
    FOREIGN KEY (item_id,       owner_id) REFERENCES items  (id, owner_id) ON DELETE CASCADE,
    FOREIGN KEY (from_store_id, owner_id) REFERENCES stores (id, owner_id) ON DELETE CASCADE,
    FOREIGN KEY (to_store_id,   owner_id) REFERENCES stores (id, owner_id) ON DELETE CASCADE,
    FOREIGN KEY (requested_by_worker_id, owner_id)
        REFERENCES workers (id, owner_id) ON DELETE SET NULL,
    FOREIGN KEY (decided_by_worker_id, owner_id)
        REFERENCES workers (id, owner_id) ON DELETE SET NULL
);

-- "What is waiting for an answer at my shop", which is the question the bot asks
-- on every /start and every approval screen.
CREATE INDEX transfers_pending_at_source
    ON transfers (from_store_id) WHERE status = 'pending';
CREATE INDEX transfers_by_owner_time ON transfers (owner_id, created_at DESC);
CREATE UNIQUE INDEX transfers_idem_idx
    ON transfers (owner_id, external_id) WHERE external_id IS NOT NULL;

COMMENT ON TABLE transfers IS
    'Stock moved between two of one owner''s shops. A request until a worker at '
    'the source approves it; immediate when the owner does it from the website.';
