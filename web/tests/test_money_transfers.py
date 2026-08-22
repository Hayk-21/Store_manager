"""Cash carried from one of an owner's shops to another.

Money is not a box on a shelf, and the difference decides everything here. The
notes really do leave the sender's drawer the moment they are handed over, so the
withdrawal is booked at once — otherwise their own count tonight is short by cash
they no longer have. They do not arrive until somebody carries them, so the
deposit waits for a worker at the other end to say they did.

Between those two moments the money is in a taxi. The tests that matter most are
about exactly that gap: what each till reads while it is open, and what happens if
it never closes because the envelope did not turn up.
"""

from __future__ import annotations

from decimal import Decimal
from unittest import mock

import pytest

from app import texts
from app.db import db
from app.errors import BotError
from app.repo import money as money_repo
from app.repo import money_transfers as transfers_repo
from app.services import money_transfers as money_transfers_service
from app.services import sales as sales_service
from app.services import shifts as shifts_service
from app.services import telegram
from tests.factories import (
    YEREVAN_LAT,
    YEREVAN_LNG,
    make_item,
    make_owner,
    make_store,
    make_worker,
)

BASE = "/api/bot/v1"


async def _two_shops():
    """One owner, two shops, a product on each shelf.

    A kilometre apart on purpose: opening a shift geofences to the *nearest* shop,
    so two at the same coordinates would put both workers in one of them and every
    test here would be about a shop sending money to itself.
    """
    owner_id = await make_owner("@ownerhandle")
    first = await make_store(owner_id, "Խանութ 1", lat=YEREVAN_LAT, lng=YEREVAN_LNG)
    second = await make_store(
        owner_id, "Խանութ 2", lat=YEREVAN_LAT + 0.01, lng=YEREVAN_LNG + 0.01
    )
    return owner_id, first, second


async def _worker_at(owner_id: int, store_id: int, name: str, key: str):
    """Somebody on shift at one particular shop."""
    worker_id, telegram_id = await make_worker(owner_id, name, salary_amount="0.00")
    worker = shifts_service.Worker(
        id=worker_id, owner_id=owner_id, name=name, salary_amount=Decimal("0.00")
    )
    store = await db.fetchrow("SELECT lat, lng FROM stores WHERE id = $1", store_id)
    await shifts_service.open_store(
        worker, store["lat"], store["lng"], 20, f"idem-open-{key}", 900
    )
    return worker, telegram_id


async def _takings(worker, store_id: int, amount: str, key: str):
    """Put cash in a drawer the honest way — by selling something for it."""
    item_id = await make_item(
        worker.owner_id, store_id, f"Ապրանք-{key}", count=100,
        self_price="0.00", sell_price=amount,
    )
    await sales_service.record_sale(
        worker, [{"item_id": item_id, "quantity": 1}], "cash", f"idem-sale-{key}"
    )


async def _session_of(store_id: int) -> int:
    return await db.fetchval(
        "SELECT id FROM store_sessions WHERE store_id = $1 AND closed_at IS NULL",
        store_id,
    )


async def _cash(store_id: int) -> Decimal:
    totals = await money_repo.totals_for_session(await _session_of(store_id))
    return Decimal(totals["cash"])


async def _ready(cash: str = "9000"):
    """Two open shops, the first holding some takings and the second holding none."""
    owner_id, first, second = await _two_shops()
    sender, _ = await _worker_at(owner_id, first, "Անի", "ani")
    receiver, _ = await _worker_at(owner_id, second, "Գոռ", "gor")
    await _takings(sender, first, cash, "one")
    return owner_id, first, second, sender, receiver


# -- sending -------------------------------------------------------------------

async def test_the_money_leaves_the_sending_drawer_at_once(client):
    """It is physically gone the moment it is handed over, and the person counting
    that drawer tonight must not be held to notes that are in a taxi."""
    _, first, second, sender, _ = await _ready()

    result = await money_transfers_service.send_by_worker(
        sender, second, Decimal("5000"), "idem-mt-01"
    )

    assert result["transfer"]["status"] == "pending"
    assert await _cash(first) == Decimal("4000.00")


async def test_it_does_not_arrive_until_somebody_says_it_has(client):
    """The other half of the same rule. A till that credited itself on somebody
    else's say-so would have the receiving worker answering for money nobody has
    handed them yet."""
    _, _, second, sender, _ = await _ready()

    await money_transfers_service.send_by_worker(
        sender, second, Decimal("5000"), "idem-mt-01"
    )

    assert await _cash(second) == Decimal("0.00")


