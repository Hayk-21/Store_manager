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

The report's per-session header answers eight different questions and says so:
**Վաճառք · Կանխիկ վաճառք · Քարտով վաճառք · Ղեկավարին · Շրջանառություն · Շահույթ ·
Մնաց խանութում · Աշխատավարձ · Բոնուս**, with lines beneath breaking each subtraction
back down into its parts.

**Two bottom lines, because there are two questions**, and one figure was answering
both under the name of the other:

```
Ղեկավարին       =  float carried in + cash sales − wages paid − cash taken out
                   − left in the shop
Շրջանառություն  =  takings − wage paid − bonus paid − cash taken out
Շահույթ         =  (takings − what the stock cost) − wage paid − bonus paid
                   − cash taken out − this shop's expenses that day
```

**Շրջանառություն** is about *cash*: what the day put into the business, counting the
whole selling price, because the stock was paid for when it was bought. That is the
figure to check the drawer against. It was labelled «Շահույթ», which it is not — a shop
selling 45,000 of stock that cost 40,000 read it as a very good day.

**Շահույթ** is about the *business*: the same day with the cost of goods taken off and
the owner's own typed expenses with it. The two differ by exactly the margin, which is
the whole reason both are worth showing.

Only expenses attached to **this shop** are in it. One left as «Ամբողջ բիզնեսը» — rent,
advertising — belongs to the business rather than to the branch that happened to be
open, and counting it here would take the same rent off every shop trading that day.
Those come off once, over a period, on the statistics page.

Breakage is in neither: the money left when the vape was bought, not when it was
dropped, and the cost of goods in «Շահույթ» already accounts for stock that actually
sold. It is named underneath both. Voided sales are out of both, for the same reason
the statistics leave them out.

**Ղեկավարին**, **Շրջանառություն** and **Շահույթ** are the figures an owner is actually
looking for, so those are coloured and the rest are not.

**Takings are net of reversals.** A voided sale is not a sale, and the tiles summed the
sale rows while leaving the reversals beside them — so a day where everything was taken
back read as a day that sold 7,000 and made money on it.

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

**A session nobody counted still says what the owner is owed.** Both figures used to read
zero, which is not what happened to the money — a shop that took 24,900 on a 400 float
showed the owner nothing on a day it had taken 24,500 for them. A worker who goes home
without counting has not emptied the shop: nothing changes `stores.till_balance` except a
count, so the float stays exactly where it is and the rest is the owner's. That is not a
guess about the drawer — it is what the system will act on when the shop opens tomorrow.
The page says the figures were worked out rather than declared, and points at the box
that corrects them.

The float is identified as **the one deposit nobody typed** (`kind = 'deposit' AND
created_by = 'system'`), so an owner topping the drawer up mid-shift is not mistaken for
it and does not quietly reduce what they are handed. That required fixing
`money.record_movement`, which labelled the owner's own deposits `'system'` against the
column's documented meaning — `corrections.add_movement` had always got it right.

**Բոնուս is its own tile, and its own correction.** A bonus is not a wage: an owner
reads that figure against the rate they set for somebody, and «Աշխատավարձ 5,500» over a
worker on 3,500 reads as an error. It is editable per shift for the same reason a wage
is — `set_bonus` moves `work_sessions.bonus_paid` and the ledger row together, and
writing 0 removes both. Deleting the ledger row on its own is refused: it is one half of
a shift, exactly like a wage, and that door was open, leaving the shift still claiming a
bonus the ledger had no record of.

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

### What one row of `/reports` says

A row is one time a shop was open, and it answers the questions an owner scans a list
for — **who worked it, what it sold, what it made**:

* **Աշխատող** is the name, not a count of shifts. One person opens a shop and closes
  it, so the count said «1» on every row for as long as it existed. Two names joined
  by `·` when a session really was shared, because silently showing one of them would
  be a lie.
* **Առաքում** is what went out by delivery — the same money through a different door,
  and the one figure in the row that cannot be worked out from the ones beside it.
* **Շահույթ** is what the shift made: margin, less wages, bonus, petty cash and this
  shop's own expenses for the day. It replaced «Հասույթ» — the takings — which a shop
  selling 45,000 of stock that cost 40,000 read as a very good day. The subtraction
  lives in `statistics.session_profit` and is called from both the column and the
  header of the report it opens, so a list and the page it links to cannot disagree
  about the word.
* **Վաճառք · Կանխիկ վաճառք · Քարտով վաճառք** are the takings and the two doors they
  came through. The columns here before were the drawer *balances*: a wage paid or
  money taken out has already come off those, so «Կանխիկ 83,500» sat beside «Վաճառք
  57,500» looking like a split that does not add up.
