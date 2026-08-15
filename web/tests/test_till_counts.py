"""The money that stays in the shop, and the money that goes to the owner.

Each store keeps a float in its drawer. At the end of a shift the worker says how much
they are leaving; that becomes the store's balance, and everything else in the till
goes to the owner:

    handed over  =  what the till held  −  what was left behind

Three things have to happen together for that to hold — the count is recorded, the
balance is updated, the handover is booked — so most of these tests are about the
three agreeing rather than about any one of them.

Nobody is asked at the start of a shift. That asked a worker to answer for a drawer
somebody else had filled.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.config import settings
from app.db import db
from app.errors import AppError, BotError
from app.repo import money as money_repo
from app.repo import till as till_repo
from app.services import corrections as corrections_service
from app.services import sales as sales_service
from app.services import shifts as shifts_service
from app.services import till as till_service
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

BASE = "/api/bot/v1"


async def _a_shop(balance: str = "0.00"):
    owner_id = await make_owner("@ownerhandle")
    store_id = await make_store(owner_id, "Խանութ 1", lat=YEREVAN_LAT, lng=YEREVAN_LNG)
    if Decimal(balance) > 0:
        await db.execute(
            "UPDATE stores SET till_balance = $2 WHERE id = $1", store_id, Decimal(balance)
        )
    item_id = await make_item(
        owner_id, store_id, "HQD Cuvie", count=50,
        self_price="1500.00", sell_price="3500.00",
    )
    return owner_id, store_id, item_id


async def _worker(owner_id: int, name: str = "Անի"):
    worker_id, telegram_id = await make_worker(owner_id, name, salary_amount="0.00")
    return shifts_service.Worker(
        id=worker_id, owner_id=owner_id, name=name, salary_amount=Decimal("0.00")
    ), telegram_id


async def _open(worker, key: str):
    await shifts_service.open_store(worker, YEREVAN_LAT, YEREVAN_LNG, 20, key, 900)


async def _lock_up(worker, key: str = "idem-lockup-1"):
    """End the shift, which shuts the shop when nobody else is on.

    Every count goes through this, because the drawer is only countable once the shop
    is shut. That ordering is the point: the count hands the owner everything above the
    float, so the wage has to come out of the till first — a worker who counted up
    mid-shift handed over 82,000 and was then paid out of a drawer that no longer had
    it.
    """
    await worked_a_full_shift(worker.id)
    await shifts_service.close_out_shift(worker, [], key)


async def _till(session_id: int) -> Decimal:
    return Decimal((await money_repo.totals_for_session(session_id))["cash"])


async def _session() -> int:
    """The newest session, by id and not by time: a test runs in one transaction, so
    ``now()`` is frozen and two sessions share an ``opened_at``."""
    return await db.fetchval("SELECT id FROM store_sessions ORDER BY id DESC LIMIT 1")


async def _opened_on(session_id: int):
    """The trading day a session belongs to, the way the application reckons it —
    a local date, not a UTC cast, or an evening either side of midnight lands on
    the wrong day and the expense silently misses the report it was written for."""
    return settings.local_day(
        await db.fetchval("SELECT opened_at FROM store_sessions WHERE id = $1", session_id)
    )


async def _balance(store_id: int) -> Decimal:
    return await db.fetchval("SELECT till_balance FROM stores WHERE id = $1", store_id)


# -- the store's own float ----------------------------------------------------

async def test_a_new_shop_starts_with_nothing_in_its_drawer(client):
    _, store_id, _ = await _a_shop()

    assert await _balance(store_id) == Decimal("0.00")


async def test_a_session_opens_with_the_shops_float(client):
    """The cash was in the drawer overnight and still is in the morning."""
    owner_id, store_id, _ = await _a_shop(balance="40000.00")
    worker, _ = await _worker(owner_id)

    await _open(worker, "idem-open-1")

    assert await _till(await _session()) == Decimal("40000.00")


async def test_the_float_arrives_as_an_ordinary_deposit(client):
    """So "how much is in the till" stays a sum over one table."""
    owner_id, _, _ = await _a_shop(balance="40000.00")
    worker, _ = await _worker(owner_id)

    await _open(worker, "idem-open-1")

    row = await db.fetchrow("SELECT kind, method, amount, note FROM cash_movements")
    assert row["kind"] == "deposit"
    assert row["amount"] == Decimal("40000.00")
    assert row["note"] == till_service.NOTE_CARRIED_OVER


async def test_an_empty_shop_opens_with_no_movement_at_all(client):
    owner_id, _, _ = await _a_shop()
    worker, _ = await _worker(owner_id)

    await _open(worker, "idem-open-1")

    assert await db.fetchval("SELECT count(*) FROM cash_movements") == 0


async def test_joining_a_running_session_does_not_add_the_float_again(client):
    """The float belongs to the session, not to whoever walks in."""
    owner_id, _, _ = await _a_shop(balance="40000.00")
    first, _ = await _worker(owner_id, "Անի")
    second, _ = await _worker(owner_id, "Գոռ")

    await _open(first, "idem-open-1")
    await _open(second, "idem-open-2")

    assert await _till(await _session()) == Decimal("40000.00")


# -- the handover -------------------------------------------------------------

async def test_what_is_left_becomes_the_shops_balance(client):
    owner_id, store_id, item_id = await _a_shop(balance="40000.00")
    worker, _ = await _worker(owner_id)
    await _open(worker, "idem-open-1")
    await sales_service.record_sale(
        worker, [{"item_id": item_id, "quantity": 2}], "cash", "idem-sale-1"
    )

    await _lock_up(worker)
    await till_service.declare_close(worker, Decimal("30000"), "idem-till-1")

    assert await _balance(store_id) == Decimal("30000.00")


async def test_the_rest_goes_to_the_owner(client):
    """40,000 float plus 7,000 taken, less 30,000 left, is 17,000 handed over."""
    owner_id, _, item_id = await _a_shop(balance="40000.00")
    worker, _ = await _worker(owner_id)
    await _open(worker, "idem-open-1")
    await sales_service.record_sale(
        worker, [{"item_id": item_id, "quantity": 2}], "cash", "idem-sale-1"
    )

    await _lock_up(worker)
    result = await till_service.declare_close(worker, Decimal("30000"), "idem-till-1")

    assert result["count"]["expected"] == "47000.00", "what the till held"
    assert result["count"]["counted"] == "30000.00", "what stays in the shop"
    assert result["count"]["handed_over"] == "17000.00"


async def test_the_owners_share_is_reported_and_never_booked(client):
    """Shown, not extracted, which is how the owner wants it: the ledger is the shop's
    record of what it took and spent, and handing the day's money over is not another
    expense in it. So the till still says what the shop earned.
    """
    owner_id, _, item_id = await _a_shop(balance="40000.00")
    worker, _ = await _worker(owner_id)
    await _open(worker, "idem-open-1")
    await sales_service.record_sale(
        worker, [{"item_id": item_id, "quantity": 2}], "cash", "idem-sale-1"
    )
    session_id = await _session()

    await _lock_up(worker)
    result = await till_service.declare_close(worker, Decimal("30000"), "idem-till-1")

    assert result["count"]["handed_over"] == "17000.00"
    assert await db.fetchval(
        "SELECT count(*) FROM cash_movements WHERE kind IN ('withdrawal', 'adjustment')"
    ) == 0, "no row for the handover, and none to correct one"
    assert await _till(session_id) == Decimal("47000.00"), "the day's cash, untouched"


async def test_the_count_leaves_the_closing_snapshot_alone(client):
    """It used to re-snapshot the closed session, because the handover changed the
    ledger under it. Nothing changes the ledger now."""
    owner_id, _, item_id = await _a_shop(balance="40000.00")
    worker, _ = await _worker(owner_id)
    await _open(worker, "idem-open-1")
    await sales_service.record_sale(
        worker, [{"item_id": item_id, "quantity": 2}], "cash", "idem-sale-1"
    )
    session_id = await _session()
    await _lock_up(worker)

    await till_service.declare_close(worker, Decimal("30000"), "idem-till-1")

    assert await db.fetchval(
        "SELECT cash_at_close FROM store_sessions WHERE id = $1", session_id
    ) == Decimal("47000.00")


async def test_the_days_takings_are_untouched_by_the_handover(client):
    """Handing cash to the owner is not un-selling anything."""
    owner_id, store_id, item_id = await _a_shop(balance="40000.00")
    worker, _ = await _worker(owner_id)
    await _open(worker, "idem-open-1")
    await sales_service.record_sale(
        worker, [{"item_id": item_id, "quantity": 2}], "cash", "idem-sale-1"
    )

    await _lock_up(worker)
    await till_service.declare_close(worker, Decimal("30000"), "idem-till-1")

    day = await money_repo.day_totals_for_store(owner_id, store_id)
    assert day["day_cash"] == Decimal("7000.00")


async def test_leaving_everything_hands_over_nothing(client):
    owner_id, _, _ = await _a_shop(balance="40000.00")
    worker, _ = await _worker(owner_id)
    await _open(worker, "idem-open-1")

    await _lock_up(worker)
    result = await till_service.declare_close(worker, Decimal("40000"), "idem-till-1")

    assert result["count"]["handed_over"] == "0.00"
    assert await db.fetchval(
        "SELECT count(*) FROM cash_movements WHERE kind = 'withdrawal'"
    ) == 0


async def test_leaving_more_than_the_till_held_owes_the_owner_nothing(client):
    """Usually an unrecorded sale or a miscount. The owner gets nothing from this shop
    today; they do not owe it money. «-5,000 ֏» beside "hand to the owner" is not a
    figure anybody can act on, which is the whole reason it is floored at zero."""
    owner_id, store_id, _ = await _a_shop(balance="40000.00")
    worker, _ = await _worker(owner_id)
    await _open(worker, "idem-open-1")
    await _lock_up(worker)

    result = await till_service.declare_close(worker, Decimal("45000"), "idem-till-1")

    assert result["count"]["handed_over"] == "0.00"
    assert await _balance(store_id) == Decimal("45000.00")


async def test_the_gap_is_still_visible_when_more_was_left_than_expected(client):
    """Flooring the owner's share must not swallow the discrepancy. Both readings stay
    on the row, so the report can show that the drawer held 5,000 more than the books
    said."""
    owner_id, _, _ = await _a_shop(balance="40000.00")
    worker, _ = await _worker(owner_id)
    await _open(worker, "idem-open-1")
    await _lock_up(worker)

    result = await till_service.declare_close(worker, Decimal("45000"), "idem-till-1")

    assert result["count"]["expected"] == "40000.00", "what the books said"
    assert result["count"]["counted"] == "45000.00", "what was found"


async def test_tomorrow_opens_with_what_was_left(client):
    """The whole cycle, which is the thing worth holding in place."""
    owner_id, store_id, item_id = await _a_shop(balance="40000.00")
    worker, _ = await _worker(owner_id)
    await _open(worker, "idem-open-1")
    await sales_service.record_sale(
        worker, [{"item_id": item_id, "quantity": 2}], "cash", "idem-sale-1"
    )
    await _lock_up(worker)
    await till_service.declare_close(worker, Decimal("30000"), "idem-till-1")

    await _open(worker, "idem-open-2")

    assert await _till(await _session()) == Decimal("30000.00")


async def test_a_later_count_replaces_an_earlier_one(client):
    """Which is what makes counting early safe rather than something to undo."""
    owner_id, store_id, _ = await _a_shop(balance="40000.00")
    worker, _ = await _worker(owner_id)
    await _open(worker, "idem-open-1")

    await _lock_up(worker)
    await till_service.declare_close(worker, Decimal("30000"), "idem-till-1")
    await till_service.declare_close(worker, Decimal("25000"), "idem-till-2")

    assert await _balance(store_id) == Decimal("25000.00")


# -- refusals and retries -----------------------------------------------------

async def test_a_negative_count_is_refused(client):
    owner_id, _, _ = await _a_shop()
    worker, _ = await _worker(owner_id)
    await _open(worker, "idem-open-1")

    with pytest.raises(BotError):
        await _lock_up(worker)
        await till_service.declare_close(worker, Decimal("-1"), "idem-till-1")


async def test_an_absurd_count_is_refused(client):
    owner_id, _, _ = await _a_shop()
    worker, _ = await _worker(owner_id)
    await _open(worker, "idem-open-1")

    with pytest.raises(BotError):
        await _lock_up(worker)
        await till_service.declare_close(
            worker, Decimal("99999999999"), "idem-till-1"
        )


async def test_it_can_be_counted_after_the_shift_has_ended(client):
    """Which is when it is asked for. Requiring an open shift would refuse the question
    at the moment it is being answered."""
    owner_id, store_id, _ = await _a_shop(balance="40000.00")
    worker, _ = await _worker(owner_id)
    await _open(worker, "idem-open-1")
    await _lock_up(worker)

    await till_service.declare_close(worker, Decimal("35000"), "idem-till-1")

    assert await _balance(store_id) == Decimal("35000.00")


async def test_counting_is_refused_while_the_shop_is_still_trading(client):
    """The ordering that mattered, and the bug it closes. The count hands the owner
    everything above the float, so anything still to come out of the till — a wage,
    above all — has to come out first. A worker counted up at 21:07, handed over 82,000
    and was paid at 21:10 out of a drawer that no longer had it: the shop closed showing
    cash of -4,500, and their next count reported 7,000 more than expected.

    Any count made while the shop is trading is also stale on the very next sale.
    """
    owner_id, store_id, _ = await _a_shop(balance="40000.00")
    worker, _ = await _worker(owner_id)
    await _open(worker, "idem-open-1")

    with pytest.raises(BotError) as caught:
        await till_service.declare_close(worker, Decimal("30000"), "idem-till-1")

    assert caught.value.code == "store_still_open"
    assert await _balance(store_id) == Decimal("40000.00"), "and nothing moved"
    assert await db.fetchval("SELECT count(*) FROM till_counts") == 0


async def test_a_colleague_going_home_early_cannot_hand_over_the_drawer(client):
    """It is the shop's drawer, not a shift's. One of two cashiers leaving cannot hand
    the owner the change the other one needs for the next four hours."""
    owner_id, store_id, _ = await _a_shop(balance="40000.00")
    first, _ = await _worker(owner_id, "Անի")
    second, _ = await _worker(owner_id, "Գոռ")
    await _open(first, "idem-open-1")
    await _open(second, "idem-open-2")
    await worked_a_full_shift(first.id)
    await shifts_service.close_out_shift(first, [], "idem-close-1")

    with pytest.raises(BotError) as caught:
        await till_service.declare_close(first, Decimal("30000"), "idem-till-1")

    assert caught.value.code == "store_still_open"
    assert await _balance(store_id) == Decimal("40000.00")


async def test_a_retry_hands_over_once(client):
    owner_id, store_id, item_id = await _a_shop(balance="40000.00")
    worker, _ = await _worker(owner_id)
    await _open(worker, "idem-open-1")
    await sales_service.record_sale(
        worker, [{"item_id": item_id, "quantity": 2}], "cash", "idem-sale-1"
    )

    await _lock_up(worker)
    first = await till_service.declare_close(worker, Decimal("30000"), "idem-till-1")
    second = await till_service.declare_close(worker, Decimal("30000"), "idem-till-1")

    assert second["duplicate"] is True
    assert second["count"]["handed_over"] == first["count"]["handed_over"]
    assert await db.fetchval("SELECT count(*) FROM till_counts") == 1


# -- the owner's correction ---------------------------------------------------

async def test_the_owner_can_set_a_shops_float(client):
    """The balance is a real quantity somebody can be wrong about, and only the owner
    can settle it — a worker cannot be asked about a drawer they are not standing at."""
    owner_id, store_id, _ = await _a_shop(balance="40000.00")

    await till_service.set_by_owner(owner_id, store_id, Decimal("25000"))

    assert await _balance(store_id) == Decimal("25000.00")


async def test_the_owners_correction_is_recorded_as_theirs(client):
    owner_id, store_id, _ = await _a_shop()

    await till_service.set_by_owner(owner_id, store_id, Decimal("25000"), "վերահաշվարկ")

    row = await db.fetchrow("SELECT kind, counted, worker_id, note FROM till_counts")
    assert row["kind"] == "owner"
    assert row["counted"] == Decimal("25000.00")
    assert row["worker_id"] is None, "not attributed to a worker"
    assert row["note"] == "վերահաշվարկ"


async def test_a_correction_never_touches_the_till(client):
    """It used to book an adjustment when a session was open, on the reasoning that the
    balance *is* the till while the shop is trading. That stopped being true when the
    handover stopped being booked: the till is what the shop took and the balance is
    what stays on the premises, so a correction to one is not a movement in the other.
    """
    owner_id, store_id, _ = await _a_shop(balance="40000.00")
    worker, _ = await _worker(owner_id)
    await _open(worker, "idem-open-1")

    await till_service.set_by_owner(owner_id, store_id, Decimal("25000"))

    assert await _balance(store_id) == Decimal("25000.00")
    assert await _till(await _session()) == Decimal("40000.00"), "the day's cash stands"
    assert await db.fetchval(
        "SELECT count(*) FROM cash_movements WHERE kind = 'adjustment'"
    ) == 0


async def test_correcting_a_closed_shop_touches_no_ledger(client):
    """There is no session for the movement to belong to, and the balance alone is the
    truth about a shut shop's drawer."""
    owner_id, store_id, _ = await _a_shop(balance="40000.00")

    await till_service.set_by_owner(owner_id, store_id, Decimal("25000"))

    assert await db.fetchval("SELECT count(*) FROM cash_movements") == 0
    assert await _balance(store_id) == Decimal("25000.00")


