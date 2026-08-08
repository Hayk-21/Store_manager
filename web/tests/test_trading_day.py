"""The trading day: takings that outlive a close and start again each morning.

Two figures live side by side and mean different things. The till belongs to the
open session and is settled when the store closes. The day's takings belong to
the store's own trading day and survive any number of opens and closes within
it. The boundary is per store, because a shop open until 2am and one that shuts
at 8pm do not share a morning.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from app.config import settings
from app.db import db
from app.repo import money as money_repo
from app.services import shifts as shifts_service
from tests.factories import (
    YEREVAN_LAT,
    YEREVAN_LNG,
    login,
    make_item,
    make_owner,
    make_store,
    make_worker,
    worked_a_full_shift,
)


async def _trade(owner_id: int, store_id: int, item_id: int, *, quantity: int, key: str):
    """One whole shift: open, sell, close up."""
    worker_id, _ = await make_worker(owner_id, "Անի", salary_amount="0.00")
    worker = shifts_service.Worker(
        id=worker_id, owner_id=owner_id, name="Անի", salary_amount=Decimal("0.00")
    )
    await shifts_service.open_store(worker, YEREVAN_LAT, YEREVAN_LNG, 20, f"open-{key}", 900)
    # A whole day, so the shift wage is the full figure: a shift under eight
    # hours is paid half, and these numbers are not about that rule.
    await worked_a_full_shift(worker_id)
    await shifts_service.close_out_shift(
        worker,
        [{"item_id": item_id, "quantity": quantity, "unit_price": "3500.00",
          "payment_method": "cash"}],
        f"close-{key}",
        close_store_too=True,
    )


async def _a_store(hour: int = 6):
    owner_id = await make_owner("@ownerhandle")
    store_id = await make_store(owner_id, "Խանութ 1", lat=YEREVAN_LAT, lng=YEREVAN_LNG)
    await db.execute("UPDATE stores SET day_start_hour = $2 WHERE id = $1", store_id, hour)
    item_id = await make_item(owner_id, store_id, "HQD Cuvie", count=100, sell_price="3500.00")
    return owner_id, store_id, item_id


async def _age_movements(days: float = 0, hours: float = 0):
    """Push every recorded movement and sale back in time."""
    delta = timedelta(days=days, hours=hours)
    await db.execute("UPDATE cash_movements SET created_at = created_at - $1::interval", delta)
    await db.execute("UPDATE sales SET sold_at = sold_at - $1::interval", delta)
    await db.execute(
        "UPDATE store_sessions SET opened_at = opened_at - $1::interval", delta
    )


# -- the boundary itself -----------------------------------------------------

@pytest.mark.parametrize(
    ("now", "hour", "expected"),
    [
        # After the boundary: today's own morning.
        ("2026-08-07 10:30", 6, "2026-08-07 06:00"),
        # Before it: the day still running started yesterday. This is the case
        # that matters -- a 1am sale belongs to last night's takings.
        ("2026-08-07 01:15", 6, "2026-08-06 06:00"),
        # Exactly on it: a new day, not the tail of the old one.
        ("2026-08-07 06:00", 6, "2026-08-07 06:00"),
        # A shop that uses midnight gets plain calendar days.
        ("2026-08-07 00:30", 0, "2026-08-07 00:00"),
        # A late-night shop can push the boundary further out.
        ("2026-08-07 03:00", 4, "2026-08-06 04:00"),
    ],
)
def test_the_day_starts_at_the_stores_own_hour(now, hour, expected):
    moment = datetime.fromisoformat(now).replace(tzinfo=settings.timezone)

    start = settings.trading_day_start(hour, moment)

    assert start.strftime("%Y-%m-%d %H:%M") == expected


def test_the_hour_defaults_to_the_configured_one():
    assert settings.trading_day_start(None, settings.now()) == settings.trading_day_start(
        settings.default_day_start_hour, settings.now()
    )


# -- what the pages read -----------------------------------------------------

async def test_todays_takings_survive_the_store_being_closed(client):
    """The point of the whole feature."""
    owner_id, store_id, item_id = await _a_store()
    await _trade(owner_id, store_id, item_id, quantity=3, key="1")

    row = (await money_repo.totals_by_store(owner_id))[0]

    assert row["store_session_id"] is None, "the worker closed up"
    assert row["cash"] == Decimal("0"), "the till was settled"
    assert row["day_cash"] == Decimal("10500.00"), "the shop still sold that"
    assert row["day_receipts"] == 1


async def test_a_second_shift_adds_to_the_same_day(client):
    owner_id, store_id, item_id = await _a_store()
    await _trade(owner_id, store_id, item_id, quantity=3, key="1")
    await _trade(owner_id, store_id, item_id, quantity=2, key="2")

    row = (await money_repo.totals_by_store(owner_id))[0]

    assert row["day_cash"] == Decimal("17500.00")
    assert row["day_receipts"] == 2


async def test_yesterdays_takings_are_gone_this_morning(client):
    owner_id, store_id, item_id = await _a_store()
    await _trade(owner_id, store_id, item_id, quantity=3, key="1")
    await _age_movements(days=1)

    row = (await money_repo.totals_by_store(owner_id))[0]

    assert row["day_cash"] == Decimal("0")
    assert row["day_receipts"] == 0


async def test_a_late_night_sale_still_belongs_to_the_evening(client):
    """Six hours ago, at 6am, is what the boundary must not cut through.

    Aged by 12 hours: whatever the clock says now, that sale and this moment are
    on the same side of a 06:00 boundary for at least half the day, so the
    assertion is made against the boundary rather than against a fixed hour.
    """
    owner_id, store_id, item_id = await _a_store()
    await _trade(owner_id, store_id, item_id, quantity=3, key="1")
    await _age_movements(hours=12)

    start = settings.trading_day_start(6)
    sold_at = await db.fetchval("SELECT sold_at FROM sales")
    row = (await money_repo.totals_by_store(owner_id))[0]

    if sold_at >= start:
        assert row["day_cash"] == Decimal("10500.00")
    else:
        assert row["day_cash"] == Decimal("0"), "it fell into yesterday, correctly"


async def test_each_store_keeps_its_own_boundary(client):
    """A store whose day starts at midnight and one at 06:00 disagree about
    what "today" holds, and both are right."""
    owner_id, store_id, item_id = await _a_store(hour=6)
    await _trade(owner_id, store_id, item_id, quantity=3, key="1")

    await db.execute("UPDATE stores SET day_start_hour = 0")
    midnight = (await money_repo.totals_by_store(owner_id))[0]["day_start"]
    await db.execute("UPDATE stores SET day_start_hour = 6")
    six = (await money_repo.totals_by_store(owner_id))[0]["day_start"]

    assert midnight != six
    assert midnight.hour == 0 and six.hour == 6


async def test_a_void_comes_off_the_days_takings(client):
    """A sale that was taken back is not money the shop took."""
    owner_id, store_id, item_id = await _a_store()
    await _trade(owner_id, store_id, item_id, quantity=3, key="1")
    from app.services import corrections

    sale_id = await db.fetchval("SELECT id FROM sales")
    await corrections.void_sale(owner_id, owner_id, sale_id)

    row = (await money_repo.totals_by_store(owner_id))[0]

    assert row["day_cash"] == Decimal("0")


async def test_a_salary_is_not_deducted_from_what_the_shop_sold(client):
    """Wages are a cost, not a negative sale — netting them off here would
    understate the takings."""
    owner_id, store_id, item_id = await _a_store()
    worker_id, _ = await make_worker(owner_id, "Անի", salary_amount="8000.00")
    worker = shifts_service.Worker(
        id=worker_id, owner_id=owner_id, name="Անի", salary_amount=Decimal("8000.00")
    )
    await shifts_service.open_store(worker, YEREVAN_LAT, YEREVAN_LNG, 20, "open-s", 900)
    await worked_a_full_shift(worker_id)
    await shifts_service.close_out_shift(
        worker,
        [{"item_id": item_id, "quantity": 3, "unit_price": "3500.00",
          "payment_method": "cash"}],
        "close-s",
        close_store_too=True,
    )

    row = (await money_repo.totals_by_store(owner_id))[0]

    assert row["day_cash"] == Decimal("10500.00")
    assert await db.fetchval(
        "SELECT cash_at_close FROM store_sessions"
    ) == Decimal("2500.00"), "the till, which does account for the wage"


async def test_another_owners_sales_are_not_in_the_figure(client):
    owner_id, store_id, item_id = await _a_store()
    await _trade(owner_id, store_id, item_id, quantity=3, key="1")
    intruder = await make_owner("@ownerother")
    await make_store(intruder, "Ուրիշի խանութ")

    rows = await money_repo.totals_by_store(intruder)

    assert len(rows) == 1
    assert rows[0]["day_cash"] == Decimal("0")


async def test_the_single_store_figure_matches_the_list(client):
    owner_id, store_id, item_id = await _a_store()
    await _trade(owner_id, store_id, item_id, quantity=3, key="1")

    listed = (await money_repo.totals_by_store(owner_id))[0]
    one = await money_repo.day_totals_for_store(owner_id, store_id)

    assert one["day_cash"] == listed["day_cash"]
    assert one["day_start"] == listed["day_start"]
    assert one["day_sessions"] == 1


# -- the pages ---------------------------------------------------------------

async def test_the_footer_shows_todays_takings_for_a_closed_store(client):
    owner_id, store_id, item_id = await _a_store()
    await _trade(owner_id, store_id, item_id, quantity=3, key="1")
    await login(client, "@ownerhandle")

    footer = await client.get("/partials/footer")

    assert "Այսօր կանխիկ" in footer.text
    assert "10,500.00" in footer.text
    assert "փակ" in footer.text


async def test_the_store_page_shows_the_day_whether_open_or_shut(client):
    owner_id, store_id, item_id = await _a_store()
    await _trade(owner_id, store_id, item_id, quantity=3, key="1")
    await login(client, "@ownerhandle")

    page = await client.get(f"/stores/{store_id}")

    assert "Այսօր ընդամենը" in page.text
    assert "10,500.00" in page.text
    assert "1 վաճառք" in page.text


# -- setting it ---------------------------------------------------------------

async def test_a_new_store_starts_at_six(client):
    await make_owner("@ownerhandle")
    await login(client, "@ownerhandle")

    await client.post(
        "/stores",
        data={"csrf_token": await _csrf(client), "name": "Նոր", "lat": "40.1772",
              "lng": "44.5032", "radius_m": "120", "day_start_hour": ""},
    )

    assert await db.fetchval("SELECT day_start_hour FROM stores") == 6


async def test_the_hour_can_be_changed_per_store(client):
    owner_id, store_id, _ = await _a_store()
    await login(client, "@ownerhandle")

    response = await client.post(
        f"/stores/{store_id}",
        data={"csrf_token": await _csrf(client), "name": "Խանութ 1", "lat": "40.1772",
              "lng": "44.5032", "radius_m": "120", "day_start_hour": "9"},
    )

    assert response.status_code == 303
    assert await db.fetchval("SELECT day_start_hour FROM stores WHERE id = $1", store_id) == 9


async def test_an_impossible_hour_is_refused(client):
    owner_id, store_id, _ = await _a_store()
    await login(client, "@ownerhandle")

    response = await client.post(
        f"/stores/{store_id}",
        data={"csrf_token": await _csrf(client), "name": "Խանութ 1", "lat": "40.1772",
              "lng": "44.5032", "radius_m": "120", "day_start_hour": "25"},
    )

    assert response.status_code == 422
    assert await db.fetchval("SELECT day_start_hour FROM stores WHERE id = $1", store_id) == 6


async def _csrf(client) -> str:
    from app.deps import SESSION_COOKIE
    from app.repo import users as users_repo
    from app.security import hash_token

    row = await users_repo.session_with_user(hash_token(client.cookies[SESSION_COOKIE]))
    return row["csrf_token"]