* **Մնաց խանութում** is the evening's last count of the drawer — or, when nobody
  counted, the float the shop opened with, marked «հաշվարկով» because that one was
  worked out rather than declared. Not zero: a worker going home without counting has
  not emptied the shop. That rule lives in `_left_in_store` and the report header
  calls it too.

Every figure is read from the same place the detail page reads it — the ledger for the
money, the sale lines for the margin — and from the ledger rather than from the
snapshot written at closing time, which is the same number once the shop has shut and
zero all day while it is still open.

### Reading the receipts in either order

`?receipts=time` (the default) or `?receipts=name`. A report is read as an evening, so
time order is what it opens in; but the other question this table gets asked is "did we
sell any Aokit tonight", and time order answers that by making somebody read forty rows.
Names sort case-insensitively — «Aokit» and «aokit» are one thing on a shelf and should
not be two blocks in a list — and ties fall back to time, so a group keeps the reading
of the evening inside it.

The value is looked up in a fixed map (`sales.RECEIPT_ORDERS`) rather than interpolated,
so a query string can only ever choose an order, never contribute to one; anything
unrecognised reads as the default rather than as a 400. Every correction form on the
page carries the current URL as `back`, so amending a price while the rows are in
alphabetical order does not resort them under the owner's hands.

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
* **how the money arrived, and where it was handed over** — cash beside card, counter
  beside delivery, each as a comparison rather than one figure and a percentage the
  reader has to subtract from a hundred in their head. Both sides carry revenue, share,
  receipts and the average receipt, and the counter/delivery split carries **margin**
  too: running deliveries costs something, and the only way to see whether it pays is to
  read what it earns beside what the counter earns. A shop can turn over more on
  delivery and make less;
* **money given away at the counter** — sales at neither list price. Its own figure
  rather than folded into retail, because the only way to notice a habit of it is to see
  the total;
* the best and worst *trading* day of the period, both linked. The worst is the worst day
  the shop traded, not the worst bar: a range with a closed Sunday in it would otherwise
  always answer "nothing, on the day you were shut". Days that sold nothing are counted
  separately, which is the figure that says something about them;
* **what broke** — the write-offs, on their own card with their own total.

### Breakage is not spending

A vape that falls off the shelf is **stock lost, not money paid**. Nothing left a drawer
or an account the day it broke: the money left when the goods were bought. Counting a
write-off as spending charged the business twice for the same thousand drams — once as
stock, once as a payment — and made «Խոտան» look like a row somebody had paid out.

So it is in no spending figure: not in the `Ծախսեր` tile, not in the payments list, not
in the ring, and not subtracted from profit. It keeps its own card on `/statistics` — the
products, the quantities, the reasons and the cost — which says in as many words that it
is a loss rather than a payment, and still carries the delete that puts the stock back if
the vape did not actually break.

That also settles a disagreement the two pages used to have: a day's profit on the report
never subtracted breakage, and the month's on `/statistics` did. They now use the same
arithmetic, so a day and the month containing it mean the same thing by «շահույթ».

### Every payment, one row each

Money leaves the business by three doors — a **wage** (and a **bonus**, its own row), a
cashier taking **petty cash**, an **expense** the owner types — and each had its own page.
So the only view of the whole was an aggregate row, «Աշխատավարձ · Փակված հերթափոխեր ·
18,000». True and unanswerable: which worker, which shop, who took what out of the drawer
and what for.

`repo/spending.py` unions the three into one list, and `_spending.html` renders it on
**both** `/statistics` and `/expenses` — "what did this month cost" is asked from both,
and the expenses page was answering it with only the entries typed on it.

Every row can be corrected where it stands, posting to the same endpoints the store page
and the report use rather than to a second set. The amount is editable inline for a wage,
a bonus, a withdrawal and an expense; a withdrawal and an expense can be deleted. A wage
or a bonus cannot: each is one half of a shift, and removing it alone would leave the
shift claiming it paid something the ledger does not have — writing 0 is the same act
done properly and keeps the two together.

Each endpoint names its own field — a wage posts `salary`, a bonus `bonus` — and this
list has to use the endpoint's name rather than one of its own. It posted `amount` to all
of them, so a wage edited from here arrived with the field the route reads left empty and
came back «պարտադիր է» every time, while the withdrawal and expense rows beside it worked.

**The row cap is never a cap on the money.** The list stops at 500 rows and says so;
`totals_between` sums the whole period in SQL. Summing the returned rows instead made a
busy month's figure quietly stop at the five-hundredth payment and report the rest as if
it did not exist — wrong in the one direction nobody checks, because a total that is too
small still looks like a total.

### Where the month went — the ring

