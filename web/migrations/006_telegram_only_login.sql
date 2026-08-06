-- Telegram is now the only way in. Remove what email login left behind.
--
-- The password fallback is gone at the owner's request, so the columns that
-- supported it are dropped rather than left lying around holding a hash of a
-- password nobody can use. Same for login_attempts, which existed only to
-- throttle password guessing -- code requests are throttled by login_codes.
--
-- The way back in without Telegram is now `manage.py user login-link`, which
-- mints a one-time session from the command line. That needs database access
-- rather than a public form, which is the right shape for an escape hatch.

-- Anybody left without a handle could never sign in again, so give the
-- constraint something to stand on before it becomes mandatory.
DELETE FROM users WHERE telegram_username IS NULL;

ALTER TABLE users DROP CONSTRAINT users_identifiable;
ALTER TABLE users ALTER COLUMN telegram_username SET NOT NULL;

ALTER TABLE users DROP COLUMN password_hash;
ALTER TABLE users DROP COLUMN password_changed_at;
ALTER TABLE users DROP COLUMN email;

DROP TABLE login_attempts;

-- One-time sign-in links, for the command-line escape hatch. Same discipline as
-- login_codes: only a hash is stored, and it works exactly once.
CREATE TABLE login_links (
    id          bigserial PRIMARY KEY,
    user_id     bigint NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    token_hash  text NOT NULL UNIQUE,
    expires_at  timestamptz NOT NULL,
    consumed_at timestamptz,
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX login_links_user_idx ON login_links (user_id);
