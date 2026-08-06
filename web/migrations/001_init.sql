-- vapestore initial schema.
--
-- Conventions:
--   * Money is numeric(12,2). Never float, never integer minor units. Currency is AMD.
--   * Timestamps are timestamptz.
--   * Every owned table carries owner_id AND UNIQUE (id, owner_id); children reference
--     parents by the composite key (id, owner_id). A row belonging to one owner cannot
--     be attached to another owner's row even if application code forgets its WHERE.
--   * The accounting period is a store_session (open -> close), not a calendar day.
--     Every figure the UI shows is a SUM over cash_movements filtered to one session.

-- =========================================================================
-- Geo helper. The ONLY definition of distance in the system.
-- =========================================================================

CREATE OR REPLACE FUNCTION distance_m(
    lat1 double precision, lng1 double precision,
    lat2 double precision, lng2 double precision
) RETURNS double precision
LANGUAGE sql IMMUTABLE PARALLEL SAFE STRICT AS $$
  -- Haversine, asin form: numerically stable at the tens-of-metres distances we
  -- care about, unlike the acos form which loses precision under a metre or so.
  SELECT 6371000.0 * 2 * asin(LEAST(1.0, sqrt(
        power(sin(radians(lat2 - lat1) / 2), 2)
      + cos(radians(lat1)) * cos(radians(lat2))
      * power(sin(radians(lng2 - lng1) / 2), 2)
  )))
$$;

-- =========================================================================
-- Accounts
-- =========================================================================

CREATE TABLE users (
    id                  bigserial PRIMARY KEY,
    email               text NOT NULL UNIQUE
                        CHECK (email = lower(btrim(email)) AND email LIKE '%_@_%._%'),
    -- Nullable on purpose. A row with NULL cannot log in with a password. Today
    -- manage.py always sets one; tomorrow an invite-by-email flow can insert the
    -- row first and let the user choose a password, with no data migration.
    password_hash       text,
    display_name        text CHECK (display_name IS NULL OR length(display_name) <= 120),
    is_active           boolean NOT NULL DEFAULT true,
    created_at          timestamptz NOT NULL DEFAULT now(),
    activated_at        timestamptz,
    password_changed_at timestamptz,
    last_login_at       timestamptz
);

-- Server-side sessions. The cookie carries a random token; only its SHA-256 lives
-- here, so logout and password changes genuinely revoke access. A signed-cookie or
-- JWT design would have to be replaced to get that property.
CREATE TABLE auth_sessions (
    token_hash   text PRIMARY KEY,
    user_id      bigint NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    csrf_token   text NOT NULL,
    user_agent   text,
    created_at   timestamptz NOT NULL DEFAULT now(),
    last_seen_at timestamptz NOT NULL DEFAULT now(),
    expires_at   timestamptz NOT NULL
);
CREATE INDEX auth_sessions_user_idx    ON auth_sessions (user_id);
CREATE INDEX auth_sessions_expires_idx ON auth_sessions (expires_at);