async def test_a_negative_correction_is_refused(client):
    owner_id, store_id, _ = await _a_shop()

    with pytest.raises(AppError):
        await till_service.set_by_owner(owner_id, store_id, Decimal("-1"))


async def test_another_owners_shop_cannot_be_corrected(client):
    owner_id, store_id, _ = await _a_shop()
    stranger = await make_owner("@stranger")

    with pytest.raises(AppError):
        await till_service.set_by_owner(stranger, store_id, Decimal("100"))


# -- through the bot API ------------------------------------------------------

async def test_the_endpoint_records_a_handover(client, bot_headers):
    owner_id, store_id, _ = await _a_shop(balance="40000.00")
    worker, telegram_id = await _worker(owner_id)
    await _open(worker, "idem-open-1")

    await _lock_up(worker)
    response = await client.post(
        f"{BASE}/shift/till",
        json={"telegram_id": telegram_id, "counted": "30000.00",
              "idempotency_key": "idem-till-01"},
        headers=bot_headers,
    )

    assert response.status_code == 201
    body = response.json()
    assert body["count"]["handed_over"] == "10000.00"
    assert set(body["count"]) >= {"id", "counted", "expected", "handed_over"}
    assert await _balance(store_id) == Decimal("30000.00")


async def test_the_endpoint_no_longer_takes_a_kind(client, bot_headers):
    """There is one count now, so ``kind`` is refused rather than ignored — the house
    rule for this API, and the thing that tells you a stale bot is still deployed
    instead of letting it quietly record the wrong count."""
    owner_id, _, _ = await _a_shop(balance="40000.00")
    worker, telegram_id = await _worker(owner_id)
    await _open(worker, "idem-open-1")

    await _lock_up(worker)
    response = await client.post(
        f"{BASE}/shift/till",
        json={"telegram_id": telegram_id, "counted": "30000.00", "kind": "close",
              "idempotency_key": "idem-till-01"},
        headers=bot_headers,
    )

    assert response.status_code == 422


