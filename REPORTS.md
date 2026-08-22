# Հաշվետվություններ — the report page, in full

`/reports` and `/reports?store_session_id=…`. One page, two halves: a list of every time
a shop was open, and — when a row is opened — everything that happened inside that one
session, with a correction box on nearly all of it.

This file is the map. The reasoning behind each figure lives in the docstrings it points
at; what is here is **what the page does, why each control exists, and which rules must
not be broken by accident.** Written down because the page has grown past what anybody
holds in their head, and because most of its rules were learned from something going
wrong once.

Related: [`FORMULAS.md`](FORMULAS.md) for the arithmetic, `README.md` → «What one row of
`/reports` says» for the summary version.

---

## The accounting period is a store session, not a day

A **store session** is one unlocking of one shop, from open to close. Every figure on
this page is scoped to one of those. Not a calendar day — a shop opened at 09:40 and
locked at 21:25 is one session, and if it opens again at 22:00 that is a second one with
its own drawer, its own receipts and its own report.

Two consequences worth holding on to:

* **The till is a `SUM` over `cash_movements`, never a stored balance.** «How much is in
  the drawer» is the sum of every movement of this session. That is what lets a closed
  session settle at zero while the shop's own float carries on (`repo/money.py`).
* **A worker's shift is not the period.** Two cashiers can share one session; a shift
  ending is not the shop closing. The drawer belongs to the shop.

---

## The list — `/reports`

The 50 most recent sessions, newest first (`sessions.recent_store_sessions`).

| Column | What it is |
|---|---|
| **Խանութ** | the shop |
| **Բացվել / փակվել է** | the two ends, with a badge when it was not the worker who closed: «ղեկավար» (force-closed) or «ավտոմատ» (the overnight sweep). «դեռ բաց» when it is still running |
| **Տևողություն** | open to close |
| **Աշխատող** | the *name*, and both names joined by `·` when a session really was shared. It used to be a count of shifts, which said «1» on every row |
| **Առաքում** | what went out by delivery, with the order count under it. The one figure in the row not recoverable from the others |
| **Շահույթ** | margin − wages − bonus − petty cash − this shop's expenses that day. Green or red. It is not the takings |
| **Վաճառք · Կանխիկ վաճառք · Քարտով վաճառք** | the takings and the two doors they came through. **Not** drawer balances — a wage paid has already come off those |
| **Աշխատավարձ** | what the drawer paid, from the ledger |
| | **Մանրամասն** opens the detail; **✕** deletes the whole session, closed ones only |

**The drawer is not in this table.** «Նախորդից մնացած» and «Մնաց խանութում» were columns
here, and the pair of them is what pushed the list past the width of a screen — eleven
columns about selling and two about the drawer. A list read by scrolling sideways is a
list nobody reads. Both figures are on the report a row opens, as tiles beside
«Ղեկավարին», where they can also be corrected; the list keeps the questions it is
actually scanned for. Nothing is computed for them here any more — the float aggregate
and the per-row `last_count` subquery came out of `recent_store_sessions` with them.

Two rules this table exists to keep:

* Every figure is read from the **same place the detail page reads it** — the ledger for
  money, the sale lines for margin — so a row can never contradict the page it opens.
  The profit subtraction itself lives in one function (`statistics.session_profit`) and
  is called from both.
* Figures come from the **ledger, not the closing snapshot**. The snapshot is the same
  number once a shop has shut and zero all day while it is still trading.

The list **scrolls inside itself** (`.table-scroll`, capped at `62vh` with a sticky
heading row). Fifty rows is a page and a half of scrolling before anything else on the
screen, and the detail of the row being read is underneath all of it. The receipts table
and the ledger get the same window for the same reason. Below 1200px, where every table
becomes a list of cards, the cap is removed — two scrollbars fighting over one finger is
worse than a long page.

**Sideways it must not scroll at all.** Two things keep it inside the screen: the drawer
columns are gone (above), and a numeric *heading* is allowed to wrap. `td.num` is
`nowrap` so that «-7,000.00 ֏» never breaks across two lines — but `th.num` inherited
that, and «Կանխիկ վաճառք» held on one unbreakable line was setting its column's width
for the whole table. Headings are words; the figures under them still hold their line.

---

## The detail — `?store_session_id=…`

### The tiles

