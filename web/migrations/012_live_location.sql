-- Live location: attaching a worker to a store from a position that tracks the
-- device, and following it while the shift runs.
--
-- A plain Telegram location and a hand-placed pin on the map arrive as exactly
-- the same object. `horizontal_accuracy` looked like it separated them and does
-- not -- many clients omit it on a genuine reading. Live location is the one
-- thing that cannot be aimed: Telegram streams it from the device for a chosen
-- period and edits the message as it moves, so a single chosen point cannot
-- produce one.
--
-- So the opening position now carries `start_live_period`, which is the length
-- the worker agreed to share for, and `live_until`, when that stream runs out.
-- The stream after it is kept in `location_pings` -- one row per reading -- with
-- the latest denormalised onto the shift so the store page can render a worker's
-- current distance without a per-row subquery.

ALTER TABLE work_sessions
    -- Seconds the worker agreed to share for; NULL for shifts opened before
    -- live location was required, which is why it is nullable rather than
    -- back-filled with a number nobody chose.
    ADD COLUMN start_live_period int CHECK (start_live_period > 0),
    ADD COLUMN live_until        timestamptz,
    ADD COLUMN last_lat          double precision CHECK (last_lat BETWEEN -90 AND 90),
    ADD COLUMN last_lng          double precision CHECK (last_lng BETWEEN -180 AND 180),
    ADD COLUMN last_distance_m   int,
    ADD COLUMN last_ping_at      timestamptz,
    ADD COLUMN ping_count        int NOT NULL DEFAULT 0,
    -- When the worker was first seen outside the store's radius during this
    -- shift. Recorded, never enforced: a cashier stepping out for ten minutes is
    -- something the owner should be able to see, not something the bot should
    -- refuse in the middle of a working day.
    ADD COLUMN left_area_at      timestamptz,
    ADD CONSTRAINT last_position_is_whole
        CHECK ((last_lat IS NULL) = (last_lng IS NULL));

CREATE TABLE location_pings (
    id               bigserial PRIMARY KEY,
    owner_id         bigint NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    worker_id        bigint NOT NULL,
    work_session_id  bigint NOT NULL,
    store_id         bigint NOT NULL,
    lat              double precision NOT NULL CHECK (lat BETWEEN -90 AND 90),
    lng              double precision NOT NULL CHECK (lng BETWEEN -180 AND 180),
    distance_m       int,
    accuracy_m       int,
    in_range         boolean NOT NULL,
    created_at       timestamptz NOT NULL DEFAULT now(),

    FOREIGN KEY (worker_id, owner_id)       REFERENCES workers (id, owner_id) ON DELETE CASCADE,
    FOREIGN KEY (work_session_id, owner_id) REFERENCES work_sessions (id, owner_id)
        ON DELETE CASCADE,
    FOREIGN KEY (store_id, owner_id)        REFERENCES stores (id, owner_id) ON DELETE CASCADE
);

-- The only two ways it is read: one shift's track, and a worker's recent trail.
CREATE INDEX location_pings_by_session ON location_pings (work_session_id, created_at DESC);
CREATE INDEX location_pings_by_owner   ON location_pings (owner_id, created_at DESC);

COMMENT ON TABLE location_pings IS
    'One row per live-location reading received while a shift is open. Append '
    'only; the newest is mirrored onto work_sessions for cheap reads.';
