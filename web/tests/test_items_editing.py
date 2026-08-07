"""Editing the stock list: the operations an owner does every day.

Three of these are regressions from the field. Deleting is a soft delete — past
sale lines reference the row forever — but the name index covered deleted rows
too, so a deleted name stayed taken and adding the product back failed against a
row the owner could no longer see. Nothing on screen could explain that.
"""

from __future__ import annotations

from decimal import Decimal

from app.db import db
from app.repo import items as items_repo
from tests.factories import login, make_item, make_owner, make_store


async def _csrf(client) -> str:
    from app.deps import SESSION_COOKIE
    from app.repo import users as users_repo
    from app.security import hash_token

    row = await users_repo.session_with_user(hash_token(client.cookies[SESSION_COOKIE]))
    return row["csrf_token"]


async def _a_store(client):
    owner_id = await make_owner("@ownerhandle")
    store_id = await make_store(owner_id, "Խանութ 1")
    await login(client, "@ownerhandle")
    return owner_id, store_id


def _item_form(csrf, **overrides):
    body = {
        "csrf_token": csrf,
        "name": "HQD Cuvie",
        "count": "10",
        "self_price": "1500",
        "sell_price": "3500",
        "wholesale_price": "",
    }
    body.update(overrides)
    return body


# -- adding ------------------------------------------------------------------

async def test_adding_an_item_puts_it_on_the_list(client):
    owner_id, store_id = await _a_store(client)

    response = await client.post(
        f"/stores/{store_id}/items", data=_item_form(await _csrf(client))
    )

    assert response.status_code == 200
    assert "HQD Cuvie" in response.text
    assert await db.fetchval("SELECT count FROM items WHERE name = 'HQD Cuvie'") == 10


async def test_a_deleted_item_can_be_added_again(client):
    """The reported bug. Deleting keeps the row, so the name stayed taken and
    the second add failed against something invisible."""
    owner_id, store_id = await _a_store(client)
    item_id = await make_item(owner_id, store_id, "HQD Cuvie", count=4)
    await client.post(f"/items/{item_id}/delete", data={"csrf_token": await _csrf(client)})

    response = await client.post(
        f"/stores/{store_id}/items", data=_item_form(await _csrf(client), count="25")
    )

    assert response.status_code == 200
    assert "HQD Cuvie" in response.text
    assert await db.fetchval(
        "SELECT count(*) FROM items WHERE lower(name) = 'hqd cuvie'"
    ) == 1, "revived, not duplicated"
    assert await db.fetchval("SELECT count FROM items WHERE id = $1", item_id) == 25


async def test_re_adding_keeps_the_history_it_already_had(client):
    """Reviving beats a second row: the product keeps the sales attached to it,
    and the shop never ends up with two names that mean the same thing."""
    owner_id, store_id = await _a_store(client)
    item_id = await make_item(owner_id, store_id, "HQD Cuvie", count=4)
    await client.post(f"/items/{item_id}/delete", data={"csrf_token": await _csrf(client)})

    await client.post(f"/stores/{store_id}/items", data=_item_form(await _csrf(client)))

    assert await db.fetchval(
        "SELECT id FROM items WHERE lower(name) = 'hqd cuvie'"
    ) == item_id
    assert await db.fetchval("SELECT is_active FROM items WHERE id = $1", item_id) is True


async def test_adding_a_name_that_is_already_on_the_list_says_so(client):
    """Rather than silently replacing somebody's prices."""
    owner_id, store_id = await _a_store(client)
    await make_item(owner_id, store_id, "HQD Cuvie", count=4, sell_price="3000.00")

    response = await client.post(
        f"/stores/{store_id}/items",
        data=_item_form(await _csrf(client), sell_price="9999"),
    )

    assert response.status_code == 422
    assert "արդեն կա" in response.text
    assert await db.fetchval(
        "SELECT sell_price FROM items WHERE lower(name) = 'hqd cuvie'"
    ) == Decimal("3000.00"), "the existing prices are untouched"


async def test_the_same_name_in_another_store_is_a_different_item(client):
    owner_id, store_id = await _a_store(client)
    other = await make_store(owner_id, "Խանութ 2")
    await make_item(owner_id, store_id, "HQD Cuvie", count=4)

    response = await client.post(
        f"/stores/{other}/items", data=_item_form(await _csrf(client))
    )

    assert response.status_code == 200
    assert await db.fetchval("SELECT count(*) FROM items WHERE lower(name) = 'hqd cuvie'") == 2


# -- editing the count -------------------------------------------------------

async def test_the_count_can_be_typed_straight_in(client):
    """The other reported bug: the count could only be nudged by a delta, so
    "I counted the shelf and there are 12" had no way in."""
    owner_id, store_id = await _a_store(client)
    item_id = await make_item(owner_id, store_id, "HQD Cuvie", count=4)

    response = await client.post(
        f"/items/{item_id}", data=_item_form(await _csrf(client), count="12")
    )

    assert response.status_code == 200
    assert await db.fetchval("SELECT count FROM items WHERE id = $1", item_id) == 12


async def test_the_count_may_be_set_to_zero(client):
    """Sold out is a number, not a missing value."""
    owner_id, store_id = await _a_store(client)
    item_id = await make_item(owner_id, store_id, "HQD Cuvie", count=40)

    await client.post(f"/items/{item_id}", data=_item_form(await _csrf(client), count="0"))

    assert await db.fetchval("SELECT count FROM items WHERE id = $1", item_id) == 0


