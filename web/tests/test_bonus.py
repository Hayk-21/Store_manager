"""Beat a target, earn a bonus.

The rule is three things together: how much has to be sold, over what stretch,
and what it pays. It is judged as the shift closes — the moment the period's
sales are finally known and the moment money already leaves the till — so a
bonus is one more row in the same ledger rather than a scheme running beside it.

Once per period, not per shift. Somebody who crosses the target in the morning
and works again in the evening has earned it once.
"""

from __future__ import annotations

from decimal import Decimal

from app.db import db
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


async def _shop_with_bonus(threshold="20000.00", bonus="5000.00", period="day",
                           salary="0.00"):
    owner_id = await make_owner("@ownerhandle")
    store_id = await make_store(owner_id, "Խանութ 1", lat=YEREVAN_LAT, lng=YEREVAN_LNG)
    item_id = await make_item(
        owner_id, store_id, "HQD Cuvie", count=500, self_price="1000.00",
        sell_price="5000.00",
    )
    worker_id, _ = await make_worker(owner_id, "Անի", salary_amount=salary)
    if threshold is not None:
        await db.execute(
            """
            UPDATE workers SET bonus_threshold = $2, bonus_amount = $3, bonus_period = $4
             WHERE id = $1
            """,
            worker_id, Decimal(threshold), Decimal(bonus), period,
        )
    worker = shifts_service.Worker(
        id=worker_id, owner_id=owner_id, name="Անի", salary_amount=Decimal(salary)
    )
    return owner_id, store_id, item_id, worker


async def _work(worker, item_id, *, sold: int, key: str, close_store=True):
    await shifts_service.open_store(worker, YEREVAN_LAT, YEREVAN_LNG, 20, f"open-{key}", 900)
    await shifts_service.close_out_shift(
        worker,
        [{"item_id": item_id, "quantity": sold, "unit_price": "5000.00",
          "payment_method": "cash"}] if sold else [],
        f"close-{key}",
        close_store_too=close_store,
    )


async def _bonus_rows() -> int:
    return await db.fetchval("SELECT count(*) FROM cash_movements WHERE kind = 'bonus'")


# -- earning it ---------------------------------------------------------------

async def test_beating_the_target_pays_the_bonus(client):
    _, _, item_id, worker = await _shop_with_bonus(threshold="20000.00", bonus="5000.00")

    await _work(worker, item_id, sold=5, key="1")  # 25,000

    assert await _bonus_rows() == 1
    assert await db.fetchval(
        "SELECT amount FROM cash_movements WHERE kind = 'bonus'"
    ) == Decimal("-5000.00"), "it leaves the till, like a wage"
    assert await db.fetchval("SELECT bonus_paid FROM work_sessions") == Decimal("5000.00")


async def test_falling_short_pays_nothing(client):
    _, _, item_id, worker = await _shop_with_bonus(threshold="20000.00")

    await _work(worker, item_id, sold=3, key="1")  # 15,000

    assert await _bonus_rows() == 0
    assert await db.fetchval("SELECT bonus_paid FROM work_sessions") is None


async def test_exactly_the_target_counts_as_beating_it(client):
    """A target of 20,000 that pays at 20,001 would be a target of 20,001."""
    _, _, item_id, worker = await _shop_with_bonus(threshold="20000.00")

    await _work(worker, item_id, sold=4, key="1")  # exactly 20,000

    assert await _bonus_rows() == 1


async def test_a_worker_with_no_rule_never_gets_one(client):
    _, _, item_id, worker = await _shop_with_bonus(threshold=None)

    await _work(worker, item_id, sold=100, key="1")

    assert await _bonus_rows() == 0


async def test_the_bonus_comes_out_of_the_till_with_the_wage(client):
    _, _, item_id, worker = await _shop_with_bonus(
        threshold="20000.00", bonus="5000.00", salary="8000.00"
    )

    await _work(worker, item_id, sold=5, key="1")  # 25,000 in

    # 25,000 taken, 8,000 wage and 5,000 bonus paid out.
    assert await db.fetchval("SELECT cash_at_close FROM store_sessions") == Decimal("12000.00")


