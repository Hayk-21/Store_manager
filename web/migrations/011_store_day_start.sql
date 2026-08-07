-- When a shop's trading day rolls over.
--
-- Takings are shown for "today" and start again each morning. Which morning is
-- a property of the shop, not of the calendar: one that serves customers until
-- 2am is still having last night's evening, and filing those sales under the
-- next date would split one night across two days.
--
-- So each store carries its own boundary hour. 6am is the default because that
-- is past closing time for a late shop and before opening for an early one, but
-- a shop that trades 24 hours or shuts at 8pm can set its own.
--
-- Nothing runs at that hour. The boundary is a timestamp queries compare
-- against, computed from now() every time it is asked for -- so there is no
-- scheduled reset to fail, no missed run after a deploy, and no state to get
-- out of step with the ledger.

ALTER TABLE stores
    ADD COLUMN day_start_hour smallint NOT NULL DEFAULT 6
        CHECK (day_start_hour BETWEEN 0 AND 23);

COMMENT ON COLUMN stores.day_start_hour IS
    'Local hour at which this store''s trading day begins; daily takings are '
    'summed from it and start again at the next one.';
