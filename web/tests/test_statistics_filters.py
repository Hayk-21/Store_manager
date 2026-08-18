"""The statistics page's own controls, and the bug that broke all of them.

Changing the period submitted the filter form, and the filter form's shop select
has an «Բոլորը» option with no value — so the request carried ``store_id=``. An
empty string is not an integer, the whole page answered 422, and every preset
looked broken. Every option is exercised here for that reason: the failure was not
in one of them, it was in the form they share.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.config import settings
from app.repo import expenses as expenses_repo
from app.services import shifts as shifts_service
from app.services import statistics
from tests.factories import (
    YEREVAN_LAT,
    YEREVAN_LNG,
    login,
    make_item,
    make_owner,
    make_store,
    make_worker,
)


async def _a_shop_with_a_days_trading():
    owner_id = await make_owner("@ownerhandle")
    store_id = await make_store(owner_id, "Խանութ 1", lat=YEREVAN_LAT, lng=YEREVAN_LNG)
    item_id = await make_item(
        owner_id, store_id, "HQD Cuvie", count=50,
        self_price="1500.00", sell_price="3500.00",
    )
    worker_id, _ = await make_worker(owner_id, "Անի", salary_amount="0.00")
    worker = shifts_service.Worker(
        id=worker_id, owner_id=owner_id, name="Անի", salary_amount=Decimal("0.00")
    )
    await shifts_service.open_store(worker, YEREVAN_LAT, YEREVAN_LNG, 20, "idem-open-1", 900)
    await shifts_service.close_out_shift(
        worker,
        [{"item_id": item_id, "quantity": 3, "unit_price": "3500.00",
          "payment_method": "cash"}],
        "idem-close-1",
        close_store_too=True,
        counted=Decimal("0"),
    )
    return owner_id, store_id


# -- the 422 -----------------------------------------------------------------

@pytest.mark.parametrize("period", ["1", "7", "30", "90", "month", "prev"])
async def test_every_period_works_with_the_shop_filter_left_on_all(client, period):
    """The regression, once per option. «Բոլորը» submits an empty store_id, and it
    used to take the page down with it."""
    await _a_shop_with_a_days_trading()
    await login(client, "@ownerhandle")

    response = await client.get(f"/statistics?period={period}&store_id=")

    assert response.status_code == 200, f"period={period} answered {response.status_code}"


@pytest.mark.parametrize("period", ["1", "7", "30", "90", "month", "prev"])
async def test_every_period_works_with_a_shop_chosen(client, period):
    _, store_id = await _a_shop_with_a_days_trading()
    await login(client, "@ownerhandle")

    response = await client.get(f"/statistics?period={period}&store_id={store_id}")

    assert response.status_code == 200


async def test_a_nonsense_shop_filter_is_ignored_rather_than_refused(client):
    """Nothing to tell the owner about a URL they did not type, and refusing it only
    hides the page."""
    await _a_shop_with_a_days_trading()
    await login(client, "@ownerhandle")

    response = await client.get("/statistics?period=7&store_id=abc")

    assert response.status_code == 200


async def test_another_owners_shop_still_reads_as_missing(client):
    """Being lenient about a blank filter must not make the page lenient about a
    shop that is not yours."""
    await _a_shop_with_a_days_trading()
    stranger = await make_owner("@stranger")
    theirs = await make_store(stranger, "Ուրիշի խանութ")
    await login(client, "@ownerhandle")

    response = await client.get(f"/statistics?store_id={theirs}")

    assert response.status_code == 404


# -- today -------------------------------------------------------------------

def test_today_is_one_of_the_periods():
    assert "1" in statistics.PRESETS


def test_today_resolves_to_a_single_day():
    since, until, preset = statistics.range_for("1")

    assert since == until
    assert preset == "1"


async def test_todays_trading_shows_under_today(client):
    await _a_shop_with_a_days_trading()
    await login(client, "@ownerhandle")

    page = await client.get("/statistics?period=1")

    assert page.status_code == 200
    assert "10,500.00" in page.text, "three at 3,500, sold today"


# -- the spending breakdown --------------------------------------------------

async def test_the_period_spending_is_listed_item_by_item(client):
    """The first question about a total is "on what?", and the answer has to be on
    the same page."""
    owner_id, store_id = await _a_shop_with_a_days_trading()

    await expenses_repo.create(
        owner_id=owner_id,
        purpose="Վարձակալություն",
        amount=Decimal("120000.00"),
        spent_on=settings.local_day(),
        store_id=store_id,
    )
    await login(client, "@ownerhandle")

    page = await client.get("/statistics?period=7")

    assert "Ծախսերը մանրամասն" in page.text
    assert "Վարձակալություն" in page.text
    assert "120,000.00" in page.text


async def test_a_period_of_giveaways_does_not_take_the_page_down(client):
    """A shelf price of nought is allowed — a giveaway, a replacement handed over —
    and the top-items bar divides by the tallest revenue. When every sale in the
    period went out at zero, that divisor was zero and the page answered 500."""
    owner_id = await make_owner("@ownerhandle")
    store_id = await make_store(owner_id, "Խանութ 1", lat=YEREVAN_LAT, lng=YEREVAN_LNG)
    item_id = await make_item(
        owner_id, store_id, "Նվեր", count=10, self_price="0.00", sell_price="0.00"
    )
    worker_id, _ = await make_worker(owner_id, "Անի", salary_amount="0.00")
    worker = shifts_service.Worker(
        id=worker_id, owner_id=owner_id, name="Անի", salary_amount=Decimal("0.00")
    )
    await shifts_service.open_store(worker, YEREVAN_LAT, YEREVAN_LNG, 20, "idem-open-1", 900)
    await shifts_service.close_out_shift(
        worker,
        [{"item_id": item_id, "quantity": 2, "unit_price": "0.00",
          "payment_method": "cash"}],
        "idem-close-1",
        close_store_too=True,
        counted=Decimal("0"),
    )
    await login(client, "@ownerhandle")

    page = await client.get("/statistics?period=7")

    assert page.status_code == 200
    assert "Նվեր" in page.text


async def test_a_period_with_no_spending_says_so(client):
    await _a_shop_with_a_days_trading()
    await login(client, "@ownerhandle")

    page = await client.get("/statistics?period=prev")

    assert page.status_code == 200
    assert "Ծախսերը մանրամասն" in page.text
