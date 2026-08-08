"""Several people working the same shop at the same time.

The store session is the shop being open; a work session is one person's stint
inside it. Two cashiers on together means one store session and two work
sessions, and the second one arriving must not disturb the first.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.db import db
from app.errors import BotError
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


async def _worker(owner_id: int, name: str):
    worker_id, telegram_id = await make_worker(owner_id, name, salary_amount="5000.00")
    return shifts_service.Worker(
        id=worker_id, owner_id=owner_id, name=name, salary_amount=Decimal("5000.00")
    )


async def _a_shop():
    owner_id = await make_owner("@ownerhandle")
    store_id = await make_store(owner_id, "Խանութ 1", lat=YEREVAN_LAT, lng=YEREVAN_LNG)
    item_id = await make_item(owner_id, store_id, "HQD Cuvie", count=100, sell_price="3500.00")
    return owner_id, store_id, item_id


async def test_a_second_worker_joins_rather_than_replacing(client):
    """The reported bug: the second person arriving ended the first one's shift."""
    owner_id, store_id, _ = await _a_shop()
    ani = await _worker(owner_id, "Անի")
    gor = await _worker(owner_id, "Գոռ")

    await shifts_service.open_store(ani, YEREVAN_LAT, YEREVAN_LNG, 20, "idem-open-ani", 900)
    await shifts_service.open_store(gor, YEREVAN_LAT, YEREVAN_LNG, 20, "idem-open-gor", 900)
    await worked_a_full_shift()

    assert await db.fetchval("SELECT count(*) FROM store_sessions") == 1, "one shop, one session"
    assert await db.fetchval(
        "SELECT count(*) FROM work_sessions WHERE ended_at IS NULL"
    ) == 2, "both are still on shift"


async def test_they_share_one_session(client):
    owner_id, store_id, _ = await _a_shop()
    ani = await _worker(owner_id, "Անի")
    gor = await _worker(owner_id, "Գոռ")

    first = await shifts_service.open_store(
        ani, YEREVAN_LAT, YEREVAN_LNG, 20, "idem-open-ani", 900
    )
    second = await shifts_service.open_store(
        gor, YEREVAN_LAT, YEREVAN_LNG, 20, "idem-open-gor", 900
    )

    assert first["session"]["store_session_id"] == second["session"]["store_session_id"]
    assert first["session"]["id"] != second["session"]["id"], "separate shifts"


async def test_the_store_page_lists_everyone_on_shift(client):
    owner_id, store_id, _ = await _a_shop()
    ani = await _worker(owner_id, "Անի")
    gor = await _worker(owner_id, "Գոռ")
    await shifts_service.open_store(ani, YEREVAN_LAT, YEREVAN_LNG, 20, "idem-open-ani", 900)
    await shifts_service.open_store(gor, YEREVAN_LAT, YEREVAN_LNG, 20, "idem-open-gor", 900)
    await worked_a_full_shift()
    await login(client, "@ownerhandle")

    page = await client.get(f"/stores/{store_id}")

    assert "Անի" in page.text
    assert "Գոռ" in page.text


async def test_one_leaving_does_not_close_the_shop_on_the_other(client):
    owner_id, store_id, item_id = await _a_shop()
    ani = await _worker(owner_id, "Անի")
    gor = await _worker(owner_id, "Գոռ")
    await shifts_service.open_store(ani, YEREVAN_LAT, YEREVAN_LNG, 20, "idem-open-ani", 900)
    await shifts_service.open_store(gor, YEREVAN_LAT, YEREVAN_LNG, 20, "idem-open-gor", 900)
    await worked_a_full_shift()

    result = await shifts_service.close_out_shift(
        ani,
        [{"item_id": item_id, "quantity": 1, "unit_price": "3500.00",
          "payment_method": "cash"}],
        "idem-close-ani",
    )

    assert result["summary"]["store_closed"] is False
    assert await db.fetchval("SELECT closed_at FROM store_sessions") is None
    assert await db.fetchval(
        "SELECT ended_at FROM work_sessions WHERE worker_id = $1", gor.id
    ) is None, "the one still working is untouched"


async def test_the_last_one_out_closes_the_shop(client):
    owner_id, store_id, item_id = await _a_shop()
    ani = await _worker(owner_id, "Անի")
    gor = await _worker(owner_id, "Գոռ")
    await shifts_service.open_store(ani, YEREVAN_LAT, YEREVAN_LNG, 20, "idem-open-ani", 900)
    await shifts_service.open_store(gor, YEREVAN_LAT, YEREVAN_LNG, 20, "idem-open-gor", 900)
    await worked_a_full_shift()
    await shifts_service.close_out_shift(ani, [], "idem-close-ani")

    result = await shifts_service.close_out_shift(gor, [], "idem-close-gor")

    assert result["summary"]["store_closed"] is True
    assert await db.fetchval("SELECT closed_at FROM store_sessions") is not None


