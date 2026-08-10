"""Every way money leaves the business, in one list.

It leaves by four doors — a wage, a cashier taking petty cash, an expense the owner
types, stock written off — and each had its own page. So the only view of the whole was
two aggregate rows: «Աշխատավարձ · Փակված հերթափոխեր · 18,000» and «Խոտան · Դուրս գրված
ապրանք · 10,976». Both true, neither answerable. Which worker. Which product. Who took
what out of the drawer, and what for.

The list is on the statistics page and on the expenses page, because "what did this month
cost" is asked from both, and every row can be corrected where it stands.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from app.config import settings
from app.db import db
from app.repo import spending as spending_repo
from app.services import money as money_service
from app.services import sales as sales_service
from app.services import shifts as shifts_service
from app.services import write_offs as write_offs_service
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


def _range():
    today = settings.local_day()
    return today - timedelta(days=6), today


async def _a_shop(float_: str = "50000.00"):
    owner_id = await make_owner("@ownerhandle")
    store_id = await make_store(owner_id, "Նուբարաշեն", lat=YEREVAN_LAT, lng=YEREVAN_LNG)
    await db.execute(
        "UPDATE stores SET till_balance = $2 WHERE id = $1", store_id, Decimal(float_)
    )
    item_id = await make_item(
        owner_id, store_id, "HQD Cuvie", count=50,
        self_price="1500.00", sell_price="3500.00",
    )
    return owner_id, store_id, item_id


async def _a_day(owner_id: int, store_id: int, item_id: int, salary: str = "8000.00"):
    """A shift with something out of each door: a sale, petty cash, breakage, a wage."""
    worker_id, _ = await make_worker(owner_id, "Անի", salary_amount=salary)
    worker = shifts_service.Worker(
        id=worker_id, owner_id=owner_id, name="Անի", salary_amount=Decimal(salary)
    )
    await shifts_service.open_store(
        worker, YEREVAN_LAT, YEREVAN_LNG, 20, "idem-open-1", 900
    )
    await sales_service.record_sale(
        worker, [{"item_id": item_id, "quantity": 4}], "cash", "idem-sale-1"
    )
    await money_service.withdraw_by_worker(
        worker, Decimal("700"), "առաքիչին", "idem-cash-01"
    )
    await write_offs_service.record(worker, item_id, 2, "ընկավ", "idem-defect-1")
    await worked_a_full_shift(worker_id)
    await shifts_service.close_out_shift(worker, [], "idem-close-1")
    return worker


async def _rows(owner_id: int, store_id: int | None = None):
    since, until = _range()
    return await spending_repo.list_between(owner_id, since, until, store_id)


def _by_kind(rows) -> dict:
    return {row["kind"]: row for row in rows}


# -- everything is in it ------------------------------------------------------

async def test_all_four_doors_are_in_one_list(client):
    owner_id, store_id, item_id = await _a_shop()
    await _a_day(owner_id, store_id, item_id)
    await db.execute(
        """
        INSERT INTO expenses (owner_id, purpose, amount, spent_on)
        VALUES ($1, 'Վարձակալություն', 120000, $2)
        """,
        owner_id, settings.local_day(),
    )

    kinds = {row["kind"] for row in await _rows(owner_id)}

    assert kinds == {"salary", "withdrawal", "expense", "breakage"}


async def test_a_wage_says_which_worker_and_which_shop(client):
    """«Աշխատավարձ · Փակված հերթափոխեր · 18,000» could not answer either."""
    owner_id, store_id, item_id = await _a_shop()
    await _a_day(owner_id, store_id, item_id)

    wage = _by_kind(await _rows(owner_id))["salary"]

    assert wage["worker_name"] == "Անի"
    assert wage["store_name"] == "Նուբարաշեն"
    assert wage["amount"] == Decimal("8000.00")


async def test_petty_cash_says_who_took_it_and_what_for(client):
    """The one a cashier can trigger, and the one whose reason matters most."""
    owner_id, store_id, item_id = await _a_shop()
    await _a_day(owner_id, store_id, item_id)

    taken = _by_kind(await _rows(owner_id))["withdrawal"]

    assert taken["amount"] == Decimal("700.00"), "positive: it is a payment, not a sign"
    assert taken["purpose"] == "առաքիչին"
    assert taken["worker_name"] == "Անի"


async def test_breakage_says_which_product_and_why(client):
    owner_id, store_id, item_id = await _a_shop()
    await _a_day(owner_id, store_id, item_id)

    broken = _by_kind(await _rows(owner_id))["breakage"]

    assert "HQD Cuvie" in broken["purpose"]
    assert "ընկավ" in broken["purpose"]
    assert broken["amount"] == Decimal("3000.00"), "two at what they cost"
    assert broken["method"] is None, "it never touched the till"


async def test_a_bonus_is_its_own_row(client):
    """Money out for work done, and not the wage — so the wage stays the wage."""
    owner_id, store_id, item_id = await _a_shop()
    worker_id, _ = await make_worker(owner_id, "Անի", salary_amount="8000.00")
    await db.execute(
        """
        UPDATE workers SET bonus_threshold = 1000, bonus_amount = 2000,
                           bonus_period = 'day'
         WHERE id = $1
        """,
        worker_id,
    )
    worker = shifts_service.Worker(
        id=worker_id, owner_id=owner_id, name="Անի", salary_amount=Decimal("8000.00")
    )
    await shifts_service.open_store(
        worker, YEREVAN_LAT, YEREVAN_LNG, 20, "idem-open-1", 900
    )
    await sales_service.record_sale(
        worker, [{"item_id": item_id, "quantity": 4}], "cash", "idem-sale-1"
    )
    await worked_a_full_shift(worker_id)
    await shifts_service.close_out_shift(worker, [], "idem-close-1")

    rows = _by_kind(await _rows(owner_id))

    assert rows["salary"]["amount"] == Decimal("8000.00")
    assert rows["bonus"]["amount"] == Decimal("2000.00")


async def test_a_wage_the_drawer_could_not_cover_carries_its_debt(client):
    owner_id, store_id, item_id = await _a_shop(float_="0.00")
    await _a_day(owner_id, store_id, item_id, salary="99000.00")

    wage = _by_kind(await _rows(owner_id))["salary"]

    assert wage["unpaid"] > 0


async def test_a_shift_that_cost_nothing_is_not_a_row(client):
    """A monthly worker costs the till nothing when their shift ends, and «0 ֏ paid»
    is not a payment."""
    owner_id, store_id, item_id = await _a_shop()
    worker_id, _ = await make_worker(
        owner_id, "Անի", salary_amount="200000.00", salary_period="month"
    )
    worker = shifts_service.Worker(
        id=worker_id, owner_id=owner_id, name="Անի",
        salary_amount=Decimal("200000.00"), salary_period="month",
    )
    await shifts_service.open_store(
        worker, YEREVAN_LAT, YEREVAN_LNG, 20, "idem-open-1", 900
    )
    await worked_a_full_shift(worker_id)
    await shifts_service.close_out_shift(worker, [], "idem-close-1")

    assert not [row for row in await _rows(owner_id) if row["kind"] == "salary"]


async def test_the_store_filter_narrows_what_belongs_to_a_shop(client):
    owner_id, store_id, item_id = await _a_shop()
    await _a_day(owner_id, store_id, item_id)
    other = await make_store(owner_id, "Կենտրոն", lat=YEREVAN_LAT, lng=YEREVAN_LNG)

    assert await _rows(owner_id, other) == []
    assert await _rows(owner_id, store_id) != []


async def test_an_expense_survives_the_store_filter(client):
    """Rent and advertising belong to the business, not to the branch that happened to
    be open. Dropping them under a filter would quietly shrink the total."""
    owner_id, store_id, _ = await _a_shop()
    other = await make_store(owner_id, "Կենտրոն", lat=YEREVAN_LAT, lng=YEREVAN_LNG)
    await db.execute(
        """
        INSERT INTO expenses (owner_id, purpose, amount, spent_on)
        VALUES ($1, 'Գովազդ', 50000, $2)
        """,
        owner_id, settings.local_day(),
    )

    kinds = {row["kind"] for row in await _rows(owner_id, other)}

    assert kinds == {"expense"}


async def test_another_owners_money_is_not_in_the_list(client):
    owner_id, store_id, item_id = await _a_shop()
    await _a_day(owner_id, store_id, item_id)
    stranger = await make_owner("@stranger")

    assert await _rows(stranger) == []


async def test_the_totals_add_up_to_the_rows(client):
    owner_id, store_id, item_id = await _a_shop()
    await _a_day(owner_id, store_id, item_id)

    rows = await _rows(owner_id)
    totals = spending_repo.totals(rows)

    assert totals["total"] == sum(Decimal(row["amount"]) for row in rows)
    assert totals["salary"] == Decimal("8000.00")
    assert totals["withdrawal"] == Decimal("700.00")


# -- on the pages -------------------------------------------------------------

async def test_the_statistics_page_lists_every_payment(client):
    owner_id, store_id, item_id = await _a_shop()
    await _a_day(owner_id, store_id, item_id)
    await login(client, "@ownerhandle")

    page = await client.get("/statistics?period=7")

    assert "Բոլոր վճարումները" in page.text
    assert "առաքիչին" in page.text, "the reason a cashier gave"
    assert "Անի" in page.text, "and who it was"


async def test_the_expenses_page_lists_every_payment_too(client):
    """It is the page an owner comes to for "what did the month cost", and it was
    showing only the entries typed on it."""
    owner_id, store_id, item_id = await _a_shop()
    await _a_day(owner_id, store_id, item_id)
    await login(client, "@ownerhandle")

    page = await client.get("/expenses")

    assert "Բոլոր վճարումները" in page.text
    assert "առաքիչին" in page.text


async def test_every_row_offers_a_way_to_correct_it(client):
    owner_id, store_id, item_id = await _a_shop()
    await _a_day(owner_id, store_id, item_id)
    await login(client, "@ownerhandle")

    page = await client.get("/statistics?period=7")

    assert "/shifts/" in page.text and "/salary" in page.text, "a wage"
    assert "/movements/" in page.text, "petty cash"
    assert "/write-offs/" in page.text, "breakage"


# -- correcting one -----------------------------------------------------------

async def test_a_withdrawal_can_be_corrected_without_losing_its_reason(client):
    """Deleting the row and typing it again was the only fix, and it takes «առաքիչին»
    with it — which is the part of a withdrawal worth keeping."""
    owner_id, store_id, item_id = await _a_shop()
    await _a_day(owner_id, store_id, item_id)
    movement_id = await db.fetchval(
        "SELECT id FROM cash_movements WHERE kind = 'withdrawal'"
    )
    await login(client, "@ownerhandle")
    csrf = await db.fetchval(
        "SELECT csrf_token FROM auth_sessions ORDER BY created_at DESC LIMIT 1"
    )

    response = await client.post(
        f"/movements/{movement_id}?back=/statistics", data={"csrf_token": csrf, "amount": "900"}
    )

    assert response.status_code in (200, 303)
    row = await db.fetchrow(
        "SELECT amount, note FROM cash_movements WHERE id = $1", movement_id
    )
    assert row["amount"] == Decimal("-900.00"), "still an outgoing"
    assert row["note"] == "առաքիչին", "and still says why"


async def test_correcting_a_sale_row_is_refused(client):
    """It is one half of a receipt: editing it alone would leave the two disagreeing
    forever, and the receipt has its own editor that moves the pair together."""
    import pytest

    from app.errors import AppError
    from app.services import corrections

    owner_id, store_id, item_id = await _a_shop()
    await _a_day(owner_id, store_id, item_id)
    sale_row = await db.fetchval("SELECT id FROM cash_movements WHERE kind = 'sale'")

    with pytest.raises(AppError):
        await corrections.set_movement_amount(
            owner_id, owner_id, sale_row, Decimal("100")
        )


async def test_correcting_a_withdrawal_can_be_undone(client):
    from app.services import corrections

    owner_id, store_id, item_id = await _a_shop()
    await _a_day(owner_id, store_id, item_id)
    movement_id = await db.fetchval(
        "SELECT id FROM cash_movements WHERE kind = 'withdrawal'"
    )

    await corrections.set_movement_amount(owner_id, owner_id, movement_id, Decimal("900"))
    event_id = await db.fetchval("SELECT id FROM audit_events ORDER BY id DESC LIMIT 1")
    await corrections.revert(owner_id, owner_id, event_id)

    assert await db.fetchval(
        "SELECT amount FROM cash_movements WHERE id = $1", movement_id
    ) == Decimal("-700.00")


async def test_an_expense_amount_can_be_fixed_from_the_list(client):
    """Nine times out of ten what is wrong is the number, and sending somebody to
    another page to fix a digit is how a wrong number stays in the books."""
    owner_id, _, _ = await _a_shop()
    expense_id = await db.fetchval(
        """
        INSERT INTO expenses (owner_id, purpose, amount, spent_on)
        VALUES ($1, 'Վարձակալություն', 120000, $2) RETURNING id
        """,
        owner_id, settings.local_day(),
    )
    await login(client, "@ownerhandle")
    csrf = await db.fetchval(
        "SELECT csrf_token FROM auth_sessions ORDER BY created_at DESC LIMIT 1"
    )

    response = await client.post(
        f"/expenses/{expense_id}/amount",
        data={"csrf_token": csrf, "amount": "130000"},
    )

    assert response.status_code in (200, 303)
    row = await db.fetchrow(
        "SELECT amount, purpose FROM expenses WHERE id = $1", expense_id
    )
    assert row["amount"] == Decimal("130000.00")
    assert row["purpose"] == "Վարձակալություն", "the other six fields are untouched"


async def test_a_correction_returns_to_the_page_it_was_made_on(client):
    """The same list is on two pages, and a fix made on one should not land on the
    other."""
    owner_id, store_id, item_id = await _a_shop()
    await _a_day(owner_id, store_id, item_id)
    movement_id = await db.fetchval(
        "SELECT id FROM cash_movements WHERE kind = 'withdrawal'"
    )
    await login(client, "@ownerhandle")
    csrf = await db.fetchval(
        "SELECT csrf_token FROM auth_sessions ORDER BY created_at DESC LIMIT 1"
    )

    response = await client.post(
        f"/movements/{movement_id}/delete?back=/expenses",
        data={"csrf_token": csrf},
        follow_redirects=False,
    )

    assert response.headers["location"] == "/expenses"


async def test_a_back_target_off_this_site_is_ignored(client):
    """An open redirect is a phishing tool, and the only thing a caller legitimately
    needs is "the page I was looking at"."""
    owner_id, store_id, item_id = await _a_shop()
    await _a_day(owner_id, store_id, item_id)
    movement_id = await db.fetchval(
        "SELECT id FROM cash_movements WHERE kind = 'withdrawal'"
    )
    await login(client, "@ownerhandle")
    csrf = await db.fetchval(
        "SELECT csrf_token FROM auth_sessions ORDER BY created_at DESC LIMIT 1"
    )

    response = await client.post(
        f"/movements/{movement_id}/delete?back=//evil.example.com",
        data={"csrf_token": csrf},
        follow_redirects=False,
    )

    assert "evil.example.com" not in response.headers["location"]
