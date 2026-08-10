-- Changing what a ledger row was for how much.
--
-- Deleting one and typing it again was the only fix, and it loses the reason with the
-- figure -- «Vhj», «առաքիչին», «մանրադրամ» -- which is the part of a withdrawal worth
-- keeping. A wrong amount here is a wrong amount in the drawer, so it needs to be
-- correctable in place, and correctable in place means undoable like everything else.
--
-- Only the kinds an owner types by hand. A 'sale' or 'salary' row is one half of a
-- receipt or a shift and has its own editor that moves the pair together.

ALTER TABLE audit_events DROP CONSTRAINT audit_events_action_check;

ALTER TABLE audit_events ADD CONSTRAINT audit_events_action_check
    CHECK (action IN (
        'void_sale', 'amend_sale', 'add_sale', 'delete_sale',
        'add_movement', 'delete_movement', 'set_salary',
        'delete_till_count', 'set_movement_amount'));
