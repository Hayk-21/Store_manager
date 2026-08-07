-- Every correction, and enough to undo it.
--
-- Corrections never destroy anything -- a void keeps its row, an amendment
-- links old to new -- so each one is already reversible in principle. This
-- table is what makes it reversible in practice: it records what was done and
-- the handful of ids needed to put it back.
--
-- Reverting works newest-first. Undoing an amendment when a later correction
-- has already replaced its replacement would leave the chain pointing at rows
-- that no longer mean what the link says, so the application refuses it and
-- asks for the later one to be undone first. "Return to that moment" is then
-- just undoing back to it, one step at a time, each step safe on its own.

CREATE TABLE audit_events (
    id               bigserial PRIMARY KEY,
    owner_id         bigint NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    -- Who made the change. NULL only if the account was later removed.
    user_id          bigint REFERENCES users (id) ON DELETE SET NULL,
    action           text NOT NULL CHECK (action IN (
                         'void_sale', 'amend_sale', 'add_sale',
                         'add_movement', 'delete_movement', 'set_salary')),
    -- Which shift it touched, so the history can be shown beside it.
    store_session_id bigint,
    -- One line a human can read without expanding anything.
    summary          text NOT NULL,
    -- The ids and values the revert needs. Deliberately small: this is not a
    -- copy of the rows, it is a recipe for undoing one action.
    payload          jsonb NOT NULL DEFAULT '{}'::jsonb,
    reverted_at      timestamptz,
    reverted_by      bigint REFERENCES users (id) ON DELETE SET NULL,
    created_at       timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (store_session_id, owner_id) REFERENCES store_sessions (id, owner_id)
        ON DELETE CASCADE,
    CHECK ((reverted_at IS NULL) = (reverted_by IS NULL))
);
CREATE INDEX audit_events_owner_time_idx ON audit_events (owner_id, created_at DESC);
CREATE INDEX audit_events_session_idx ON audit_events (store_session_id)
    WHERE store_session_id IS NOT NULL;
-- Finding "the newest thing not yet undone", which is the only thing revertible.
CREATE INDEX audit_events_pending_idx ON audit_events (owner_id, id DESC)
    WHERE reverted_at IS NULL;
