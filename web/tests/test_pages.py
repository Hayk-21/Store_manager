"""Stores, stock and the fixed footer — requirements 2 and 4."""

from __future__ import annotations

import inspect
import re
from decimal import Decimal
from pathlib import Path

from app.db import db
from app.routes import pages as pages_module
from tests.factories import login, make_item, make_owner, make_store

TEMPLATES = Path(__file__).resolve().parents[1] / "app" / "templates"


def test_the_first_render_of_the_live_status_gets_the_whole_context():
    """The status block is rendered twice: inline on page load, and again by its own
    poll ten seconds later. The poll passes the context keys as top-level names, so
    the inline render has to hand them over by hand — and a key added to
    ``_status_context`` but forgotten in that ``{% with %}`` renders as nothing on
    load and then appears when the poll lands.

    That is a bug that looks like a slow network, which is why it gets a test rather
    than a comment: I shipped it once already with ``drawer``.
    """
    provided = set(
        re.findall(r'"(\w+)":', inspect.getsource(pages_module._status_context))
    )
    handed_over = set(
        re.findall(
            r"(\w+) = status\.",
            (TEMPLATES / "store_detail.html").read_text(encoding="utf-8"),
        )
    )

    assert provided, "could not read the status context"
    missing = provided - handed_over
    assert not missing, (
        f"store_detail.html does not pass {sorted(missing)} to _store_status.html, "
        "so it renders empty until the first poll"
    )


async def _signed_in(client) -> int:
    owner_id = await make_owner("@ownerhandle")
    await login(client, "@ownerhandle")
    return owner_id


async def _token(owner_id: int) -> str:
    return await db.fetchval(
        "SELECT csrf_token FROM auth_sessions WHERE user_id = $1", owner_id
    )


async def test_store_list_renders(client):
    owner_id = await _signed_in(client)
    await make_store(owner_id, "Խանութ Հյուսիսային")

    response = await client.get("/stores")

    assert response.status_code == 200
    assert "Խանութ Հյուսիսային" in response.text


async def test_creating_a_store_redirects_to_it(client):
    owner_id = await _signed_in(client)

    response = await client.post(
        "/stores",
        data={
            "name": "Նոր խանութ",
            "address": "Հյուսիսային պող. 1",
            "lat": "40.177200",
            "lng": "44.503200",
            "radius_m": "150",
            "csrf_token": await _token(owner_id),
        },
    )

    assert response.status_code == 303
    store = await db.fetchrow("SELECT * FROM stores WHERE owner_id = $1", owner_id)
    assert store["name"] == "Նոր խանութ"
    assert store["radius_m"] == 150
    assert response.headers["location"] == f"/stores/{store['id']}"


async def test_half_a_coordinate_is_refused(client):
    owner_id = await _signed_in(client)

    response = await client.post(
        "/stores",
        data={"name": "Կիսատ", "lat": "40.1772", "lng": "", "csrf_token": await _token(owner_id)},
    )

    assert response.status_code == 422
    assert await db.fetchval("SELECT count(*) FROM stores") == 0


async def test_the_five_columns_are_on_the_store_page(client):
    owner_id = await _signed_in(client)
    store_id = await make_store(owner_id)
    await make_item(owner_id, store_id, "HQD Cuvie", count=10,
                    self_price="1500.00", sell_price="3500.00")

    response = await client.get(f"/stores/{store_id}")

    for header in (
        "Անուն", "Քանակ", "Ինքնարժեք", "Մեծածախ", "Մանրածախ", "Հնարավոր շահույթ",
    ):
        assert header in response.text, f"missing column {header}"
    # possible_profit = (3500 - 1500) * 10
    assert "20,000.00" in response.text