Thirteen figures, and they answer different questions. They are deliberately *not* a
column of things that add up.

| Tile | |
|---|---|
| **Վաճառք** | the takings, net of reversals |
| **Կանխիկ վաճառք · Քարտով վաճառք** | how they were paid. Labelled «…վաճառք» and not «Կանխիկ», because a bare «Կանխիկ» beside «Վաճառք» reads as the drawer balance — which is a different quantity and made three figures look like a split that does not add up |
| **Առաքումով վաճառք** | how much of «Վաճառք» left by the door rather than over the counter. Already inside the two above; not a fourth kind of money |
| **Ղեկավարին** | what the owner should have received. Coloured amber — one of the two figures an owner actually looks for |
| **Շրջանառություն** | cash: վաճառք − աշխատավարձ − բոնուս − վերցված |
| **Շահույթ** | the business: margin − աշխատավարձ − բոնուս − վերցված − this shop's expenses that day. The two differ by exactly the margin, which is why both are shown |
| **Նախորդից մնացած** | **editable.** The float this session opened on |
| **Մնաց խանութում** | **editable.** What stayed on the premises |
| **Աշխատավարձ · Բոնուս** | what the drawer paid, kept apart because a bonus is not a wage |

Only two tiles are coloured. A page where every figure is coloured points at nothing.

### The sentences under the tiles

Four paragraphs, and each one exists because a figure above it was being taken on trust:

1. **The two doors.** «Վաճառքից՝ խանութում X (կանխիկ… քարտով…) · առաքումով Y (…)», plus
   the rule that a delivery counts in the shop's sales but never in the worker's own
   figures or their bonus.
2. **Both bottom lines, written out as subtractions** — every term that was taken off,
   named. Plus the note that «Օրվա ծախսեր» is only *this shop's* expenses: one filed as
   «Ամբողջ բիզնեսը» is subtracted once, in the statistics.
3. **What the drawer paid**, opening balance first: the float carried in, then wages,
   bonus, petty cash, voids, deposits and «Ճշգրտում» rows, ending at «ըստ գրքերի
   դրամարկղում մնացել է X».
4. **Where that X went**, in one of four sentences chosen by the state:
   * nobody counted → what is shown is inferred, and here is the box to correct it;
   * the count is above what the books hold → no handover, and the gap is on show;
   * the ordinary case → «Դրանից X մնում է խանութում, իսկ Y-ը՝ ղեկավարին»;
   * nothing owed → «Ամբողջը մնում է խանութում».

   Plus **«Ուշադրություն»** when the count has gone stale: its `expected` no longer
   matches the books, because a sale was voided or added after the count was taken.

A **«Հանել դրամարկղից»** link sits here, jumping to the form at the foot of the page.
The form was reported as missing when it was collapsed further down — an owner could
find no way to take cash out of the drawer, and it had been there all along.

### Հերթափոխեր

One row per worker. **Wage** and **bonus** are editable per shift and the ledger entry
moves with the figure, so the report and the till cannot end up disagreeing. Writing `0`
into the bonus removes it and its ledger row. A badge shows what the drawer was too thin
to pay («պարտք …»), in full rather than abbreviated: `short_money` renders 2,500 as
«2հզ», which is fine for a round bonus and wrong for a figure somebody is owed.

### Չեկեր

Sortable and filterable, both carried in the URL so choosing one never discards the
other:

* `?receipts=time` (default) or `name` — case-insensitive, ties fall back to time;
* `?only=all|cash|card|delivery` — three questions, **not** a partition: a delivery is
  paid in cash or by card like anything else.

Both values are looked up in fixed maps (`sales.RECEIPT_ORDERS`, `RECEIPT_KINDS`), so a
query string can only ever *choose* an order, never contribute to one; anything
unrecognised reads as the default rather than as a 400.

A receipt with exactly one line is editable in place — quantity, price, payment method —
because a close-out writes one sale per line, so most of them are. The `price_kind`
rides along hidden, so editing a quantity cannot silently re-file a wholesale line as
retail. Anything longer is **void-and-re-enter**.

**Void vs delete.** Void keeps the receipt, strikes it through, returns the stock and
writes a negative ledger row — the record says it was taken back. Delete removes it from
the report entirely. Both are undoable from `/history`.