async def test_money_never_arrives_as_a_float(client, bot_headers):
    owner_id, _, _ = await _a_shop()
    worker, telegram_id = await _worker(owner_id)
    await _open(worker, "idem-open-1")

    await _lock_up(worker)
    response = await client.post(
        f"{BASE}/shift/till",
        json={"telegram_id": telegram_id, "counted": 30000.0,
              "idempotency_key": "idem-till-01"},
        headers=bot_headers,
    )

    assert response.status_code == 422


# -- what the owner sees ------------------------------------------------------

async def test_the_footer_shows_a_closed_shops_float(client):
    owner_id, store_id, _ = await _a_shop(balance="40000.00")
    await login(client, "@ownerhandle")

    page = await client.get("/partials/footer")

    assert "40,000.00" in page.text


async def test_the_store_page_shows_the_float_and_who_set_it(client):
    owner_id, store_id, item_id = await _a_shop(balance="40000.00")
    worker, _ = await _worker(owner_id)
    await _open(worker, "idem-open-1")
    await sales_service.record_sale(
        worker, [{"item_id": item_id, "quantity": 2}], "cash", "idem-sale-1"
    )
    await _lock_up(worker)
    await till_service.declare_close(worker, Decimal("30000"), "idem-till-1")
    await login(client, "@ownerhandle")

    page = await client.get(f"/stores/{store_id}")

    assert "Խանութի դրամարկղը" in page.text
    assert "30,000.00" in page.text
    assert "Անի" in page.text


async def test_the_owner_can_edit_it_from_the_store_page(client):
    owner_id, store_id, _ = await _a_shop(balance="40000.00")
    await login(client, "@ownerhandle")
    csrf = await db.fetchval(
        "SELECT csrf_token FROM auth_sessions ORDER BY created_at DESC LIMIT 1"
    )

    response = await client.post(
        f"/stores/{store_id}/till-balance",
        data={"csrf_token": csrf, "amount": "25000", "note": "վերահաշվարկ"},
    )

    assert response.status_code == 200
    assert await _balance(store_id) == Decimal("25000.00")


async def test_the_handover_shows_on_the_report(client):
    owner_id, _, item_id = await _a_shop(balance="40000.00")
    worker, _ = await _worker(owner_id)
    await _open(worker, "idem-open-1")
    await sales_service.record_sale(
        worker, [{"item_id": item_id, "quantity": 2}], "cash", "idem-sale-1"
    )
    session_id = await _session()
    await _lock_up(worker)
    await till_service.declare_close(worker, Decimal("30000"), "idem-till-1")
    await login(client, "@ownerhandle")

    page = await client.get(f"/reports?store_session_id={session_id}")

    assert "Դրամարկղի հանձնում" in page.text
    assert "17,000.00" in page.text, "what the owner should have received"
    assert "30,000.00" in page.text, "and what stayed in the shop"


async def test_the_header_no_longer_labels_the_drawer_as_cash_takings(client):
    """The four figures across the top were «Կանխիկ · Քարտ · Վաճառք · Աշխատավարձ», where
    the first was the drawer *balance* sitting beside two figures about takings. So
    «Կանխիկ 2,500 · Քարտ 16,000 · Վաճառք 101,500» read as a payment split that does not
    add up. Nothing was miscomputed; the labels described the wrong kinds of thing.
    """
    owner_id, _, item_id = await _a_shop(balance="40000.00")
    worker, _ = await _worker(owner_id)
    await _open(worker, "idem-open-1")
    await sales_service.record_sale(
        worker, [{"item_id": item_id, "quantity": 2}], "cash", "idem-sale-1"
    )
    session_id = await _session()
    await _lock_up(worker)
    await till_service.declare_close(worker, Decimal("30000"), "idem-till-1")
    await login(client, "@ownerhandle")

    page = await client.get(f"/reports?store_session_id={session_id}")

    assert "Ղեկավարին" in page.text, "what the owner should get"
    assert "Մնաց խանութում" in page.text, "and what stays behind"
    assert "Կանխիկ վաճառք" in page.text, "the split, named for what it is"
    assert "Քարտով վաճառք" in page.text


async def test_the_header_shows_both_bottom_lines(client):
    """Two questions, two figures:

        Շրջանառություն = վաճառք − աշխատավարձ − բոնուս − դրամարկղից հանված
        Շահույթ        = (վաճառք − ինքնարժեք) − the same three − օրվա ծախսեր

    Two vapes at 3,500 that cost 1,500 each: 7,000 sold, 4,000 of margin, and a
    1,000 wage. So 6,000 of cash and 3,000 of actual profit — and the second is the
    one that says whether the evening was worth opening for.
    """
    owner_id, _, item_id = await _a_shop(balance="40000.00")
    worker_id, _ = await make_worker(owner_id, "Անի", salary_amount="1000.00")
    worker = shifts_service.Worker(
        id=worker_id, owner_id=owner_id, name="Անի", salary_amount=Decimal("1000.00")
    )
    await _open(worker, "idem-open-1")
    await sales_service.record_sale(
        worker, [{"item_id": item_id, "quantity": 2}], "cash", "idem-sale-1"
    )
    session_id = await _session()
    await worked_a_full_shift(worker.id)
    await shifts_service.close_out_shift(worker, [], "idem-close-1")
    await login(client, "@ownerhandle")

    page = await client.get(f"/reports?store_session_id={session_id}")

    tiles = page.text.split('class="totals"')[1].split("</div>\n  </div>")[0]
    assert "Շրջանառություն" in tiles
    assert "Շահույթ" in tiles
    assert "6,000.00" in tiles, "cash: 7,000 sold less a 1,000 wage"
    assert "3,000.00" in tiles, "profit: 4,000 of margin less the same wage"


async def test_the_profit_takes_off_what_the_stock_cost(client):
    """The whole reason for the second figure. «Շահույթ» used to be the cash line
    under a name it did not deserve, so a shop selling 45,000 of stock that cost
    40,000 read it as a very good day."""
    owner_id, _, item_id = await _a_shop(balance="40000.00")
    worker, _ = await _worker(owner_id)
    await _open(worker, "idem-open-1")
    await sales_service.record_sale(
        worker, [{"item_id": item_id, "quantity": 2}], "cash", "idem-sale-1"
    )
    session_id = await _session()
    await _lock_up(worker)
    await login(client, "@ownerhandle")

    page = await client.get(f"/reports?store_session_id={session_id}")

    tiles = page.text.split('class="totals"')[1].split("</div>\n  </div>")[0]
    assert "7,000.00" in tiles, "the cash line keeps the whole selling price"
    assert "4,000.00" in tiles, "and the profit line is the margin"


