-- Sign in with a Telegram handle and a code sent by the bot, instead of email.
--
-- The constraint that shapes this: a bot cannot start a conversation. It may
-- only reply to somebody who has messaged it first. So an admin's handle has to
-- be *bound* to a chat before any code can be delivered, exactly the way a
-- worker's is -- they press /start once, and from then on we can reach them.
--
-- Email stops being the identifier but is not dropped: it is still a useful
-- label, and the password column stays as the way back in if Telegram is
-- unreachable (see /login/password).

ALTER TABLE users ALTER COLUMN email DROP NOT NULL;

ALTER TABLE users ADD COLUMN telegram_username text
    CHECK (telegram_username IS NULL OR telegram_username ~ '^[A-Za-z0-9_]{4,32}$');
ALTER TABLE users ADD COLUMN telegram_id bigint CHECK (telegram_id IS NULL OR telegram_id > 0);
ALTER TABLE users ADD COLUMN telegram_name text;
ALTER TABLE users ADD COLUMN telegram_bound_at timestamptz;

-- Case-insensitive, because Telegram treats @JustHayk and @justhayk as one
-- account. Globally unique: the handle is what a login attempt resolves by.
CREATE UNIQUE INDEX users_telegram_username_idx
    ON users (lower(telegram_username)) WHERE telegram_username IS NOT NULL;
CREATE UNIQUE INDEX users_telegram_id_idx
    ON users (telegram_id) WHERE telegram_id IS NOT NULL;

-- An account has to be reachable by something.
ALTER TABLE users ADD CONSTRAINT users_identifiable
    CHECK (email IS NOT NULL OR telegram_username IS NOT NULL);

-- One-time codes, delivered over Telegram.
--
-- Only the hash is stored, so a database dump does not hand over live codes.
-- attempts is on the row rather than counted elsewhere, so a wrong guess costs
-- the guesser something even if they open a fresh page each time.
CREATE TABLE login_codes (
    id           bigserial PRIMARY KEY,
    user_id      bigint NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    code_hash    text NOT NULL,
    attempts     smallint NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    expires_at   timestamptz NOT NULL,
    consumed_at  timestamptz,
    created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX login_codes_user_created_idx ON login_codes (user_id, created_at DESC);
-- At most one code alive per account: asking for a new one retires the old.
CREATE UNIQUE INDEX one_live_login_code_per_user
    ON login_codes (user_id) WHERE consumed_at IS NULL;
