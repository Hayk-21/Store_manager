# Why the site is slow, and what to do about it

Written 2026-08-14. **This is a plan. Nothing in the application has been changed.**

Everything below was measured, not guessed. The method and the raw numbers are at
the bottom so anyone can re-run them.

---

## The short answer

Three things, in this order:

1. **Nothing is compressed.** `/statistics` sends **685 KB of HTML** for a 30-day
   period and **970 KB** for 90 days. It gzips to **25 KB** and **32 KB** — a 27–31×
   reduction. On a phone on a 3 Mbit/s connection that is **1.8–2.6 seconds of
   pure download**, every single time, and it dwarfs everything the server does.
   The fix is one line of middleware.

2. **Every date filter in the reporting queries is written in a form no index can
   serve.** `(sold_at AT TIME ZONE $4)::date BETWEEN $2 AND $3` forces a full scan
   of `sales` — 24,450 rows read and thrown away to find 1,950 — and the statistics
   page does that **six times**. Adding indexes does **not** fix this: I added them
   and measured **zero** improvement, because the planner cannot use an index
   against a function of the column. The predicate has to be rewritten first. The
   correct pattern already exists in this codebase — `write_offs.cost_between` does
   it right and runs in 0.05 ms.

3. **Neon is a network database and the app talks to it one query at a time.**
   `/statistics` makes **19 sequential round-trips**, `/reports` detail **17**, and
   the footer makes **3 every ten seconds on every open tab, forever**. At a 10 ms
   round-trip that is 190 ms of pure waiting before any work happens; at 30 ms it is
   570 ms. Nothing here is concurrent and nothing is batched.

Four things commonly blamed are **fine here** and should be left alone — see
"What is already fine" below. In particular there is **no N+1 anywhere on a page
render**, and `_sessions_with_profit` is genuinely fast.

---

## What was measured

A throwaway copy of the schema (all 28 migrations, unmodified) seeded with a
plausible year of trading for this business:

| table | rows |
|---|---|
| `sale_items` | 52,800 |
| `cash_movements` | 30,484 |
| `sales` | 26,400 |
| `expenses` | 2,400 |
| `store_sessions` / `work_sessions` | 1,200 each |
| `till_counts` | 1,197 |
| `items` | 420 (3 stores × 140) |

3 stores, 5 workers, 400 days, ~22 receipts per session. `sales` is 14 MB,
`sale_items` 8 MB, `cash_movements` 6.8 MB.

The real ASGI app was driven in-process over a logged-in session, with every
`db.fetch*` counted and timed. **Postgres was local (~0.1 ms round-trip), so these
numbers are the floor.** Production adds one Neon network round-trip per query.

| page | db round-trips | server ms | HTML |
|---|---:|---:|---:|
| `/stores` (the home page) | 3 | 33 | 7 KB |
| `/partials/footer` (**every 10 s**) | 3 | 41 | 2 KB |
| `/reports` (list) | 3 | 18 | **96 KB** |
| `/reports?store_session_id=…` | **17** | 49 | **200 KB** |
| `/statistics?period=30` | **19** | 203 | **685 KB** |
| `/statistics?period=90` | **19** | 326 | **970 KB** |
| `/expenses` | 12 | 71 | **488 KB** |
| `/stores/1` | 11 | 45 | **289 KB** |
| `/workers` | 3 | 13 | 25 KB |
| `/transfers`, `/history` | 4 | 7 | 4 KB |

Where `/statistics?period=30`'s 203 ms goes (measured per query):

```
 38.7 ms  x5   the spending union CTE  (spending.list/totals/by_category/count)
 25.5 ms  x1   stats.top_items
 21.0 ms  x1   stats.by_store
 19.3 ms  x1   stats.by_worker
 18.0 ms  x1   stats.daily
 16.0 ms  x1   stats.summary
 16.0 ms  x1   stats.by_hour
  4.0 ms  x1   UPDATE auth_sessions  (touch_session — a write, on every request)
 ~17 ms   x9   everything else
------------
 175 ms of SQL, ~28 ms of Jinja + serialisation
```

Six of those queries scan `sales` end to end. Five of them are the *same*
spending union CTE, run five times with different `GROUP BY`s.

---

## Free wins

No behaviour change. Do these first.