async def test_the_days_own_expenses_come_off_the_profit(client):
    """What the owner typed on /expenses against this shop for this day. Money out is
    money out, and a profit figure that ignores the rent is not one."""
    owner_id, store_id, item_id = await _a_shop(balance="40000.00")
    worker, _ = await _worker(owner_id)
    await _open(worker, "idem-open-1")
    await sales_service.record_sale(
        worker, [{"item_id": item_id, "quantity": 2}], "cash", "idem-sale-1"
    )
    session_id = await _session()
    await _lock_up(worker)
    await db.execute(
        """
        INSERT INTO expenses (owner_id, store_id, purpose, amount, spent_on)
        VALUES ($1, $2, 'Վարձակալություն', 1000, $3)
        """,
        owner_id, store_id, await _opened_on(session_id),
    )
    await login(client, "@ownerhandle")

    page = await client.get(f"/reports?store_session_id={session_id}")

    tiles = page.text.split('class="totals"')[1].split("</div>\n  </div>")[0]
    assert "3,000.00" in tiles, "4,000 of margin less 1,000 of rent"
    assert "7,000.00" in tiles, "the cash line is untouched by it"


async def test_a_business_wide_expense_is_not_charged_to_one_shop(client):
    """Rent left as «Ամբողջ բիզնեսը» belongs to the business, not to the branch that
    happened to be open — charging it here would take the same rent off every shop
    trading that day."""
    owner_id, _, item_id = await _a_shop(balance="40000.00")
    worker, _ = await _worker(owner_id)
    await _open(worker, "idem-open-1")
    await sales_service.record_sale(
        worker, [{"item_id": item_id, "quantity": 2}], "cash", "idem-sale-1"
    )
    session_id = await _session()
    await _lock_up(worker)
    await db.execute(
        """
        INSERT INTO expenses (owner_id, store_id, purpose, amount, spent_on)
        VALUES ($1, NULL, 'Գովազդ', 1000, $2)
        """,
        owner_id, await _opened_on(session_id),
    )
    await login(client, "@ownerhandle")

    page = await client.get(f"/reports?store_session_id={session_id}")

    tiles = page.text.split('class="totals"')[1].split("</div>\n  </div>")[0]
    assert "4,000.00" in tiles, "the full margin — the advert is not this shop's"


async def test_breakage_comes_off_neither_figure(client):
    """The money left when the vape was bought, not when it was dropped — which is what
    the page has always said about breakage, and the stock cost in «Շահույթ» already
    accounts for stock that was actually sold."""
    owner_id, _, item_id = await _a_shop(balance="40000.00")
    worker, _ = await _worker(owner_id)
    await _open(worker, "idem-open-1")
    await sales_service.record_sale(
        worker, [{"item_id": item_id, "quantity": 2}], "cash", "idem-sale-1"
    )
    await write_offs_service.record(worker, item_id, 1, "ընկավ", "idem-defect-1")
    session_id = await _session()
    await _lock_up(worker)
    await login(client, "@ownerhandle")

    page = await client.get(f"/reports?store_session_id={session_id}")

    tiles = page.text.split('class="totals"')[1].split("</div>\n  </div>")[0]
    assert "7,000.00" in tiles, "the cash line"
    assert "4,000.00" in tiles, "and the profit line — neither has the 1,500 in it"
    assert "1,500.00" in page.text, "but it is named underneath"


async def test_a_voided_sale_is_not_counted_at_all(client):
    """Not as takings and so not as profit either. A day where everything was taken back
    read as a day that sold 7,000 and made money on it, because the tile summed the sale
    rows and left the reversals beside them."""
    owner_id, _, item_id = await _a_shop(balance="40000.00")
    worker, _ = await _worker(owner_id)
    await _open(worker, "idem-open-1")
    await sales_service.record_sale(
        worker, [{"item_id": item_id, "quantity": 2}], "cash", "idem-sale-1"
    )
    sale_id = await db.fetchval("SELECT id FROM sales")
    await corrections_service.void_sale(owner_id, owner_id, sale_id)
    session_id = await _session()
    await _lock_up(worker)
    await login(client, "@ownerhandle")

    page = await client.get(f"/reports?store_session_id={session_id}")

    tiles = page.text.split('class="totals"')[1].split("</div>\n  </div>")[0]
    assert "7,000" not in tiles


async def test_the_two_figures_an_owner_looks_for_are_coloured(client):
    """So the eye lands on them without reading six labels first. Two and no more — a
    page where every figure is coloured points at nothing."""
    owner_id, _, _ = await _a_shop(balance="40000.00")
    worker, _ = await _worker(owner_id)
    await _open(worker, "idem-open-1")
    session_id = await _session()
    await _lock_up(worker)
    await login(client, "@ownerhandle")

    page = await client.get(f"/reports?store_session_id={session_id}")

    assert "accent-owner" in page.text
    assert "accent-profit" in page.text or "negative" in page.text


async def test_a_day_that_lost_money_says_so_in_red(client):
    """A wage larger than the day's takings is a real evening, and a negative is the
    answer to it."""
    owner_id, _, item_id = await _a_shop(balance="40000.00")
    worker_id, _ = await make_worker(owner_id, "Անի", salary_amount="10000.00")
    worker = shifts_service.Worker(
        id=worker_id, owner_id=owner_id, name="Անի", salary_amount=Decimal("10000.00")
    )
    await _open(worker, "idem-open-1")
    await sales_service.record_sale(
        worker, [{"item_id": item_id, "quantity": 1}], "cash", "idem-sale-1"
    )
    session_id = await _session()
    await worked_a_full_shift(worker.id)
    await shifts_service.close_out_shift(worker, [], "idem-close-1")
    await login(client, "@ownerhandle")

    page = await client.get(f"/reports?store_session_id={session_id}")

    assert "-6,500.00" in page.text, "3,500 sold against a 10,000 wage"
    tiles = page.text.split('class="totals"')[1].split("</div>\n  </div>")[0]
    assert "negative" in tiles


async def test_a_share_of_nothing_explains_itself(client):
    """A zero over a day that took 45,400 is startling, and the arithmetic behind it —
    card money never reaching the drawer, a wage and a withdrawal emptying what did — is
    not something an owner should have to reconstruct.

    The sentence this replaces read «մնացել է 0.00 ֏, որից 1,500 մնում է խանութում»,
    which is nonsense: nothing cannot have 1,500 of it staying anywhere.
    """
    owner_id, _, item_id = await _a_shop(balance="40000.00")
    worker, _ = await _worker(owner_id)
    await _open(worker, "idem-open-1")
    session_id = await _session()
    await _lock_up(worker)
    # Left more than the till ever held: an unrecorded sale, or a number typed
    # without counting.
    await till_service.declare_close(worker, Decimal("45000"), "idem-till-1")
    await login(client, "@ownerhandle")

    page = await client.get(f"/reports?store_session_id={session_id}")

    assert "ղեկավարին հանձնելու գումար չկա" in page.text
    assert "որից 1,500" not in page.text


async def test_a_normal_evening_says_what_splits_where(client):
    owner_id, _, item_id = await _a_shop(balance="40000.00")
    worker, _ = await _worker(owner_id)
    await _open(worker, "idem-open-1")
    await sales_service.record_sale(
        worker, [{"item_id": item_id, "quantity": 2}], "cash", "idem-sale-1"
    )
    session_id = await _session()
    await _lock_up(worker)
    await till_service.declare_close(worker, Decimal("30000"), "idem-till-1")
    await login(client, "@ownerhandle")

    page = await client.get(f"/reports?store_session_id={session_id}")

    assert "մնում է խանութում" in page.text
    assert "ղեկավարին" in page.text.lower()


async def test_an_uncounted_session_still_says_what_the_owner_is_owed(client):
    """A worker who goes home without counting has not emptied the shop, and both
    figures reading 0.00 was not what happened to the money — it was the report
    claiming a settlement nobody made, on a day that took 7,000.

    Nothing changes the shop's balance except a count, so the float stays exactly where
    it is and the owner is owed the rest. That is not a guess about the drawer: it is
    what the system will actually act on when the shop opens tomorrow.
    """
    owner_id, _, item_id = await _a_shop(balance="40000.00")
    worker, _ = await _worker(owner_id)
    await _open(worker, "idem-open-1")
    await sales_service.record_sale(
        worker, [{"item_id": item_id, "quantity": 2}], "cash", "idem-sale-1"
    )
    session_id = await _session()
    await _lock_up(worker)
    await login(client, "@ownerhandle")

    page = await client.get(f"/reports?store_session_id={session_id}")

    tiles = page.text.split('class="totals"')[1].split("</div>\n  </div>")[0]
    assert 'value="40000.00"' in tiles, "the float stays in the shop"
    assert "7,000.00" in tiles, "and the day's cash goes to the owner"
    assert "Դրամարկղը հաշվված չէ" in page.text, "said to be worked out, not declared"