async def test_confirming_credits_the_receiving_till(client):
    _, _, second, sender, receiver = await _ready()
    result = await money_transfers_service.send_by_worker(
        sender, second, Decimal("5000"), "idem-mt-01"
    )

    answered = await money_transfers_service.decide_by_worker(
        receiver, result["transfer"]["id"], True
    )

    assert answered["transfer"]["status"] == "received"
    assert await _cash(second) == Decimal("5000.00")


async def test_more_than_the_drawer_holds_is_refused(client):
    """Physics, not permission: you cannot put notes in an envelope that are not
    in the drawer."""
    _, first, second, sender, _ = await _ready()

    with pytest.raises(BotError) as raised:
        await money_transfers_service.send_by_worker(
            sender, second, Decimal("12000"), "idem-mt-01"
        )

    assert raised.value.code == "validation_error"
    assert "9,000" in raised.value.message
    assert await _cash(first) == Decimal("9000.00"), "and nothing left the drawer"


async def test_the_whole_drawer_may_be_sent(client):
    """The float is in the till like everything else, and a shop closing early can
    hand its whole drawer to the one staying open."""
    _, first, second, sender, _ = await _ready()

    await money_transfers_service.send_by_worker(
        sender, second, Decimal("9000"), "idem-mt-01"
    )

    assert await _cash(first) == Decimal("0.00")


async def test_a_closed_shop_cannot_be_sent_money(client):
    """Money goes to a person. A shut shop has nobody to hand it to and no session
    to book it into, so it is refused rather than left in limbo."""
    owner_id, first, second = await _two_shops()
    sender, _ = await _worker_at(owner_id, first, "Անի", "ani")
    await _takings(sender, first, "9000", "one")

    with pytest.raises(BotError) as raised:
        await money_transfers_service.send_by_worker(
            sender, second, Decimal("5000"), "idem-mt-01"
        )

    assert raised.value.code == "validation_error"
    assert await _cash(first) == Decimal("9000.00")


async def test_a_shop_cannot_send_money_to_itself(client):
    _, first, _, sender, _ = await _ready()

    with pytest.raises(BotError):
        await money_transfers_service.send_by_worker(
            sender, first, Decimal("1000"), "idem-mt-01"
        )


async def test_sending_twice_under_one_key_sends_once(client):
    """The key belongs to the tap, not to the attempt. A flaky connection must not
    empty the drawer twice."""
    _, first, second, sender, _ = await _ready()

    await money_transfers_service.send_by_worker(
        sender, second, Decimal("5000"), "idem-mt-01"
    )
    again = await money_transfers_service.send_by_worker(
        sender, second, Decimal("5000"), "idem-mt-01"
    )

    assert again["duplicate"] is True
    assert await _cash(first) == Decimal("4000.00")
    assert await db.fetchval("SELECT count(*) FROM money_transfers") == 1


# -- the row the ledger keeps --------------------------------------------------

async def test_both_ledger_rows_name_the_other_shop(client):
    """A withdrawal with no destination on it is the shortfall this whole thing
    exists to prevent, just with a number attached."""
    _, first, second, sender, receiver = await _ready()
    result = await money_transfers_service.send_by_worker(
        sender, second, Decimal("5000"), "idem-mt-01"
    )
    await money_transfers_service.decide_by_worker(
        receiver, result["transfer"]["id"], True
    )

    out = await db.fetchval(
        "SELECT note FROM cash_movements WHERE store_id = $1 AND kind = 'withdrawal'",
        first,
    )
    landed = await db.fetchval(
        "SELECT note FROM cash_movements WHERE store_id = $1 AND kind = 'deposit'"
        " AND note IS NOT NULL",
        second,
    )

    assert "Խանութ 2" in out
    assert "Խանութ 1" in landed


async def test_sending_money_does_not_eat_the_lunch_allowance(client):
    """It is not petty cash — it is the drawer moving — so a cashier who has just
    sent 5,000 across can still buy lunch."""
    from app.services import money as money_service

    _, _, second, sender, _ = await _ready()
    await money_transfers_service.send_by_worker(
        sender, second, Decimal("5000"), "idem-mt-01"
    )

    result = await money_service.withdraw_by_worker(
        sender, Decimal("1000"), "Ճաշ", "idem-cash-01", reason="lunch"
    )

    assert result["ok"] is True


