-- A target a worker can beat, and what they get for beating it.
--
-- Three columns rather than one, because a bonus is a rule and not a number:
-- how much they have to sell, over what stretch, and what they earn for it.
-- All three are nullable together -- most shops will never set one, and the
-- absence has to mean "no bonus" rather than "a bonus of zero".
--
-- Paid the same way a wage is: out of the till, when the shift ends, as a row
-- in the ledger. That keeps one answer to "what did today cost" instead of a
-- second scheme running beside the first.
--
-- Once per period, not once per shift. A worker who crosses the target in the
-- morning and works again in the evening has earned the bonus once; the second
-- shift finds it already paid and adds nothing.

ALTER TABLE workers
    ADD COLUMN bonus_threshold numeric(12,2) CHECK (bonus_threshold > 0),
    ADD COLUMN bonus_amount    numeric(12,2) CHECK (bonus_amount > 0),
    ADD COLUMN bonus_period    text CHECK (bonus_period IN ('day', 'month')),
    -- Either the whole rule is set or none of it is. A threshold with no amount
    -- would silently never pay, which is worse than being refused at the form.
    ADD CONSTRAINT bonus_rule_is_whole CHECK (
        (bonus_threshold IS NULL AND bonus_amount IS NULL AND bonus_period IS NULL)
     OR (bonus_threshold IS NOT NULL AND bonus_amount IS NOT NULL
         AND bonus_period IS NOT NULL)
    );

ALTER TABLE work_sessions
    ADD COLUMN bonus_paid numeric(12,2) CHECK (bonus_paid >= 0);

-- 'bonus' joins the kinds a ledger row can be. Negative like a wage: it leaves
-- the till.
ALTER TABLE cash_movements DROP CONSTRAINT IF EXISTS cash_movements_kind_check;
ALTER TABLE cash_movements ADD CONSTRAINT cash_movements_kind_check
    CHECK (kind IN ('sale', 'void', 'salary', 'bonus',
                    'withdrawal', 'deposit', 'adjustment'));
ALTER TABLE cash_movements ADD CONSTRAINT bonus_leaves_the_till
    CHECK (kind <> 'bonus' OR (amount < 0 AND method = 'cash'
                               AND work_session_id IS NOT NULL));

-- One bonus per shift at most. The "once per period" rule is enforced in the
-- service, which can see the period; this is the backstop against a retry.
CREATE UNIQUE INDEX one_bonus_per_work_session
    ON cash_movements (work_session_id) WHERE kind = 'bonus';

COMMENT ON COLUMN workers.bonus_threshold IS
    'Sales this worker must reach over bonus_period before bonus_amount is paid.';
