# Every figure, and how it is worked out

Written 2026-08-14, after separating delivery orders from counter sales.

The rule underneath all of it:

> A **delivery** is money the shop took. It is **not** a sale the worker made.
> Somebody entered an order that arrived by phone; nobody stood at the counter and
> sold it.

So wherever the subject is **the worker** — their own screen, their write-up, their
bonus — deliveries are excluded and shown separately. Wherever the subject is **the
shop** — the owner's report, the statistics page — everything is counted, and the
split is shown beside it.

Two words used throughout:

* **counter sale** — `sales.is_delivery = false`
* **delivery** — `sales.is_delivery = true`

A **voided** sale is never in any figure below. It was taken back, so it is not a
sale, and the reversing ledger row carries the id of the sale it reverses — which
is what keeps a voided delivery netting out of the *delivery* half rather than
against the counter.

---

## 1. What the worker sees in the bot

`sales.summary_for_work_session` — one shift, voids excluded.

```
Ձեր վաճառքը          = Σ sales.total   where work_session = this shift
                                         and NOT is_delivery
  կանխիկ             = the same, and payment_method = 'cash'
  քարտ               = the same, and payment_method = 'card'
  չեկ                = count of those sales

Առաքում              = count of sales  where work_session = this shift
                                         and is_delivery
                       — the count, and nothing else
```

**The worker is never shown what a delivery was worth.** They did not sell it, and
neither their wage nor their bonus turns on it, so what it came to is the owner's
business. What they do get is the count — enough to tell that the orders they
entered actually landed — and, in the write-up, the products and quantities, so the
shelf they are about to be asked to count adds up.

**The two never add together on the worker's screen.** «Ձեր վաճառքը» is the first
line, «Առաքում» is its own line underneath, and nothing is shown at all on a shift
that took no orders.

Where it appears: the status screen (`/me`), the end-of-shift summary, and the
write-up before closing (`/shift/review`), where the products are listed in two
sections — «Գրանցված վաճառք» with amounts, «Առաքում» without.

### The running line after each sale

```
Ձեր վաճառքը՝ կանխիկ X · քարտ Y
  X = Σ ledger amount  where kind in ('sale','void') and NOT is_delivery
                         and method = 'cash'
  Y = the same, and method = 'card'
```

This line used to be the **drawer** and the shop's card income — which is how a
cashier who had taken one card payment of their own came to read «Քարտ՝ 16,000»
with 3,000 of somebody else's delivery inside it.

### The one figure that keeps its deliveries

```
Դրամարկղում կանխիկ = Σ ledger amount where method = 'cash'   ← everything
```

**A delivery paid in cash at the door is physically in the drawer.** This is the
number the worker counts against when they close up, so taking anything out of it
would make their count come out over. It is labelled as the drawer and it is the
only worker-facing figure that still contains delivery money.

The card line that used to sit beside it on the status screen is gone: card money
is not in the drawer, and the worker's own card sales are already in «Ձեր վաճառքը».

The delivery money still exists, is still in every owner-facing figure below, and
is still sent to the bot by the API; the bot simply never puts it on the screen.

---

## 2. The bonus

`tracking.sold_by_worker_since`, read inside the transaction that closes the shift.

```
sold = Σ sales.total  where worker = this worker
                        and sold_at >= start of the bonus period
                        and voided_at is null
                        and NOT is_delivery          ← this is the change

bonus is paid  ⟺  sold >= workers.bonus_threshold
                   and no bonus already paid this period
```

The period start is the shop's own trading day (or calendar month), using the
store's `day_start_hour` — the same boundary the takings use, so a shop open past
midnight does not have its evening split across two bonus days.

**Once per period, not per shift.** Somebody who crosses the target in the morning
and works again in the evening has earned it once.

`bonus_paid` on the shift records what was *earned*; if the till could not cover it,
the remainder is in `bonus_unpaid` and is a debt, not a saving.

---

## 3. What the owner sees on a report

`money.totals_for_session` — off the ledger (`cash_movements`), not off `sales`.

```
Վաճառք           = Σ amount  where kind in ('sale','void')
Կանխիկ վաճառք    = the same, and method = 'cash'
Քարտով վաճառք    = the same, and method = 'card'

խանութում        = Σ amount  where kind in ('sale','void') and NOT is_delivery
  կանխիկ/քարտով  = the same, split by method
առաքումով        = Σ amount  where kind in ('sale','void') and is_delivery
  կանխիկ/քարտով  = the same, split by method
```

and by construction:

```
խանութում + առաքումով = Վաճառք
```

**The owner's total is unchanged.** These say what it is made of.

The rest of the report header, unchanged by this work:

```
Ղեկավարին        = max(0, till cash − what stays in the shop)
Մնաց խանութում   = the evening's last count, or the float the shop opened with
                   when nobody counted
Շրջանառություն   = Վաճառք − աշխատավարձ − բոնուս − դրամարկղից վերցված
Շահույթ          = (վաճառք − ինքնարժեք) − աշխատավարձ − բոնուս
                   − դրամարկղից վերցված − օրվա ծախսեր
```

Wages here are what the **till paid**, not what the shift cost; what it could not
cover is stated separately, because both figures drop by it the day it is settled.

---

## 4. The statistics page

All from the sale lines (`sale_items`), which carry the price and cost as they were
at the moment of sale — so repricing a vape today cannot rewrite what last month
earned.

```
Վաճառք               = Σ line_total
Կանխիկ / Քարտով      = Σ line_total, split by sales.payment_method
Առաքում              = Σ line_total  where is_delivery
Մեծածախ              = Σ line_total  where price_kind = 'wholesale'
Ապրանքի վրա շահույթ  = Σ (unit_price − unit_cost) × quantity
ապրանքի ինքնարժեք    = Վաճառք − Ապրանքի վրա շահույթ

Ծախսեր   = աշխատավարձ + բոնուս + դրամարկղից վերցված + այլ ծախսեր
Շահույթ  = Ապրանքի վրա շահույթ − Ծախսեր
```

«Վաճառքի կտրվածքով» splits the first line by door, each with its own cash and card:

```
Խանութում  = Σ line_total where NOT is_delivery   (+ cash/card, + margin)
Առաքումով  = Σ line_total where is_delivery       (+ cash/card, + margin)
```

Breakage is in none of it: the money left when the goods were bought, not when they
broke. It has its own figure and its own list.

An expense filed as «Ամբողջ բիզնեսի համար» is subtracted here, once, and belongs to
no single shop's report — which is the one legitimate reason this page and the sum
of the reports under it can differ.

---

## 5. How to check any of it yourself

Every figure above is one `sum` over one table with one filter. To check a shift:

```sql
-- what the worker sold, and what merely passed through
SELECT is_delivery, payment_method, count(*), sum(total)
  FROM sales
 WHERE work_session_id = <shift> AND voided_at IS NULL
 GROUP BY is_delivery, payment_method;

-- the same money as the ledger sees it
SELECT sa.is_delivery, m.method, sum(m.amount)
  FROM cash_movements m
  LEFT JOIN sales sa ON sa.id = m.sale_id
 WHERE m.store_session_id = <session> AND m.kind IN ('sale','void')
 GROUP BY sa.is_delivery, m.method;
```

The two must agree. The tests in `web/tests/test_delivery.py` assert exactly that,
including that a voided delivery leaves the delivery half rather than the counter.