# -- the envelope that did not arrive ------------------------------------------

async def test_denying_puts_the_money_back_where_it_came_from(client):
    """Nothing was spent, so nothing should have left the books."""
    _, first, second, sender, receiver = await _ready()
    result = await money_transfers_service.send_by_worker(
        sender, second, Decimal("5000"), "idem-mt-01"
    )

    answered = await money_transfers_service.decide_by_worker(
        receiver, result["transfer"]["id"], False
    )

    assert answered["transfer"]["status"] == "rejected"
    assert await _cash(first) == Decimal("9000.00")
    assert await _cash(second) == Decimal("0.00")


async def test_it_cannot_be_denied_once_the_sending_shop_has_shut(client):
    """The money would have nowhere honest to go: that till has been settled and
    handed over, and burying a deposit in it would rewrite an evening somebody has
    already answered for. The transfer stays pending for the owner to sort out."""
    _, first, second, sender, receiver = await _ready()
    result = await money_transfers_service.send_by_worker(
        sender, second, Decimal("5000"), "idem-mt-01"
    )
    await shifts_service.close_store_session_as_owner(
        sender.owner_id, await _session_of(first)
    )

    with pytest.raises(BotError) as raised:
        await money_transfers_service.decide_by_worker(
            receiver, result["transfer"]["id"], False
        )

    assert raised.value.code == "validation_error"
    row = await transfers_repo.get(sender.owner_id, result["transfer"]["id"])
    assert row["status"] == "pending", "still answerable"


async def test_a_colleague_answering_first_settles_it(client):
    """Two workers at the destination can be looking at the same envelope. The
    second tap must be told it is answered rather than crediting the till twice."""
    _, _, second, sender, receiver = await _ready()
    result = await money_transfers_service.send_by_worker(
        sender, second, Decimal("5000"), "idem-mt-01"
    )
    await money_transfers_service.decide_by_worker(
        receiver, result["transfer"]["id"], True
    )

    with pytest.raises(BotError):
        await money_transfers_service.decide_by_worker(
            receiver, result["transfer"]["id"], True
        )

    assert await _cash(second) == Decimal("5000.00")


async def test_only_the_shop_it_was_sent_to_may_answer(client):
    """Reads as missing rather than forbidden, like every other cross-shop lookup."""
    _, _, second, sender, _ = await _ready()
    result = await money_transfers_service.send_by_worker(
        sender, second, Decimal("5000"), "idem-mt-01"
    )

    with pytest.raises(BotError) as raised:
        await money_transfers_service.decide_by_worker(
            sender, result["transfer"]["id"], True
        )

    assert raised.value.code == "unknown_item"


# -- telling the other shop ----------------------------------------------------

async def test_the_receiving_shop_is_told_with_the_buttons_to_answer(client):
    """The one thing in the bot where the people who have to act are not the people
    who started the action. Making them go and find the right screen is the
    difference between an answer and a shrug."""
    sent = []

    async def fake_send(chat_id, text, reply_markup=None):
        sent.append((chat_id, text, reply_markup))

    _, _, second, sender, receiver = await _ready()
    with mock.patch.object(telegram, "send_message", fake_send):
        result = await money_transfers_service.send_by_worker(
            sender, second, Decimal("5000"), "idem-mt-01"
        )

    assert len(sent) == 1, "one message, to the one person on shift there"
    _, body, markup = sent[0]
    assert "5,000" in body
    assert "Խանութ 1" in body, "the shop it is coming from"
    assert "Անի" in body, "and who sent it, so there is somebody to ring"
    transfer_id = result["transfer"]["id"]
    assert [
        button["callback_data"] for row in markup["inline_keyboard"] for button in row
    ] == [f"mt:{transfer_id}:y", f"mt:{transfer_id}:n"]


async def test_the_sender_is_told_when_it_comes_back(client):
    """The money is back in their drawer, which changes the figure they are about
    to count. Finding that out at closing time is finding it out too late."""
    sent = []

    async def fake_send(chat_id, text, reply_markup=None):
        sent.append(text)

    _, _, second, sender, receiver = await _ready()
    result = await money_transfers_service.send_by_worker(
        sender, second, Decimal("5000"), "idem-mt-01"
    )
    with mock.patch.object(telegram, "send_message", fake_send):
        await money_transfers_service.decide_by_worker(
            receiver, result["transfer"]["id"], False
        )

    assert any("5,000" in body for body in sent)