async def test_an_uncounted_session_with_no_float_owes_the_whole_till(client):
    """A shop that started empty keeps nothing, so all of it is the owner's."""
    owner_id, _, item_id = await _a_shop()
    worker, _ = await _worker(owner_id)
    await _open(worker, "idem-open-1")
    await sales_service.record_sale(
        worker, [{"item_id": item_id, "quantity": 2}], "cash", "idem-sale-1"
    )
    session_id = await _session()
    await _lock_up(worker)
    await login(client, "@ownerhandle")

    page = await client.get(f"/reports?store_session_id={session_id}")

    tiles = page.text.split('class="totals"')[1].split("</div>\n  </div>")[0]
    assert "7,000.00" in tiles
    assert "Դրամարկղը հաշվված չէ" in page.text


async def test_an_owners_own_deposit_is_not_mistaken_for_the_float(client):
    """The float is the one deposit nobody typed. An owner topping the drawer up
    mid-shift is money they put in, not money the shop is entitled to keep, and
    counting it as the float would hand them back less than they are owed."""
    owner_id, store_id, item_id = await _a_shop(balance="40000.00")
    worker, _ = await _worker(owner_id)
    await _open(worker, "idem-open-1")
    await sales_service.record_sale(
        worker, [{"item_id": item_id, "quantity": 2}], "cash", "idem-sale-1"
    )
    from app.services import money as money_service

    await money_service.record_movement(
        owner_id, store_id, "cash", "deposit", Decimal("5000"), "մանրադրամ"
    )
    session_id = await _session()
    await _lock_up(worker)
    await login(client, "@ownerhandle")

    page = await client.get(f"/reports?store_session_id={session_id}")

    tiles = page.text.split('class="totals"')[1].split("</div>\n  </div>")[0]
    assert 'value="40000.00"' in tiles, "the float, not the 45,000 of deposits"
    assert "12,000.00" in tiles, "7,000 sold plus the 5,000 the owner put in"


async def test_the_takings_split_is_a_figure_not_a_footnote(client):
    """At the same size as the total it belongs to, because it is the same kind of
    number. It was small print under the tiles, which is where a figure goes when it is
    an aside — and «how much of that was cash» is not an aside."""
    owner_id, _, item_id = await _a_shop(balance="40000.00")
    worker, _ = await _worker(owner_id)
    await _open(worker, "idem-open-1")
    await sales_service.record_sale(
        worker, [{"item_id": item_id, "quantity": 2}], "cash", "idem-sale-1"
    )
    session_id = await _session()
    await _lock_up(worker)
    await login(client, "@ownerhandle")

    page = await client.get(f"/reports?store_session_id={session_id}")

    tiles = page.text.split('class="totals"')[1].split("</div>\n  </div>")[0]
    for label in ("Վաճառք", "Կանխիկ վաճառք", "Քարտով վաճառք", "Ղեկավարին",
                  "Մնաց խանութում", "Աշխատավարձ"):
        assert f">{label}</span>" in tiles, f"«{label}» is not one of the big figures"


async def test_the_header_answers_a_session_nobody_counted(client):
    """A shop closed without anybody counting the drawer. Both figures are unknown
    rather than zero, but zero is the honest render: nothing was declared."""
    owner_id, _, item_id = await _a_shop(balance="40000.00")
    worker, _ = await _worker(owner_id)
    await _open(worker, "idem-open-1")
    session_id = await _session()
    await _lock_up(worker)
    await login(client, "@ownerhandle")

    page = await client.get(f"/reports?store_session_id={session_id}")

    assert page.status_code == 200


# -- the owner correcting a count ---------------------------------------------

async def test_the_owner_can_correct_what_the_till_held(client):
    """It was frozen at the moment of counting, so a sale voided afterwards leaves it
    describing books that have since changed. The owner's share is the difference, so it
    follows."""
    owner_id, store_id, _ = await _a_shop(balance="40000.00")
    worker, _ = await _worker(owner_id)
    await _open(worker, "idem-open-1")
    await _lock_up(worker)
    await till_service.declare_close(worker, Decimal("30000"), "idem-till-1")
    count_id = await db.fetchval("SELECT id FROM till_counts")

    await till_service.correct_a_count(
        owner_id, count_id, Decimal("30000"), Decimal("47000")
    )

    row = await db.fetchrow(
        "SELECT expected, counted, handed_over FROM till_counts WHERE id = $1", count_id
    )
    assert row["expected"] == Decimal("47000.00")
    assert row["handed_over"] == Decimal("17000.00")
    assert await _balance(store_id) == Decimal("30000.00"), "the float is about counted"


async def test_the_till_reading_may_be_negative(client):
    """Under the old rules a drawer really was recorded as holding less than nothing, and
    a row saying so is a record of that evening. The *share* is what must never be
    negative."""
    owner_id, _, _ = await _a_shop(balance="40000.00")
    worker, _ = await _worker(owner_id)
    await _open(worker, "idem-open-1")
    await _lock_up(worker)
    await till_service.declare_close(worker, Decimal("2500"), "idem-till-1")
    count_id = await db.fetchval("SELECT id FROM till_counts")

    await till_service.correct_a_count(
        owner_id, count_id, Decimal("2500"), Decimal("-4500")
    )

    row = await db.fetchrow(
        "SELECT expected, handed_over FROM till_counts WHERE id = $1", count_id
    )
    assert row["expected"] == Decimal("-4500.00")
    assert row["handed_over"] == Decimal("0.00"), "and the owner is owed nothing, not -7,000"


async def test_a_count_that_has_drifted_from_the_books_is_named(client):
    """Two numbers on the same screen that ought to match is something to be spotted;
    saying which one moved is something to act on."""
    owner_id, _, item_id = await _a_shop(balance="40000.00")
    worker, _ = await _worker(owner_id)
    await _open(worker, "idem-open-1")
    await sales_service.record_sale(
        worker, [{"item_id": item_id, "quantity": 2}], "cash", "idem-sale-1"
    )
    session_id = await _session()
    await _lock_up(worker)
    await till_service.declare_close(worker, Decimal("30000"), "idem-till-1")
    count_id = await db.fetchval("SELECT id FROM till_counts")
    await login(client, "@ownerhandle")

    settled = await client.get(f"/reports?store_session_id={session_id}")
    assert "Ուշադրություն" not in settled.text

    await till_service.correct_a_count(
        owner_id, count_id, Decimal("30000"), Decimal("99000")
    )
    drifted = await client.get(f"/reports?store_session_id={session_id}")

    assert "Ուշադրություն" in drifted.text


async def test_the_header_follows_a_corrected_count(client):
    """Both readings are editable, so the tile has to come from them. A header computed
    from the live ledger instead would sit there contradicting the row the owner had just
    corrected."""
    owner_id, _, item_id = await _a_shop(balance="40000.00")
    worker, _ = await _worker(owner_id)
    await _open(worker, "idem-open-1")
    await sales_service.record_sale(
        worker, [{"item_id": item_id, "quantity": 2}], "cash", "idem-sale-1"
    )
    session_id = await _session()
    await _lock_up(worker)
    await till_service.declare_close(worker, Decimal("30000"), "idem-till-1")
    count_id = await db.fetchval("SELECT id FROM till_counts")
    await till_service.correct_a_count(
        owner_id, count_id, Decimal("20000"), Decimal("47000")
    )
    await login(client, "@ownerhandle")

    page = await client.get(f"/reports?store_session_id={session_id}")

    assert "27,000.00" in page.text, "47,000 less the corrected 20,000"


async def test_the_owner_can_correct_what_was_left_in_the_shop(client):
    """The figure a cashier types at the door is the one most often wrong — a nought
    too many, a bundle counted twice — and the report used to show it and nothing
    else."""
    owner_id, store_id, item_id = await _a_shop(balance="40000.00")
    worker, _ = await _worker(owner_id)
    await _open(worker, "idem-open-1")
    await sales_service.record_sale(
        worker, [{"item_id": item_id, "quantity": 2}], "cash", "idem-sale-1"
    )
    await _lock_up(worker)
    await till_service.declare_close(worker, Decimal("300000"), "idem-till-1")
    count_id = await db.fetchval("SELECT id FROM till_counts")

    await till_service.correct_a_count(owner_id, count_id, Decimal("30000"))

    row = await db.fetchrow(
        "SELECT counted, handed_over FROM till_counts WHERE id = $1", count_id
    )
    assert row["counted"] == Decimal("30000.00")
    assert row["handed_over"] == Decimal("17000.00"), "the owner's share follows it"
    assert await _balance(store_id) == Decimal("30000.00"), "and so does the shop's float"