async def test_a_monthly_paid_worker_still_earns_a_daily_bonus(client):
    """Their shift costs the till nothing, but they still beat the target and
    that is money they have earned."""
    owner_id, _, item_id, worker = await _shop_with_bonus(threshold="20000.00")
    await db.execute("UPDATE workers SET salary_period = 'month'")
    worker = shifts_service.Worker(
        id=worker.id, owner_id=owner_id, name="Անի",
        salary_amount=Decimal("50000.00"), salary_period="month",
    )

    await _work(worker, item_id, sold=5, key="1")

    assert await _bonus_rows() == 1
    assert await db.fetchval("SELECT count(*) FROM cash_movements WHERE kind = 'salary'") == 0


# -- once per period ----------------------------------------------------------

async def test_a_second_shift_the_same_day_does_not_pay_it_twice(client):
    _, _, item_id, worker = await _shop_with_bonus(threshold="20000.00")

    await _work(worker, item_id, sold=5, key="1")   # earns it
    await _work(worker, item_id, sold=5, key="2")   # same day, beats it again

    assert await _bonus_rows() == 1, "earned once in the period"


async def test_the_second_shift_is_what_tips_them_over(client):
    """The target is measured over the period, not over one shift — two 15,000
    mornings are 30,000 on the day."""
    _, _, item_id, worker = await _shop_with_bonus(threshold="20000.00")

    await _work(worker, item_id, sold=3, key="1")   # 15,000 — not yet
    assert await _bonus_rows() == 0

    await _work(worker, item_id, sold=3, key="2")   # 30,000 on the day

    assert await _bonus_rows() == 1


async def test_yesterdays_sales_do_not_count_towards_today(client):
    _, _, item_id, worker = await _shop_with_bonus(threshold="20000.00")
    await _work(worker, item_id, sold=3, key="1")  # 15,000
    await db.execute("UPDATE sales SET sold_at = sold_at - interval '1 day'")

    await _work(worker, item_id, sold=3, key="2")  # 15,000 today

    assert await _bonus_rows() == 0


async def test_a_monthly_target_counts_the_whole_month(client):
    _, _, item_id, worker = await _shop_with_bonus(threshold="40000.00", period="month")
    await _work(worker, item_id, sold=5, key="1")  # 25,000
    await db.execute("UPDATE sales SET sold_at = sold_at - interval '2 days'")
    assert await _bonus_rows() == 0

    await _work(worker, item_id, sold=4, key="2")  # 20,000 more, 45,000 in the month

    assert await _bonus_rows() == 1


async def test_a_voided_sale_does_not_count_towards_the_target(client):
    """Money that came back is not money the shop took."""
    owner_id, _, item_id, worker = await _shop_with_bonus(threshold="20000.00")
    await _work(worker, item_id, sold=5, key="1", close_store=True)
    assert await _bonus_rows() == 1

    from app.services import corrections

    for sale_id in await db.fetch("SELECT id FROM sales"):
        await corrections.void_sale(owner_id, owner_id, sale_id["id"])
    await db.execute("DELETE FROM cash_movements WHERE kind = 'bonus'")
    await db.execute("UPDATE work_sessions SET bonus_paid = NULL")

    await _work(worker, item_id, sold=3, key="2")  # 15,000 standing

    assert await _bonus_rows() == 0


async def test_one_workers_sales_do_not_earn_another_a_bonus(client):
    owner_id, store_id, item_id, ani = await _shop_with_bonus(threshold="20000.00")
    gor_id, _ = await make_worker(owner_id, "Գոռ", salary_amount="0.00")
    await db.execute(
        """
        UPDATE workers SET bonus_threshold = 20000, bonus_amount = 5000, bonus_period = 'day'
         WHERE id = $1
        """,
        gor_id,
    )
    gor = shifts_service.Worker(
        id=gor_id, owner_id=owner_id, name="Գոռ", salary_amount=Decimal("0.00")
    )

    await _work(ani, item_id, sold=5, key="1")     # Ani earns it
    await _work(gor, item_id, sold=1, key="2")     # Gor sells 5,000

    assert await _bonus_rows() == 1
    assert await db.fetchval(
        "SELECT worker_id FROM cash_movements WHERE kind = 'bonus'"
    ) == ani.id


# -- setting the rule ---------------------------------------------------------

async def _csrf(client) -> str:
    from app.deps import SESSION_COOKIE
    from app.repo import users as users_repo
    from app.security import hash_token

    row = await users_repo.session_with_user(hash_token(client.cookies[SESSION_COOKIE]))
    return row["csrf_token"]


