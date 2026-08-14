-- Removing a stock correction the cashier made.
--
-- A correction is a claim about the shelf — «there are ten more of these than the
-- screen says» — and a cashier can be wrong about it: the wrong product, the wrong
-- number, a delivery counted twice. The owner could read the claim on the report and
-- do nothing about it, so the only way to fix a bad one was to make another one,
-- which left the log saying the shelf had changed twice when it had not changed at
-- all.
--
-- Like every other owner correction it is audited and undoable, which is what this
-- constraint has to be told about. Same shape as 015, 025, 026, 027 and 028.

ALTER TABLE audit_events DROP CONSTRAINT audit_events_action_check;

ALTER TABLE audit_events ADD CONSTRAINT audit_events_action_check
    CHECK (action IN (
        'void_sale', 'amend_sale', 'add_sale', 'delete_sale',
        'add_movement', 'delete_movement', 'set_salary',
        'delete_till_count', 'set_movement_amount', 'set_bonus',
        'add_write_off', 'delete_adjustment'));