async def test_correcting_an_older_count_leaves_the_float_alone(client):
    """Fixing yesterday for the record must not overwrite what today established about
    the same drawer."""
    owner_id, store_id, _ = await _a_shop(balance="40000.00")
    worker, _ = await _worker(owner_id)
    await _open(worker, "idem-open-1")
    await _lock_up(worker, "idem-lockup-1")
    await till_service.declare_close(worker, Decimal("30000"), "idem-till-1")
    older = await db.fetchval("SELECT id FROM till_counts")
    await _open(worker, "idem-open-2")
    await _lock_up(worker, "idem-lockup-2")
    await till_service.declare_close(worker, Decimal("25000"), "idem-till-2")

    await till_service.correct_a_count(owner_id, older, Decimal("31000"))

    assert await db.fetchval(
        "SELECT counted FROM till_counts WHERE id = $1", older
    ) == Decimal("31000.00")
    assert await _balance(store_id) == Decimal("25000.00"), "today still stands"


async def test_a_negative_correction_to_a_count_is_refused(client):
    owner_id, _, _ = await _a_shop(balance="40000.00")
    worker, _ = await _worker(owner_id)
    await _open(worker, "idem-open-1")
    await _lock_up(worker)
    await till_service.declare_close(worker, Decimal("30000"), "idem-till-1")
    count_id = await db.fetchval("SELECT id FROM till_counts")

    with pytest.raises(AppError):
        await till_service.correct_a_count(owner_id, count_id, Decimal("-1"))


async def test_another_owners_count_cannot_be_corrected(client):
    owner_id, _, _ = await _a_shop(balance="40000.00")
    worker, _ = await _worker(owner_id)
    await _open(worker, "idem-open-1")
    await _lock_up(worker)
    await till_service.declare_close(worker, Decimal("30000"), "idem-till-1")
    count_id = await db.fetchval("SELECT id FROM till_counts")
    stranger = await make_owner("@stranger")

    with pytest.raises(AppError):
        await till_service.correct_a_count(stranger, count_id, Decimal("100"))


async def test_the_report_offers_the_correction_box(client):
    owner_id, _, _ = await _a_shop(balance="40000.00")
    worker, _ = await _worker(owner_id)
    await _open(worker, "idem-open-1")
    session_id = await _session()
    await _lock_up(worker)
    await till_service.declare_close(worker, Decimal("30000"), "idem-till-1")
    count_id = await db.fetchval("SELECT id FROM till_counts")
    await login(client, "@ownerhandle")

    page = await client.get(f"/reports?store_session_id={session_id}")

    assert f"/till-counts/{count_id}/counted" in page.text
    assert 'name="expected"' in page.text, "both readings are editable"
    assert 'name="counted"' in page.text


async def test_correcting_a_count_from_the_report(client):
    owner_id, store_id, _ = await _a_shop(balance="40000.00")
    worker, _ = await _worker(owner_id)
    await _open(worker, "idem-open-1")
    await _lock_up(worker)
    await till_service.declare_close(worker, Decimal("300000"), "idem-till-1")
    count_id = await db.fetchval("SELECT id FROM till_counts")
    await login(client, "@ownerhandle")
    csrf = await db.fetchval(
        "SELECT csrf_token FROM auth_sessions ORDER BY created_at DESC LIMIT 1"
    )

    response = await client.post(
        f"/till-counts/{count_id}/counted",
        data={"csrf_token": csrf, "counted": "30000"},
    )

    assert response.status_code in (200, 303)
    assert await _balance(store_id) == Decimal("30000.00")


async def test_the_owners_share_is_worked_out_rather_than_read_back(client):
    """From the ledger and the last count, not from the figure stored on that count.
    So it follows a sale corrected next week, and rows written before the share was
    floored at nothing cannot put a negative in the header — the shop that closed on
    -4,500 has a count saying the owner is owed -7,000.
    """
    owner_id, _, item_id = await _a_shop(balance="40000.00")
    worker, _ = await _worker(owner_id)
    await _open(worker, "idem-open-1")
    await sales_service.record_sale(
        worker, [{"item_id": item_id, "quantity": 2}], "cash", "idem-sale-1"
    )
    session_id = await _session()
    await _lock_up(worker)
    await till_service.declare_close(worker, Decimal("30000"), "idem-till-1")
    # A stale figure of the kind the old rules left behind.
    await db.execute("UPDATE till_counts SET handed_over = -7000")
    await login(client, "@ownerhandle")

    page = await client.get(f"/reports?store_session_id={session_id}")

    assert "17,000.00" in page.text, "the ledger, not the stored figure"
    assert "-7,000" not in page.text


async def test_the_drawer_table_has_no_column_for_the_owners_share(client):
    """One figure for the evening, not one per row — a second count replaces the first
    rather than adding to it — so a column of them invited adding two numbers that must
    not be added."""
    owner_id, _, _ = await _a_shop(balance="40000.00")
    worker, _ = await _worker(owner_id)
    await _open(worker, "idem-open-1")
    session_id = await _session()
    await _lock_up(worker)
    await till_service.declare_close(worker, Decimal("30000"), "idem-till-1")
    await login(client, "@ownerhandle")

    page = await client.get(f"/reports?store_session_id={session_id}")

    table = page.text.split("Դրամարկղի հանձնում")[1].split("</table>")[0]
    assert "Ղեկավարին" not in table
    assert "Ղեկավարին" in page.text, "but it is still on the page, as one tile"


# -- deleting a count ---------------------------------------------------------

async def test_a_count_can_be_deleted(client):
    """Not a correction of a figure but a statement that the reading should never have
    been there: a duplicate, a count against the wrong shop, a row left behind by a
    rule that has since changed."""
    owner_id, _, _ = await _a_shop(balance="40000.00")
    worker, _ = await _worker(owner_id)
    await _open(worker, "idem-open-1")
    await _lock_up(worker)
    await till_service.declare_close(worker, Decimal("30000"), "idem-till-1")
    count_id = await db.fetchval("SELECT id FROM till_counts")

    await till_service.delete_count(owner_id, owner_id, count_id)

    assert await db.fetchval("SELECT count(*) FROM till_counts") == 0


async def test_deleting_the_latest_count_falls_back_to_the_one_before(client):
    """Leaving the float where the deleted row put it would keep a figure whose only
    justification has just been thrown away."""
    owner_id, store_id, _ = await _a_shop(balance="40000.00")
    worker, _ = await _worker(owner_id)
    await _open(worker, "idem-open-1")
    await _lock_up(worker, "idem-lockup-1")
    await till_service.declare_close(worker, Decimal("30000"), "idem-till-1")
    await _open(worker, "idem-open-2")
    await _lock_up(worker, "idem-lockup-2")
    await till_service.declare_close(worker, Decimal("25000"), "idem-till-2")
    newest = await db.fetchval("SELECT id FROM till_counts ORDER BY id DESC LIMIT 1")

    await till_service.delete_count(owner_id, owner_id, newest)

    assert await _balance(store_id) == Decimal("30000.00")


async def test_deleting_the_only_count_leaves_the_shop_with_nothing_declared(client):
    """Which is the honest answer: nobody has said what is in that drawer."""
    owner_id, store_id, _ = await _a_shop(balance="40000.00")
    worker, _ = await _worker(owner_id)
    await _open(worker, "idem-open-1")
    await _lock_up(worker)
    await till_service.declare_close(worker, Decimal("30000"), "idem-till-1")
    count_id = await db.fetchval("SELECT id FROM till_counts")

    await till_service.delete_count(owner_id, owner_id, count_id)

    assert await _balance(store_id) == Decimal("0.00")


async def test_deleting_an_older_count_leaves_the_float_alone(client):
    owner_id, store_id, _ = await _a_shop(balance="40000.00")
    worker, _ = await _worker(owner_id)
    await _open(worker, "idem-open-1")
    await _lock_up(worker, "idem-lockup-1")
    await till_service.declare_close(worker, Decimal("30000"), "idem-till-1")
    older = await db.fetchval("SELECT id FROM till_counts ORDER BY id LIMIT 1")
    await _open(worker, "idem-open-2")
    await _lock_up(worker, "idem-lockup-2")
    await till_service.declare_close(worker, Decimal("25000"), "idem-till-2")

    await till_service.delete_count(owner_id, owner_id, older)

    assert await _balance(store_id) == Decimal("25000.00")


async def test_deleting_a_count_is_recorded_in_the_history(client):
    owner_id, _, _ = await _a_shop(balance="40000.00")
    worker, _ = await _worker(owner_id)
    await _open(worker, "idem-open-1")
    await _lock_up(worker)
    await till_service.declare_close(worker, Decimal("30000"), "idem-till-1")
    count_id = await db.fetchval("SELECT id FROM till_counts")

    await till_service.delete_count(owner_id, owner_id, count_id)

    row = await db.fetchrow(
        "SELECT action, summary FROM audit_events ORDER BY id DESC LIMIT 1"
    )
    assert row["action"] == "delete_till_count"
    assert "30,000" in row["summary"]


