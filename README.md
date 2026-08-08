# Store Manager

A small retail system for a chain of vape shops.

* **`web/`** — the website you (the owner) log into. Stores, stock, live till
  money, shift history. FastAPI + Jinja2 + HTMX, Postgres on Neon.
* **`bot/`** — the Telegram bot your cashiers use. Open the store, sell, undo,
  close. python-telegram-bot.

Workers never touch the website; owners never touch the bot. Every number is
computed on the server — the bot forwards a location, an item id and a quantity,
and prints back what the server says happened.

Two Railway services from this one repository, distinguished by **Root
Directory** (`web` and `bot`). Do not put a `railway.toml` at the repo root: it
silently overrides both services.

---

## The one idea you need

**There is no calendar day in the money model.** The accounting period is a
*store session*, which begins when a worker presses «Բացել խանութը» in the bot
and ends when the store is closed.

```
worker presses «Բացել»  ──►  store session opens  ──►  sales and salaries accumulate
                                (till starts at 0)
                                        │
                        the last worker ends their shift
                                        ▼
                        till is settled, cash/card snapshotted,
                        the store's visible total returns to 0
```

A store's current cash is `SUM(cash_movements.amount)` over its *currently open*
session. There is no running-balance column anywhere, which is why closing the
store resets the till with nothing running at midnight, and why every past
session stays readable at `/reports`.

Several workers can share one store session. The first to arrive opens it; the
others join. Each gets their own shift row and their own salary.

Closing the shop is not a button a cashier has. «Ավարտել իմ հերթափոխը» opens the
«Հերթափոխի ամփոփում» — the write-up of what they sold — and ends their own shift
and nobody else's. The session closes on its own once the last of them has left,
so no cashier can end a colleague's day by mistake. The owner can still force one
closed from `/reports`.

`Asia/Yerevan` is used for *displaying* times and for grouping report rows. It
never decides which bucket money lands in.

---

## Running it locally

You need Python 3.12+ and a Postgres. Docker is the easiest:

```bash
docker run -d --name storemanager-pg \
  -e POSTGRES_PASSWORD=storemanager -e POSTGRES_USER=storemanager -e POSTGRES_DB=storemanager \
  -p 55432:5432 postgres:16
```

### web

```bash
cd web
python -m venv .venv && .venv/Scripts/pip install -r requirements-dev.txt
cp .env.example .env          # then fill it in
python migrate.py
python manage.py user add --email you@example.com
.venv/Scripts/uvicorn app.main:app --reload --port 8000
```

Open <http://localhost:8000>. Registration is closed by design — accounts are
made with `manage.py`.

### bot

```bash
cd bot
python -m venv .venv && .venv/Scripts/pip install -r requirements-dev.txt
cp .env.example .env          # BOT_TOKEN, API_BASE_URL, BOT_SHARED_SECRET
python -m app
```

### Registering a worker

The owner writes a **`@username`** and a salary on `/workers`. That is the whole
form — no numeric id, no name.

Telegram's Bot API cannot turn a username into a numeric id, so the binding
happens on first contact: the first time that person messages the bot, their
`telegram_id` is written to the row and from then on **the id is what identifies
them**. Usernames can be renamed or handed to somebody else; a numeric id cannot.
If the wrong person claims a handle, the owner can unbind it and the next
matching account claims it instead.

Registration stays closed throughout — an account matching nothing on `/workers`
is refused. A worker with no `@username` in Telegram has to create one first.

The **name arrives by itself** too, from the Telegram profile. Two columns keep
that honest:

* `workers.name` — what the owner typed, if anything. Wins when set, so a
  correction is never undone by the next tap.
* `workers.telegram_name` — what Telegram reports, refreshed on contact.

A worker with neither shows as `@justhayk`, never as a blank row.

### Salary

`workers.salary_amount` with `workers.salary_period`, both editable at any time:

* **`shift`** (Օրվա վերջում) — deducted from that store session's cash when the
  worker ends their shift. The system settles it.
* **`month`** (Ամսվա վերջում) — a monthly wage. Ending a shift costs the till
  **nothing**; the owner pays it separately. `salary_paid` on the shift is 0 and
  no `cash_movements` row is written.

---

## Tests

```bash
cd web && .venv/Scripts/python -m pytest        # needs the postgres above
cd bot && .venv/Scripts/python -m pytest        # no database needed
```

The web suite runs against a real Postgres because the schema does real work —
generated columns, partial unique indexes, composite foreign keys — and a fake
would exercise none of it. Migrations run once per session; each test then wraps
itself in a transaction and rolls back, so a test starts from an empty schema in
about a millisecond.

`tests/test_concurrency.py` deliberately opts out of that and uses the real
connection pool, because "exactly one sale survives ten simultaneous retries" is
a property of Postgres indexes that a single-connection test cannot exercise.

After a deploy:

```bash
cd web && python smoke.py --base-url https://your-web.up.railway.app --bot-secret ...
cd bot && TELEGRAM_ID=... API_BASE_URL=... BOT_SHARED_SECRET=... python smoke.py
```

The bot's `smoke.py` is the one that proves both services agree, because it
drives the real `Api` client rather than a hand-written request.

---

## Where things are

```
web/app/
  config.py      env + clean_dsn() + the display timezone
  db.py          asyncpg pool, Neon retry, and bind() — the test hook
  repo/          every SQL statement in the project. No logic.
  services/      the only code that moves stock or money
    geofence.py    nearest store whose own radius covers the point
    shifts.py      open / end shift / close store / auto-close
    sales.py       record_sale, void_last_sale — the atomic ones
  routes/        auth · pages · partials · bot_api
  templates/     Jinja2; base.html carries the fixed footer
migrations/      append-only, applied in filename order. Never edit a shipped one.
```

