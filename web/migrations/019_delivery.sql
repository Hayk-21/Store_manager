-- Whether a sale went out for delivery.
--
-- It changes nothing about the money: the same price, the same payment method,
-- the same stock movement. What it changes is what the owner can see -- how much
-- of the day went out of the door versus over the counter -- and that is not
-- recoverable afterwards from anything else in the row.
--
-- A flag rather than a payment method, because it is orthogonal to one. A
-- delivery can be paid in cash at the door or by card in advance, and folding
-- the two together would make "how much did we take in cash" unanswerable.

ALTER TABLE sales ADD COLUMN is_delivery boolean NOT NULL DEFAULT false;

-- Only ever read as a filter over a session or a period, and deliveries are the
-- minority, so a partial index is the cheap one.
CREATE INDEX sales_delivery_idx ON sales (store_session_id) WHERE is_delivery;

COMMENT ON COLUMN sales.is_delivery IS
    'The goods were delivered rather than handed over the counter. Affects no '
    'money -- it is there so the split can be seen.';
