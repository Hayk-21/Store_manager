-- A second selling price: wholesale alongside retail.
--
-- Nullable on purpose. The owner's own price sheet leaves it blank for several
-- models, and "we do not sell this one wholesale" is a real answer that a zero
-- would misrepresent as "free".
--
-- possible_profit stays defined against sell_price. That column values the
-- stock at what it would fetch over the counter, which is the number the shop
-- is actually managed by; a wholesale sale is the exception.

ALTER TABLE items ADD COLUMN wholesale_price numeric(12,2)
    CHECK (wholesale_price IS NULL OR wholesale_price >= 0);

COMMENT ON COLUMN items.sell_price IS 'Retail price (մանրածախ).';
COMMENT ON COLUMN items.wholesale_price IS
    'Wholesale price (մեծածախ). NULL means this item is not sold wholesale.';