Rules worth knowing before changing anything:

* **Money is `numeric(12,2)`, always.** Never float, never integer minor units.
  The bot API passes money as decimal strings and refuses floats outright.
* **Every owned table carries `owner_id` and `UNIQUE (id, owner_id)`**, and
  children reference parents by that composite key. One owner's row physically
  cannot be attached to another's, even if a query forgets its `WHERE`.
* **Another owner's row renders 404, never 403.** A 403 confirms the id exists.
* **`possible_profit` is a generated column** — `(sell_price - self_price) * count`.
  It cannot drift from the numbers beside it.
* **The website never writes an absolute item count.** Restock is a `+N` delta,
  so a concurrent bot sale is not clobbered by a stale page.

---

## Deploying to Railway

Two services from this repo:

| Service | Root Directory | Start |
|---|---|---|
| `web` | `web` | `python run.py`, pre-deploy `python migrate.py`, healthcheck `/health` |
| `bot` | `bot` | `python -m app`, **`numReplicas` must stay 1** |

Two processes polling `getUpdates` on one bot token fight, and Telegram answers
`Conflict: terminated by other getUpdates request`. Keep the bot at one replica.

`run.py` binds `::` rather than `0.0.0.0`: Railway's private network
(`web.railway.internal`, which the bot talks to) is IPv6-only.

### Environment

**web** — `DATABASE_URL` (Neon **pooled**), `DIRECT_DATABASE_URL` (Neon direct,
used by `migrate.py`), `SESSION_SECRET`, `BOT_SHARED_SECRET`,
`APP_TIMEZONE=Asia/Yerevan`, `APP_BASE_URL`, `APP_CURRENCY=֏`. Optional:
`AUTO_CLOSE_HOURS` (16), `MAX_ACCURACY_M` (100), `DB_POOL_MAX` (5),
`SESSION_TTL_DAYS` (30), `LOGIN_MAX_ATTEMPTS` (10).

**bot** — `BOT_TOKEN`, `API_BASE_URL`
(`http://web.railway.internal:8080/api/bot/v1`), `BOT_SHARED_SECRET` — the
*identical* string as on web.

Note what the bot does **not** have: no store list, no coordinates, no radius, no
store names. The server geofences from the database, so there is nothing to keep
in sync between the two services.

### Two Neon quirks the code already handles

* Neon's URLs carry `channel_binding=require`, which asyncpg rejects outright.
  `config.clean_dsn()` strips the query string and passes TLS explicitly.
* The pooled endpoint is PgBouncer in transaction mode and cannot keep prepared
  statements, so `statement_cache_size` is forced to 0 on `-pooler` hosts.
  Migrations prefer `DIRECT_DATABASE_URL` because DDL through a transaction
  pooler is unreliable.

On the free tier Neon suspends the compute after ~5 minutes idle and the first
connection can take tens of seconds; `healthcheckTimeout` is 90 for that reason.

---

## Managing accounts

```bash
railway run --service web python manage.py user add --email you@example.com
railway run --service web python manage.py user set-password --email you@example.com
railway run --service web python manage.py user list
```

The password is read with `getpass` when `--password` is omitted, so it stays out
of shell history. Setting a password signs that account out everywhere.

There is no self-service password reset yet. The schema is already shaped for one
— `users.password_hash` is nullable, sessions are server-side rows, and
`routes/auth.py` deliberately owns only `/login` and `/logout`, leaving `/start`,
`/verify` and `/set-password` free — so adding an emailed-code flow is one new
migration and one new route file, with nothing existing moved.

---

## The bot HTTP contract

Base `POST/GET <web>/api/bot/v1/…`, header `X-Bot-Secret: <BOT_SHARED_SECRET>`
(or `Authorization: Bearer`). Every response is `{"ok": true, …}` or
`{"ok": false, "error": {"code", "message", "details"}}` where **`message` is
already Armenian and display-ready** — the bot prints it verbatim, so the two
services cannot drift apart on what a failure means.

| Method | Path | Purpose |
|---|---|---|
| GET | `/me?telegram_id=` | identity + open shift. Safe to call on every `/start` |
| POST | `/checkin` | read-only geofence probe; 200 even when out of range |
| POST | `/store/open` | geofence, open-or-join the session, start the shift → 201 |
| GET | `/items?telegram_id=&q=` | stock of the store of the open shift |
| POST | `/items` | put a new product on the shelf of that store → 201. `wholesale_price` is optional — absent means "not sold wholesale", never 0 |
| POST | `/sale` | the atomic sale → 201. `is_delivery` marks it as delivered |
| POST | `/sale/void` | undo the worker's last receipt in this shift |
| POST | `/write-off` | stock that broke or expired, off the shelf without a sale → 201 |
| POST | `/cash/withdraw` | cash out of the till, with the reason → 201 |
| POST | `/shift/close-out` | declare the day's sales and end the shift, in one transaction |
| POST | `/shift/end` | end the shift, pay the salary, close the store if last out |
| POST | `/store/close` | close the store for everyone. Owner-side only — the bot never calls it |

Error codes: `unauthorized` · `unknown_worker` · `worker_inactive` ·
`no_store_in_range` · `no_stores_located` · `location_too_vague` ·
`session_already_open` · `no_open_session` · `store_not_open` · `unknown_item` ·
`insufficient_stock` · `nothing_to_void` · `empty_basket` · `validation_error`.

**`idempotency_key` is required on every mutating endpoint** (8–128 chars). The
bot mints one per *user action* and reuses it across every retry of that action.
A replay returns the original result with `"duplicate": true` — the same answer,
not a second sale. It is the only thing standing between a flaky mobile
connection and a double-sold vape, so there is no best-effort mode.