### 1. Turn on gzip — 10 minutes, near-zero risk, biggest single win

**What is slow.** Nothing is compressed. `app/main.py` installs **no middleware at
all** (`grep -rn "add_middleware" app/` returns nothing), and Railway's proxy does
not compress application responses for you.

**Evidence** (measured with `gzip.compress(body, 6)`, and the transfer time at a
generous 3 Mbit/s):

| page | raw | gzipped | ratio | transfer saved |
|---|---:|---:|---:|---:|
| `/statistics?period=90` | 970 KB | 32 KB | **30.8×** | **2.51 s** |
| `/statistics?period=30` | 685 KB | 25 KB | **27.5×** | **1.76 s** |
| `/expenses` | 488 KB | 16 KB | 30.4× | 1.26 s |
| `/stores/1` | 289 KB | 14 KB | 20.4× | 0.73 s |
| `/reports` | 96 KB | 5 KB | 21.2× | 0.24 s |
| `/static/app.css` | 31 KB | 9 KB | 3.6× | 0.06 s |
| `/static/htmx.min.js` | 50 KB | 16 KB | 3.1× | 0.09 s |

The 27× ratio is itself a finding: the HTML is that repetitive because the same
edit form is emitted 500 times (see Behavioural §9).

**Fix.** `app.add_middleware(GZipMiddleware, minimum_size=1000)` in `app/main.py`.

**Files.** `app/main.py` (one import, one line).
**Risk.** Very low. Starlette's `GZipMiddleware` honours `Accept-Encoding` and
leaves HTMX partial swaps working unchanged. Costs a few ms of CPU per response,
which is nothing against 1.8 s of transfer.
**Time.** 10 minutes.
**Verify first:** `curl -sI -H 'Accept-Encoding: gzip' https://<prod>/statistics |
grep -i content-encoding` — confirm it is absent today.

### 2. `DB_POOL_MIN=5` — 2 minutes, env var only

**What is slow.** `DB_POOL_MIN=1`, `DB_POOL_MAX=5` (`app/config.py`), and
`max_inactive_connection_lifetime=300.0` (`app/db.py`) means the pool **shrinks
back to one connection after five idle minutes**. The next moment two things
overlap — a page load and the 10-second footer poll, or a page load and a sale
coming in from the bot — the second one must open a fresh connection to Neon:
TLS handshake plus SCRAM authentication, which over the pooled endpoint is tens
to low hundreds of milliseconds. That cost lands on a random request, which is
exactly what "sometimes it just hangs" feels like.

**Fix.** Set `DB_POOL_MIN=5` and `DB_POOL_MAX=10` in the Railway variables.
`asyncpg.create_pool` opens `min_size` connections eagerly at startup, so the
handshakes happen once during boot instead of during somebody's page load. Ten
connections is well inside Neon's pooled budget for one service with one worker.

**Files.** None — Railway service variables. Optionally update `.env.example`'s
documented defaults.
**Risk.** Very low. Check the Neon connection count afterwards.
**Time.** 2 minutes.

### 3. Cache the static assets properly — 15 minutes

**What is slow.** `app/templating.py:static()` already fingerprints every asset URL
with a content hash (`/static/app.css?v=a1b2c3d4e5`), which is exactly right — but
`StaticFiles` serves them with only `ETag`/`Last-Modified` and no `Cache-Control`.
So every navigation re-validates every asset: 2 conditional requests on most
pages, 5 on `/stores` and `/stores/{id}` (leaflet). On mobile that is 2–5 extra
network round-trips per page, each returning "304, nothing changed".

Because the URL already changes when the file changes, these can be cached
permanently and safely.

**Fix.** Wrap the static mount so responses carry
`Cache-Control: public, max-age=31536000, immutable`.

**Files.** `app/main.py` (a small `StaticFiles` subclass overriding
`file_response`, or a scoped middleware).
**Risk.** Low, *conditional on keeping the `?v=` fingerprint*. Never mark a
non-fingerprinted URL immutable.
**Time.** 15 minutes.

### 4. Slow the footer poll down — 5 minutes

**What is slow.** `app/templates/base.html:49`:

```html
<footer id="vs-footer" hx-get="/partials/footer" hx-trigger="load, every 10s" ...>
```

