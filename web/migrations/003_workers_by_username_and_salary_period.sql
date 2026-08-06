-- Register a worker by @username, and let salary be monthly as well as per shift.
--
-- Why username: the owner knows "@justhayk", not "555000777". Telegram's Bot API
-- cannot turn a username into a numeric id, so the binding happens on first
-- contact instead: the owner writes the username, and the first time that person
-- messages the bot the server fills in their telegram_id and never trusts the
-- username again. Usernames can be changed or handed to somebody else; a numeric
-- id cannot, so it is the id that identifies a worker once known.

-- telegram_id is no longer known at the moment of registration.
ALTER TABLE workers ALTER COLUMN telegram_id DROP NOT NULL;

ALTER TABLE workers ADD COLUMN telegram_username text
    CHECK (telegram_username IS NULL OR telegram_username ~ '^[A-Za-z0-9_]{4,32}$');

-- Stored without the leading @ and matched case-insensitively, because Telegram
-- treats @JustHayk and @justhayk as the same account.
-- Globally unique for the same reason telegram_id is: it is what resolves an
-- unbound worker to exactly one owner.
CREATE UNIQUE INDEX workers_username_idx
    ON workers (lower(telegram_username)) WHERE telegram_username IS NOT NULL;

-- A row has to be reachable by something.
ALTER TABLE workers ADD CONSTRAINT workers_identifiable
    CHECK (telegram_id IS NOT NULL OR telegram_username IS NOT NULL);

-- The amount no longer means "per shift" in every case, so the column stops
-- claiming it does.
ALTER TABLE workers RENAME COLUMN salary_per_shift TO salary_amount;

-- 'shift'  -- paid at the end of each shift, taken out of that store session's
--             cash. This is what the system settles automatically.
-- 'month'  -- a monthly wage. Ending a shift costs the till nothing; the owner
--             pays it separately, and the reports show what is owed.
ALTER TABLE workers ADD COLUMN salary_period text NOT NULL DEFAULT 'shift'
    CHECK (salary_period IN ('shift', 'month'));
