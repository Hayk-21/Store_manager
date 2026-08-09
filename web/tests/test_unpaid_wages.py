"""A wage the drawer cannot pay.

Cash in a drawer cannot be negative, and for a while this one could. A shop with
1,000 in the till that took 6,552 on card paid a 3,500 salary out of it and closed
with cash of -2,500 — and then everything downstream computed from that fiction: the
worker who left 5,000 in the shop was told the drawer had held 7,500 more than
expected, which was true only of a number, not of any money.

So the till pays what it can. The rest is a debt the owner settles from the card
takings or their own pocket, carried on the shift row so nobody has to remember it.

Two figures, not one. ``salary_paid`` stays what the shift *cost* — the wage bill in
the statistics and the box the owner edits on the report do not change because the
cash happened to be short on the night — and ``salary_unpaid`` is what is still
owed. Netting them would answer neither "what did this shift cost" nor "what is this
person waiting for".
"""

from __future__ import annotations

from decimal import Decimal

from app.db import db
from app.repo import money as money_repo
from app.services import corrections as corrections_service
from app.services import sales as sales_service
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


async def _a_shop(float_: str = "1000.00"):
    owner_id = await make_owner("@ownerhandle")
    store_id = await make_store(owner_id, "Նուբարաշեն", lat=YEREVAN_LAT, lng=YEREVAN_LNG)
    await db.execute(
        "UPDATE stores SET till_balance = $2 WHERE id = $1", store_id, Decimal(float_)
    )
    item_id = await make_item(
        owner_id, store_id, "HQD Cuvie", count=50,
        self_price="1500.00", sell_price="3276.00",
    )
    return owner_id, store_id, item_id


async def _worker(owner_id: int, salary: str = "7000.00", period: str = "shift"):
    worker_id, telegram_id = await make_worker(
        owner_id, "Անի", salary_amount=salary, salary_period=period
    )
    return shifts_service.Worker(
        id=worker_id, owner_id=owner_id, name="Անի",
        salary_amount=Decimal(salary), salary_period=period,
    ), telegram_id


async def _open(worker, key: str = "idem-open-1"):
    await shifts_service.open_store(worker, YEREVAN_LAT, YEREVAN_LNG, 20, key, 900)


async def _shift() -> int:
    return await db.fetchval("SELECT id FROM work_sessions ORDER BY id DESC LIMIT 1")


async def _session() -> int:
    return await db.fetchval("SELECT id FROM store_sessions ORDER BY id DESC LIMIT 1")


async def _cash(session_id: int) -> Decimal:
    return Decimal((await money_repo.totals_for_session(session_id))["cash"])


async def _wages(shift_id: int):
    return await db.fetchrow(
        """
        SELECT salary_paid, salary_unpaid, bonus_paid, bonus_unpaid
          FROM work_sessions WHERE id = $1
        """,
        shift_id,
    )


# -- the drawer sets the limit ------------------------------------------------

async def test_a_thin_drawer_pays_what_it_has_and_no_more(client):
    """The screenshot, reproduced: 1,000 in the drawer, the day taken on card, a
    3,500 wage due."""
    owner_id, _, item_id = await _a_shop(float_="1000.00")
    worker, _ = await _worker(owner_id, salary="7000.00")
    await _open(worker)
    await sales_service.record_sale(
        worker, [{"item_id": item_id, "quantity": 2}], "card", "idem-sale-1"
    )
    session_id, shift_id = await _session(), await _shift()

    await shifts_service.end_shift(worker, None, None, "idem-end-1")

    assert await _cash(session_id) == Decimal("0.00"), "a drawer cannot go negative"
    wages = await _wages(shift_id)
    assert wages["salary_paid"] == Decimal("3500.00"), "what the shift cost"
    assert wages["salary_unpaid"] == Decimal("2500.00"), "what is still owed"


