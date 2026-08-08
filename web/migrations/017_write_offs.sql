-- Stock that left without being sold: broken, leaking, expired.
--
-- Not a sale at a price of zero. A sale means a customer and a receipt, and
-- filing a damaged vape as one would put it in the revenue figures, the receipt
-- count and the worker's bonus progress -- all of which would be wrong, and the
-- last one would pay somebody for breaking things.
--
-- No cash movement either. The money left when the stock was bought; nothing
-- moves in the till when a box turns out to be broken. What it costs is the
-- price we paid for it, snapshotted here so a later change to the item's cost
-- cannot rewrite what this loss was worth.

CREATE TABLE write_offs (
    id               bigserial PRIMARY KEY,
    owner_id         bigint NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    store_id         bigint NOT NULL,
    item_id          bigint NOT NULL,
    -- Who wrote it off, and during which shift. The shift is nullable because
    -- the owner may record one from the website with nobody on duty.
    worker_id        bigint,
    work_session_id  bigint,
    store_session_id bigint,
    quantity         int NOT NULL CHECK (quantity > 0),
    unit_cost        numeric(12,2) NOT NULL CHECK (unit_cost >= 0),
    total_cost       numeric(12,2) NOT NULL CHECK (total_cost >= 0),
    reason           text,
    -- The bot mints one per action and reuses it across retries, so a flaky
    -- connection cannot write the same breakage off twice.
    external_id      text CHECK (external_id IS NULL OR length(external_id) BETWEEN 8 AND 128),
    created_at       timestamptz NOT NULL DEFAULT now(),

    FOREIGN KEY (store_id, owner_id) REFERENCES stores (id, owner_id) ON DELETE CASCADE,
    FOREIGN KEY (item_id, owner_id) REFERENCES items  (id, owner_id) ON DELETE RESTRICT,
    FOREIGN KEY (worker_id, owner_id) REFERENCES workers (id, owner_id) ON DELETE SET NULL,
    FOREIGN KEY (work_session_id, owner_id)
        REFERENCES work_sessions (id, owner_id) ON DELETE SET NULL,
    FOREIGN KEY (store_session_id, owner_id)
        REFERENCES store_sessions (id, owner_id) ON DELETE SET NULL
);

CREATE UNIQUE INDEX write_offs_idem_idx
    ON write_offs (owner_id, external_id) WHERE external_id IS NOT NULL;
CREATE INDEX write_offs_by_owner   ON write_offs (owner_id, created_at DESC);
CREATE INDEX write_offs_by_session ON write_offs (store_session_id);
CREATE INDEX write_offs_by_item    ON write_offs (item_id);

COMMENT ON TABLE write_offs IS
    'Stock removed without a sale -- damaged, expired, lost. Reduces items.count '
    'and costs what we paid for it; never touches the till.';
