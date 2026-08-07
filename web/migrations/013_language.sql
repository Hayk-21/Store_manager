-- Which language each person reads the system in.
--
-- Stored against the person rather than kept in a cookie or in the bot's memory,
-- because the same human meets this system in two places: the owner on the
-- website, the cashier in Telegram. A preference that lived in one client would
-- have to be set again in the other, and the server's own error sentences --
-- which the bot prints verbatim -- would have no way to know which language to
-- speak.
--
-- Armenian is the default and stays the source language: every string is written
-- in Armenian first and translated from it, so a missing translation degrades to
-- correct Armenian rather than to a blank or a key.

ALTER TABLE users
    ADD COLUMN language text NOT NULL DEFAULT 'hy'
        CHECK (language IN ('hy', 'en'));

ALTER TABLE workers
    ADD COLUMN language text NOT NULL DEFAULT 'hy'
        CHECK (language IN ('hy', 'en'));

COMMENT ON COLUMN users.language IS 'Website language for this owner: hy | en.';
COMMENT ON COLUMN workers.language IS
    'Language the bot speaks to this worker, and the language of the server '
    'error sentences it prints back to them: hy | en.';
