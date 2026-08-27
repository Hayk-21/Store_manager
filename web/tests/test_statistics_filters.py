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


# -- the product table -------------------------------------------------------

async def _a_shop_selling(how_many: int):
    """One shift in which ``how_many`` different products each sold once.

    Prices descend with the number, so «Ապրանք-00» is always the best seller and the
    order the table draws them in is known rather than guessed at.
    """
    owner_id = await make_owner("@ownerhandle")
    store_id = await make_store(owner_id, "Խանութ 1", lat=YEREVAN_LAT, lng=YEREVAN_LNG)
    lines = []
    for n in range(how_many):
        price = f"{1000 * (how_many - n)}.00"
        item_id = await make_item(
            owner_id, store_id, f"Ապրանք-{n:02d}", count=10,
            self_price="100.00", sell_price=price,
        )
        lines.append({"item_id": item_id, "quantity": 1,
                      "unit_price": price, "payment_method": "cash"})
    worker_id, _ = await make_worker(owner_id, "Անի", salary_amount="0.00")
    worker = shifts_service.Worker(
        id=worker_id, owner_id=owner_id, name="Անի", salary_amount=Decimal("0.00")
    )
    await shifts_service.open_store(worker, YEREVAN_LAT, YEREVAN_LNG, 20, "idem-open-1", 900)
    await shifts_service.close_out_shift(
        worker, lines, "idem-close-1", close_store_too=True, counted=Decimal("0"),
    )
    return owner_id, store_id


async def test_the_product_table_stops_at_ten_but_says_how_many_there_are(client):
    """Ten answers "what earns". It cannot answer "and the rest", and a table that
    stops without saying it has stopped answers that question wrong."""
    await _a_shop_selling(14)
    await login(client, "@ownerhandle")

    page = await client.get("/statistics?period=7")

    assert "Ապրանք-00" in page.text
    assert "Ապրանք-09" in page.text
    assert "Ապրանք-10" not in page.text, "the eleventh is behind the link, not on the page"
    assert "բոլորը (14)" in page.text, "and the link says how many it would add"


async def test_every_product_is_shown_when_the_long_list_is_asked_for(client):
    await _a_shop_selling(14)
    await login(client, "@ownerhandle")

    page = await client.get("/statistics?period=7&items=all")

    for n in range(14):
        assert f"Ապրանք-{n:02d}" in page.text, f"Ապրանք-{n:02d} is missing from the full list"


async def test_opening_the_product_table_keeps_the_period_and_the_shop(client):
    """The link is built from the URL being looked at. Losing the filters would answer
    a question about one shop's week with the whole business's month."""
    _, store_id = await _a_shop_selling(12)
    await login(client, "@ownerhandle")

    page = await client.get(f"/statistics?period=90&store_id={store_id}")

    assert f"period=90&amp;store_id={store_id}&amp;items=all#items" in page.text


async def test_a_shop_with_few_products_is_not_offered_a_longer_list(client):
    """There is no eleventh to go and look at, and a link to the same page reads as
    one that is broken."""
    await _a_shop_selling(3)
    await login(client, "@ownerhandle")

    page = await client.get("/statistics?period=7")

    assert "Ապրանք-02" in page.text
    assert "items=all" not in page.text


async def test_a_product_that_never_sold_is_not_one_of_the_products_sold(client):
    """The table is about sales, so the count beside it has to be a count of things
    that sold — stock sitting on a shelf has no line here to be counted."""
    owner_id, store_id = await _a_shop_selling(3)
    # A name of its own, and not one the page could say for its own reasons: the
    # footnote under the table is about unsold stock and contains the word.
    await make_item(owner_id, store_id, "Դարակի-վրա-մնացած", count=5,
                    self_price="100.00", sell_price="900.00")
    await login(client, "@ownerhandle")

    page = await client.get("/statistics?period=7&items=all")

    assert "Դարակի-վրա-մնացած" not in page.text
    assert "բոլոր 3 ապրանքը" in page.text


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