async def test_the_owner_can_set_a_bonus_when_registering(client):
    await make_owner("@ownerhandle")
    await login(client, "@ownerhandle")

    response = await client.post(
        "/workers",
        data={"csrf_token": await _csrf(client), "telegram_username": "@anivape",
              "salary_amount": "8000", "salary_period": "shift",
              "bonus_threshold": "200000", "bonus_amount": "10000",
              "bonus_period": "month"},
    )

    assert response.status_code == 303
    row = await db.fetchrow(
        "SELECT bonus_threshold, bonus_amount, bonus_period FROM workers"
    )
    assert row["bonus_threshold"] == Decimal("200000.00")
    assert row["bonus_amount"] == Decimal("10000.00")
    assert row["bonus_period"] == "month"


async def test_leaving_the_bonus_blank_means_no_bonus(client):
    await make_owner("@ownerhandle")
    await login(client, "@ownerhandle")

    await client.post(
        "/workers",
        data={"csrf_token": await _csrf(client), "telegram_username": "@anivape",
              "salary_amount": "8000", "salary_period": "shift",
              "bonus_threshold": "", "bonus_amount": "", "bonus_period": "day"},
    )

    assert await db.fetchval("SELECT bonus_threshold FROM workers") is None


async def test_half_a_rule_is_refused(client):
    """A threshold with no amount would never pay; an amount with no threshold
    would pay every shift. Neither is what anybody meant."""
    await make_owner("@ownerhandle")
    await login(client, "@ownerhandle")

    response = await client.post(
        "/workers",
        data={"csrf_token": await _csrf(client), "telegram_username": "@anivape",
              "salary_amount": "8000", "salary_period": "shift",
              "bonus_threshold": "200000", "bonus_amount": "", "bonus_period": "day"},
    )

    assert response.status_code == 422
    assert await db.fetchval("SELECT count(*) FROM workers") == 0


async def test_the_rule_can_be_changed_later(client):
    owner_id, _, _, worker = await _shop_with_bonus(threshold="20000.00")
    await login(client, "@ownerhandle")

    await client.post(
        f"/workers/{worker.id}",
        data={"csrf_token": await _csrf(client), "telegram_username": "@anivape",
              "name": "Անի", "salary_amount": "0", "salary_period": "shift",
              "is_active": "1", "bonus_threshold": "50000",
              "bonus_amount": "7000", "bonus_period": "month"},
    )

    row = await db.fetchrow("SELECT bonus_threshold, bonus_period FROM workers")
    assert row["bonus_threshold"] == Decimal("50000.00")
    assert row["bonus_period"] == "month"


async def test_the_rule_can_be_removed(client):
    owner_id, _, _, worker = await _shop_with_bonus(threshold="20000.00")
    await login(client, "@ownerhandle")

    await client.post(
        f"/workers/{worker.id}",
        data={"csrf_token": await _csrf(client), "telegram_username": "@anivape",
              "name": "Անի", "salary_amount": "0", "salary_period": "shift",
              "is_active": "1", "bonus_threshold": "", "bonus_amount": "",
              "bonus_period": "day"},
    )

    assert await db.fetchval("SELECT bonus_threshold FROM workers") is None


async def test_a_negative_bonus_is_refused(client):
    await make_owner("@ownerhandle")
    await login(client, "@ownerhandle")

    response = await client.post(
        "/workers",
        data={"csrf_token": await _csrf(client), "telegram_username": "@anivape",
              "salary_amount": "8000", "salary_period": "shift",
              "bonus_threshold": "-5", "bonus_amount": "1000",
              "bonus_period": "day"},
    )

    assert response.status_code == 422
    assert await db.fetchval("SELECT count(*) FROM workers") == 0


async def test_an_unknown_bonus_period_is_refused(client):
    """The column has a CHECK, but a form field should not be the thing that
    discovers it — the refusal should read as a sentence."""
    await make_owner("@ownerhandle")
    await login(client, "@ownerhandle")

    response = await client.post(
        "/workers",
        data={"csrf_token": await _csrf(client), "telegram_username": "@anivape",
              "salary_amount": "8000", "salary_period": "shift",
              "bonus_threshold": "1000", "bonus_amount": "100",
              "bonus_period": "fortnight"},
    )

    assert response.status_code == 422
    assert await db.fetchval("SELECT count(*) FROM workers") == 0