async def test_each_ones_sales_stay_their_own(client):
    """Two people on one till: the shop's total is shared, the attribution is not."""
    owner_id, store_id, item_id = await _a_shop()
    ani = await _worker(owner_id, "Անի")
    gor = await _worker(owner_id, "Գոռ")
    await shifts_service.open_store(ani, YEREVAN_LAT, YEREVAN_LNG, 20, "idem-open-ani", 900)
    await shifts_service.open_store(gor, YEREVAN_LAT, YEREVAN_LNG, 20, "idem-open-gor", 900)
    await worked_a_full_shift()

    await shifts_service.close_out_shift(
        ani,
        [{"item_id": item_id, "quantity": 2, "unit_price": "3500.00",
          "payment_method": "cash"}],
        "idem-close-ani",
    )
    await shifts_service.close_out_shift(
        gor,
        [{"item_id": item_id, "quantity": 1, "unit_price": "3500.00",
          "payment_method": "cash"}],
        "idem-close-gor",
    )

    by_worker = dict(
        await db.fetch(
            "SELECT worker_id, sum(total) FROM sales GROUP BY worker_id"
        )
    )
    assert by_worker[ani.id] == Decimal("7000.00")
    assert by_worker[gor.id] == Decimal("3500.00")


async def test_one_worker_cannot_close_the_shop_on_a_colleague(client):
    """The reported bug, and the expensive half of it.

    Closing the store force-ends every shift open in it. For the person pressing
    the button that is what they asked for; for a colleague still serving
    customers it ends their shift without ever asking what they sold — and the
    close-out *is* the sales record, so that day's takings are simply never
    written down.
    """
    owner_id, store_id, item_id = await _a_shop()
    ani = await _worker(owner_id, "Անի")
    gor = await _worker(owner_id, "Գոռ")
    await shifts_service.open_store(ani, YEREVAN_LAT, YEREVAN_LNG, 20, "idem-open-ani", 900)
    await shifts_service.open_store(gor, YEREVAN_LAT, YEREVAN_LNG, 20, "idem-open-gor", 900)
    await worked_a_full_shift()

    with pytest.raises(BotError) as caught:
        await shifts_service.close_out_shift(
            ani, [], "idem-close-ani", close_store_too=True
        )

    assert caught.value.code == "others_on_shift"
    assert "Գոռ" in caught.value.message, "it says who is still working"
    assert await db.fetchval(
        "SELECT count(*) FROM work_sessions WHERE ended_at IS NULL"
    ) == 2, "nothing was closed"


async def test_the_bots_close_store_button_is_refused_too(client):
    owner_id, store_id, _ = await _a_shop()
    ani = await _worker(owner_id, "Անի")
    gor = await _worker(owner_id, "Գոռ")
    await shifts_service.open_store(ani, YEREVAN_LAT, YEREVAN_LNG, 20, "idem-open-ani", 900)
    await shifts_service.open_store(gor, YEREVAN_LAT, YEREVAN_LNG, 20, "idem-open-gor", 900)
    await worked_a_full_shift()

    with pytest.raises(BotError) as caught:
        await shifts_service.close_store(ani, "idem-close-ani")

    assert caught.value.code == "others_on_shift"
    assert await db.fetchval("SELECT closed_at FROM store_sessions") is None


async def test_the_last_one_out_may_still_close_it_deliberately(client):
    """Refusing must not block the normal case: nobody else is left."""
    owner_id, store_id, _ = await _a_shop()
    ani = await _worker(owner_id, "Անի")
    await shifts_service.open_store(ani, YEREVAN_LAT, YEREVAN_LNG, 20, "idem-open-ani", 900)

    result = await shifts_service.close_out_shift(
        ani, [], "idem-close-ani", close_store_too=True
    )

    assert result["summary"]["store_closed"] is True


async def test_the_owner_can_still_force_it_closed(client):
    """The escape hatch for a shift somebody forgot to end. It belongs to the
    owner, who can see the whole shop, not to a colleague who cannot."""
    owner_id, store_id, _ = await _a_shop()
    ani = await _worker(owner_id, "Անի")
    gor = await _worker(owner_id, "Գոռ")
    await shifts_service.open_store(ani, YEREVAN_LAT, YEREVAN_LNG, 20, "idem-open-ani", 900)
    await shifts_service.open_store(gor, YEREVAN_LAT, YEREVAN_LNG, 20, "idem-open-gor", 900)
    await worked_a_full_shift()
    session_id = await db.fetchval("SELECT id FROM store_sessions")

    await shifts_service.close_store_session_as_owner(owner_id, session_id)

    assert await db.fetchval("SELECT closed_at FROM store_sessions") is not None
    assert await db.fetchval(
        "SELECT count(*) FROM work_sessions WHERE ended_at IS NULL"
    ) == 0


async def test_both_salaries_come_out_when_the_shop_closes(client):
    owner_id, store_id, item_id = await _a_shop()
    ani = await _worker(owner_id, "Անի")
    gor = await _worker(owner_id, "Գոռ")
    await shifts_service.open_store(ani, YEREVAN_LAT, YEREVAN_LNG, 20, "idem-open-ani", 900)
    await shifts_service.open_store(gor, YEREVAN_LAT, YEREVAN_LNG, 20, "idem-open-gor", 900)
    await worked_a_full_shift()

    # Each writes their own day up; the second one out closes the shop.
    await shifts_service.close_out_shift(
        ani,
        [{"item_id": item_id, "quantity": 4, "unit_price": "3500.00",
          "payment_method": "cash"}],
        "idem-close-ani",
    )
    await shifts_service.close_out_shift(gor, [], "idem-close-gor")

    assert await db.fetchval(
        "SELECT count(*) FROM cash_movements WHERE kind = 'salary'"
    ) == 2, "each shift pays its own wage"
    # 14000 taken in, two 5000 wages out.
    assert await db.fetchval("SELECT cash_at_close FROM store_sessions") == Decimal("4000.00")
    assert await db.fetchval(
        "SELECT count(*) FROM work_sessions WHERE ended_at IS NULL"
    ) == 0
