-- The indexes the reporting pages have always needed, and could not use.
--
-- Every date filter on those pages used to be written as
--
--     (sold_at AT TIME ZONE 'Asia/Yerevan')::date BETWEEN $since AND $until
--
-- which asks the right question but which Postgres cannot answer with an index:
-- the expression is STABLE rather than IMMUTABLE, so no btree on the column can
-- match it. Adding these indexes on their own changed nothing, measurably --
-- statistics stayed at ~205 ms -- because the planner was never able to reach for
-- them. The predicates were rewritten to a half-open range on the raw timestamp in
-- the same change as this migration, and these are what that rewrite unlocks:
--
--     stats.summary over 30 days   21.5 ms / 6,674 buffers -> 9.5 ms / 706
--     the withdrawals door          3.1 ms /   483 buffers -> 0.4 ms / 138
--     the footer's day totals       8.2 ms, seq scan       -> 0.06 ms /  26
--
-- The buffer counts matter more than the milliseconds on Neon, where storage is
-- disaggregated and a shared-buffer miss is fetched over the network.
--
-- expenses (owner_id, spent_on DESC) and cash_movements (owner_id, created_at DESC)
-- already existed and start earning their keep with the same rewrite.

-- Revenue, profit, the daily chart, the hourly chart and the best/worst day all
-- narrow sales by owner and by when they were sold, and all of them ignore voided
-- ones -- so the partial index is both smaller and a complete answer.
CREATE INDEX IF NOT EXISTS sales_owner_sold_idx
    ON sales (owner_id, sold_at) WHERE voided_at IS NULL;

-- The footer polls this one for every open tab: "what has this shop taken since
-- its trading day began". It was a sequential scan of the largest table in the
-- database, three times a minute, forever.
CREATE INDEX IF NOT EXISTS cash_movements_store_time_idx
    ON cash_movements (store_id, created_at DESC);

-- The wage and bonus doors of «Ծախսեր», which are bucketed by when the shift
-- ended. An open shift has not been paid yet and is never in that question.
CREATE INDEX IF NOT EXISTS work_sessions_owner_ended_idx
    ON work_sessions (owner_id, ended_at DESC) WHERE ended_at IS NOT NULL;
