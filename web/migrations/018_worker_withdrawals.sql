-- A cashier taking money out of the till.
--
-- It happens: paying a delivery, buying bags, sending somebody for change. The
-- money leaves whether or not the system knows, so the choice is between an
-- unexplained shortfall at close and a row saying where it went.
--
-- 'worker' joins the two ways a movement can come about. The distinction is
-- worth keeping: 'system' is what the software recorded from a sale or a shift,
-- 'owner' is what the owner typed afterwards, and 'worker' is money taken at the
-- counter by somebody who was standing there. A reader of the ledger can tell
-- them apart, which is the whole reason the column exists.
--
-- The reason requirement widens with it. An amount with no purpose is a
-- shortfall with a number attached, and is exactly what this is meant to
-- replace, so it is refused for a worker as firmly as for an owner.

ALTER TABLE cash_movements DROP CONSTRAINT cash_movements_created_by_check;
ALTER TABLE cash_movements ADD CONSTRAINT cash_movements_created_by_check
    CHECK (created_by IN ('system', 'owner', 'worker'));

ALTER TABLE cash_movements DROP CONSTRAINT owner_movements_explain_themselves;
ALTER TABLE cash_movements ADD CONSTRAINT typed_movements_explain_themselves
    CHECK (
        created_by = 'system'
        OR kind IN ('sale', 'void', 'salary', 'bonus')
        OR (note IS NOT NULL AND length(btrim(note)) > 0)
    );

-- Taking money out is the one ledger write a phone can trigger directly, so it
-- needs the same protection every other bot write has: one key per tap, reused
-- across retries, and a unique index as the arbiter. A flaky connection must not
-- empty the till twice.
ALTER TABLE cash_movements
    ADD COLUMN external_id text
        CHECK (external_id IS NULL OR length(external_id) BETWEEN 8 AND 128);

CREATE UNIQUE INDEX cash_movements_idem_idx
    ON cash_movements (owner_id, external_id) WHERE external_id IS NOT NULL;

COMMENT ON COLUMN cash_movements.created_by IS
    'system = recorded from a sale or a shift; owner = typed on the website; '
    'worker = taken from the till at the counter. The last two must carry a note.';