async def test_a_deleted_count_can_be_undone(client):
    """The history page offers the newest event whatever it is, so an action with no
    reverter behind it is a button that crashes. Every other deletion here is undoable,
    and nothing else holds a deleted count to rebuild it from."""
    owner_id, store_id, item_id = await _a_shop(balance="40000.00")
    worker, _ = await _worker(owner_id)
    await _open(worker, "idem-open-1")
    await sales_service.record_sale(
        worker, [{"item_id": item_id, "quantity": 2}], "cash", "idem-sale-1"
    )
    await _lock_up(worker)
    await till_service.declare_close(worker, Decimal("30000"), "idem-till-1")
    count_id = await db.fetchval("SELECT id FROM till_counts")
    await till_service.delete_count(owner_id, owner_id, count_id)
    event_id = await db.fetchval("SELECT id FROM audit_events ORDER BY id DESC LIMIT 1")

    await corrections_service.revert(owner_id, owner_id, event_id)

    row = await db.fetchrow(
        "SELECT counted, expected, handed_over, worker_id, external_id FROM till_counts"
    )
    assert row["counted"] == Decimal("30000.00")
    assert row["expected"] == Decimal("47000.00")
    assert row["handed_over"] == Decimal("17000.00")
    assert row["worker_id"] == worker.id, "still theirs"
    assert row["external_id"] == "idem-till-1", "and the bot's key still points at it"
    assert await _balance(store_id) == Decimal("30000.00"), "and the float came back"


async def test_the_restored_count_keeps_its_place_in_the_evening(client):
    """``created_at`` comes back with it. Restoring it stamped with now() would move it
    to the end of a table ordered by time, which is not where it happened."""
    owner_id, _, _ = await _a_shop(balance="40000.00")
    worker, _ = await _worker(owner_id)
    await _open(worker, "idem-open-1")
    await _lock_up(worker)
    await till_service.declare_close(worker, Decimal("30000"), "idem-till-1")
    count_id = await db.fetchval("SELECT id FROM till_counts")
    was = await db.fetchval("SELECT created_at FROM till_counts WHERE id = $1", count_id)
    await till_service.delete_count(owner_id, owner_id, count_id)
    event_id = await db.fetchval("SELECT id FROM audit_events ORDER BY id DESC LIMIT 1")

    await corrections_service.revert(owner_id, owner_id, event_id)

    assert await db.fetchval("SELECT created_at FROM till_counts") == was


async def test_another_owners_count_cannot_be_deleted(client):
    owner_id, store_id, _ = await _a_shop(balance="40000.00")
    worker, _ = await _worker(owner_id)
    await _open(worker, "idem-open-1")
    await _lock_up(worker)
    await till_service.declare_close(worker, Decimal("30000"), "idem-till-1")
    count_id = await db.fetchval("SELECT id FROM till_counts")
    stranger = await make_owner("@stranger")

    with pytest.raises(AppError):
        await till_service.delete_count(stranger, stranger, count_id)

    assert await db.fetchval("SELECT count(*) FROM till_counts") == 1
    assert await _balance(store_id) == Decimal("30000.00")


async def test_deleting_a_count_from_the_report(client):
    owner_id, store_id, _ = await _a_shop(balance="40000.00")
    worker, _ = await _worker(owner_id)
    await _open(worker, "idem-open-1")
    session_id = await _session()
    await _lock_up(worker)
    await till_service.declare_close(worker, Decimal("30000"), "idem-till-1")
    count_id = await db.fetchval("SELECT id FROM till_counts")
    await login(client, "@ownerhandle")
    csrf = await db.fetchval(
        "SELECT csrf_token FROM auth_sessions ORDER BY created_at DESC LIMIT 1"
    )

    page = await client.get(f"/reports?store_session_id={session_id}")
    assert f"/till-counts/{count_id}/delete" in page.text

    response = await client.post(
        f"/till-counts/{count_id}/delete", data={"csrf_token": csrf}
    )

    assert response.status_code in (200, 303)
    assert await db.fetchval("SELECT count(*) FROM till_counts") == 0
    assert await _balance(store_id) == Decimal("0.00")


async def test_a_forgotten_sale_can_be_added_without_hunting_for_the_form(client):
    """It was collapsed behind «▸ Ավելացնել մոռացված վաճառք» and got reported as
    missing. A triangle beside a heading does not read as "there is a form here"."""
    owner_id, _, _ = await _a_shop(balance="40000.00")
    worker, _ = await _worker(owner_id)
    await _open(worker, "idem-open-1")
    session_id = await _session()
    await _lock_up(worker)
    await login(client, "@ownerhandle")

    page = await client.get(f"/reports?store_session_id={session_id}")

    # The action carries a ?back= of the page it was submitted from, so that adding a
    # sale returns to the report in whatever order the owner had put it in.
    assert f'action="/store-sessions/{session_id}/sales?back=' in page.text
    assert 'class="sub-card" open' in page.text, "the form is open, not collapsed"


async def test_the_session_rows_carry_the_handover(client):
    owner_id, _, item_id = await _a_shop(balance="40000.00")
    worker, _ = await _worker(owner_id)
    await _open(worker, "idem-open-1")
    await sales_service.record_sale(
        worker, [{"item_id": item_id, "quantity": 2}], "cash", "idem-sale-1"
    )
    session_id = await _session()
    await _lock_up(worker)
    await till_service.declare_close(worker, Decimal("30000"), "idem-till-1")

    rows = await till_repo.for_session(session_id)

    assert len(rows) == 1
    assert rows[0]["handed_over"] == Decimal("17000.00")
    assert rows[0]["counted"] == Decimal("30000.00")
    assert rows[0]["worker_name"] == "Անի"


# -- correcting either end of the drawer --------------------------------------

async def _csrf() -> str:
    return await db.fetchval(
        "SELECT csrf_token FROM auth_sessions ORDER BY created_at DESC LIMIT 1"
    )


async def test_the_owner_can_correct_what_the_shop_opened_with(client):
    """The float was the one drawer figure that could not be touched.

    It is written by the system out of a balance that may itself be wrong — a count
    typed with an extra nought last week propagates into every morning after it — and
    the owner had no way to say what was actually in the drawer that day.
    """
    owner_id, _, item_id = await _a_shop(balance="40000.00")
    worker, _ = await _worker(owner_id)
    await _open(worker, "idem-open-1")
    await sales_service.record_sale(
        worker, [{"item_id": item_id, "quantity": 2}], "cash", "idem-sale-1"
    )
    session_id = await _session()
    await _lock_up(worker)
    await login(client, "@ownerhandle")

    await client.post(
        f"/store-sessions/{session_id}/carried-in",
        data={"amount": "4000", "csrf_token": await _csrf()},
    )

    totals = await money_repo.totals_for_session(session_id)
    assert Decimal(totals["carried_in"]) == Decimal("4000.00")
    assert Decimal(totals["cash"]) == Decimal("11000.00"), "4,000 carried + 7,000 sold"


async def test_correcting_the_float_moves_everything_that_follows_from_it(client):
    """Nothing has to be recomputed by hand, which is the whole reason the figure is a
    ledger row and not a column: the cash, the owner's share and the sentence that
    explains the subtraction are all sums over the same table."""
    owner_id, _, item_id = await _a_shop(balance="40000.00")
    worker, _ = await _worker(owner_id)
    await _open(worker, "idem-open-1")
    await sales_service.record_sale(
        worker, [{"item_id": item_id, "quantity": 2}], "cash", "idem-sale-1"
    )
    session_id = await _session()
    await _lock_up(worker)
    await login(client, "@ownerhandle")

    await client.post(
        f"/store-sessions/{session_id}/carried-in",
        data={"amount": "4000", "csrf_token": await _csrf()},
    )
    page = (await client.get(f"/reports?store_session_id={session_id}")).text

    assert 'value="4000.00"' in page, "the morning now reads 4,000"
    # Nobody counted, so the shop keeps what it opened with and the owner gets the rest.
    assert "7,000.00" in page, "the day's cash is still the owner's"


async def test_a_float_the_system_never_recorded_can_be_typed_in(client):
    """The case with no row to edit: a shop whose balance was zero because nobody had
    ever counted it, which is every shop's first week. The row is written now, and it
    counts as the float because of what it is — the opening balance of that drawer —
    not because of who entered it."""
    owner_id, _, item_id = await _a_shop()
    worker, _ = await _worker(owner_id)
    await _open(worker, "idem-open-1")
    await sales_service.record_sale(
        worker, [{"item_id": item_id, "quantity": 2}], "cash", "idem-sale-1"
    )
    session_id = await _session()
    await _lock_up(worker)
    await login(client, "@ownerhandle")

    await client.post(
        f"/store-sessions/{session_id}/carried-in",
        data={"amount": "30000", "csrf_token": await _csrf()},
    )

    totals = await money_repo.totals_for_session(session_id)
    assert Decimal(totals["carried_in"]) == Decimal("30000.00")
    assert Decimal(totals["cash"]) == Decimal("37000.00")


