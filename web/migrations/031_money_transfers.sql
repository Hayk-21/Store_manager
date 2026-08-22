-- Cash moving from one shop's drawer to another's.
--
-- One shop needs change and a sister shop is sitting on the day's takings, so
-- somebody carries an envelope across. Until now the only way to record that was
-- as a withdrawal at one end and nothing at all at the other: the money left a
-- drawer the books could account for and arrived in one they could not, and the
-- receiving shop's count came up over with no explanation attached.
--
-- The row is a movement at one end and a request at the other. The cash genuinely
-- leaves the sender's drawer the moment they hand it over — so their withdrawal is
-- booked at once, or their own count tonight would be short — but it must not
-- appear in the receiving till before it is physically there, or the person
-- counting that drawer is held to money still in somebody's pocket. So it waits at
-- 'pending' until a worker at the destination says it arrived.
--
-- 'rejected' is the envelope that did not turn up. It puts the money back in the
-- sending shop's drawer, which is the only honest place for it: nothing was
-- spent, so nothing should have left the books.

CREATE TABLE money_transfers (
    id               bigserial PRIMARY KEY,
    owner_id         bigint NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    from_store_id    bigint NOT NULL,
    to_store_id      bigint NOT NULL,
    amount           numeric(12,2) NOT NULL CHECK (amount > 0),
    -- 'pending'  — sent, nobody at the destination has said it arrived.
    -- 'received' — confirmed, and the deposit booked in the same step. An
    --              acknowledgement that did not credit the till would be a promise,
    --              and the drawer would disagree with the screen.
    -- 'rejected' — it did not arrive, and the money is back where it came from.
    status           text NOT NULL DEFAULT 'pending'
                     CHECK (status IN ('pending', 'received', 'rejected')),
    -- The session the money left, and the one it landed in. The second is null
    -- until somebody confirms, and stays null on a rejection: nothing landed.
    from_session_id  bigint NOT NULL,
    to_session_id    bigint,
    sent_by_worker_id     bigint,
    decided_by_worker_id  bigint,
    decided_at       timestamptz,
    external_id      text CHECK (external_id IS NULL OR length(external_id) BETWEEN 8 AND 128),
    created_at       timestamptz NOT NULL DEFAULT now(),
    -- Sending money to yourself is a no-op that would still print in the history.
    CHECK (from_store_id <> to_store_id),
    -- A decision has a time, and a time has a decision.
    CHECK ((status = 'pending') = (decided_at IS NULL)),
    -- Only a confirmed transfer landed anywhere.
    CHECK ((to_session_id IS NULL) = (status <> 'received')),
    FOREIGN KEY (from_store_id, owner_id) REFERENCES stores (id, owner_id) ON DELETE CASCADE,
    FOREIGN KEY (to_store_id,   owner_id) REFERENCES stores (id, owner_id) ON DELETE CASCADE,
    FOREIGN KEY (from_session_id, owner_id)
        REFERENCES store_sessions (id, owner_id) ON DELETE CASCADE,
    FOREIGN KEY (to_session_id, owner_id)
        REFERENCES store_sessions (id, owner_id) ON DELETE CASCADE,
    FOREIGN KEY (sent_by_worker_id, owner_id)
        REFERENCES workers (id, owner_id) ON DELETE SET NULL,
    FOREIGN KEY (decided_by_worker_id, owner_id)
        REFERENCES workers (id, owner_id) ON DELETE SET NULL
);

-- "What is waiting to be confirmed at my shop", which is the question the bot asks
-- whenever the transfers screen is opened.
CREATE INDEX money_transfers_pending_at_destination
    ON money_transfers (to_store_id) WHERE status = 'pending';
CREATE INDEX money_transfers_by_owner_time ON money_transfers (owner_id, created_at DESC);
CREATE UNIQUE INDEX money_transfers_idem_idx
    ON money_transfers (owner_id, external_id) WHERE external_id IS NOT NULL;

COMMENT ON TABLE money_transfers IS
    'Cash carried from one of an owner''s shops to another. The withdrawal is '
    'booked when it is sent; the deposit only when somebody at the destination '
    'confirms it arrived.';
