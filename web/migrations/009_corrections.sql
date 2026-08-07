-- Correcting the books after the fact.
--
-- The rule that makes this safe: a completed sale is never edited in place.
-- Changing a quantity would mean unpicking a stock movement and a ledger entry
-- and rewriting both consistently, and getting that subtly wrong corrupts the
-- accounts silently. Instead a correction *voids* the original -- which already
-- has working reversal logic -- and records a replacement beside it. The books
-- then read "this was recorded, then it was replaced by that", which is what
-- actually happened.
--
-- So the only new column a sale needs is the link to whatever superseded it.

ALTER TABLE sales ADD COLUMN superseded_by_sale_id bigint REFERENCES sales (id)
    ON DELETE SET NULL;
CREATE INDEX sales_superseded_idx ON sales (superseded_by_sale_id)
    WHERE superseded_by_sale_id IS NOT NULL;

-- A sale that supersedes another cannot also be the one it replaced.
ALTER TABLE sales ADD CONSTRAINT sales_not_self_superseding
    CHECK (superseded_by_sale_id IS DISTINCT FROM id);

-- Who put this row in the ledger. 'system' is the ordinary path -- a sale, a
-- salary. 'owner' is a correction made from the reports page, and the reports
-- page marks them so a reader can tell a recorded fact from a later amendment.
ALTER TABLE cash_movements ADD COLUMN created_by text NOT NULL DEFAULT 'system'
    CHECK (created_by IN ('system', 'owner'));

-- An owner-entered movement carries its reason in `note`, and that reason is
-- the entire point of the entry: "paid the influencer" is not deducible from
-- an amount. Required for the kinds only an owner can create.
ALTER TABLE cash_movements ADD CONSTRAINT owner_movements_explain_themselves
    CHECK (
        created_by <> 'owner'
        OR kind IN ('sale', 'void', 'salary')
        OR (note IS NOT NULL AND length(btrim(note)) > 0)
    );

-- Closing a store session snapshots its totals. Correcting a *closed* session
-- afterwards makes that snapshot stale, so it is recomputed from the ledger --
-- which is why the ledger, not the snapshot, is the source of truth.
COMMENT ON COLUMN store_sessions.cash_at_close IS
    'Snapshot taken at close. Recomputed from cash_movements when a correction '
    'touches a closed session.';