Every open tab, on every page, forever, issues **3 database round-trips including
one `UPDATE`** (`session_with_user`, `touch_session`, `money_repo.totals_by_store`)
every ten seconds. Measured at **41 ms of server time per poll**, of which 28 ms is
`totals_by_store` scanning `cash_movements` once per store. That is 6 polls a
minute per tab competing for the same small pool as the page the owner is actually
waiting for, and it keeps a write going to Neon around the clock.

**Fix.** `hx-trigger="load, every 30s"`, and add HTMX's visibility condition so a
backgrounded tab stops polling entirely:
`hx-trigger="load, every 30s [document.visibilityState === 'visible']"`.

**Files.** `app/templates/base.html` (one attribute).
**Risk.** Low, but it *is* a small freshness change — a sale made through the bot
takes up to 30 s rather than 10 s to appear in the footer chips. Worth mentioning
to the owner even though nothing on the page looks different.
**Time.** 5 minutes.

### A free win that isn't: indexes alone do nothing

Worth stating plainly, because it is the obvious first thing to try. I created
the three missing indexes on the seeded database and re-ran every page:

```
                             before adding indexes    after
/statistics (30 days)          203 ms                 209 ms
/expenses                       71 ms                  60 ms
/stores  (home page)            37 ms                  30 ms
/reports (one report)           49 ms                  59 ms
```

Nothing changed, because the queries filter on `(column AT TIME ZONE $tz)::date`
and Postgres cannot match that against a btree on `column`. The index is
necessary but not sufficient — it only pays off together with §5.

---

## Structural

Changes code. Does not change what the user sees.

### 5. Make the date filters sargable, then add the three indexes — 2–3 hours, the biggest server-side win

**What is slow.** Every reporting query narrows a period like this:

```sql
AND (sa.sold_at AT TIME ZONE $4)::date BETWEEN $2 AND $3
```

`timestamptz AT TIME ZONE text` is `STABLE`, not `IMMUTABLE`, so it cannot be
indexed and cannot drive an index scan. The result is a sequential scan of the
whole table, every time, growing forever.

**Evidence** — `EXPLAIN (ANALYZE, BUFFERS)` of `stats.summary` as the application
sends it today:

```
 Aggregate (actual time=21.385..21.387 rows=1 loops=1)
   Buffers: shared hit=6674
   ->  Nested Loop (actual time=1.000..19.342 rows=3900 loops=1)
         ->  Seq Scan on sales sa (actual time=0.988..15.957 rows=1950 loops=1)
               Filter: ((voided_at IS NULL) AND (owner_id = 1)
                        AND (((sold_at AT TIME ZONE 'Asia/Yerevan'))::date <= CURRENT_DATE)
                        AND (((sold_at AT TIME ZONE 'Asia/Yerevan'))::date >= (CURRENT_DATE - 29)))
               Rows Removed by Filter: 24450          <-- reads all 26,400 to keep 1,950
               Buffers: shared hit=821
         ->  Index Scan using sale_items_sale_id_item_id_key on sale_items si
 Execution Time: 21.515 ms
```

The same question asked as a half-open range on the raw timestamp, with an index:

```
 Aggregate (actual time=9.439..9.442 rows=1 loops=1)
   Buffers: shared hit=697 read=9                     <-- 6,674 buffers -> 706
   ->  Bitmap Heap Scan on sales sa (actual time=0.149..0.380 rows=1950 loops=1)
         ->  Bitmap Index Scan on sales_owner_sold_idx (actual time=0.138 rows=1950)
 Execution Time: 9.486 ms
```

I checked the two forms return identical answers (`old_way 1950 | new_way 1950`).

The same rewrite on the other three hot shapes:

| query | today | rewritten + indexed |
|---|---:|---:|
| `stats.summary` (30 d) | 21.5 ms, 6,674 buffers | **9.5 ms, 706 buffers** |
| `spending` withdrawals door | 3.05 ms, 483 buffers | **0.39 ms, 138 buffers** |
| `money.totals_by_store` day scan (**× stores, every 10 s**) | 8.2 ms, seq scan | **0.06 ms, 26 buffers** |
| `spending` wages door | 0.68 ms, seq scan of `work_sessions` | **0.08 ms** |