async def test_a_negative_count_is_refused(client):
    owner_id, store_id = await _a_store(client)
    item_id = await make_item(owner_id, store_id, "HQD Cuvie", count=4)

    response = await client.post(
        f"/items/{item_id}", data=_item_form(await _csrf(client), count="-3")
    )

    assert response.status_code == 422
    assert await db.fetchval("SELECT count FROM items WHERE id = $1", item_id) == 4


async def test_name_prices_and_count_all_save_together(client):
    """One row, one Save button — the owner should not have to submit twice."""
    owner_id, store_id = await _a_store(client)
    item_id = await make_item(owner_id, store_id, "HQD Cuvie", count=4)

    await client.post(
        f"/items/{item_id}",
        data=_item_form(
            await _csrf(client), name="HQD Cuvie Plus", count="7",
            self_price="1800", sell_price="4000", wholesale_price="3000",
        ),
    )

    row = await db.fetchrow(
        "SELECT name, count, self_price, sell_price, wholesale_price FROM items WHERE id = $1",
        item_id,
    )
    assert row["name"] == "HQD Cuvie Plus"
    assert row["count"] == 7
    assert row["self_price"] == Decimal("1800.00")
    assert row["sell_price"] == Decimal("4000.00")
    assert row["wholesale_price"] == Decimal("3000.00")


async def test_clearing_the_wholesale_price_means_not_sold_wholesale(client):
    owner_id, store_id = await _a_store(client)
    item_id = await make_item(owner_id, store_id, "HQD Cuvie", count=4)
    await client.post(
        f"/items/{item_id}", data=_item_form(await _csrf(client), wholesale_price="2500")
    )

    await client.post(
        f"/items/{item_id}", data=_item_form(await _csrf(client), wholesale_price="")
    )

    assert await db.fetchval(
        "SELECT wholesale_price FROM items WHERE id = $1", item_id
    ) is None


async def test_the_generated_profit_follows_the_new_numbers(client):
    owner_id, store_id = await _a_store(client)
    item_id = await make_item(
        owner_id, store_id, "HQD Cuvie", count=4, self_price="1500.00", sell_price="3500.00"
    )

    await client.post(
        f"/items/{item_id}",
        data=_item_form(await _csrf(client), count="10", self_price="1000", sell_price="4000"),
    )

    assert await db.fetchval(
        "SELECT possible_profit FROM items WHERE id = $1", item_id
    ) == Decimal("30000.00"), "(4000 − 1000) × 10"


# -- the shape of the page ---------------------------------------------------

async def test_the_count_is_an_editable_field_not_a_plus_button(client):
    owner_id, store_id = await _a_store(client)
    await make_item(owner_id, store_id, "HQD Cuvie", count=4)

    page = await client.get(f"/stores/{store_id}")

    assert 'name="count"' in page.text
    assert "/restock" not in page.text, "the +1 control is gone"


async def test_nothing_is_hidden_off_the_right_of_the_table(client):
    """Every column an owner came for has to be reachable without scrolling
    sideways, so the layout is fixed and the inputs share the width."""
    owner_id, store_id = await _a_store(client)
    await make_item(owner_id, store_id, "HQD Cuvie", count=4)

    page = await client.get(f"/stores/{store_id}")

    for column in ("Անուն", "Քանակ", "Ինքնարժեք", "Մանրածախ", "Մեծածախ", "Հնարավոր շահույթ"):
        assert column in page.text


# -- tenancy -----------------------------------------------------------------

async def test_another_owners_item_cannot_be_edited(client):
    owner_id, store_id = await _a_store(client)
    item_id = await make_item(owner_id, store_id, "HQD Cuvie", count=4)
    await make_owner("@ownerother")
    await login(client, "@ownerother")

    response = await client.post(
        f"/items/{item_id}", data=_item_form(await _csrf(client), count="999")
    )

    assert response.status_code == 404
    assert await db.fetchval("SELECT count FROM items WHERE id = $1", item_id) == 4


async def test_reviving_only_ever_touches_your_own_store(client):
    """The revive is keyed on (store, name), so another owner adding the same
    product name must create their own row."""
    owner_id, store_id = await _a_store(client)
    item_id = await make_item(owner_id, store_id, "HQD Cuvie", count=4)
    await client.post(f"/items/{item_id}/delete", data={"csrf_token": await _csrf(client)})

    other_owner = await make_owner("@ownerother")
    other_store = await make_store(other_owner, "Ուրիշի խանութ")
    await login(client, "@ownerother")
    await client.post(f"/stores/{other_store}/items", data=_item_form(await _csrf(client)))

    assert await db.fetchval("SELECT is_active FROM items WHERE id = $1", item_id) is False
    assert await db.fetchval("SELECT count(*) FROM items WHERE lower(name) = 'hqd cuvie'") == 2


async def test_the_repo_reports_a_live_collision_rather_than_overwriting(client):
    owner_id, store_id = await _a_store(client)
    await make_item(owner_id, store_id, "HQD Cuvie", count=4)

    result = await items_repo.create(
        owner_id=owner_id, store_id=store_id, name="  hqd cuvie  ",
        count=9, self_price=Decimal("1"), sell_price=Decimal("2"),
    )

    assert result is None, "matched ignoring case and surrounding space, and refused"