-- Login throttling. In the database rather than in memory so it survives a restart.
CREATE TABLE login_attempts (
    id           bigserial PRIMARY KEY,
    email        text NOT NULL,
    ip           inet,
    succeeded    boolean NOT NULL,
    attempted_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX login_attempts_email_time_idx ON login_attempts (email, attempted_at DESC);
CREATE INDEX login_attempts_ip_time_idx    ON login_attempts (ip, attempted_at DESC)
    WHERE ip IS NOT NULL;

-- =========================================================================
-- Stores
-- =========================================================================

CREATE TABLE stores (
    id         bigserial PRIMARY KEY,
    owner_id   bigint NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    name       text NOT NULL CHECK (length(btrim(name)) > 0 AND length(name) <= 120),
    address    text CHECK (address IS NULL OR length(address) <= 300),
    -- double precision, not numeric: these feed trigonometry, not accounting.
    lat        double precision CHECK (lat BETWEEN -90 AND 90),
    lng        double precision CHECK (lng BETWEEN -180 AND 180),
    -- Urban phone GPS is routinely +/- 30 m; a radius under 50 rejects honest workers
    -- standing in the doorway.
    radius_m   int NOT NULL DEFAULT 120 CHECK (radius_m BETWEEN 50 AND 5000),
    is_active  boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    -- Half a coordinate is not a location.
    CONSTRAINT stores_coords_paired CHECK ((lat IS NULL) = (lng IS NULL)),
    UNIQUE (id, owner_id)
);
CREATE UNIQUE INDEX stores_owner_name_idx ON stores (owner_id, lower(btrim(name)));
CREATE INDEX stores_owner_active_idx ON stores (owner_id) WHERE is_active;
-- Geofence candidate scan: located, active stores of one owner.
CREATE INDEX stores_geofence_idx ON stores (owner_id) WHERE is_active AND lat IS NOT NULL;

-- =========================================================================
-- Items
-- =========================================================================

CREATE TABLE items (
    id              bigserial PRIMARY KEY,
    owner_id        bigint NOT NULL,
    store_id        bigint NOT NULL,
    name            text NOT NULL CHECK (length(btrim(name)) > 0 AND length(name) <= 200),
    count           int NOT NULL DEFAULT 0 CHECK (count >= 0),
    self_price      numeric(12,2) NOT NULL DEFAULT 0 CHECK (self_price >= 0),
    sell_price      numeric(12,2) NOT NULL DEFAULT 0 CHECK (sell_price >= 0),
    -- Derived by Postgres so it can never drift from the columns behind it.
    -- May be negative when sell_price < self_price; that is information, not an error.
    possible_profit numeric(16,2)
        GENERATED ALWAYS AS ((sell_price - self_price) * count) STORED,
    is_active       boolean NOT NULL DEFAULT true,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (store_id, owner_id) REFERENCES stores (id, owner_id) ON DELETE CASCADE,
    UNIQUE (id, owner_id)
);
CREATE UNIQUE INDEX items_store_name_idx  ON items (store_id, lower(btrim(name)));
CREATE INDEX        items_store_active_idx ON items (store_id) WHERE is_active;
-- Backs the bot's "type part of the name" search.
CREATE INDEX items_store_name_search_idx
    ON items (store_id, lower(name) text_pattern_ops) WHERE is_active;

-- =========================================================================
-- Workers
-- =========================================================================

-- telegram_id is globally unique: a bot request carries only that number, and it
-- is the only thing that resolves the call to exactly one owner's stores.
CREATE TABLE workers (
    id               bigserial PRIMARY KEY,
    owner_id         bigint NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    name             text NOT NULL CHECK (length(btrim(name)) > 0 AND length(name) <= 120),
    telegram_id      bigint NOT NULL UNIQUE CHECK (telegram_id > 0),
    salary_per_shift numeric(12,2) NOT NULL DEFAULT 0 CHECK (salary_per_shift >= 0),
    is_active        boolean NOT NULL DEFAULT true,
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now(),
    UNIQUE (id, owner_id)
);
CREATE INDEX workers_owner_idx ON workers (owner_id);

-- =========================================================================
-- Store sessions -- THE accounting period
-- =========================================================================
--
-- A worker pressing "open" in the bot starts one; pressing "close" ends it. There
-- is no calendar day anywhere in the money model. A store's current cash is the
-- sum of movements attached to its currently-open session, which is why closing
-- resets the visible total to zero without anybody running a job at midnight.

CREATE TABLE store_sessions (
    id                  bigserial PRIMARY KEY,
    owner_id            bigint NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    store_id            bigint NOT NULL,
    opened_at           timestamptz NOT NULL DEFAULT now(),
    closed_at           timestamptz,
    opened_by_worker_id bigint NOT NULL,
    closed_by           text NOT NULL DEFAULT 'worker'
                        CHECK (closed_by IN ('worker', 'owner', 'auto')),
    -- Snapshots taken at close. The till is settled and handed over; the next
    -- opening starts from zero, and this row is what the report reads.
    cash_at_close       numeric(12,2),
    card_at_close       numeric(12,2),
    salaries_at_close   numeric(12,2),
    -- Local calendar date of opened_at. Display and grouping only -- nothing in
    -- the money model reads this column.
    opened_day          date NOT NULL,
    FOREIGN KEY (store_id, owner_id) REFERENCES stores (id, owner_id) ON DELETE CASCADE,
    FOREIGN KEY (opened_by_worker_id, owner_id) REFERENCES workers (id, owner_id)
        ON DELETE RESTRICT,
    CHECK (closed_at IS NULL OR closed_at >= opened_at),
    CHECK ((closed_at IS NULL) = (cash_at_close IS NULL)),
    UNIQUE (id, owner_id)
);
-- At most one open session per store. Two workers arriving at once cannot both
-- open it; the second one joins the session the first created.
CREATE UNIQUE INDEX one_open_session_per_store
    ON store_sessions (store_id) WHERE closed_at IS NULL;
CREATE INDEX store_sessions_owner_opened_idx ON store_sessions (owner_id, opened_at DESC);
CREATE INDEX store_sessions_store_opened_idx ON store_sessions (store_id, opened_at DESC);
CREATE INDEX store_sessions_open_idx ON store_sessions (opened_at) WHERE closed_at IS NULL;

-- =========================================================================
-- Work sessions (a worker's shift, inside a store session)
-- =========================================================================

CREATE TABLE work_sessions (
    id               bigserial PRIMARY KEY,
    owner_id         bigint NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    worker_id        bigint NOT NULL,
    store_id         bigint NOT NULL,
    store_session_id bigint NOT NULL,
    started_at       timestamptz NOT NULL DEFAULT now(),
    ended_at         timestamptz,
    start_lat        double precision,
    start_lng        double precision,
    start_distance_m int CHECK (start_distance_m IS NULL OR start_distance_m >= 0),
    end_lat          double precision,
    end_lng          double precision,
    -- Snapshot written at end: editing a worker's rate later must not rewrite
    -- what a past shift cost.
    salary_paid      numeric(12,2) CHECK (salary_paid IS NULL OR salary_paid >= 0),
    closed_by        text NOT NULL DEFAULT 'worker'
                     CHECK (closed_by IN ('worker', 'owner', 'auto')),
    start_idem_key   text,
    end_idem_key     text,
    FOREIGN KEY (worker_id, owner_id) REFERENCES workers (id, owner_id) ON DELETE CASCADE,
    FOREIGN KEY (store_id,  owner_id) REFERENCES stores  (id, owner_id) ON DELETE CASCADE,
    FOREIGN KEY (store_session_id, owner_id) REFERENCES store_sessions (id, owner_id)
        ON DELETE CASCADE,
    CHECK (ended_at IS NULL OR ended_at >= started_at),
    CHECK ((ended_at IS NULL) = (salary_paid IS NULL)),
    UNIQUE (id, owner_id)
);
-- "One open shift per worker" is a database guarantee: two simultaneous taps
-- cannot both win.
CREATE UNIQUE INDEX one_open_session_per_worker
    ON work_sessions (worker_id) WHERE ended_at IS NULL;
CREATE INDEX work_sessions_store_session_idx ON work_sessions (store_session_id);
CREATE INDEX work_sessions_store_open_idx    ON work_sessions (store_id) WHERE ended_at IS NULL;
CREATE UNIQUE INDEX work_sessions_start_idem_idx
    ON work_sessions (owner_id, start_idem_key) WHERE start_idem_key IS NOT NULL;
CREATE UNIQUE INDEX work_sessions_end_idem_idx
    ON work_sessions (owner_id, end_idem_key) WHERE end_idem_key IS NOT NULL;

-- =========================================================================
-- Sales
-- =========================================================================

-- One row per basket the bot reports (a receipt); the lines live in sale_items.
-- external_id is the bot's idempotency key, so a retried POST resolves to this
-- same receipt instead of selling twice.
CREATE TABLE sales (
    id                  bigserial PRIMARY KEY,
    owner_id            bigint NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    store_id            bigint NOT NULL,
    worker_id           bigint NOT NULL,
    work_session_id     bigint NOT NULL,
    store_session_id    bigint NOT NULL,
    payment_method      text NOT NULL CHECK (payment_method IN ('cash', 'card')),
    total               numeric(12,2) NOT NULL CHECK (total >= 0),
    external_id         text NOT NULL CHECK (length(external_id) BETWEEN 8 AND 128),
    sold_at             timestamptz NOT NULL DEFAULT now(),
    -- A worker may void their own most recent receipt from the bot. The row is
    -- never deleted, so the owner always sees that it happened and who did it.
    voided_at           timestamptz,
    voided_by_worker_id bigint,
    void_reason         text CHECK (void_reason IS NULL OR length(void_reason) <= 300),
    FOREIGN KEY (store_id,         owner_id) REFERENCES stores         (id, owner_id) ON DELETE CASCADE,
    FOREIGN KEY (worker_id,        owner_id) REFERENCES workers        (id, owner_id) ON DELETE CASCADE,
    FOREIGN KEY (work_session_id,  owner_id) REFERENCES work_sessions  (id, owner_id) ON DELETE CASCADE,
    FOREIGN KEY (store_session_id, owner_id) REFERENCES store_sessions (id, owner_id) ON DELETE CASCADE,
    FOREIGN KEY (voided_by_worker_id, owner_id) REFERENCES workers (id, owner_id) ON DELETE RESTRICT,
    CHECK ((voided_at IS NULL) = (voided_by_worker_id IS NULL)),
    UNIQUE (worker_id, external_id),
    UNIQUE (id, owner_id)
);
CREATE INDEX sales_store_session_idx ON sales (store_session_id);
CREATE INDEX sales_work_session_idx  ON sales (work_session_id);
-- Backs "the worker's most recent voidable receipt".
CREATE INDEX sales_worker_recent_idx ON sales (worker_id, sold_at DESC) WHERE voided_at IS NULL;

CREATE TABLE sale_items (
    id         bigserial PRIMARY KEY,
    owner_id   bigint NOT NULL,
    sale_id    bigint NOT NULL,
    item_id    bigint NOT NULL,
    quantity   int NOT NULL CHECK (quantity > 0),
    -- Snapshots. Editing an item's price later must not rewrite past revenue, and
    -- unit_cost is what makes realised profit computable per receipt.
    unit_price numeric(12,2) NOT NULL CHECK (unit_price >= 0),
    unit_cost  numeric(12,2) NOT NULL DEFAULT 0 CHECK (unit_cost >= 0),
    line_total numeric(12,2) NOT NULL CHECK (line_total >= 0),
    CHECK (line_total = unit_price * quantity),
    FOREIGN KEY (sale_id, owner_id) REFERENCES sales (id, owner_id) ON DELETE CASCADE,
    FOREIGN KEY (item_id, owner_id) REFERENCES items (id, owner_id) ON DELETE RESTRICT,
    -- One line per item per receipt: duplicate lines are merged before insert.
    UNIQUE (sale_id, item_id)
);
CREATE INDEX sale_items_item_idx ON sale_items (item_id);

-- =========================================================================
-- Money ledger
-- =========================================================================

-- Append-only. Every figure the UI shows is a SUM over this table filtered by
-- store_session_id. There is no running-balance column anywhere, which is what
-- makes the "cash resets when the store closes" behaviour fall out for free.
CREATE TABLE cash_movements (
    id               bigserial PRIMARY KEY,
    owner_id         bigint NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    store_id         bigint NOT NULL,
    store_session_id bigint NOT NULL,
    method           text NOT NULL CHECK (method IN ('cash', 'card')),
    kind             text NOT NULL
                     CHECK (kind IN ('sale', 'void', 'salary', 'withdrawal', 'deposit', 'adjustment')),
    amount           numeric(12,2) NOT NULL,   -- signed: income positive, payouts negative
    sale_id          bigint,
    work_session_id  bigint,
    worker_id        bigint,
    note             text CHECK (note IS NULL OR length(note) <= 300),
    created_at       timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (store_id,         owner_id) REFERENCES stores         (id, owner_id) ON DELETE CASCADE,
    FOREIGN KEY (store_session_id, owner_id) REFERENCES store_sessions (id, owner_id) ON DELETE CASCADE,
    FOREIGN KEY (sale_id,          owner_id) REFERENCES sales          (id, owner_id) ON DELETE CASCADE,
    FOREIGN KEY (work_session_id,  owner_id) REFERENCES work_sessions  (id, owner_id) ON DELETE CASCADE,
    FOREIGN KEY (worker_id,        owner_id) REFERENCES workers        (id, owner_id) ON DELETE CASCADE,
    -- Sign discipline, so a bug cannot pay a negative salary or book a negative sale.
    CHECK (kind <> 'sale'       OR amount >= 0),
    CHECK (kind <> 'void'       OR (amount <= 0 AND sale_id IS NOT NULL)),
    CHECK (kind <> 'salary'     OR (amount < 0 AND method = 'cash' AND work_session_id IS NOT NULL)),
    CHECK (kind <> 'withdrawal' OR amount < 0),
    CHECK (kind <> 'deposit'    OR amount > 0)
);
CREATE INDEX cash_movements_session_method_idx ON cash_movements (store_session_id, method);
CREATE INDEX cash_movements_owner_time_idx     ON cash_movements (owner_id, created_at DESC);
CREATE INDEX cash_movements_sale_idx           ON cash_movements (sale_id) WHERE sale_id IS NOT NULL;
-- A closed shift may only ever pay its salary once.
CREATE UNIQUE INDEX one_salary_per_work_session
    ON cash_movements (work_session_id) WHERE kind = 'salary';