The buffer counts matter more on Neon than the milliseconds do. Neon's storage is
disaggregated: a shared-buffer miss is fetched from a page server **over the
network**. `/statistics` touches roughly 40,000 buffers across its six scanning
queries. On a compute that has just resumed, with an empty local file cache, those
are network fetches — which is precisely why the page feels catastrophic the first
time it is opened in a while and merely slow afterwards.

**The pattern to copy is already in this repo.** `write_offs.cost_between` writes
`created_at >= $2 AND created_at < ($3::date + 1)` and its plan is a bitmap index
scan finishing in **0.048 ms**. Nothing new has to be invented.

**Fix.**
```sql
-- instead of  (col AT TIME ZONE $tz)::date BETWEEN $since AND $until
AND col >= ($since::date)::timestamp AT TIME ZONE $tz
AND col <  ($until::date + 1)::timestamp AT TIME ZONE $tz
```
plus a new migration:
```sql
CREATE INDEX sales_owner_sold_idx        ON sales (owner_id, sold_at) WHERE voided_at IS NULL;
CREATE INDEX cash_movements_store_time_idx ON cash_movements (store_id, created_at DESC);
CREATE INDEX work_sessions_owner_ended_idx ON work_sessions (owner_id, ended_at DESC)
    WHERE ended_at IS NOT NULL;
```
(`cash_movements (owner_id, created_at DESC)` and `expenses (owner_id, spent_on DESC)`
already exist and start being used the moment the predicate is fixed.)

**Files.** `app/repo/stats.py` (the shared `_WHERE`, `daily`, `by_hour`),
`app/repo/spending.py` (the `wages`/`bonuses`/`taken` CTEs in `_SOURCES`),
`app/repo/money.py` (`totals_by_store`, `day_totals_for_store`), and a new
`migrations/029_reporting_indexes.sql`. **Do not** touch
`sessions.recent_store_sessions` — it is already fast (§ "What is already fine").

**Risk.** Low, but real, and it is an arithmetic risk rather than a code one: the
two forms differ only at a DST boundary, where a local day is 23 or 25 hours long.
Armenia has not observed DST since 2012, so `Asia/Yerevan` is a fixed +04 and the
forms are exactly equivalent. Anyone running this in a DST timezone should be told.
The existing tests in `tests/test_statistics.py`, `test_statistics_filters.py`,
`test_spending.py` and `test_trading_day.py` cover the boundaries; run them.
**Time.** 2–3 hours including tests.
**Saves.** ~70–90 ms of the 175 ms of SQL on `/statistics`, ~35 ms on `/expenses`,
~25 ms on every footer poll, and — the point — it stops all of it growing with the
table.

### 6. Ask the spending question once instead of five times — 2 hours

**What is slow.** `_spending_context` (`app/routes/pages.py:1161`) issues four
separate queries that all begin with the identical 4-CTE union in
`spending.py:_SOURCES` — `list_between`, `by_category_between`, `totals_between`,
`count_between` — and `statistics.overview` calls `totals_between` a fifth time
independently. On `/statistics` that union executes **5 times**; on `/expenses`,
**4 times**. Measured: 38.7 ms and 57.9 ms respectively, the largest single line
item on both pages.

It is worse than the wall-clock suggests. `EXPLAIN` on that union reports:

```
 Planning:
   Buffers: shared hit=841
 Planning Time: 2.636 ms
 Execution Time: 5.957 ms
```

**44% of the cost is planning** — and because `DATABASE_URL` points at Neon's
pooled endpoint, `clean_dsn` correctly sets `statement_cache_size=0`
(`app/config.py`), so nothing is ever prepared and that 2.6 ms of planning is paid
again on every single call. Five calls = **13 ms of pure re-planning per page
view**, plus 5 network round-trips.

**Fix.** One query returning the rows, the per-kind totals, the per-category totals
and the count together — the rows from a CTE, the three aggregates as `json_agg`
columns or a second `UNION ALL` block over the same `everything` CTE, so the union
is built once and scanned once. `_spending_context` unpacks the single result.
`statistics.overview` takes its wage/bonus/withdrawal/expense split from the same
result instead of calling `totals_between` again.

