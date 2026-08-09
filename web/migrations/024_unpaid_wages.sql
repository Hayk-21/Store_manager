-- What the drawer could not cover.
--
-- A shift's wage came out of the till in full, whatever the till held. A shop
-- that took 6,552 all on card and had 1,000 in cash paid a 3,500 salary and
-- closed with cash of -2,500 -- a drawer holding negative money, which is not a
-- thing. Everything downstream then computed from that fiction: the worker who
-- left 5,000 in the shop was told the drawer held 7,500 more than expected.
--
-- So the till pays what it can, and the rest is a debt the owner settles out of
-- the card money or their own pocket. These columns are that debt.
--
-- 'paid' keeps its meaning: what the shift cost. Stats sum it as the wage bill
-- and the owner edits it on the report, and neither of those changes because the
-- cash happened to be short on the night. The unpaid part is carried beside it,
-- not subtracted from it.

ALTER TABLE work_sessions
    ADD COLUMN salary_unpaid numeric(12,2) NOT NULL DEFAULT 0
        CHECK (salary_unpaid >= 0),
    ADD COLUMN bonus_unpaid numeric(12,2) NOT NULL DEFAULT 0
        CHECK (bonus_unpaid >= 0);

-- No backfill. Past shifts were paid in full by the code that closed them, and
-- the overdrawn ones are already recorded as overdrawn: inventing a debt for
-- them now would be a guess about money somebody may well have handed over.
