-- Deleting a receipt outright is a fourth thing that can happen to a sale.
--
-- Voiding is the honest correction when a customer brought something back: the
-- receipt stays and shows it was reversed. But a row that should never have
-- existed -- a duplicate, a test entry -- left struck through forever only makes
-- the report harder to read, so the owner can remove it entirely.
--
-- Nothing is lost by it. Unlike the other actions, whose payload is a handful of
-- ids, this one carries the whole receipt: the sale row, its lines and its
-- ledger entries. That is what makes the undo able to rebuild something that no
-- longer exists anywhere else.

ALTER TABLE audit_events DROP CONSTRAINT audit_events_action_check;

ALTER TABLE audit_events ADD CONSTRAINT audit_events_action_check
    CHECK (action IN (
        'void_sale', 'amend_sale', 'add_sale', 'delete_sale',
        'add_movement', 'delete_movement', 'set_salary'));
