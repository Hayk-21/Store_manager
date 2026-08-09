-- The money that lives in a shop's drawer between shifts.
--
-- Each store keeps a float. It might be nothing, it might be forty thousand, and it
-- differs from shop to shop. It is not takings and it is not the owner's yet: it is
-- the change the next person needs to open up with.
--
-- Until now this was inferred — "whatever the last worker said they left" — read
-- back out of the count history. That worked but it made the balance a derived
-- fact, and derived facts cannot be corrected: the owner had no way to say "there
-- is actually 25,000 in that drawer" without inventing a shift. Now it is a column,
-- the one place that answers "how much is in this shop's casa", and the count
-- history in till_counts becomes what it should always have been — the record of
-- how the figure got there rather than the figure itself.
--
-- Set from two places and no others: a worker declaring what they are leaving at
-- the end of their shift, and the owner correcting it on the website.

ALTER TABLE stores
    ADD COLUMN till_balance numeric(12,2) NOT NULL DEFAULT 0
        CHECK (till_balance >= 0);

COMMENT ON COLUMN stores.till_balance IS
    'Cash left in this shop''s drawer between shifts. Not takings, not the owner''s '
    'yet — the float the next worker opens with. Set by a closing count or by the '
    'owner.';

-- Backfill from the counts already recorded, so a shop that has been handing over
-- correctly does not lose its float on the way to the new model.
UPDATE stores s
   SET till_balance = last_count.counted
  FROM (
      SELECT DISTINCT ON (store_id) store_id, counted
        FROM till_counts
       WHERE kind = 'close'
       ORDER BY store_id, created_at DESC, id DESC
  ) AS last_count
 WHERE last_count.store_id = s.id;

-- 'owner' joins the two ends a count could come from. The distinction earns its
-- place: a worker's count is an observation of a drawer they were standing at, and
-- the owner's is a correction made from somewhere else entirely. A reader of the
-- history can tell them apart.
--
-- 'open' stays permitted even though nothing writes it any more. Counting at the
-- start of a shift has been dropped — it asked a worker to answer for a drawer
-- somebody else had filled — but rows already recorded that way are real
-- observations and deleting them would be rewriting what happened.
ALTER TABLE till_counts DROP CONSTRAINT till_counts_kind_check;
ALTER TABLE till_counts ADD CONSTRAINT till_counts_kind_check
    CHECK (kind IN ('open', 'close', 'owner'));

-- What went to the owner as a result of this count: the till at that moment less
-- what was left behind. Stored rather than recomputed because it is the answer to
-- "how much should I have received", and a sale amended next week must not change
-- what was handed over on the night.
ALTER TABLE till_counts
    ADD COLUMN handed_over numeric(12,2);

COMMENT ON COLUMN till_counts.handed_over IS
    'Cash the worker handed to the owner at this count: the till then, less what '
    'they left in the drawer. Null for an owner correction, which hands over nothing.';