Every correction form carries the current URL as `back`, so amending a price while the
rows are in alphabetical order does not resort them under the owner's hands.

### Ավելացնել մոռացված վաճառք

Open, not collapsed — it was behind a `▸` and got reported as missing. Worker, item,
quantity, price list, price, payment method, and **delivery yes/no**, because a
forgotten sale is most often a delivery (the cashier was on the phone, not at the till)
and whether it went out of the door is not recoverable from the row afterwards. An empty
price takes the chosen list's price; a typed one is recorded as «փոփոխված».

### Խոտան / Գրանցել խոտան

Stock that left without a sale, costed at **self price**, and it does not touch the
drawer — the money went out when the goods were bought. Deleting a write-off *does* put
the stock back: if the vape did not break, it is still on the shelf. The form is shown
even on an evening when nothing broke, because breakage found after the shop has shut
had nowhere else to go.

### Դրամարկղի հանձնում

Every count of the evening: **Դրամարկղում էր** (the books at that moment, frozen) and
**Մնաց խանութում** (the reading off the drawer). One ✓ saves the pair — the owner's
share is the difference, and saving half would leave a row whose numbers are not about
the same evening. **✕** deletes a count, and the shop's float falls back to whatever
count is now the latest, or to nothing.

There is deliberately **no «Ղեկավարին» column**: it is one figure for the evening, not
one per row (a second count *replaces* the first), so a column of them invited adding
two numbers that must never be added.

### Պահեստի ուղղումներ

Counts a cashier corrected by hand. Neither a sale nor breakage, and it touches no
money — shown because a stock number that can be changed silently is one that can hide a
shortfall. Deletable, which restores the previous count.

### Դրամարկղի շարժ

Every movement of this session, newest first. Columns: Ժամ · Տեսակ · **Ապրանք** · Ձև ·
Աշխատող · Նշում · Գումար.

`Տեսակ` is one of: Վաճառք, Չեղարկում, Աշխատավարձ, Բոնուս, Հանված, Ավելացված, Ճշգրտում.

**Ապրանք** lists what was sold, as «name ×quantity», the same rendering the receipts
table uses. Without it a column of «Վաճառք · Կանխիկ · 3,500 ֏» is a list of amounts with
nothing to check them against, and two identical rows an hour apart were told apart only
by the minute they landed. Blank — not dashed — on rows that bought no goods: a wage and
petty cash came through no door at all.

Only `withdrawal`, `deposit` and `adjustment` rows are deletable here. A `sale`, `void`
or `salary` row is one half of something else, and the cell says where to go instead
(«→ Չեկեր», «→ Հերթափոխեր»). A movement's *amount* is edited from the spending panel
(`/statistics`, `/expenses`), not from this table — here it is delete and re-enter.

### Հանել դրամարկղից · Ավելացնել գրառում

Purpose (**required** — an amount without a reason is not a record), amount, direction
(Հանել / Ավելացնել / Ճշգրտում) and method. Open rather than collapsed, and named after
the thing owners come here to do: «գրառում» is not the word somebody with cash in their
hand scans a page for.

### Այս հերթափոխի ուղղումները

Every amendment made to this session, struck through when it has been reverted. Undo
lives on `/history`.

---

## The rules that must not break

These are the ones that were learned the hard way. Each has tests behind it.

**1. The count is taken before the shop shuts, and the shop does not shut without it.**
`/shift/close-out` refuses with `till_count_required` from *inside* its transaction, so a
refused close writes nothing at all — stock on the shelf, no wage paid, shift still open.
The bot cannot know in advance whether its close shuts the shop (that depends on who else
is on), so it sends without a figure, is refused, asks, and re-sends **under the same
idempotency key**.

The refusal carries the arithmetic with it — what the drawer held, what the wage and
bonus took out of it, and what is therefore left to leave behind — so the bot can show
the subtraction instead of asking for a number against a drawer the worker has to add up
themselves. The wage leaves the till before the reading is taken, and the commonest wrong
answer was the takings *before* it did.

**2. The count is a reading, and nothing caps it.** Whatever is in the drawer goes down,
above the books as readily as below them. The drawer is sometimes ahead of the ledger —
a sale entered late, change put back, money returned from an errand — and a refusal at
that moment leaves whoever is locking up unable to shut the shop over a gap they cannot
fix from the door.