async def test_an_undeliverable_message_does_not_undo_the_transfer(client):
    """The row is already written and the money already gone. A nudge that could
    not be delivered is a nudge, not a transaction."""
    async def fake_send(chat_id, text, reply_markup=None):
        raise telegram.Undeliverable("chat not found", blocked=True)

    _, first, second, sender, _ = await _ready()
    with mock.patch.object(telegram, "send_message", fake_send):
        await money_transfers_service.send_by_worker(
            sender, second, Decimal("5000"), "idem-mt-01"
        )

    assert await _cash(first) == Decimal("4000.00")


# -- over the wire -------------------------------------------------------------

async def test_only_open_shops_are_offered(client, bot_headers):
    """And never the worker's own. A closed shop in the list is a dead end with a
    name on it."""
    owner_id, first, second = await _two_shops()
    third = await make_store(
        owner_id, "Խանութ 3", lat=YEREVAN_LAT + 0.02, lng=YEREVAN_LNG + 0.02
    )
    sender, telegram_id = await _worker_at(owner_id, first, "Անի", "ani")
    await _worker_at(owner_id, second, "Գոռ", "gor")
    await _takings(sender, first, "9000", "one")

    body = (await client.get(
        f"{BASE}/cash/transfers/stores",
        params={"telegram_id": telegram_id},
        headers=bot_headers,
    )).json()

    assert [store["id"] for store in body["stores"]] == [second]
    assert third not in [store["id"] for store in body["stores"]]
    assert body["available"] == "9000.00", "so the question can say what there is"


async def test_the_whole_round_trip_over_the_wire(client, bot_headers):
    _, first, second, sender, receiver = await _ready()
    sender_tg = await db.fetchval(
        "SELECT telegram_id FROM workers WHERE id = $1", sender.id
    )
    receiver_tg = await db.fetchval(
        "SELECT telegram_id FROM workers WHERE id = $1", receiver.id
    )

    sent = await client.post(
        f"{BASE}/cash/transfers",
        json={
            "telegram_id": sender_tg,
            "to_store_id": second,
            "amount": "5000",
            "idempotency_key": "idem-mt-wire-01",
        },
        headers=bot_headers,
    )
    assert sent.status_code == 201

    waiting = (await client.get(
        f"{BASE}/cash/transfers/pending",
        params={"telegram_id": receiver_tg},
        headers=bot_headers,
    )).json()
    assert [row["amount"] for row in waiting["incoming"]] == ["5000.00"]
    assert waiting["incoming"][0]["from_store"] == "Խանութ 1"

    transfer_id = sent.json()["transfer"]["id"]
    answered = await client.post(
        f"{BASE}/cash/transfers/{transfer_id}/decide",
        json={"telegram_id": receiver_tg, "accept": True},
        headers=bot_headers,
    )

    assert answered.status_code == 200
    assert answered.json()["transfer"]["status"] == "received"
    assert await _cash(second) == Decimal("5000.00")
    assert await _cash(first) == Decimal("4000.00")


async def test_a_float_amount_is_refused_outright(client, bot_headers):
    """Money travels as a decimal string. A float has already lost digits by the
    time this service sees it."""
    _, _, second, sender, _ = await _ready()
    telegram_id = await db.fetchval(
        "SELECT telegram_id FROM workers WHERE id = $1", sender.id
    )

    response = await client.post(
        f"{BASE}/cash/transfers",
        json={
            "telegram_id": telegram_id,
            "to_store_id": second,
            "amount": 5000.5,
            "idempotency_key": "idem-mt-wire-01",
        },
        headers=bot_headers,
    )

    assert response.status_code == 422


def test_the_buttons_carry_the_prefix_the_bot_listens_for():
    """Minted here, tapped there. CB_MONEY_TRANSFER in bot/app/keyboards.py and
    BTN_MONEY_GOT_IT / BTN_MONEY_MISSING in bot/app/texts.py."""
    assert texts.MONEY_TRANSFER_CALLBACK == "mt"
    assert texts.BTN_MONEY_TRANSFER_GOT_IT == "✅ Ստացա"
    assert texts.BTN_MONEY_TRANSFER_MISSING == "❌ Չեմ ստացել"
    assert texts.money_transfer_buttons(7)["inline_keyboard"][0][0]["callback_data"] == (
        "mt:7:y"
    )
