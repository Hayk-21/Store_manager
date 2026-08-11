"""The numbers behind the statistics page, and the shapes the charts draw.

Two jobs kept together because they answer one question: what did the business
earn, and what did that cost. Gross profit comes from the sale lines; net profit
subtracts what the shop actually paid out — wages and expenses — over the same
days. Anything else would flatter the figure.

Bar heights are computed here rather than in the template. A percentage is
arithmetic, and arithmetic in Jinja is where rounding bugs hide.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from app.config import settings
from app.repo import expenses as expenses_repo
from app.repo import stats as stats_repo
from app.repo import stores as stores_repo
from app.repo import write_offs as write_offs_repo

ZERO = Decimal("0.00")

# How far back each preset looks. 'month' and 'prev' are calendar months; the
# rest are rolling windows ending today, which is what "last 30 days" means to
# somebody looking at a shop.
PRESETS = {
    # "1" is a rolling window of one day, which is today — the same arithmetic as
    # the others rather than a case of its own.
    "1": "Այսօր",
    "7": "Վերջին 7 օրը",
    "30": "Վերջին 30 օրը",
    "90": "Վերջին 90 օրը",
    "month": "Այս ամիս",
    "prev": "Անցած ամիս",
}
DEFAULT_PRESET = "30"

# What a range asked for by date calls itself. Not in PRESETS: it is not something to
# pick from a list, it is what you get by clicking a day on the chart.
CUSTOM = "custom"

# Above this many days the per-day bars become slivers, so the chart switches to
# weekly buckets. Chosen to keep a 90-day range readable.
MAX_DAILY_BARS = 45


def range_for(
    preset: str | None, since: date | None = None, until: date | None = None
) -> tuple[date, date, str]:
    """Resolve a preset, or an explicit range, to concrete inclusive dates.

    An explicit range wins and reports itself as the preset ``custom``, which is how
    clicking a bar on the chart works: the link asks for that one day. Given back-to-
    front the two are swapped rather than refused — a range is a pair of ends, and
    "which is which" is not something to make somebody re-enter a form over.
    """
    if since is not None or until is not None:
        first = since or until
        last = until or since
        return (last, first, CUSTOM) if last < first else (first, last, CUSTOM)

    key = preset if preset in PRESETS else DEFAULT_PRESET
    today = settings.local_day()

    if key == "month":
        first = today.replace(day=1)
        return first, today, key
    if key == "prev":
        first_this = today.replace(day=1)
        last = first_this - timedelta(days=1)
        return last.replace(day=1), last, key

    return today - timedelta(days=int(key) - 1), today, key


def _percent(value: Decimal, peak: Decimal) -> float:
    """Bar height as a percentage of the tallest bar.

    Negative values (a day that only saw refunds) read as zero height rather
    than a bar growing downwards off the axis.
    """
    if peak <= ZERO or value <= ZERO:
        return 0.0
    return float(min(Decimal(100), value / peak * 100))


def _bucketed(rows: list) -> tuple[list[dict], bool]:
    """Group days into weeks once there are too many to draw one bar each.

    Every bar carries ``day`` and ``until`` — the same date twice for a day, the ends of
    the week for a bucket — so a bar can link to the period it stands for. A weekly bar's
    ``until`` is the last day actually in the range, not Sunday: the newest bucket is
    usually a part-week, and offering to open days that have not happened yet would draw
    an empty chart.
    """
    if len(rows) <= MAX_DAILY_BARS:
        return [
            {
                "label": row["day"].strftime("%d.%m"),
                "day": row["day"],
                "until": row["day"],
                "revenue": Decimal(row["revenue"]),
                "profit": Decimal(row["profit"]),
                "receipts": row["receipts"],
            }
            for row in rows
        ], False

    weeks: list[dict] = []
    for row in rows:
        # Monday of that row's week.
        start = row["day"] - timedelta(days=row["day"].weekday())
        if not weeks or weeks[-1]["day"] != start:
            weeks.append({
                "label": start.strftime("%d.%m"),
                "day": start,
                "until": row["day"],
                "revenue": ZERO,
                "profit": ZERO,
                "receipts": 0,
            })
        weeks[-1]["until"] = row["day"]
        weeks[-1]["revenue"] += Decimal(row["revenue"])
        weeks[-1]["profit"] += Decimal(row["profit"])
        weeks[-1]["receipts"] += row["receipts"]
    return weeks, True


async def overview(
    owner_id: int, since: date, until: date, store_id: int | None = None
) -> dict:
    """Everything the statistics page shows, for one period and one filter."""
    tz = settings.tzname

    summary = await stats_repo.summary(owner_id, since, until, tz, store_id)
    rows = await stats_repo.daily(owner_id, since, until, tz, store_id)
    bars, weekly = _bucketed(rows)
    peak = max((bar["revenue"] for bar in bars), default=ZERO)
    for bar in bars:
        bar["revenue_pct"] = _percent(bar["revenue"], peak)
        bar["profit_pct"] = _percent(bar["profit"], peak)

    salaries = Decimal(await stats_repo.salaries_between(owner_id, since, until, tz, store_id))
    # Expenses are business-wide by nature (rent, advertising), so they are not
    # narrowed by the store filter — attributing them per shop would be a guess.
    spending = Decimal(await expenses_repo.total_between(owner_id, since, until))
    # Stock lost, at what it cost — and deliberately *not* part of the spending figure
    # below it. Nothing left a drawer or an account the day a vape fell off the shelf:
    # the money left when the goods were bought. Counting it as spending charged the
    # business twice for the same thousand drams and made a write-off look like a
    # payment. It keeps its own figure and its own list, where it says what it is.
    breakage = Decimal(await write_offs_repo.cost_between(owner_id, since, until, store_id))

    gross = Decimal(summary["profit"])
    days = (until - since).days + 1

    return {
        "since": since,
        "until": until,
        "days": days,
        "store_id": store_id,
        "stores": await stores_repo.list_for_owner(owner_id),
        "summary": summary,
        "bars": bars,
        "weekly": weekly,
        "peak": peak,
        "top_items": await stats_repo.top_items(owner_id, since, until, tz, store_id),
        "by_store": await stats_repo.by_store(owner_id, since, until, tz),
        "by_worker": await stats_repo.by_worker(owner_id, since, until, tz, store_id),
        "by_category": await expenses_repo.by_category_between(owner_id, since, until),
        # The individual entries behind the «Ծախսեր» figure. A total that cannot
        # be broken back down into the things it is made of is a number the owner
        # has to take on trust, and the first question about it is always "on
        # what?". Wages are the other part of that figure and are an aggregate.
        "spending_rows": await expenses_repo.list_between(owner_id, since, until),
        # And the breakage, for the same reason. It was the one third of «Ծախսեր»
        # with no way back to what it was made of.
        "breakage_rows": await write_offs_repo.list_between(
            owner_id, since, until, store_id
        ),
        # When the shop sells, which the daily chart cannot answer and a rota is
        # built from.
        "hours": _shaped(await stats_repo.by_hour(owner_id, since, until, tz, store_id)),
        "stock": await stats_repo.stock_value(owner_id, store_id),
        # Named on the hourly chart, because "when does this shop sell" has a different
        # answer in a different timezone and the page should say which one it used.
        "tzname": tz,
        "salaries": salaries,
        "spending": spending,
        "gross_profit": gross,
        "breakage": breakage,
        # Wages and what the owner paid out. Not breakage — see above — and the same
        # arithmetic the report header uses for a single day, so a day and the month
        # containing it cannot disagree about what profit means.
        "net_profit": gross - salaries - spending,
        "average_receipt": (
            Decimal(summary["revenue"]) / summary["receipts"]
            if summary["receipts"] else ZERO
        ),
        "average_day": Decimal(summary["revenue"]) / days if days else ZERO,
        # The best and worst of the period, and how many days sold nothing at all.
        # All three are in the bars already, and all three are the kind of thing an
        # owner reads a chart to find out — so they are said rather than eyeballed.
        **_the_shape_of_the_period(bars),
    }


def _shaped(rows: list) -> list[dict]:
    """Hourly rows with a bar height each, scaled to the busiest hour."""
    revenues = [Decimal(row["revenue"]) for row in rows]
    peak = max(revenues, default=ZERO)
    return [
        {
            "hour": row["hour"],
            "revenue": Decimal(row["revenue"]),
            "receipts": row["receipts"],
            "pct": _percent(Decimal(row["revenue"]), peak),
        }
        for row in rows
    ]


def _the_shape_of_the_period(bars: list[dict]) -> dict:
    """Best day, worst trading day, and how many days sold nothing.

    The worst is the worst day the shop *traded*, not the worst bar: a range with a
    Sunday in it would otherwise always answer "nothing, on the day you were shut",
    which is true and useless. Days that sold nothing are counted separately, which is
    the figure that actually says something about them.
    """
    trading = [bar for bar in bars if bar["revenue"] > ZERO]
    return {
        "best_day": max(trading, key=lambda bar: bar["revenue"], default=None),
        "worst_day": min(trading, key=lambda bar: bar["revenue"], default=None),
        "quiet_days": len(bars) - len(trading),
        "trading_days": len(trading),
    }