What the books said is frozen beside the reading as `expected`:

```
        the float carried in
      + today's cash sales
      − the wage just paid
      − anything taken out of the drawer
      = expected
```

so an evening where the two disagree says so on this page, which is where it can be
looked into. `Ղեկավարին` is floored at zero, so a drawer that came up over hands the
owner nothing rather than showing them a negative.

**3. The handover is shown, never booked.** No `withdrawal` row, no adjustment when the
count disagrees with the books. The ledger is the shop's record of what it took and
spent, and handing the day's money to the owner is not another expense in it. So the
session's closing cash is the day's cash as the shop earned it.

**4. A skipped count leaves the float alone.** Nothing changes `stores.till_balance`
except a count. A worker going home without counting has not emptied the shop, so the
balance stays at the last figure anybody actually gave, the report says «հաշվարկով», and
that day's cash sits in the drawer uncredited until somebody counts. It surfaces on the
next count as more in the till than the books expected — both readings are on the row —
rather than being quietly absorbed. Only the owner's force-close and the overnight sweep
can now produce such an evening.

**5. Correcting an older count does not move today's drawer.** A correction to last
Tuesday is a correction to the record; the money in the drawer today is whatever the
evenings since made it. Same rule for a reading back-filled onto an old session.

**6. The owner's share is worked out, never read back.** From the live ledger and the
last count's two readings, so it follows a sale corrected next week — and floored at
zero, so rows written before that rule cannot put a negative in the header.

**7. Deleting a report does not restore stock.** "This record should not exist" is not
"these goods came back". Reversing a sale is what the void button is for. Open sessions
cannot be deleted at all: the shifts and sales standing in them point at that row.

**8. Only this shop's own expenses come off its profit.** One filed as «Ամբողջ բիզնեսը»
belongs to the business, and charging it here would take the same rent off every shop
trading that day.

---

## Routes

Everything on the page, all `POST` unless noted, all CSRF-protected.

| Route | What it does |
|---|---|
| `GET /reports` | the list; `?store_session_id=` adds the detail, `?receipts=`, `?only=` |
| `/reports/{id}/delete` | erase a whole closed session — sales, ledger, breakage, counts |
| `/sales/{id}/amend` | quantity, price, payment method of a one-line receipt |
| `/sales/{id}/void` | reverse it: stock back, negative ledger row, row struck through |
| `/sales/{id}/delete` | remove it from the report entirely |
| `/store-sessions/{id}/sales` | add a forgotten sale |
| `/store-sessions/{id}/write-offs` | record breakage |
| `/store-sessions/{id}/movements` | take money out of the drawer, put money in, or adjust |
| `/store-sessions/{id}/carried-in` | correct the float this session opened on |
| `/store-sessions/{id}/left-in-store` | correct — or make — the reading of what stayed |
| `/till-counts/{id}/counted` | correct a count's `counted` and `expected` together |
| `/till-counts/{id}/delete` | remove a count; the float falls back to the previous one |
| `/shifts/{id}/salary` · `/shifts/{id}/bonus` | correct what a shift paid |
| `/movements/{id}/delete` | remove a withdrawal, deposit or adjustment |
| `/write-offs/{id}/delete` | remove breakage — **and put the stock back** |
| `/adjustments/{id}/delete` | remove a stock correction, restoring the previous count |

---

## Where the code is

| | |
|---|---|
| the page | `web/app/templates/reports.html` |
| the route and its helpers | `web/app/routes/pages.py` — `reports_page`, `_the_owners_share`, `_left_in_store`, `_what_the_day_made`, `_sessions_with_profit` |
| the list query | `web/app/repo/sessions.py` — `recent_store_sessions` |
| the money | `web/app/repo/money.py` — `totals_on`, `ledger_for_session` |
| the receipts | `web/app/repo/sales.py` — `receipts_in_store_session` |
| the drawer | `web/app/services/till.py` |
| corrections | `web/app/services/corrections.py` |
| the scroll window | `web/app/static/app.css` — `.table-scroll` |

Tests: `test_reports.py` (the page), `test_till_counts.py` (the drawer, end to end),
`test_the_float_carries_over.py` (the float across days), `test_corrections.py` and
`test_corrections_pages.py` (every fix on this page), `test_report_deletion.py`.
