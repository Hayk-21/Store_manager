"""Costs that are not till movements: rent, advertising, paying an influencer."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from app.db import db
from app.repo import expenses as expenses_repo
from tests.factories import login, make_owner, make_store

TODAY = date.today()


async def _signed_in(client) -> tuple[int, str]:
    owner_id = await make_owner("@ownerhandle")
    await login(client, "@ownerhandle")
    token = await db.fetchval(
        "SELECT csrf_token FROM auth_sessions WHERE user_id = $1", owner_id
    )
    return owner_id, token


async def _record(client, token, **fields):
    payload = {
        "purpose": "Ինստագրամ բլոգերի վճար",
        "amount": "50000",
        "spent_on": TODAY.isoformat(),
        "method": "cash",
        "recurrence": "once",
        "csrf_token": token,
    }
    payload.update(fields)
    return await client.post("/expenses", data=payload)


async def test_recording_an_expense_keeps_its_purpose(client):
    """An unexplained payment in the books is worse than no record of it."""
    owner_id, token = await _signed_in(client)

    response = await _record(client, token)

    assert response.status_code == 303
    row = await db.fetchrow("SELECT * FROM expenses WHERE owner_id = $1", owner_id)
    assert row["purpose"] == "Ինստագրամ բլոգերի վճար"
    assert row["amount"] == Decimal("50000.00")
    assert row["store_id"] is None, "an advertising spend belongs to the business"


async def test_a_purpose_is_required(client):
    _, token = await _signed_in(client)

    response = await _record(client, token, purpose="")

    assert response.status_code == 422
    assert await db.fetchval("SELECT count(*) FROM expenses") == 0


async def test_an_expense_can_belong_to_one_store(client):
    owner_id, token = await _signed_in(client)
    store_id = await make_store(owner_id, "Tumo")

    await _record(client, token, purpose="Վարձակալություն", store_id=str(store_id))

    assert await db.fetchval("SELECT store_id FROM expenses") == store_id


async def test_a_monthly_cost_is_marked_as_such(client):
    _, token = await _signed_in(client)

    await _record(client, token, purpose="Վարձակալություն", recurrence="monthly")

    assert await db.fetchval("SELECT recurrence FROM expenses") == "monthly"


async def test_expenses_do_not_touch_the_till(client):
    """They are a cost of the business, not of whichever shift was open."""
    _, token = await _signed_in(client)

    await _record(client, token, amount="100000")

    assert await db.fetchval("SELECT count(*) FROM cash_movements") == 0


async def test_an_expense_can_be_corrected(client):
    owner_id, token = await _signed_in(client)
    await _record(client, token)
    expense_id = await db.fetchval("SELECT id FROM expenses")

    response = await client.post(
        f"/expenses/{expense_id}",
        data={"purpose": "Ուղղված", "amount": "42000", "spent_on": TODAY.isoformat(),
              "method": "bank", "recurrence": "once", "csrf_token": token},
    )

    assert response.status_code == 303
    row = await db.fetchrow("SELECT purpose, amount, method FROM expenses")
    assert row["purpose"] == "Ուղղված"
    assert row["amount"] == Decimal("42000.00")
    assert row["method"] == "bank"


async def test_an_expense_can_be_deleted(client):
    """Unlike a sale, an expense references nothing, so a mistyped one is best
    simply gone rather than archived."""
    _, token = await _signed_in(client)
    await _record(client, token)
    expense_id = await db.fetchval("SELECT id FROM expenses")

    response = await client.post(f"/expenses/{expense_id}/delete", data={"csrf_token": token})

    assert response.status_code == 303
    assert await db.fetchval("SELECT count(*) FROM expenses") == 0


async def test_another_owners_expense_is_not_reachable(client):
    await _signed_in(client)
    other = await make_owner()
    theirs = await expenses_repo.create(other, "Ուրիշի ծախս", Decimal("1000"), TODAY)
    token = await db.fetchval("SELECT csrf_token FROM auth_sessions LIMIT 1")

    edit = await client.post(
        f"/expenses/{theirs}",
        data={"purpose": "hijacked", "amount": "1", "spent_on": TODAY.isoformat(),
              "method": "cash", "recurrence": "once", "csrf_token": token},
    )
    drop = await client.post(f"/expenses/{theirs}/delete", data={"csrf_token": token})

    assert edit.status_code == 404 and drop.status_code == 404
    assert await db.fetchval("SELECT purpose FROM expenses") == "Ուրիշի ծախս"


async def test_the_page_totals_the_month_and_groups_by_category(client):
    owner_id, token = await _signed_in(client)
    await expenses_repo.ensure_starter_categories(owner_id)
    rent = await db.fetchval(
        "SELECT id FROM expense_categories WHERE owner_id = $1 AND name = 'Վարձակալություն'",
        owner_id,
    )
    await _record(client, token, purpose="Վարձ", amount="200000", category_id=str(rent))
    await _record(client, token, purpose="Գովազդ", amount="50000")

    page = await client.get("/expenses")

    assert page.status_code == 200
    assert "250,000.00" in page.text, "the month's total"
    assert "Վարձակալություն" in page.text


async def test_a_different_month_is_not_counted(client):
    owner_id, token = await _signed_in(client)
    await _record(client, token, amount="70000")
    await db.execute("UPDATE expenses SET spent_on = spent_on - interval '2 months'")

    page = await client.get("/expenses")

    assert "70,000.00" not in page.text
    assert await expenses_repo.total_between(
        owner_id, TODAY.replace(day=1), TODAY
    ) == Decimal("0")


async def test_starter_categories_are_offered_once(client):
    owner_id, _ = await _signed_in(client)

    await expenses_repo.ensure_starter_categories(owner_id)
    await expenses_repo.ensure_starter_categories(owner_id)

    names = [c["name"] for c in await expenses_repo.categories(owner_id)]
    assert len(names) == len(set(names)) == len(expenses_repo.STARTER_CATEGORIES)


async def test_a_category_can_be_added(client):
    owner_id, token = await _signed_in(client)

    await client.post("/expenses/categories", data={"name": "Վերանորոգում",
                                                    "csrf_token": token})

    assert "Վերանորոգում" in [c["name"] for c in await expenses_repo.categories(owner_id)]


async def test_the_expenses_link_is_in_the_navigation(client):
    await _signed_in(client)

    page = await client.get("/stores")

    assert 'href="/expenses"' in page.text
