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
from app.services import money as money_service
from app.services import sales as sales_service
from app.services import shifts as shifts_service
from app.services import till as till_service
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


async def test_a_real_evening_end_to_end(client):
    """One shop's actual books, reproduced figure for figure, because every part of this
    passing its own test is not the same as the evening adding up.

    A 234 float; 40,000 and 3,000 on card; 2,400 in cash; 1,000 taken for something;
    a 3,500 wage and a 2,000 bonus against a drawer holding 1,634.
    """
    owner_id, store_id, _ = await _a_shop(float_="234.00")
    worker_id, _ = await make_worker(
        owner_id, "Հայկ", salary_amount="3500.00", salary_period="shift"
    )
    await db.execute(
        """
        UPDATE workers SET bonus_threshold = 1000, bonus_amount = 2000,
                           bonus_period = 'day'
         WHERE id = $1
        """,
        worker_id,
    )
    worker = shifts_service.Worker(
        id=worker_id, owner_id=owner_id, name="Հայկ",
        salary_amount=Decimal("3500.00"), salary_period="shift",
    )
    cheap = await make_item(
        owner_id, store_id, "Cfvb", count=50, self_price="6.00", sell_price="600.00"
    )
    dear = await make_item(
        owner_id, store_id, "asdasd", count=50, self_price="6.00", sell_price="10000.00"
    )
    await _open(worker)
    session_id, shift_id = await _session(), await _shift()

    await sales_service.record_sale(
        worker, [{"item_id": dear, "quantity": 4}], "card", "idem-sale-1"
    )
    await sales_service.record_sale(
        worker, [{"item_id": cheap, "quantity": 5}], "card", "idem-sale-2"
    )
    await sales_service.record_sale(
        worker, [{"item_id": cheap, "quantity": 4}], "cash", "idem-sale-3"
    )
    await money_service.withdraw_by_worker(
        worker, Decimal("1000"), "առաքիչին", "idem-cash-01"
    )
    await worked_a_full_shift(worker.id)
    await shifts_service.close_out_shift(worker, [], "idem-close-1", counted=Decimal("0"))

    # The drawer held 234 + 2,400 − 1,000 = 1,634 when the wage fell due.
    wages = await _wages(shift_id)
    assert wages["salary_paid"] == Decimal("3500.00"), "what the shift cost"
    assert wages["bonus_paid"] == Decimal("2000.00")
    assert wages["salary_unpaid"] + wages["bonus_unpaid"] == Decimal("3866.00")
    assert await _cash(session_id) == Decimal("0.00"), "the drawer paid all it had"

    # And the count that follows sees an empty till, not the float it opened with.
    result = await till_service.declare_close(worker, Decimal("1500"), "idem-till-01")
    assert result["count"]["expected"] == "0.00"
    assert result["count"]["handed_over"] == "0.00", "there is nothing to hand over"


async def test_the_owners_share_follows_a_sale_added_after_the_count(client):
    """The owner's arithmetic, in their words:

        float + cash sales − wage paid − cash taken out − left in the shop
        234   + 4,400      − 1,634     − 1,000          − 1,500  =  500

    Which is the drawer as the books stand now, less what stays in the shop. It used to
    come from the reading frozen on the count instead, so a sale entered afterwards never
    reached it: a shop whose drawer really held 2,000 was told the owner was owed
    nothing, because the count had been made when only the 234 float was in it.
    """
    owner_id, store_id, _ = await _a_shop(float_="234.00")
    worker, _ = await _worker(owner_id, salary="7000.00")
    cheap = await make_item(
        owner_id, store_id, "Cfvb", count=50, self_price="6.00", sell_price="600.00"
    )
    await _open(worker)
    session_id = await _session()
    await sales_service.record_sale(
        worker, [{"item_id": cheap, "quantity": 4}], "cash", "idem-sale-1"
    )
    await money_service.withdraw_by_worker(
        worker, Decimal("1000"), "առաքիչին", "idem-cash-01"
    )
    await shifts_service.close_out_shift(worker, [], "idem-close-1", counted=Decimal("0"))
    # Counted while the drawer held 2,400 + 234 − 1,000 − 1,634 = 0.
    await till_service.declare_close(worker, Decimal("1500"), "idem-till-01")
    assert await db.fetchval("SELECT expected FROM till_counts") == Decimal("0.00")

    # Then the owner enters a sale the cashier forgot: 2,000 more in cash.
    await corrections_service.add_sale(
        owner_id, owner_id, session_id, worker.id,
        [{"item_id": cheap, "quantity": 4, "unit_price": Decimal("500.00")}],
        "cash",
    )
    await login(client, "@ownerhandle")

    page = await client.get(f"/reports?store_session_id={session_id}")

    assert "500.00" in page.text, "the drawer holds 2,000 now, less the 1,500 left"


