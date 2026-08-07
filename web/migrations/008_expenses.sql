-- Money that leaves the business without a customer being involved.
--
-- Rent, advertising, paying an influencer, restocking, a repair. These are not
-- till movements: they belong to the business over a period, not to whichever
-- store session happened to be open when the invoice arrived -- and some are
-- paid on days when no shop was open at all.
--
-- So they are their own table rather than another `kind` on cash_movements,
-- which is deliberately tied to a store session and cannot represent "the
-- landlord was paid on Sunday".

CREATE TABLE expense_categories (
    id         bigserial PRIMARY KEY,
    owner_id   bigint NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    name       text NOT NULL CHECK (length(btrim(name)) > 0 AND length(name) <= 80),
    is_active  boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (id, owner_id)
);
CREATE UNIQUE INDEX expense_categories_owner_name_idx
    ON expense_categories (owner_id, lower(btrim(name)));

CREATE TABLE expenses (
    id          bigserial PRIMARY KEY,
    owner_id    bigint NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    -- NULL means "the business", not any one shop: an advertising campaign is
    -- not an expense of the branch that happened to be open.
    store_id    bigint,
    category_id bigint,
    -- What it was for, in the owner's own words. Required: an unexplained
    -- payment in the books is worse than no record of it.
    purpose     text NOT NULL CHECK (length(btrim(purpose)) > 0 AND length(purpose) <= 300),
    amount      numeric(12,2) NOT NULL CHECK (amount > 0),
    method      text NOT NULL DEFAULT 'cash' CHECK (method IN ('cash', 'card', 'bank', 'other')),
    -- The day it belongs to, which is not always the day it was entered.
    spent_on    date NOT NULL,
    -- 'once'    -- a single payment
    -- 'monthly' -- a standing cost, recorded once per month it covers
    recurrence  text NOT NULL DEFAULT 'once' CHECK (recurrence IN ('once', 'monthly')),
    note        text CHECK (note IS NULL OR length(note) <= 1000),
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (store_id, owner_id) REFERENCES stores (id, owner_id) ON DELETE SET NULL,
    FOREIGN KEY (category_id, owner_id) REFERENCES expense_categories (id, owner_id)
        ON DELETE SET NULL,
    UNIQUE (id, owner_id)
);
CREATE INDEX expenses_owner_date_idx ON expenses (owner_id, spent_on DESC);
CREATE INDEX expenses_store_date_idx ON expenses (store_id, spent_on DESC)
    WHERE store_id IS NOT NULL;
CREATE INDEX expenses_category_idx ON expenses (category_id) WHERE category_id IS NOT NULL;
