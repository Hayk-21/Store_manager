"""One owner must never reach another owner's data.

Two layers are tested separately on purpose:

* the HTTP layer, where a missing WHERE would leak; and
* the schema itself, where the composite foreign keys make the leak impossible
  even if the application forgets. The second is the one that still holds after
  somebody writes a new query in a hurry.
"""

from __future__ import annotations

import asyncpg
import pytest

from app.db import db
from tests.factories import login, make_item, make_owner, make_store


async def test_another_owners_store_reads_as_missing_not_forbidden(client):
    """404 rather than 403: a 403 would confirm the id exists."""
    await make_owner("a@example.com")
    other_id = await make_owner("b@example.com")
    other_store = await make_store(other_id, "Ուրիշի խանութ")
    await login(client, "a@example.com")

    response = await client.get(f"/stores/{other_store}")

    assert response.status_code == 404
    assert "Ուրիշի խանութ" not in response.text


async def test_cannot_add_an_item_to_another_owners_store(client):
    owner_id = await make_owner("a@example.com")
    other_id = await make_owner("b@example.com")
    other_store = await make_store(other_id)
    await login(client, "a@example.com")
    token = await db.fetchval(
        "SELECT csrf_token FROM auth_sessions WHERE user_id = $1", owner_id
    )

    response = await client.post(
        f"/stores/{other_store}/items",
        data={"name": "smuggled", "count": "1", "self_price": "1", "sell_price": "2",
              "csrf_token": token},
    )

    assert response.status_code == 404
    assert await db.fetchval("SELECT count(*) FROM items") == 0


async def test_cannot_edit_another_owners_item(client):
    await make_owner("a@example.com")
    other_id = await make_owner("b@example.com")
    other_store = await make_store(other_id)
    other_item = await make_item(other_id, other_store, "Ուրիշի ապրանք")
    await login(client, "a@example.com")
    token = await db.fetchval("SELECT csrf_token FROM auth_sessions LIMIT 1")

    response = await client.post(
        f"/items/{other_item}",
        data={"name": "hijacked", "self_price": "1", "sell_price": "2", "csrf_token": token},
    )

    assert response.status_code == 404
    name = await db.fetchval("SELECT name FROM items WHERE id = $1", other_item)
    assert name == "Ուրիշի ապրանք"


async def test_the_footer_only_ever_shows_your_own_stores(client):
    await make_owner("a@example.com")
    other_id = await make_owner("b@example.com")
    await make_store(other_id, "Ուրիշի խանութ")
    await login(client, "a@example.com")

    response = await client.get("/partials/footer")

    assert "Ուրիշի խանութ" not in response.text


async def test_the_schema_refuses_a_cross_owner_item(client):
    """No application code involved: the composite FK is the guarantee."""
    owner_a = await make_owner()
    owner_b = await make_owner()
    store_b = await make_store(owner_b)

    with pytest.raises(asyncpg.exceptions.ForeignKeyViolationError):
        await db.execute(
            "INSERT INTO items (owner_id, store_id, name) VALUES ($1, $2, 'smuggled')",
            owner_a,
            store_b,
        )


async def test_the_schema_refuses_a_cross_owner_sale_line(client):
    owner_a = await make_owner()
    owner_b = await make_owner()
    store_a = await make_store(owner_a)
    store_b = await make_store(owner_b)
    item_b = await make_item(owner_b, store_b)

    worker_a = await db.fetchval(
        """
        INSERT INTO workers (owner_id, name, telegram_id) VALUES ($1, 'A', 1) RETURNING id
        """,
        owner_a,
    )
    store_session = await db.fetchval(
        """
        INSERT INTO store_sessions (owner_id, store_id, opened_by_worker_id, opened_day)
        VALUES ($1, $2, $3, CURRENT_DATE) RETURNING id
        """,
        owner_a, store_a, worker_a,
    )
    work_session = await db.fetchval(
        """
        INSERT INTO work_sessions (owner_id, worker_id, store_id, store_session_id)
        VALUES ($1, $2, $3, $4) RETURNING id
        """,
        owner_a, worker_a, store_a, store_session,
    )
    sale = await db.fetchval(
        """
        INSERT INTO sales (owner_id, store_id, worker_id, work_session_id, store_session_id,
                           payment_method, total, external_id)
        VALUES ($1, $2, $3, $4, $5, 'cash', 100.00, 'idem-key-abcd') RETURNING id
        """,
        owner_a, store_a, worker_a, work_session, store_session,
    )

    with pytest.raises(asyncpg.exceptions.ForeignKeyViolationError):
        await db.execute(
            """
            INSERT INTO sale_items (owner_id, sale_id, item_id, quantity, unit_price, line_total)
            VALUES ($1, $2, $3, 1, 100.00, 100.00)
            """,
            owner_a, sale, item_b,
        )