async def test_the_wage_tiles_show_what_the_drawer_paid(client):
    """They sit beside «Ղեկավարին» and «Մնաց խանութում», which are drawer questions, and
    the owner's own arithmetic subtracts the 1,634 that came out of the drawer rather
    than the 5,500 the shift was worth. What the till could not cover is stated
    underneath instead of folded in."""
    owner_id, store_id, _ = await _a_shop(float_="234.00")
    worker_id, _ = await make_worker(
        owner_id, "Հայկ", salary_amount="7000.00", salary_period="shift"
    )
    await db.execute(
        """
        UPDATE workers SET bonus_threshold = 1000, bonus_amount = 2000,
                           bonus_period = 'day'
         WHERE id = $1
        """,
        worker_id,
    )
    worker = shifts_service.Worker(
        id=worker_id, owner_id=owner_id, name="Հայկ",
        salary_amount=Decimal("7000.00"), salary_period="shift",
    )
    cheap = await make_item(
        owner_id, store_id, "Cfvb", count=50, self_price="6.00", sell_price="600.00"
    )
    await _open(worker)
    session_id = await _session()
    await sales_service.record_sale(
        worker, [{"item_id": cheap, "quantity": 4}], "cash", "idem-sale-1"
    )
    await money_service.withdraw_by_worker(
        worker, Decimal("1000"), "առաքիչին", "idem-cash-01"
    )
    await shifts_service.close_out_shift(worker, [], "idem-close-1", counted=Decimal("0"))
    await login(client, "@ownerhandle")

    page = await client.get(f"/reports?store_session_id={session_id}")

    tiles = page.text.split('class="totals"')[1].split("</div>\n  </div>")[0]
    assert "1,634.00" in tiles, "what the drawer paid, not the 3,500 the shift was worth"
    assert "3,500.00" not in tiles
    assert "0.00" in tiles, "and no bonus was paid, because there was nothing to pay it with"
    assert "3,866.00" in page.text, "what is still owed, said underneath"


async def test_that_evenings_report_subtracts_what_the_shift_cost(client):
    """Շահույթ = վաճառք − աշխատավարձ − բոնուս − դրամարկղից հանված, which for this evening
    is 45,400 − 1,634 − 0 − 1,000.

    What the stock cost is not in it: the shop paid for those vapes when it bought them,
    so on the day one sells the whole price is money the business is better off by. Same
    reasoning as breakage, which the page has always excluded for exactly that reason.

    A report on one evening is a report about that evening's money, so the wage in it is
    the one that left the drawer. The 3,866 the till could not cover is a real liability
    and the profit will drop by it the day it is settled — which the page says, rather
    than folding a debt into a figure about tonight's cash.
    """
    owner_id, store_id, _ = await _a_shop(float_="234.00")
    worker_id, _ = await make_worker(
        owner_id, "Հայկ", salary_amount="3500.00", salary_period="shift"
    )
    await db.execute(
        """
        UPDATE workers SET bonus_threshold = 1000, bonus_amount = 2000,
                           bonus_period = 'day'
         WHERE id = $1
        """,
        worker_id,
    )
    worker = shifts_service.Worker(
        id=worker_id, owner_id=owner_id, name="Հայկ",
        salary_amount=Decimal("3500.00"), salary_period="shift",
    )
    cheap = await make_item(
        owner_id, store_id, "Cfvb", count=50, self_price="6.00", sell_price="600.00"
    )
    dear = await make_item(
        owner_id, store_id, "asdasd", count=50, self_price="6.00", sell_price="10000.00"
    )
    await _open(worker)
    session_id = await _session()
    await sales_service.record_sale(
        worker, [{"item_id": dear, "quantity": 4}], "card", "idem-sale-1"
    )
    await sales_service.record_sale(
        worker, [{"item_id": cheap, "quantity": 5}], "card", "idem-sale-2"
    )
    await sales_service.record_sale(
        worker, [{"item_id": cheap, "quantity": 4}], "cash", "idem-sale-3"
    )
    await money_service.withdraw_by_worker(
        worker, Decimal("1000"), "առաքիչին", "idem-cash-01"
    )
    await worked_a_full_shift(worker.id)
    await shifts_service.close_out_shift(worker, [], "idem-close-1", counted=Decimal("0"))
    await login(client, "@ownerhandle")

    page = await client.get(f"/reports?store_session_id={session_id}")

    assert "45,400.00" in page.text, "revenue"
    assert "1,634.00" in page.text, "the wage the drawer paid"
    assert "42,766.00" in page.text, "45,400 sold − 1,634 of wages − 1,000 taken"
    assert "45,322.00" in page.text, "and the margin, stated but not subtracted"
    assert "3,866.00" in page.text, "and what the drawer could not cover, said apart"


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