async def test_an_empty_drawer_pays_nothing_and_books_nothing(client):
    owner_id, _, _ = await _a_shop(float_="0.00")
    worker, _ = await _worker(owner_id, salary="7000.00")
    await _open(worker)
    session_id, shift_id = await _session(), await _shift()

    await shifts_service.end_shift(worker, None, None, "idem-end-1")

    assert await db.fetchval(
        "SELECT count(*) FROM cash_movements WHERE kind = 'salary'"
    ) == 0, "a movement of zero is not a payment"
    assert await _cash(session_id) == Decimal("0.00")
    assert (await _wages(shift_id))["salary_unpaid"] == Decimal("3500.00")


async def test_a_full_drawer_pays_the_whole_wage_and_owes_nothing(client):
    owner_id, _, item_id = await _a_shop(float_="40000.00")
    worker, _ = await _worker(owner_id, salary="7000.00")
    await _open(worker)
    await sales_service.record_sale(
        worker, [{"item_id": item_id, "quantity": 2}], "cash", "idem-sale-1"
    )
    session_id, shift_id = await _session(), await _shift()
    await worked_a_full_shift(worker.id)

    await shifts_service.end_shift(worker, None, None, "idem-end-1")

    assert await _cash(session_id) == Decimal("39552.00")
    wages = await _wages(shift_id)
    assert wages["salary_paid"] == Decimal("7000.00")
    assert wages["salary_unpaid"] == Decimal("0.00")


async def test_the_wage_paid_is_booked_as_an_ordinary_salary(client):
    """Not an adjustment or a part-payment of its own kind. It is the wage, for less
    than the wage was worth, and every report that already understands salaries
    should keep understanding it."""
    owner_id, _, _ = await _a_shop(float_="1000.00")
    worker, _ = await _worker(owner_id, salary="7000.00")
    await _open(worker)

    await shifts_service.end_shift(worker, None, None, "idem-end-1")

    row = await db.fetchrow(
        "SELECT kind, method, amount FROM cash_movements WHERE kind = 'salary'"
    )
    assert row["amount"] == Decimal("-1000.00")
    assert row["method"] == "cash"


async def test_a_monthly_wage_owes_nothing_because_it_costs_the_till_nothing(client):
    owner_id, _, _ = await _a_shop(float_="0.00")
    worker, _ = await _worker(owner_id, salary="200000.00", period="month")
    await _open(worker)
    shift_id = await _shift()

    await shifts_service.end_shift(worker, None, None, "idem-end-1")

    wages = await _wages(shift_id)
    assert wages["salary_paid"] == Decimal("0.00")
    assert wages["salary_unpaid"] == Decimal("0.00"), "the owner pays it separately"


async def test_the_closing_snapshot_is_never_negative(client):
    """It is the figure on the report, and a report saying a drawer held less than
    nothing is a report nobody can act on."""
    owner_id, _, item_id = await _a_shop(float_="1000.00")
    worker, _ = await _worker(owner_id, salary="7000.00")
    await _open(worker)
    await sales_service.record_sale(
        worker, [{"item_id": item_id, "quantity": 2}], "card", "idem-sale-1"
    )
    session_id = await _session()

    await shifts_service.end_shift(worker, None, None, "idem-end-1")

    assert await db.fetchval(
        "SELECT cash_at_close FROM store_sessions WHERE id = $1", session_id
    ) == Decimal("0.00")


async def test_two_thin_shifts_do_not_share_the_same_thousand(client):
    """The second worker finds the drawer already emptied by the first, so their whole
    wage is owed. Read from the ledger rather than assumed, which is the point of
    checking the till each time instead of once."""
    owner_id, _, _ = await _a_shop(float_="1000.00")
    first, _ = await _worker(owner_id, salary="7000.00")
    second_id, _ = await make_worker(
        owner_id, "Գոռ", salary_amount="7000.00", salary_period="shift"
    )
    second = shifts_service.Worker(
        id=second_id, owner_id=owner_id, name="Գոռ",
        salary_amount=Decimal("7000.00"), salary_period="shift",
    )
    await _open(first, "idem-open-1")
    await _open(second, "idem-open-2")
    first_shift = await db.fetchval(
        "SELECT id FROM work_sessions WHERE worker_id = $1", first.id
    )
    second_shift = await db.fetchval(
        "SELECT id FROM work_sessions WHERE worker_id = $1", second_id
    )

    await shifts_service.end_shift(first, None, None, "idem-end-1")
    await shifts_service.end_shift(second, None, None, "idem-end-2")

    assert (await _wages(first_shift))["salary_unpaid"] == Decimal("2500.00")
    assert (await _wages(second_shift))["salary_unpaid"] == Decimal("3500.00")