**Files.** `app/repo/spending.py`, `app/routes/pages.py` (`_spending_context`),
`app/services/statistics.py` (`overview`).
**Risk.** Low-medium. It is the one place where "what did the month cost" is
defined, and the module comments are emphatic that a figure and the rows behind it
must come from one question — which this change makes *more* true, not less.
`tests/test_spending.py` covers it.
**Time.** 2 hours.
**Saves.** ~30 ms and **4 round-trips** on `/statistics`; ~45 ms and 3 round-trips
on `/expenses`. At a 20 ms Neon RTT the round-trips are worth more than the ms.

> ⚠️ `app/routes/pages.py` is being edited by another job right now. Coordinate,
> or do the `spending.py` half first and the `pages.py` half after they land.

### 7. Run the independent queries concurrently — 1–2 hours, best ratio on Neon

**What is slow.** `statistics.overview` (`app/services/statistics.py`) awaits
**13 queries strictly one after another**: `summary`, `daily`, `totals_between`,
`cost_between`, `list_for_owner`, `top_items`, `by_store`, `by_worker`,
`by_category_between`, `list_between` ×2, `by_hour`, `stock_value`. Every one is
independent of the others — none consumes another's result. The dict literal they
sit in evaluates its values in order, so they serialise for no reason.

The same is true of `/reports` detail (`pages.py:621`), where 14 of the 17
round-trips only need `session["store_id"]` and `session["owner_id"]`.

Sequential round-trips are the entire story on a network database. 19 × RTT:

| Neon RTT | waiting, today | after §6+§7 (≈9 round-trips, ~3 waves) |
|---|---:|---:|
| 5 ms | 95 ms | ~15 ms |
| 20 ms | 380 ms | ~60 ms |
| 50 ms (cross-region) | 950 ms | ~150 ms |

(I attempted to confirm this by injecting artificial latency into `Database._run`;
the numbers moved the right way — `/statistics` 203 → 966 ms at 30 ms injected —
but Windows' `asyncio.sleep` granularity is ~15 ms so the absolute figures are not
trustworthy. The arithmetic above is the honest version, and it is not in dispute:
round-trips × RTT is additive and nothing overlaps them today.)

**Fix.** `asyncio.gather` the independent queries in `statistics.overview` and in
the `/reports` detail block. `db._run` acquires its own pooled connection per
call, so this works with no plumbing change.

**Files.** `app/services/statistics.py`, `app/routes/pages.py`.
**Risk.** **Medium — the one item here that can bite.** Thirteen concurrent
acquisitions against `DB_POOL_MAX=5` will queue, and against Neon it multiplies
concurrent connection use across all visitors. Do §2 first (`DB_POOL_MAX=10`), cap
the fan-out (`asyncio.Semaphore(4)`, or gather in two deliberate waves), and watch
Neon's connection count. Do **not** gather anything inside a `db.transaction()` —
that would run statements on different connections and break the atomicity the
write paths depend on. Read paths only.
**Time.** 1–2 hours.

### 8. Stop rendering the whole stock list into a report — 30 minutes

`/reports?store_session_id=…` calls `items_repo.list_for_store(user.id,
session["store_id"])` with **no limit** and renders every active item into the
page, purely so a correction form has a dropdown to choose from. That is 140 items
here and unbounded in general, and it is most of that page's 200 KB. `/stores/{id}`
does the same and is 289 KB.

`app/routes/partials.py` already exposes `GET /store-items` returning
`_item_options.html`. Load the options from there on demand instead.

**Files.** `app/routes/pages.py`, `app/templates/reports.html`.
**Risk.** Low.
**Time.** 30 minutes.
> ⚠️ Both files are owned by another job right now. Defer.

---

## Behavioural — these change what is on the page. Ask the owner first.

### 9. 500 payments and 900 hidden forms

`spending.list_between` caps at **500 rows**, and `_spending.html` walks that list
**twice**: once for the table, and again at line 105 to emit one or two `<form>`
elements per row (the comment explains why — a `<form>` inside a `<tr>` is invalid
and browsers hoist it out). Each form carries a CSRF input and a fully URL-encoded
`back` querystring.

Measured on `/statistics?period=30`: **599 `<tr>` and ~900 `<form>` elements**,
about 1.9 KB of HTML per payment. That is what makes the page 685 KB, and gzip
fixes the *download* but not the DOM — 1,500 extra elements is why the page is
sluggish to scroll and to tap on a phone even once it has arrived.

