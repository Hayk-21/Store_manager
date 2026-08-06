-- The owner registers a worker by Telegram id alone; the name arrives by itself.
--
-- Registration stays closed: an id that is not in this table is still refused.
-- What changes is that the owner no longer has to type a name to create the row.
-- The bot reports the worker's Telegram profile name on every request, and it is
-- stored here, so the /workers page fills in on its own the first time somebody
-- uses the bot.
--
-- Two columns rather than one so an owner can override a nickname without the
-- next bot request overwriting the correction:
--   name          -- what the owner typed, if anything. Wins when set.
--   telegram_name -- what Telegram reports. Refreshed on contact.

ALTER TABLE workers ALTER COLUMN name DROP NOT NULL;

-- The old CHECK still applies when name IS NOT NULL: a NULL makes the whole
-- expression NULL, which a CHECK constraint accepts.

ALTER TABLE workers ADD COLUMN telegram_name text
    CHECK (telegram_name IS NULL OR length(telegram_name) <= 200);

-- A worker with neither name yet is shown as their id, never as a blank row.
COMMENT ON COLUMN workers.name IS
    'Owner-supplied label. NULL means "use telegram_name".';
COMMENT ON COLUMN workers.telegram_name IS
    'Telegram profile name, refreshed whenever the bot calls on this worker''s behalf.';
