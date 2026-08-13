-- Breakage the owner records themselves, from the report page.
--
-- Writing stock off needed a worker standing in an open shift, because the bot is
-- where the feature grew up. So a box found broken in the morning, after the shop
-- had shut, could not be recorded at all -- the owner's only write-off button on
-- the website was the one that deletes somebody else's. The store now comes from
-- the session instead of from a shift, and the row carries no worker.
--
-- Like every other owner correction it is audited, which is what this constraint
-- has to be told about, and undoing it puts the goods back on the shelf.

ALTER TABLE audit_events DROP CONSTRAINT audit_events_action_check;

ALTER TABLE audit_events ADD CONSTRAINT audit_events_action_check
    CHECK (action IN (
        'void_sale', 'amend_sale', 'add_sale', 'delete_sale',
        'add_movement', 'delete_movement', 'set_salary',
        'delete_till_count', 'set_movement_amount', 'set_bonus',
        'add_write_off'));