`/expenses` announced **«0.00 ֏ · 0 գրառում»** directly above a list of twenty-two
payments. The headline was counting the expenses typed on that page, and a month can
easily have none of those and still cost hundreds of thousands in wages and petty cash.
It now leads with every payment; the typed ones stay as their own tile, named, because
"what did I enter by hand" is a real question — just not the headline one.

Above the list, on both `/expenses` and `/statistics`, is a donut of the same period split
by **what the money was for**: `Աշխատավարձ`, `Բոնուս`, `Դրամարկղից վերցված`, and each
expense category by name. Split by which door a payment came in through instead, and
three-quarters of a normal month lands in one nameless lump — the chart would be drawing
the software's own plumbing.

It is server-rendered inline SVG (`app/charts.py`, `_donut.html`): one `<circle>` per
slice, `stroke-dasharray` on a radius of 15.9155 so the circumference is exactly 100 and
a dash length *is* the percentage. No JavaScript, no request for a chart library, and it
prints.

The palette is six hues checked with the `dataviz` validator against the dark surface —
lightness band, chroma floor, adjacent-pair colour-vision-deficiency separation,
normal-vision floor, contrast. Donut segments touch only their neighbours, so the
adjacent-pair list is the one that applies. Past six the tail folds into a grey «Այլ»
rather than a seventh generated hue, which to a colourblind reader is not a new colour.
The legend under it carries swatch, name, amount and share for every slice: identity is
never colour alone, and it doubles as the table view.

**Every slice is a link to the payments behind it** — the arc and its legend row both.
The first question about «Աշխատավարձ · 184,000» is *which* wages, and a chart that cannot
be asked leaves the owner scrolling a list of everything to find out. Clicking narrows the
list and the total under it; clicking the same slice again goes back to the whole, which
is what anybody expects from a second click.

Three things deliberately do **not** move when a slice is chosen: the ring keeps all of
its categories, because it is what the choosing is done from and a ring redrawn as one
remaining slice leaves nothing to click back to; the headline stays the period's own
total, so a filter can never be mistaken for a shrinking month; and the list says in words
which category it is showing, with the way back beside it.

The filter travels as a repeated `?category=` rather than one joined value — an owner
names their own categories and a name is free text, so any separator we picked would one
day be inside one. `«Այլ»` is the slice that is not a category: it carries the labels of
the whole folded tail, so it can be clicked like the rest instead of being the one part of
the ring that does not answer. A correction made under a filter returns to the filtered
page, not to a silently widened one.

`?back=` carries the page the correction was made on, so a fix on one list does not land
on the other. Only a path of this site: an open redirect is a phishing tool. Rejecting a
leading `//` is not enough — browsers read a backslash in that position as a forward
slash, so `/\evil.example` normalises to `//evil.example` after passing a check that only
looked for two slashes. Backslashes and control characters are refused outright: there is
no legitimate one in a path this application generates.

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

### Typing an amount into the bot

`format.parse_money` is the one place a typed amount is read, and it has to accept two
grouping conventions without ever guessing wrong between them.

The bot prints every amount comma-grouped with no decimals — `40,000 ֏`, from
`format.money` — and a worker who reads that off the screen types it back the same way.
But every prompt asking for a number was written for the Armenian convention,
space-thousands and comma-decimal (`1 000,00`), from before the bot echoed amounts back
at all. A bare `.replace(",", ".")` cannot honour both: it turned `40,000` into `40.00`
— forty dram typed as forty thousand — and raised nothing, because the result was still
a valid number. The same parse sat in five handlers: the till count, petty cash, a sale
price, a write-up price, and a new item's cost.

The two conventions agree on everything except what a comma means, and that is settled
by what follows it: a comma followed by exactly three digits, with digits before it, is
a thousands separator; any other comma is a decimal point. That split is safe here
*because* a fractional dram is never a real amount, so no comma is legitimately followed
by three decimal places.

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

**"Already earned this period" is read off the shift row, not the ledger.** A bonus
earned against an empty drawer books no `cash_movements` row — there is nothing to pay
it with — and the guard used to look for exactly that row. So a worker who crossed the
target on a day taken entirely on card crossed it "again" on their next shift and was
credited a second full bonus for one achievement. `bonus_paid` is set the moment a bonus
is credited, whether or not the drawer could cover it, which is the fact the guard
actually needs.

**A shared drawer is locked, not just a shared worker.** `withdraw_by_worker` locked the
calling worker's own shift row, which serialises that worker's own double-taps but not
two different cashiers on the same store session: both could read the same "1,500 in the
till" and both approve a 900 withdrawal against it, each under every limit on its own and
the till at -300 between them. It locks the store session now.
`web/tests/test_till_concurrency.py` is the one file in the suite that opens a real
connection pool rather than binding a single transaction, because a race between two
workers is a race between two connections and cannot be reproduced on one.

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
