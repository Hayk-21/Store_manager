-- Deleting a drawer count is a ninth thing the owner can do to a report.
--
-- Correcting the figure is the usual fix: the number a cashier typed at the door was
-- wrong and gets replaced. But a row that should never have existed -- a count against
-- the wrong shop, a duplicate, one left behind by a rule that has since changed --
-- should go, and the shop's float then falls back to whatever count is now the latest.
--
-- The payload keeps both readings, so the history says what was thrown away rather than
-- only that something was.

ALTER TABLE audit_events DROP CONSTRAINT audit_events_action_check;

ALTER TABLE audit_events ADD CONSTRAINT audit_events_action_check
    CHECK (action IN (
        'void_sale', 'amend_sale', 'add_sale', 'delete_sale',
        'add_movement', 'delete_movement', 'set_salary',
        'delete_till_count'));
