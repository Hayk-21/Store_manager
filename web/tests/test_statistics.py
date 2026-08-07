"""/statistics — what the business earned, and what that cost.

The figures come from the sale lines, so the tests care most about two things:
that a repriced item does not rewrite history, and that "profit" means profit
after what was actually paid out.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from app.config import settings
from app.db import db
from app.repo import stats as stats_repo
from app.services import corrections, statistics
from app.services import shifts as shifts_service
from tests.factories import (
    YEREVAN_LAT,
    YEREVAN_LNG,
    login,
    make_item,
    make_owner,
    make_store,
    make_worker,
)

TZ = "Asia/Yerevan"


async def _sold(
    owner_id: int,
    store_id: int,
    lines: list[dict],
    *,
    worker_name: str = "Անի",
    salary: str = "8000.00",
    key: str = "1",
):
    """A whole shift: open, sell, close."""
    worker_id, _ = await make_worker(owner_id, worker_name, salary_amount=salary)
    worker = shifts_service.Worker(
        id=worker_id, owner_id=owner_id, name=worker_name, salary_amount=Decimal(salary)
    )
    await shifts_service.open_store(worker, YEREVAN_LAT, YEREVAN_LNG, 20, f"open-{key}")
    await shifts_service.close_out_shift(
        worker, lines, f"close-{key}", close_store_too=True
    )
    return worker_id


async def _a_days_trading():
    owner_id = await make_owner("@ownerhandle")
    store_id = await make_store(owner_id, "Խանութ 1", lat=YEREVAN_LAT, lng=YEREVAN_LNG)
    item_id = await make_item(
        owner_id, store_id, "HQD Cuvie", count=50, self_price="1500.00", sell_price="3500.00"
    )
    await _sold(
        owner_id,
        store_id,
        [{"item_id": item_id, "quantity": 4, "unit_price": "3500.00",
          "payment_method": "cash"}],
    )
    return owner_id, store_id, item_id


def _range():
    today = settings.local_day()
    return today - timedelta(days=6), today


# -- the numbers -------------------------------------------------------------

async def test_revenue_and_profit_come_from_the_lines(client):
    owner_id, _, _ = await _a_days_trading()
    since, until = _range()

    row = await stats_repo.summary(owner_id, since, until, TZ)

    assert row["revenue"] == Decimal("14000.00")
    assert row["profit"] == Decimal("8000.00"), "(3500 − 1500) × 4"
    assert row["units"] == 4
    assert row["cash"] == Decimal("14000.00")


async def test_repricing_an_item_does_not_rewrite_what_it_earned(client):
    """The point of the unit_cost snapshot on every line."""
    owner_id, _, item_id = await _a_days_trading()
    since, until = _range()

    await db.execute(
        "UPDATE items SET self_price = 3400, sell_price = 9000 WHERE id = $1", item_id
    )

    row = await stats_repo.summary(owner_id, since, until, TZ)
    assert row["revenue"] == Decimal("14000.00")
    assert row["profit"] == Decimal("8000.00")


async def test_a_voided_sale_is_not_revenue(client):
    owner_id, _, _ = await _a_days_trading()
    since, until = _range()
    sale_id = await db.fetchval("SELECT id FROM sales")
    await corrections.void_sale(owner_id, owner_id, sale_id)

    row = await stats_repo.summary(owner_id, since, until, TZ)

    assert row["revenue"] == Decimal("0")
    assert row["receipts"] == 0


async def test_another_owners_trading_is_invisible(client):
    await _a_days_trading()
    intruder = await make_owner("@ownerother")
    since, until = _range()

    row = await stats_repo.summary(intruder, since, until, TZ)

    assert row["revenue"] == Decimal("0")


async def test_the_range_excludes_what_falls_outside_it(client):
    owner_id, _, _ = await _a_days_trading()
    long_ago = settings.local_day() - timedelta(days=90)

    row = await stats_repo.summary(owner_id, long_ago, long_ago + timedelta(days=5), TZ)

    assert row["revenue"] == Decimal("0")


async def test_net_profit_subtracts_wages_and_expenses(client):
    owner_id, store_id, _ = await _a_days_trading()
    since, until = _range()
    await db.execute(
        """
        INSERT INTO expenses (owner_id, purpose, amount, spent_on, method)
        VALUES ($1, 'վարձակալություն', 100000, $2, 'bank')
        """,
        owner_id, settings.local_day(),
    )

    data = await statistics.overview(owner_id, since, until)

    assert data["gross_profit"] == Decimal("8000.00")
    assert data["salaries"] == Decimal("8000.00")
    assert data["spending"] == Decimal("100000.00")
    assert data["net_profit"] == Decimal("-100000.00")


async def test_a_store_filter_narrows_the_sales_but_not_the_comparison(client):
    """Rent belongs to the business, so it is not attributed to one shop."""
    owner_id, store_id, _ = await _a_days_trading()
    other = await make_store(owner_id, "Խանութ 2", lat=41.0, lng=45.0)
    since, until = _range()

    data = await statistics.overview(owner_id, since, until, other)

    assert data["summary"]["revenue"] == Decimal("0")
    assert len(data["by_store"]) == 1, "the comparison still shows every shop that traded"
    assert data["by_store"][0]["name"] == "Խանութ 1"


# -- the chart ---------------------------------------------------------------

async def test_days_with_no_sales_still_get_a_bar(client):
    """A chart that drops empty days draws a smooth line across a closed week."""
    owner_id, _, _ = await _a_days_trading()
    since, until = _range()

    rows = await stats_repo.daily(owner_id, since, until, TZ)

    assert len(rows) == 7
    assert sum(1 for r in rows if r["revenue"] > 0) == 1
    assert all(r["revenue"] is not None for r in rows)


async def test_a_long_range_is_bucketed_into_weeks(client):
    """90 daily bars in one card is a smear, not a reading."""
    owner_id, _, _ = await _a_days_trading()
    today = settings.local_day()

    data = await statistics.overview(owner_id, today - timedelta(days=89), today)

    assert data["weekly"] is True
    assert len(data["bars"]) <= 14
    assert sum(b["revenue"] for b in data["bars"]) == Decimal("14000.00")


async def test_a_short_range_keeps_one_bar_per_day(client):
    owner_id, _, _ = await _a_days_trading()
    since, until = _range()

    data = await statistics.overview(owner_id, since, until)

    assert data["weekly"] is False
    assert len(data["bars"]) == 7
    assert max(b["revenue_pct"] for b in data["bars"]) == 100.0


async def test_bar_heights_are_relative_to_the_tallest(client):
    owner_id, store_id, item_id = await _a_days_trading()
    since, until = _range()

    data = await statistics.overview(owner_id, since, until)
    tallest = next(b for b in data["bars"] if b["revenue"] > 0)

    assert tallest["revenue_pct"] == 100.0
    # 8000 profit out of 14000 revenue.
    assert round(tallest["profit_pct"]) == 57


async def test_an_empty_period_does_not_divide_by_zero(client):
    owner_id = await make_owner("@ownerhandle")
    await make_store(owner_id)
    since, until = _range()

    data = await statistics.overview(owner_id, since, until)

    assert data["peak"] == Decimal("0.00")
    assert data["average_receipt"] == Decimal("0.00")
    assert all(b["revenue_pct"] == 0.0 for b in data["bars"])


# -- presets -----------------------------------------------------------------

async def test_the_presets_resolve_to_real_ranges(client):
    today = settings.local_day()

    since, until, key = statistics.range_for("7")
    assert (until - since).days == 6 and key == "7"

    since, until, key = statistics.range_for("month")
    assert since.day == 1 and until == today

    since, _, key = statistics.range_for("prev")
    assert since.day == 1 and since < today.replace(day=1)


async def test_an_unknown_preset_falls_back_rather_than_failing(client):
    since, until, key = statistics.range_for("../etc/passwd")

    assert key == statistics.DEFAULT_PRESET
    assert (until - since).days == 29


# -- the page ----------------------------------------------------------------

async def test_the_page_shows_the_headline_figures(client):
    await _a_days_trading()
    await login(client, "@ownerhandle")

    page = await client.get("/statistics")

    assert page.status_code == 200
    assert "Զուտ շահույթ" in page.text
    assert "14,000.00" in page.text, "revenue"
    assert "HQD Cuvie" in page.text, "the best seller"
    assert "bar bar-revenue" in page.text, "the chart drew"


async def test_the_page_can_be_filtered_to_one_store(client):
    owner_id, store_id, _ = await _a_days_trading()
    await login(client, "@ownerhandle")

    page = await client.get(f"/statistics?period=7&store_id={store_id}")

    assert page.status_code == 200
    assert "14,000.00" in page.text


async def test_another_owners_store_cannot_be_filtered_to(client):
    _, store_id, _ = await _a_days_trading()
    await make_owner("@ownerother")
    await login(client, "@ownerother")

    page = await client.get(f"/statistics?store_id={store_id}")

    assert page.status_code == 404


async def test_the_page_says_so_when_nothing_was_sold(client):
    owner_id = await make_owner("@ownerhandle")
    await make_store(owner_id)
    await login(client, "@ownerhandle")

    page = await client.get("/statistics")

    assert page.status_code == 200
    assert "վաճառք չի եղել" in page.text
