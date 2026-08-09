-- What was actually in the drawer, counted by hand.
--
-- Everything else here is what the books say: cash is a SUM over cash_movements
-- for the open session, and it is right only if every sale was entered. The money
-- in the drawer is a separate fact, and the difference between the two is the
-- thing an owner most wants to see and currently cannot.
--
-- Two moments are worth recording, and they are the same act from either side:
--
--   'close' — the worker leaving says what they are leaving behind. That money
--             does not go anywhere: it stays in the shop overnight and is the
--             float the next session opens with.
--   'open'  — the worker arriving says what they found. Compared against the
--             float carried over, so a drawer that is short is noticed at the
--             start of a shift by the person who did not cause it, rather than at
--             the end by the person who gets blamed.
--
-- `expected` is what the books said at that moment, frozen beside the count. Not
-- recomputed later: the point is what the two figures were when somebody stood
-- there with the drawer open, and a sale amended a week afterwards must not
-- rewrite whether the till balanced that night.
--
-- The count never overwrites the ledger. A declaration is an observation, and
-- letting it silently become the balance would destroy the only record of the
-- discrepancy — which is the reason for counting at all. The carry-over is an
-- ordinary 'deposit' movement, so the till total stays a sum over one table.

CREATE TABLE till_counts (
    id               bigserial PRIMARY KEY,
    owner_id         bigint NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    store_id         bigint NOT NULL,
    store_session_id bigint,
    work_session_id  bigint,
    worker_id        bigint,
    kind             text NOT NULL CHECK (kind IN ('open', 'close')),
    counted          numeric(12,2) NOT NULL CHECK (counted >= 0),
    expected         numeric(12,2) NOT NULL,
    note             text CHECK (note IS NULL OR length(note) <= 300),
    -- One declaration per tap, however many times a flaky connection resends it.
    external_id      text CHECK (external_id IS NULL OR length(external_id) BETWEEN 8 AND 128),
    created_at       timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (store_id, owner_id) REFERENCES stores (id, owner_id) ON DELETE CASCADE,
    FOREIGN KEY (store_session_id, owner_id)
        REFERENCES store_sessions (id, owner_id) ON DELETE SET NULL,
    FOREIGN KEY (work_session_id, owner_id)
        REFERENCES work_sessions (id, owner_id) ON DELETE SET NULL,
    FOREIGN KEY (worker_id, owner_id) REFERENCES workers (id, owner_id) ON DELETE SET NULL
);

-- "What did the last person to leave this shop say they left?" — asked every time
-- a session opens, to work out the float.
CREATE INDEX till_counts_last_close
    ON till_counts (store_id, created_at DESC) WHERE kind = 'close';
CREATE INDEX till_counts_by_session ON till_counts (store_session_id);
CREATE UNIQUE INDEX till_counts_idem_idx
    ON till_counts (owner_id, external_id) WHERE external_id IS NOT NULL;

COMMENT ON TABLE till_counts IS
    'Hand counts of the cash drawer, at the start and end of a shift. Never '
    'overwrites the ledger — the gap between counted and expected is the point.';
