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
                          (till starts at the shop's float)
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

**Each shop keeps its own float, though.** `stores.till_balance` — its *casa*.
Might be nothing, might be forty thousand, differs from shop to shop. It is not
takings and it is not the owner's yet: it is the change the next person needs to
open up with. A session seeds its till from that column as an ordinary `deposit`,
which keeps "how much is in the till" a sum over one table.

One person sets it, once, and it is **whoever locks up**. `💰 Դրամարկղի մնացորդ` is
offered on the message that ends the last shift and on the off-shift keyboard, and
the server refuses a count while the session is open (`store_still_open`). Both the
button's position and the refusal are the same rule: the count is the closing act.

That ordering is not decoration. The owner's share is the till less the float, so
anything still to come out of the till — a wage above all — has to come out before it
is worked out. The button used to be on the working keyboard: a worker counted up at
21:07 and was quoted a handover of 82,000, then paid 6,000 at 21:10, and the shop
closed showing cash of **-4,500** — the owner's figure 6,000 too high and the drawer
recorded as holding less than nothing. A mid-shift count is also stale on the very
next sale, and one of two cashiers going home cannot settle the change the other needs
for the next four hours.

What they leave becomes the store's balance. Everything else in the drawer is the
owner's:

```
the owner's share  =  what the till held  −  what was left behind
                   =  (yesterday's float + today's takings − wages − petty cash)
                      −  the new float
```

**Shown, not booked.** That subtraction is a figure on the report and nothing else —
no `withdrawal` row for it, and no adjustment when the count disagrees with the
books. The ledger is the shop's record of what it took and spent, and handing the
day's money to the owner is not another expense in it. So a session's closing cash is
the day's cash as the shop earned it, and «Ղեկավարին» on the report is worked out
from it against what the worker left.

It is **floored at nothing**. Leaving more than the books say the drawer held — an
unrecorded sale, a miscount, somebody topping it up — means the owner gets nothing
from this shop today, not that they owe it money; «-5,000 ֏» beside "hand to the
owner" is not a figure anybody can act on. The gap is not swallowed: `expected` sits
beside `counted` on the count, so both readings are on the report.

**Nobody is asked at the start of a shift.** That asked a worker to answer for a
drawer somebody else had filled, and bound nobody to anything. The float is what the
last person said they left, and the owner can correct it two ways when that is wrong:
«Ուղղել մնացորդը» on the store page, or the editable «Մնաց խանութում» box on the
report. Neither touches the ledger — the balance is what stays on the premises and
the till is what the shop took, and since the handover stopped being booked those are
no longer the same quantity.

The report's per-session header answers seven different questions and says so:
**Վաճառք · Կանխիկ վաճառք · Քարտով վաճառք · Ղեկավարին · Շահույթ · Մնաց խանութում ·
Աշխատավարձ**, with two lines beneath breaking down the last two of those.

**Շահույթ** is the point of the page and was missing from it. Revenue is the loudest
figure there and the least useful alone — a shop can sell 101,500 of stock that cost
90,000 and pay 7,000 in wages out of it. It is the same subtraction the statistics page
makes over a period, applied to one evening:

```
gross    =  what the goods sold for  −  what they cost
the day  =  gross  −  wages  −  petty cash  −  breakage
```

Breakage is in it because that stock was paid for and did not come back; it never
touched the till, which is exactly why it would go missing unless taken off here. Voided
sales are out, for the same reason the statistics leave them out.

**Ղեկավարին** and **Շահույթ** are the two figures an owner is actually looking for, so
those two are coloured and the rest are not. Two colours and no more — a page where
every figure is coloured points at nothing.

The whole header is one arithmetic, and it is the owner's:

```
Ղեկավարին  =  float carried in + cash sales − wages paid − cash taken out
              − left in the shop
Շահույթ    =  takings − wage paid − bonus paid − cash taken out
```

**What the stock cost is not in the profit**, and that is consistent rather than an
omission: the shop paid for those vapes when it bought them, so on the day one sells,
the whole price is money the business is better off by. It is the same reasoning the
page has always given for breakage — «փողը դուրս է եկել գնելու պահին» — and breakage is
out for the same reason. What the goods themselves earned is stated under the tile, so
the margin is in front of the owner without being subtracted from a figure about cash.
The statistics page takes the other view over a period, where margin and the cost of
goods are what the question actually is.

**Takings are net of reversals.** A voided sale is not a sale, and the tiles summed the
sale rows while leaving the reversals beside them — so a day where everything was taken
back read as a day that sold 7,000 and made money on it.

**A report on one evening is a report about that evening's money**, so the wages in it
are what the drawer *paid*. Those stopped being the same as what the shift cost when the
drawer started paying only as far as it reaches, and the figures beside them —
«Ղեկավարին», «Մնաց խանութում» — are drawer questions. What the till could not cover is a
real liability, so it is stated underneath with the warning that the profit above will
drop by it the day it is settled, rather than folded into a number about tonight's cash.

The statistics page takes the other view over a period, where the wage bill has to be
what the shifts *cost* or an unpaid wage would never appear in the books at all. Bonuses
belong in that bill and were missing entirely — `salaries_between` summed `salary_paid`
alone, so a target-beating month cost the business nothing in every figure there.

**Ղեկավարին comes from the books as they stand, not from the reading frozen on the
count.** A sale the owner enters afterwards has to reach it: a shop whose drawer really
held 2,000 was told the owner was owed nothing, because the count had been made when only
the 234 float was in it. The count keeps its own reading beside it, and the page says so
when the two have drifted apart.

**Բոնուս is its own tile.** A bonus is not a wage: an owner reads that figure against the
rate they set for somebody, and «Աշխատավարձ 5,500» over a worker on 3,500 reads as an
error.

Every one of those tiles is a different question, so the page **says why the answers are
what they are**. A «Ղեկավարին» of nothing over a day that took 45,400 is startling, and
the arithmetic behind it — card money never reaching the drawer, a wage and a withdrawal
emptying what did — is not something an owner should reconstruct. Where the count claims
more was left than the till ever held, it says that instead; where nothing has been
counted, it says that rather than showing two zeroes that look like a settlement.

It used to read «Կանխիկ · Քարտ · Վաճառք · Աշխատավարձ», where the first was the drawer
*balance* — takings less wages, petty cash and the handover — sitting beside two
figures about takings. So «Կանխիկ 2,500 · Քարտ 16,000 · Վաճառք 101,500» read as a
payment split that does not add up, when the cash sales were 85,500. Nothing was
miscomputed; the labels described the wrong kinds of thing. The split is still there
and is now labelled «կանխիկ *վաճառք*», because a bare «Կանխիկ» beside «Վաճառք» is
exactly what invited the wrong reading.

**Ղեկավարին** is the last count's two readings subtracted, floored at nothing. From the
count rather than from the live ledger because both of those readings are editable, and a
header computed from the ledger instead would sit there contradicting the row the owner
had just corrected. Floored because rows written before that rule exist: the shop that
closed on -4,500 carries a count saying the owner is owed -7,000, and nobody can act on
that. The ledger's own figure is printed beside it, and the report says plainly when the
count has drifted from it.

Several workers can share one store session. The first to arrive opens it; the
others join. Each gets their own shift row and their own salary.

Closing the shop is not a button a cashier has. «Ավարտել իմ հերթափոխը» opens the
«Հերթափոխի ամփոփում» — the write-up of what they sold — and ends their own shift
and nobody else's. The session closes on its own once the last of them has left,
so no cashier can end a colleague's day by mistake. The owner can still force one
closed from `/reports`.

Before the write-up the shift is **read back in full** (`GET /shift/review`): what
went out, what was thrown away, what was corrected on the shelf, what moved to or
from another shop, what came out of the drawer for petty cash, what the drawer holds
now and what the shift is about to pay. This is the last moment any of it can be
fixed without the owner, so showing only the sales — which is what it did — was
showing one of the things it settles.

Sections with nothing in them are left out; a heading with nothing under it reads as
something that failed to load. Sections are also cut off past 25 rows, and say so:
Telegram *rejects* a message over 4096 characters rather than trimming it, so an
unbounded list does not make a long screen but an empty one, on exactly the shifts
that most need reading back.

### What the owner can fix on a report

Everything a cashier can get wrong, from `/reports?store_session_id=…`, because the
alternative is an owner reading a figure they know is wrong and having no way to say
so:

* a sale's quantity, price or payment method — or void it entirely;
* **a sale the cashier forgot**, under «Ավելացնել մոռացված վաճառք», which is open
  rather than collapsed: it was behind a `▸` and got reported as missing, and a
  triangle beside a heading does not read as "there is a form here";
* **both figures on the drawer table**, editable in place with one ✓ for the pair.
  Neither is a claim about what happened to any money: «Մնաց խանութում» is a reading
  off a drawer at the door and «Դրամարկղում էր» a reading off the books at the same
  moment, frozen there — so a sale voided afterwards leaves the second describing books
  that have since changed, and the report says so when the two have drifted apart. The
  owner's share is the difference, so it follows either; the shop's float follows the
  count, and correcting an *older* count deliberately leaves the float alone. «Դրամարկղում
  էր» may be negative, because under the old rules a drawer really was recorded as
  holding less than nothing and a row saying so is a record of that evening;
* **a whole count deleted**, for a reading that should never have been there: a
  duplicate, one against the wrong shop, a row left behind by a rule that has since
  changed. The float falls back to whatever count is now the latest, or to nothing if
  that was the only one. Undoable like every other deletion — the audit payload
  carries the entire row, including `created_at` so the restored count lands back in
  its own place in the evening, because nothing else holds it once it is gone;
* a shift's wage, which also clears whatever the till was too thin to pay;
* breakage and shelf corrections;
* a ledger entry, added or removed.

### The float, end to end

`web/tests/test_the_float_carries_over.py` holds the chain rather than its links: what
worker A leaves is what worker B finds, in the store's balance, in the till B's session
opens with, in what the bot reads back to B, in the owner's share at the end of B's day,
and in tomorrow's float again. Every link can be right and the chain still wrong — that
is the exact shape of the bug that had somebody hand over 82,000 and then be paid out of
an emptied drawer.

One consequence worth knowing: **a skipped count leaves the balance alone.** A worker who
goes home without counting has not said the drawer holds nothing, so the float stays at
the last figure anybody actually gave, and that day's cash sits in the drawer uncredited
until somebody counts. It surfaces on the next count as more in the till than the books
expected — both readings are on the report — rather than being quietly absorbed.

## Statistics

`/statistics` over a preset period, or over **any range by date**: `?since=&until=`.
That is what makes the chart clickable — each bar links to its own day, or to its own
week once the range is long enough that the bars are buckets. "What happened on the 8th"
is the question a chart provokes, and the only answer used to be to go and change the
filter to something that happened to contain it. A range asked for by date calls itself
`custom` and is listed in the period selector while you are looking at it, because
leaving nothing selected showed «Այսօր» over a page about the 8th.

Beyond the headline figures, the page answers:

* **when** the shop sells — takings by hour of the trading day, over the whole period.
  The one question the daily chart cannot answer and the one a rota is built from. All 24
  hours are drawn, including the dead ones, for the same reason the daily chart fills its
  gaps;
* how the money arrived — the cash/card split, which matters because card takings are
  already in the bank and cash is not;
* **money given away at the counter** — sales at neither list price. Its own figure
  rather than folded into retail, because the only way to notice a habit of it is to see
  the total;
* the best and worst *trading* day of the period, both linked. The worst is the worst day
  the shop traded, not the worst bar: a range with a closed Sunday in it would otherwise
  always answer "nothing, on the day you were shut". Days that sold nothing are counted
  separately, which is the figure that says something about them;
* **what broke** — the write-offs behind the breakage figure. It was the one third of
  «Ծախսեր» with no way back to the things it was made of.

### Every payment, one row each

Money leaves the business by four doors — a **wage** (and a **bonus**, its own row), a
cashier taking **petty cash**, an **expense** the owner types, stock **written off** —
and each had its own page. So the only view of the whole was two aggregate rows,
«Աշխատավարձ · Փակված հերթափոխեր · 18,000» and «Խոտան · Դուրս գրված ապրանք · 10,976».
Both true, neither answerable: which worker, which product, who took what out of the
drawer and what for.

`repo/spending.py` unions the four into one list, and `_spending.html` renders it on
**both** `/statistics` and `/expenses` — "what did this month cost" is asked from both,
and the expenses page was answering it with only the entries typed on it.

Every row can be corrected where it stands, posting to the same endpoints the store page
and the report use rather than to a second set. The amount is editable inline for a
wage, a withdrawal and an expense; a withdrawal, an expense and breakage can be deleted.
A wage cannot: it is one half of a shift, and removing it alone would leave the shift
claiming it paid something the ledger does not have — setting it to nothing is the same
act and keeps the two together. Deleting breakage puts the stock back, because if the
vape did not break it is still on the shelf.

`?back=` carries the page the correction was made on, so a fix on one list does not land
on the other. Only a path of this site: an open redirect is a phishing tool.

`Asia/Yerevan` is used for *displaying* times, for grouping report rows and for deciding
which hour a sale falls in. It never decides which bucket money lands in.

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

**A shift under 8 hours pays half.** The figure is a day's pay, and somebody who
left after two hours has not worked a day. One step rather than pay-by-the-minute,
because the wage is agreed as a day rate and billing it by the second turns every
late start into an argument about four minutes. Applied in
`shifts._pay_and_close_shift`, which every close path goes through — the worker
ending their own shift, the write-up, the last one out, the owner forcing it and
the auto-close — so the five cannot disagree about what a day's work is. The bot
tells the worker why the number is half.

**The drawer pays what it has.** A shop that took the whole day on card cannot
settle a cash wage out of a thin float, and for a while it tried: 1,000 in the
till, a 3,500 wage, and a session closing with cash of **-2,500** — a drawer
holding negative money. Everything downstream then computed from that fiction; the
worker who left 5,000 in the shop was told the drawer had held 7,500 more than
expected.

So the till pays as far as it reaches and the rest is a debt, on
`work_sessions.salary_unpaid` (and `bonus_unpaid`). The wage first, then the
bonus — when there is not enough for both, what is owed for the day should be the
part in the worker's hand.

`salary_paid` keeps its meaning throughout: **what the shift cost.** The wage bill
in the statistics and the box the owner edits on the report do not change because
the cash happened to be short that night, and the unpaid part is carried beside it
rather than subtracted from it. Netting the two would answer neither "what did this
shift cost" nor "what is this person still waiting for". The bot says both, and the
report shows a `պարտք` badge beside the wage.

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
    stock.py       counts corrected by hand, logged with a name against them
    till.py        the shop’s float, the closing count, and the owner’s share of it
    transfers.py   stock moved between two of one owner's shops
  routes/        auth · pages · partials · bot_api
  templates/     Jinja2; base.html carries the fixed footer
migrations/      append-only, applied in filename order. Never edit a shipped one.
```

Four things move stock, and they are deliberately different tables:

| | What it means | Money | Stock |
|---|---|---|---|
| `sales` | somebody bought it | till up | down |
| `write_offs` | it broke, leaked or expired | none — the money left when it was bought | down |
| `stock_adjustments` | the count on the screen disagreed with the shelf | none | either way |
| `transfers` | it is on a different shelf now | none | down here, up there |

Folding any of them into another tells the reports a lie: a transfer recorded as a
write-off plus a correction reads as breakage that never broke, and a correction
recorded as a sale invents revenue. The last two are logged with a worker's name
against them, because a count that can be changed silently can be changed to cover
a shortfall.

**A transfer needs an answer, unless the owner made it.** Two shops are two people:
a cashier cannot reach into a shelf they are not standing at, so their request sits
at `pending` until a worker at the *source* shop approves it, and approving is what
moves the stock — an approval that did not move it would be a promise, and the shelf
would disagree with the screen until somebody noticed. The owner sees both shelves
and answers to nobody, so `/transfers` on the website applies immediately and records
itself as decided by them. The quantity is always *added* at the far end: a transfer
tops a shelf up, it does not replace what is on it, and the cost price travels with
the box so moving stock cannot revalue it.

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
| POST | `/items/adjust` | correct several counts at once → 201. Signed deltas; logged against the worker |
| GET | `/transfers/stores` | the owner's other shops, to ask one of them for stock |
| GET | `/transfers/items?store_id=` | another shop's shelf — names and counts, no prices |
| POST | `/transfers` | ask that shop for a box → 201. Moves nothing yet |
| GET | `/transfers/pending` | requests waiting for an answer at this shop |
| POST | `/transfers/{id}/decide` | approve (moves the stock) or reject |
| POST | `/sale` | the atomic sale → 201. `is_delivery` marks it as delivered |
| POST | `/sale/void` | undo the worker's last receipt in this shift |
| POST | `/write-off` | stock that broke or expired, off the shelf without a sale → 201 |
| POST | `/cash/withdraw` | cash out of the till, with the reason → 201 |
| GET | `/shift/review` | the whole open shift: sales, breakage, shelf corrections, cash out, the drawer, the wage due |
| POST | `/shift/till` | what the worker leaves in the drawer; the rest is booked as handed over → 201 |
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