async def test_a_float_set_to_nothing_leaves_no_row_behind(client):
    """Zero and no float are the same fact. A «0 ֏» line in the ledger would be a row
    that never means anything, sitting among rows that all do."""
    owner_id, _, _ = await _a_shop(balance="40000.00")
    worker, _ = await _worker(owner_id)
    await _open(worker, "idem-open-1")
    session_id = await _session()
    await _lock_up(worker)
    await login(client, "@ownerhandle")

    await client.post(
        f"/store-sessions/{session_id}/carried-in",
        data={"amount": "0", "csrf_token": await _csrf()},
    )

    assert await db.fetchval(
        "SELECT count(*) FROM cash_movements WHERE store_session_id = $1", session_id
    ) == 0
    assert await _till(session_id) == Decimal("0.00")


async def test_every_correction_to_the_float_is_undoable(client):
    """It goes through the ledger correction it already is, so the history records it
    and the undo already knows how to reverse it. No new kind of event."""
    owner_id, _, _ = await _a_shop(balance="40000.00")
    worker, _ = await _worker(owner_id)
    await _open(worker, "idem-open-1")
    session_id = await _session()
    await _lock_up(worker)
    await login(client, "@ownerhandle")

    await client.post(
        f"/store-sessions/{session_id}/carried-in",
        data={"amount": "4000", "csrf_token": await _csrf()},
    )
    event = await db.fetchrow("SELECT id, action FROM audit_events ORDER BY id DESC LIMIT 1")
    assert event["action"] == "set_movement_amount"

    await corrections_service.revert(owner_id, owner_id, event["id"])

    totals = await money_repo.totals_for_session(session_id)
    assert Decimal(totals["carried_in"]) == Decimal("40000.00")


async def test_the_owner_can_say_what_was_left_when_nobody_counted(client):
    """The evening that most needed this.

    The report can only *infer* that drawer — the shop keeps whatever it opened with —
    and an owner who knows the real figure had nowhere to put it. «Ուղղել մնացորդը» on
    the store page sets today's drawer, not that evening's.
    """
    owner_id, store_id, item_id = await _a_shop(balance="40000.00")
    worker, _ = await _worker(owner_id)
    await _open(worker, "idem-open-1")
    await sales_service.record_sale(
        worker, [{"item_id": item_id, "quantity": 2}], "cash", "idem-sale-1"
    )
    session_id = await _session()
    await _lock_up(worker)  # goes home without counting
    await login(client, "@ownerhandle")

    await client.post(
        f"/store-sessions/{session_id}/left-in-store",
        data={"counted": "45000", "csrf_token": await _csrf()},
    )

    count = await till_repo.latest_for_session(session_id)
    assert Decimal(count["counted"]) == Decimal("45000.00")
    assert Decimal(count["expected"]) == Decimal("47000.00"), "the books, frozen beside it"
    assert await _balance(store_id) == Decimal("45000.00"), "and tomorrow opens on it"


async def test_the_first_reading_records_who_actually_made_it(client):
    """The owner, not the worker who went home. A count says somebody stood at a drawer
    and looked, and the report says which of them it was."""
    owner_id, _, _ = await _a_shop(balance="40000.00")
    worker, _ = await _worker(owner_id)
    await _open(worker, "idem-open-1")
    session_id = await _session()
    await _lock_up(worker)
    await login(client, "@ownerhandle")

    await client.post(
        f"/store-sessions/{session_id}/left-in-store",
        data={"counted": "35000", "csrf_token": await _csrf()},
    )

    row = await db.fetchrow(
        "SELECT kind, worker_id FROM till_counts WHERE store_session_id = $1", session_id
    )
    assert row["kind"] == "owner"
    assert row["worker_id"] is None


async def test_correcting_an_existing_count_goes_through_the_same_door(client):
    """The header box and the row in the table below it edit one figure. Two paths that
    could disagree about the shop's float is the bug this shape avoids."""
    owner_id, store_id, item_id = await _a_shop(balance="40000.00")
    worker, _ = await _worker(owner_id)
    await _open(worker, "idem-open-1")
    await sales_service.record_sale(
        worker, [{"item_id": item_id, "quantity": 2}], "cash", "idem-sale-1"
    )
    session_id = await _session()
    await _lock_up(worker)
    await till_service.declare_close(worker, Decimal("30000"), "idem-till-1")
    await login(client, "@ownerhandle")

    await client.post(
        f"/store-sessions/{session_id}/left-in-store",
        data={"counted": "25000", "csrf_token": await _csrf()},
    )

    assert await db.fetchval(
        "SELECT count(*) FROM till_counts WHERE store_session_id = $1", session_id
    ) == 1, "corrected in place, not stacked"
    count = await till_repo.latest_for_session(session_id)
    assert Decimal(count["counted"]) == Decimal("25000.00")
    assert await _balance(store_id) == Decimal("25000.00")


async def test_a_reading_filled_in_for_an_old_evening_leaves_todays_drawer_alone(client):
    """A correction to last Tuesday is a correction to the record. The money in the
    drawer today is whatever the evenings since then made it, and letting a back-filled
    reading overwrite it would be the report editing the present."""
    owner_id, store_id, item_id = await _a_shop(balance="40000.00")
    worker, _ = await _worker(owner_id)

    await _open(worker, "idem-open-1")
    await sales_service.record_sale(
        worker, [{"item_id": item_id, "quantity": 2}], "cash", "idem-sale-1"
    )
    tuesday = await _session()
    await _lock_up(worker, "idem-lockup-1")  # nobody counted

    await _open(worker, "idem-open-2")
    await _lock_up(worker, "idem-lockup-2")
    await till_service.declare_close(worker, Decimal("12000"), "idem-till-2")
    await login(client, "@ownerhandle")

    await client.post(
        f"/store-sessions/{tuesday}/left-in-store",
        data={"counted": "45000", "csrf_token": await _csrf()},
    )

    count = await till_repo.latest_for_session(tuesday)
    assert Decimal(count["counted"]) == Decimal("45000.00"), "the record is fixed"
    assert await _balance(store_id) == Decimal("12000.00"), "and today's drawer is not"


async def test_both_boxes_are_on_the_report(client):
    owner_id, _, _ = await _a_shop(balance="40000.00")
    worker, _ = await _worker(owner_id)
    await _open(worker, "idem-open-1")
    session_id = await _session()
    await _lock_up(worker)
    await login(client, "@ownerhandle")

    page = (await client.get(f"/reports?store_session_id={session_id}")).text

    assert f"/store-sessions/{session_id}/carried-in" in page
    assert f"/store-sessions/{session_id}/left-in-store" in page


async def test_neither_box_takes_a_negative(client):
    owner_id, _, _ = await _a_shop(balance="40000.00")
    worker, _ = await _worker(owner_id)
    await _open(worker, "idem-open-1")
    session_id = await _session()
    await _lock_up(worker)
    await login(client, "@ownerhandle")
    token = await _csrf()

    for path, field in (("carried-in", "amount"), ("left-in-store", "counted")):
        response = await client.post(
            f"/store-sessions/{session_id}/{path}",
            data={field: "-100", "csrf_token": token},
        )
        assert response.status_code < 500

    totals = await money_repo.totals_for_session(session_id)
    assert Decimal(totals["carried_in"]) == Decimal("40000.00")
    assert await db.fetchval(
        "SELECT count(*) FROM till_counts WHERE store_session_id = $1", session_id
    ) == 0


async def test_another_owners_session_cannot_be_touched(client):
    """The session id is in the URL, so the scoping is the only thing standing between
    one shop's books and another's."""
    owner_id, _, _ = await _a_shop(balance="40000.00")
    worker, _ = await _worker(owner_id)
    await _open(worker, "idem-open-1")
    session_id = await _session()
    await _lock_up(worker)

    stranger = await make_owner("@stranger")
    await make_store(stranger, "Ուրիշ", lat=YEREVAN_LAT, lng=YEREVAN_LNG)
    await login(client, "@stranger")
    token = await _csrf()

    for path, field in (("carried-in", "amount"), ("left-in-store", "counted")):
        response = await client.post(
            f"/store-sessions/{session_id}/{path}",
            data={field: "1", "csrf_token": token},
        )
        assert response.status_code < 500

    assert await db.fetchval(
        "SELECT count(*) FROM till_counts WHERE store_session_id = $1", session_id
    ) == 0
    totals = await money_repo.totals_for_session(session_id)
    assert Decimal(totals["carried_in"]) == Decimal("40000.00")
