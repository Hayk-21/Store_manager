-- Whether a line went out at retail, wholesale, or a price somebody typed.
--
-- The price itself was already recorded, so this adds no money -- it adds the
-- *reason*. Without it a wholesale run and a haggled discount are the same row
-- with a lower number, and "how much do we sell wholesale" is unanswerable.
--
-- Defaults to 'retail' rather than being back-filled from a comparison against
-- items.wholesale_price: prices move, and guessing the intent of an old line
-- from today's price list would invent history rather than record it.

ALTER TABLE sale_items
    ADD COLUMN price_kind text NOT NULL DEFAULT 'retail'
        CHECK (price_kind IN ('retail', 'wholesale', 'custom'));

COMMENT ON COLUMN sale_items.price_kind IS
    'retail | wholesale | custom -- which price list this line came from, as '
    'chosen at the point of sale. The amount lives in unit_price.';