Period → size, measured:

| period | payments in range | HTML | server |
|---|---:|---:|---:|
| 1 day | ~12 | 24 KB | 127 ms |
| 7 days | ~85 | 171 KB | 146 ms |
| 30 days | 355 | 685 KB | 203 ms |
| 90 days | 1,045 (capped at 500) | 970 KB | 326 ms |

**Options, in increasing order of what the owner would notice:**

- **(a) Emit the edit forms on demand.** Keep all the rows; render the `<form>` for
  a row only when it is clicked (`hx-get` a one-row partial). Removes ~900 elements
  and roughly half the bytes. The owner sees no difference until they click, and
  then sees one extra ~50 ms fetch. *This is the one I would propose first.*
- **(b) Page the list.** 50 rows with "Ցույց տալ ավելին". The headline total is
  already computed over the whole period in SQL rather than summed from the rows
  (`totals_between`), so a shorter list cannot make a figure wrong — that hazard is
  already handled. Real change: the owner can no longer Ctrl-F the whole month.
- **(c) Lazy-load the heavy sections.** The payments list, the breakage list and the
  top-items table could load after the charts and headline figures, via `hx-trigger`
  on reveal. First paint would land in ~120 ms with the numbers that matter.

Same shape on `/reports` (50 sessions → 96 KB) — smaller, and fine as it is.

### 10. `/expenses` writes on every page view

`expenses_page` calls `ensure_starter_categories(user.id)` on every `GET` — an
`INSERT … ON CONFLICT DO NOTHING` measured at 2.3 ms plus a round-trip plus a WAL
write on Neon, to do nothing for the 999th time. Move it to account creation, or
guard it with a cheap `SELECT` first. Behavioural only in the sense that a user who
deletes every starter category would no longer see them come back.

**Files.** `app/routes/pages.py:466`. 15 minutes.

---

## What is already fine — do not spend time here

- **There are no N+1 queries on any page render.** I scanned every `await` on a
  repo/db call nested inside a `for`/`while` across all of `app/`. Every hit is in
  a *write* path — `services/sales.py`, `services/corrections.py`,
  `services/stock.py`, `services/shifts.py` — iterating the 1–5 lines of a basket
  inside a single transaction on a single connection. Not a page-load problem.
- **`_sessions_with_profit` / `sessions.recent_store_sessions` is fast.** The row
  the other job just added, with its two `LATERAL` aggregates and two correlated
  subqueries, plans as: index scan on `store_sessions_owner_opened_idx` → `Memoize`
  on stores → bitmap index scans on `cash_movements_session_method_idx`,
  `sales_store_session_idx`, `till_counts_by_session` and `expenses_store_date_idx`.
  **4.2 ms for 50 sessions.** It was written to avoid an N+1 and it succeeded.
  Leave it alone.
- **The `sale_items` join is indexed.** There is no explicit index on
  `sale_items(sale_id)`, but `UNIQUE (sale_id, item_id)` provides one and the
  planner uses it (`Index Scan using sale_items_sale_id_item_id_key`). Not a gap.
- **Jinja is not the bottleneck.** ~28 ms to render a 685 KB page. Even the worst
  page is 86% SQL.
- **Front-end asset weight is fine.** `app.css` 31 KB and `htmx.min.js` 50 KB on
  every page; `leaflet.js` 144 KB only on `/stores` and `/stores/{id}`, which is
  where the map is. All content-hashed by `templating.static()`, which is exactly
  right. Only §3 (cache headers) is missing.
- **The daily chart is already bounded.** `MAX_DAILY_BARS = 45` switches to weekly
  buckets, so a long range cannot produce thousands of bars.
- **`statement_cache_size=0` is correct, not a bug.** Neon's pooler is PgBouncer in
  transaction mode and cannot keep prepared statements alive; the comment in
  `config.py` is right. It is worth knowing only because it is *why* §6 pays off
  more than it normally would: every query is re-planned every time, so five calls
  cost five plannings.
