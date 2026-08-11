-- Correcting a shift's bonus, the same way a wage is already correctable.
--
-- A bonus row was deletable through the general "remove a ledger entry" door with
-- no guard at all, unlike a salary row: deleting it left work_sessions.bonus_paid
-- and bonus_unpaid still claiming a bonus the ledger no longer had any record of,
-- and the report and the till would disagree about that shift forever. The fix
-- gives a bonus the same "set it, including to zero" path a wage already has.

ALTER TABLE audit_events DROP CONSTRAINT audit_events_action_check;

ALTER TABLE audit_events ADD CONSTRAINT audit_events_action_check
    CHECK (action IN (
        'void_sale', 'amend_sale', 'add_sale', 'delete_sale',
        'add_movement', 'delete_movement', 'set_salary',
        'delete_till_count', 'set_movement_amount', 'set_bonus'));