async def test_possible_profit_follows_the_columns_behind_it(client):
    """It is a generated column, so it cannot drift — check that after each edit."""
    owner_id = await _signed_in(client)
    store_id = await make_store(owner_id)
    item_id = await make_item(owner_id, store_id, count=10,
                              self_price="1500.00", sell_price="3500.00")

    async def profit() -> Decimal:
        return await db.fetchval("SELECT possible_profit FROM items WHERE id = $1", item_id)

    assert await profit() == Decimal("20000.00")

    await client.post(
        f"/items/{item_id}",
        data={"name": "HQD", "count": "10", "self_price": "1000.00",
              "sell_price": "3500.00", "csrf_token": await _token(owner_id)},
    )
    assert await profit() == Decimal("25000.00")

    await client.post(
        f"/items/{item_id}",
        data={"name": "HQD", "count": "15", "self_price": "1000.00",
              "sell_price": "3500.00", "csrf_token": await _token(owner_id)},
    )
    assert await profit() == Decimal("37500.00")


async def test_the_count_is_written_as_the_owner_typed_it(client):
    """It is what they counted off the shelf, so it is the last word.

    This replaces a rule that refused to write the count at all, on the grounds
    that a sale might land between the page rendering and the form being sent.
    That protected a race nobody hit and removed the only way to correct a wrong
    figure, which people hit constantly.
    """
    owner_id = await _signed_in(client)
    store_id = await make_store(owner_id)
    item_id = await make_item(owner_id, store_id, count=10)

    await client.post(
        f"/items/{item_id}",
        data={
            "name": "renamed",
            "count": "999",
            "self_price": "1500.00",
            "sell_price": "3500.00",
            "csrf_token": await _token(owner_id),
        },
    )

    assert await db.fetchval("SELECT count FROM items WHERE id = $1", item_id) == 999


async def test_the_count_cannot_be_set_negative(client):
    owner_id = await _signed_in(client)
    store_id = await make_store(owner_id)
    item_id = await make_item(owner_id, store_id, count=3)

    response = await client.post(
        f"/items/{item_id}",
        data={"name": "x", "count": "-5", "self_price": "1500.00",
              "sell_price": "3500.00", "csrf_token": await _token(owner_id)},
    )

    assert response.status_code == 422
    assert await db.fetchval("SELECT count FROM items WHERE id = $1", item_id) == 3


async def test_item_sorting_only_accepts_known_columns(client):
    owner_id = await _signed_in(client)
    store_id = await make_store(owner_id)
    await make_item(owner_id, store_id, "Բ երկրորդ")
    await make_item(owner_id, store_id, "Ա առաջին")

    injected = await client.get(
        f"/partials/stores/{store_id}/items?sort=name;%20DROP%20TABLE%20items"
    )

    assert injected.status_code == 200
    # The table is still there, and the fallback ordering was applied.
    assert await db.fetchval("SELECT count(*) FROM items") == 2
    assert injected.text.index("Ա առաջին") < injected.text.index("Բ երկրորդ")


async def test_footer_lists_every_store_and_shows_closed_ones(client):
    owner_id = await _signed_in(client)
    await make_store(owner_id, "Առաջին")
    await make_store(owner_id, "Երկրորդ")

    response = await client.get("/partials/footer")

    assert response.status_code == 200
    assert "Առաջին" in response.text and "Երկրորդ" in response.text
    # No store session is open, so neither shows a money figure.
    assert response.text.count("փակ") == 2


async def test_footer_is_wired_into_every_signed_in_page(client):
    owner_id = await _signed_in(client)
    await make_store(owner_id)

    response = await client.get("/stores")

    assert 'id="vs-footer"' in response.text
    assert 'hx-get="/partials/footer"' in response.text


async def test_deleting_a_store_is_a_soft_delete(client):
    owner_id = await _signed_in(client)
    store_id = await make_store(owner_id)

    response = await client.post(
        f"/stores/{store_id}/delete", data={"csrf_token": await _token(owner_id)}
    )

    assert response.status_code == 303
    row = await db.fetchrow("SELECT is_active FROM stores WHERE id = $1", store_id)
    assert row is not None, "the row must survive: sales reference it forever"
    assert row["is_active"] is False


async def test_a_post_without_a_csrf_token_is_refused(client):
    owner_id = await _signed_in(client)

    response = await client.post("/stores", data={"name": "Առանց token"})

    assert response.status_code == 403
    assert await db.fetchval("SELECT count(*) FROM stores WHERE owner_id = $1", owner_id) == 0


async def test_anonymous_visitors_are_sent_to_login(client):
    response = await client.get("/stores")

    assert response.status_code == 303
    assert response.headers["location"] == "/login"