- **`DIRECT_DATABASE_URL` is used correctly.** It exists for `migrate.py` only —
  DDL through a transaction-mode pooler is unreliable — and `migrate.apply_all`
  prefers it while the application correctly uses the pooled `DATABASE_URL`. No
  change needed. (It also means the pre-deploy migration cannot hide a bad
  `DATABASE_URL`, which is the right trade.)
- **`/health` deliberately returns 200 when the database is asleep.** Correct, and
  the reasoning in `main.py` is sound — don't "fix" it.

---

## The deployment: cold containers and a sleeping compute

Worth saying plainly, because part of "slow and laggy" is not a query at all.

- **Neon scales the compute to zero when idle.** `app/db.py` says so in a comment
  and carries retry logic for exactly that. On the default idle timeout, the first
  request after a quiet spell waits for the compute to resume — typically several
  hundred ms to a few seconds — *and* arrives at a compute whose local file cache is
  empty, so the 40,000 buffer touches on `/statistics` become page-server fetches.
  **This is why the first page of the morning is dramatically worse than the
  second.** Check the Neon project's autosuspend setting; if the plan allows
  disabling it, that alone removes the worst single experience the owner has.
  Ironically the 10-second footer poll (§4) currently prevents this whenever a tab
  is open — slowing it to 30 s is still well inside any autosuspend window.
- **Railway: 1 replica, 1 uvicorn worker** (`railway.json`, `run.py`). Correct for
  a small pool, but it means one slow request can queue behind another. After §5–§7
  there is headroom; before them, `/statistics` occupies the worker for 200 ms+.
- **Check whether the Railway service is set to sleep.** It is not in
  `railway.json` (that is a service setting), and if it is on, the first request
  after idle pays a full container cold start *plus* the Neon resume above.
- **`command_timeout=15.0`** in `db.py` is a sensible ceiling, but note that a
  `/statistics` request making 19 sequential queries can spend far longer than 15 s
  in total without any single query tripping it. Worth a per-request budget later,
  not now.

---

## Suggested order of work

| # | change | effort | user-visible saving |
|---|---|---|---|
| 1 | gzip middleware (§1) | 10 min | **1.8–2.6 s** on `/statistics`, 1.3 s on `/expenses` |
| 2 | `DB_POOL_MIN=5`, `DB_POOL_MAX=10` (§2) | 2 min | removes random connect stalls |
| 3 | footer poll 10 s → 30 s + visibility (§4) | 5 min | frees ~250 ms/min of server per tab |
| 4 | static `Cache-Control: immutable` (§3) | 15 min | 2–5 round-trips per navigation |
| 5 | sargable dates + 3 indexes (§5) | 2–3 h | ~70–90 ms on `/statistics`, and stops it growing |
| 6 | one spending query, not five (§6) | 2 h | ~30 ms + 4 round-trips |
| 7 | `asyncio.gather` the read fan-out (§7) | 1–2 h | 10 × RTT — the largest win on Neon |
| 8 | lazy item options (§8) | 30 min | ~90 KB off `/reports` and `/stores/{id}` |
| 9 | forms on demand / paging (§9) | ask owner | the phone stops being sluggish to scroll |

Items 1–4 are half a morning and no behaviour change. If only those get done, the
owner's experience improves more than from anything else on this list.

---

## Appendix: how to reproduce

Nothing here lives in the repo. Scripts were written to
`%LOCALAPPDATA%\Temp\claude\…\scratchpad\`:

- `setup_perf_db.py` — creates `storemanager_perf` on the local container
  (`localhost:55432`) and runs `migrate.apply_all` against it.
- `seed_perf.sql` — the fixture above, pure SQL, then `ANALYZE`.
- `timeit_pages.py` — drives the real ASGI app over `httpx.ASGITransport` with a
  logged-in cookie, monkey-patching `Database._run` to count and time every
  round-trip. `--detail` prints the per-query breakdown, `--latency-ms N` injects
  artificial network delay (unreliable below ~15 ms on Windows).
- `explain.sql`, `prove_fix.sql` — `EXPLAIN (ANALYZE, BUFFERS)` for each hot shape,
  before and after the rewrite, including an equality check that both forms return
  the same rows.
- `weigh.py`, `gzip_check.py` — page weight, element counts, compression ratios.

The production database was never touched. `storemanager_perf` is separate from
`storemanager_test` so the test suite was not disturbed; drop it when finished.