# -- the bonus, behind the wage ----------------------------------------------

async def test_the_wage_is_paid_before_the_bonus(client):
    """When there is not enough cash for both, the one that should be in the worker's
    hand is what they are owed for the day. A bonus is on top of it."""
    owner_id, _, item_id = await _a_shop(float_="4000.00")
    worker_id, _ = await make_worker(
        owner_id, "Անի", salary_amount="7000.00", salary_period="shift",
    )
    await db.execute(
        """
        UPDATE workers SET bonus_threshold = 1000, bonus_amount = 5000,
                           bonus_period = 'day'
         WHERE id = $1
        """,
        worker_id,
    )
    worker = shifts_service.Worker(
        id=worker_id, owner_id=owner_id, name="Անի",
        salary_amount=Decimal("7000.00"), salary_period="shift",
    )
    await _open(worker)
    await sales_service.record_sale(
        worker, [{"item_id": item_id, "quantity": 1}], "card", "idem-sale-1"
    )
    shift_id = await _shift()
    await worked_a_full_shift(worker.id)

    await shifts_service.end_shift(worker, None, None, "idem-end-1")

    wages = await _wages(shift_id)
    assert wages["salary_paid"] == Decimal("7000.00")
    assert wages["salary_unpaid"] == Decimal("3000.00"), "4,000 of a 7,000 wage"
    assert wages["bonus_paid"] == Decimal("5000.00"), "earned is earned"
    assert wages["bonus_unpaid"] == Decimal("5000.00"), "and none of it was in the till"


# -- what the bot and the owner are told -------------------------------------

async def test_the_bot_is_told_what_the_worker_is_still_owed(client, bot_headers):
    owner_id, _, item_id = await _a_shop(float_="1000.00")
    worker, telegram_id = await _worker(owner_id, salary="7000.00")
    await _open(worker)
    await sales_service.record_sale(
        worker, [{"item_id": item_id, "quantity": 2}], "card", "idem-sale-1"
    )

    response = await client.post(
        "/api/bot/v1/shift/end",
        json={"telegram_id": telegram_id, "idempotency_key": "idem-end-0001"},
        headers=bot_headers,
    )

    summary = response.json()["summary"]
    assert summary["salary_deducted"] == "3500.00", "what the shift was worth"
    assert summary["salary_unpaid"] == "2500.00", "what they are still waiting for"


async def test_the_report_shows_the_debt_beside_the_wage(client):
    owner_id, _, item_id = await _a_shop(float_="1000.00")
    worker, _ = await _worker(owner_id, salary="7000.00")
    await _open(worker)
    await sales_service.record_sale(
        worker, [{"item_id": item_id, "quantity": 2}], "card", "idem-sale-1"
    )
    session_id = await _session()
    await shifts_service.end_shift(worker, None, None, "idem-end-1")
    await login(client, "@ownerhandle")

    page = await client.get(f"/reports?store_session_id={session_id}")

    assert "պարտք" in page.text
    assert "2,500" in page.text


async def test_the_owner_restating_a_wage_clears_the_debt(client):
    """They are saying what the shift was paid, and by the time they are editing the
    row they have settled whatever the till was short."""
    owner_id, _, _ = await _a_shop(float_="1000.00")
    worker, _ = await _worker(owner_id, salary="7000.00")
    await _open(worker)
    shift_id = await _shift()
    await shifts_service.end_shift(worker, None, None, "idem-end-1")

    await corrections_service.set_salary(owner_id, owner_id, shift_id, Decimal("3500"))

    wages = await _wages(shift_id)
    assert wages["salary_paid"] == Decimal("3500.00")
    assert wages["salary_unpaid"] == Decimal("0.00")
