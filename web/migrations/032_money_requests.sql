-- Asking for cash, rather than being sent it.
--
-- The drawer runs dry. It is the wage that exposes it: the till pays as far as it
-- reaches and the rest becomes a debt, so a worker locking up on 2,000 with a 5,000
-- wage due goes home short and the shop opens tomorrow with nothing to give change
-- from. The money exists — it is in a sister shop's drawer, or in the owner's pocket
-- — and there was no way to say so from behind the counter.
--
-- So a request is the mirror of the transfer added beside it. It is a request rather
-- than a command for the same reason a stock request is: a cashier cannot reach into
-- a drawer they are not standing at, and the owner answers to nobody at all. Whoever
-- is asked says yes or no, and saying yes is what makes the money move.
--
-- **An accepted request becomes a transfer.** It has to: money asked for and money
-- sent settle identically at the receiving end — it arrives in somebody's hand and
-- the till only rises when they say so — and two tables answering "is it here yet"
-- would eventually disagree. So this table holds the asking, and points at the
-- ``money_transfers`` row the answer created.

-- Money can now come from the owner, who has no shop and no session. Both columns
-- were NOT NULL because until now every transfer had a drawer behind it.
ALTER TABLE money_transfers ALTER COLUMN from_store_id   DROP NOT NULL;
ALTER TABLE money_transfers ALTER COLUMN from_session_id DROP NOT NULL;

-- A source is a shop and its open session, or it is neither.
ALTER TABLE money_transfers ADD CONSTRAINT money_from_a_shop_has_a_session
    CHECK ((from_store_id IS NULL) = (from_session_id IS NULL));

-- Still never to itself, but "no source" is not "the same source". The name is the
-- one Postgres gave the unnamed table CHECK in 031 — the first of three, hence no
-- suffix; `\d money_transfers` confirms it.
ALTER TABLE money_transfers DROP CONSTRAINT money_transfers_check;
ALTER TABLE money_transfers ADD CONSTRAINT money_goes_somewhere_else
    CHECK (from_store_id IS NULL OR from_store_id <> to_store_id);

COMMENT ON COLUMN money_transfers.from_store_id IS
    'The shop the cash left. NULL when it came from the owner, who has no drawer to '
    'take it out of — nothing is booked at that end.';


CREATE TABLE money_requests (
    id               bigserial PRIMARY KEY,
    owner_id         bigint NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    -- The shop doing the asking, and the session it was asking during.
    to_store_id      bigint NOT NULL,
    to_session_id    bigint NOT NULL,
    amount           numeric(12,2) NOT NULL CHECK (amount > 0),
    -- Who is being asked: another shop, or the owner. Exactly one, because "ask
    -- somebody" with nobody named is a request that can never be answered.
    asked_of_store_id bigint,
    asked_the_owner  boolean NOT NULL DEFAULT false,
    CHECK ((asked_of_store_id IS NULL) = asked_the_owner),
    -- And never itself: a shop asking its own drawer for money is the drawer it has
    -- just found to be empty.
    CHECK (asked_of_store_id IS NULL OR asked_of_store_id <> to_store_id),
    -- 'pending'  — asked, nobody has answered.
    -- 'accepted' — agreed, and the transfer created in the same step. An acceptance
    --              that moved nothing would be a promise the asking shop could not
    --              tell from a refusal.
    -- 'rejected' — no.
    status           text NOT NULL DEFAULT 'pending'
                     CHECK (status IN ('pending', 'accepted', 'rejected')),
    -- What the acceptance created. Null while pending and on a refusal.
    transfer_id      bigint REFERENCES money_transfers (id) ON DELETE SET NULL,
    requested_by_worker_id bigint,
    -- Who answered. A worker at the shop being asked, or the owner — and the boolean
    -- says which, rather than being inferred from an empty column.
    decided_by_worker_id   bigint,
    decided_by_owner boolean NOT NULL DEFAULT false,
    decided_at       timestamptz,
    external_id      text CHECK (external_id IS NULL OR length(external_id) BETWEEN 8 AND 128),
    created_at       timestamptz NOT NULL DEFAULT now(),
    -- A decision has a time, and a time has a decision.
    CHECK ((status = 'pending') = (decided_at IS NULL)),
    -- Only an accepted request moved anything.
    CHECK (transfer_id IS NULL OR status = 'accepted'),
    FOREIGN KEY (to_store_id, owner_id) REFERENCES stores (id, owner_id) ON DELETE CASCADE,
    FOREIGN KEY (to_session_id, owner_id)
        REFERENCES store_sessions (id, owner_id) ON DELETE CASCADE,
    FOREIGN KEY (asked_of_store_id, owner_id)
        REFERENCES stores (id, owner_id) ON DELETE CASCADE,
    FOREIGN KEY (requested_by_worker_id, owner_id)
        REFERENCES workers (id, owner_id) ON DELETE SET NULL,
    FOREIGN KEY (decided_by_worker_id, owner_id)
        REFERENCES workers (id, owner_id) ON DELETE SET NULL
);

-- "What is waiting for an answer at my shop", asked every time the transfers screen
-- is opened.
CREATE INDEX money_requests_pending_at_source
    ON money_requests (asked_of_store_id) WHERE status = 'pending';
-- And the owner's own list, which is every shop's asking at once.
CREATE INDEX money_requests_pending_for_owner
    ON money_requests (owner_id) WHERE status = 'pending' AND asked_the_owner;
CREATE INDEX money_requests_by_owner_time ON money_requests (owner_id, created_at DESC);
CREATE UNIQUE INDEX money_requests_idem_idx
    ON money_requests (owner_id, external_id) WHERE external_id IS NOT NULL;

COMMENT ON TABLE money_requests IS
    'A shop asking another shop, or the owner, for cash. Accepting creates the '
    'money_transfers row that actually moves it.';
