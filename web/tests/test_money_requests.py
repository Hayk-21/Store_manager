"""Asking another shop, or the owner, for cash.

The mirror of a transfer, and the one the drawer running dry actually needs. A
worker locking up on 2,000 with a 5,000 wage due goes home short *and* the shop
opens tomorrow with no change; the money is in a sister shop's drawer or the owner's
pocket, and this is how they say so from behind the counter.

Two things are worth most of the tests here. Saying yes has to actually move the
money — an acceptance that moved nothing is indistinguishable from a refusal at the
far end — and it moves it by creating exactly the transfer the asking shop then
confirms, so there is one way for money to arrive and not two. And the owner answers
as themselves: no drawer, nothing booked at their end, resolved from the Telegram
account that tapped rather than from anything the request declared.
"""

from __future__ import annotations

from decimal import Decimal
from unittest import mock

import pytest

from app import texts
from app.db import db
from app.errors import BotError
from app.repo import money as money_repo
from app.repo import money_requests as requests_repo
from app.repo import money_transfers as transfers_repo
from app.services import money_requests as requests_service
from app.services import money_transfers as transfers_service
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
OWNER_TG = 999_000_111


async def _two_shops():
    """One owner with a bound Telegram chat, and two shops a kilometre apart.

    The distance matters: opening a shift geofences to the *nearest* shop, so two at
    one coordinate would put both workers in the same one.
    """
    owner_id = await make_owner("@ownerhandle")
    await db.execute(
        "UPDATE users SET telegram_id = $2, telegram_bound_at = now() WHERE id = $1",
        owner_id, OWNER_TG,
    )
    first = await make_store(owner_id, "Խանութ 1", lat=YEREVAN_LAT, lng=YEREVAN_LNG)
    second = await make_store(
        owner_id, "Խանութ 2", lat=YEREVAN_LAT + 0.01, lng=YEREVAN_LNG + 0.01
    )
    return owner_id, first, second


async def _worker_at(owner_id: int, store_id: int, name: str, key: str):
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


async def _ready(theirs: str = "9000"):
    """Two open shops. The second is holding takings; the first has an empty drawer
    and is the one doing the asking."""
    owner_id, first, second = await _two_shops()
    asker, asker_tg = await _worker_at(owner_id, first, "Անի", "ani")
    giver, giver_tg = await _worker_at(owner_id, second, "Գոռ", "gor")
    await _takings(giver, second, theirs, "one")
    return owner_id, first, second, asker, giver, asker_tg, giver_tg


# -- asking --------------------------------------------------------------------

async def test_asking_moves_nothing_yet(client):
    """The point of it being a request. Until somebody agrees, both drawers are
    exactly as they were."""
    _, first, second, asker, _, _, _ = await _ready()

    result = await requests_service.ask(asker, str(second), Decimal("5000"), "idem-mq-01")

    assert result["request"]["status"] == "pending"
    assert await _cash(first) == Decimal("0.00")
    assert await _cash(second) == Decimal("9000.00")


async def test_the_owner_can_be_asked_and_has_no_shop(client):
    """They are the answer when every other till is as empty as this one, so they are
    always on the list — there is no shop of theirs to be open or shut."""
    _, _, _, asker, _, _, _ = await _ready()

    result = await requests_service.ask(
        asker, requests_service.OWNER, Decimal("5000"), "idem-mq-01"
    )

    assert result["request"]["asked_the_owner"] is True
    assert result["request"]["asked_of"] is None


async def test_a_shop_cannot_ask_itself(client):
    _, first, _, asker, _, _, _ = await _ready()

    with pytest.raises(BotError):
        await requests_service.ask(asker, str(first), Decimal("5000"), "idem-mq-01")


async def test_a_closed_shop_cannot_be_asked(client):
    """There is nobody there to answer, and nobody to take it out of the drawer."""
    owner_id, first, second = await _two_shops()
    asker, _ = await _worker_at(owner_id, first, "Անի", "ani")

    with pytest.raises(BotError) as raised:
        await requests_service.ask(asker, str(second), Decimal("5000"), "idem-mq-01")

    assert raised.value.code == "validation_error"


async def test_asking_twice_under_one_key_asks_once(client):
    _, _, second, asker, _, _, _ = await _ready()

    await requests_service.ask(asker, str(second), Decimal("5000"), "idem-mq-01")
    again = await requests_service.ask(asker, str(second), Decimal("5000"), "idem-mq-01")

    assert again["duplicate"] is True
    assert await db.fetchval("SELECT count(*) FROM money_requests") == 1


# -- a shop answering ----------------------------------------------------------

async def test_accepting_takes_the_money_out_and_starts_a_transfer(client):
    """An acceptance that moved nothing would be indistinguishable from a refusal at
    the far end. It comes out of the giving drawer now, and arrives at the other only
    when somebody there says it has."""
    _, first, second, asker, _, _, giver_tg = await _ready()
    asked = await requests_service.ask(
        asker, str(second), Decimal("5000"), "idem-mq-01"
    )

    answered = await requests_service.decide(giver_tg, asked["request"]["id"], True)

    assert answered["request"]["status"] == "accepted"
    assert await _cash(second) == Decimal("4000.00"), "out of the giving drawer"
    assert await _cash(first) == Decimal("0.00"), "and not yet into the asking one"


async def test_the_asking_shop_confirms_it_the_ordinary_way(client):
    """The acceptance creates exactly the transfer a sent envelope creates, so money
    asked for and money sent arrive by one route and not two."""
    owner_id, first, second, asker, _, _, giver_tg = await _ready()
    asked = await requests_service.ask(
        asker, str(second), Decimal("5000"), "idem-mq-01"
    )
    await requests_service.decide(giver_tg, asked["request"]["id"], True)

    request = await requests_repo.get(owner_id, asked["request"]["id"])
    await transfers_service.decide_by_worker(asker, request["transfer_id"], True)

    assert await _cash(first) == Decimal("5000.00")


async def test_a_drawer_that_cannot_cover_it_refuses(client):
    """Physics, not permission. Checked when they answer rather than when they were
    asked: the money leaves at this moment, and the till as it stands now is the only
    one that counts."""
    _, _, second, asker, _, _, giver_tg = await _ready(theirs="3000")
    asked = await requests_service.ask(
        asker, str(second), Decimal("5000"), "idem-mq-01"
    )

    with pytest.raises(BotError) as raised:
        await requests_service.decide(giver_tg, asked["request"]["id"], True)

    assert raised.value.code == "validation_error"
    assert "3,000" in raised.value.message
    assert await _cash(second) == Decimal("3000.00"), "and nothing left the drawer"


async def test_refusing_moves_nothing_and_creates_no_transfer(client):
    _, _, second, asker, _, _, giver_tg = await _ready()
    asked = await requests_service.ask(
        asker, str(second), Decimal("5000"), "idem-mq-01"
    )

    answered = await requests_service.decide(giver_tg, asked["request"]["id"], False)

    assert answered["request"]["status"] == "rejected"
    assert await _cash(second) == Decimal("9000.00")
    assert await db.fetchval("SELECT count(*) FROM money_transfers") == 0


async def test_only_the_shop_that_was_asked_may_answer(client):
    """Reads as missing rather than forbidden, like every other cross-shop lookup."""
    _, _, second, asker, _, asker_tg, _ = await _ready()
    asked = await requests_service.ask(
        asker, str(second), Decimal("5000"), "idem-mq-01"
    )

    with pytest.raises(BotError) as raised:
        await requests_service.decide(asker_tg, asked["request"]["id"], True)

    assert raised.value.code == "unknown_item"


async def test_a_colleague_answering_first_settles_it(client):
    _, _, second, asker, _, _, giver_tg = await _ready()
    asked = await requests_service.ask(
        asker, str(second), Decimal("5000"), "idem-mq-01"
    )
    await requests_service.decide(giver_tg, asked["request"]["id"], True)

    with pytest.raises(BotError):
        await requests_service.decide(giver_tg, asked["request"]["id"], True)

    assert await _cash(second) == Decimal("4000.00"), "taken out once"


# -- the owner answering -------------------------------------------------------

async def test_the_owner_accepting_books_nothing_anywhere(client):
    """Their money comes out of no till. There is nothing to take out and nothing to
    give back — only the promise that it is on its way."""
    owner_id, first, second, asker, _, _, _ = await _ready()
    asked = await requests_service.ask(
        asker, requests_service.OWNER, Decimal("5000"), "idem-mq-01"
    )

    answered = await requests_service.decide(OWNER_TG, asked["request"]["id"], True)

    assert answered["request"]["status"] == "accepted"
    assert await _cash(second) == Decimal("9000.00"), "no shop paid for this"
    assert await _cash(first) == Decimal("0.00"), "and it has not arrived yet"
    transfer = await db.fetchrow("SELECT from_store_id, from_session_id FROM money_transfers")
    assert transfer["from_store_id"] is None, "money from the owner has no drawer"
    assert transfer["from_session_id"] is None


async def test_the_owners_money_lands_when_the_shop_confirms_it(client):
    owner_id, first, _, asker, _, _, _ = await _ready()
    asked = await requests_service.ask(
        asker, requests_service.OWNER, Decimal("5000"), "idem-mq-01"
    )
    await requests_service.decide(OWNER_TG, asked["request"]["id"], True)

    request = await requests_repo.get(owner_id, asked["request"]["id"])
    await transfers_service.decide_by_worker(asker, request["transfer_id"], True)

    assert await _cash(first) == Decimal("5000.00")
    note = await db.fetchval(
        "SELECT note FROM cash_movements WHERE store_id = $1 AND kind = 'deposit'"
        " AND note IS NOT NULL",
        first,
    )
    assert note == transfers_service.NOTE_FROM_THE_OWNER, (
        "a reader of the ledger can tell the owner funding a shop from a sister shop"
    )


async def test_the_owners_money_that_never_arrived_gives_nothing_back(client):
    """There was no drawer it came out of, so there is nowhere to put it back. It is
    marked and the owner is told; what happened to it is between them and whoever was
    supposed to have carried it."""
    owner_id, first, _, asker, _, _, _ = await _ready()
    asked = await requests_service.ask(
        asker, requests_service.OWNER, Decimal("5000"), "idem-mq-01"
    )
    await requests_service.decide(OWNER_TG, asked["request"]["id"], True)
    request = await requests_repo.get(owner_id, asked["request"]["id"])

    await transfers_service.decide_by_worker(asker, request["transfer_id"], False)

    row = await transfers_repo.get(owner_id, request["transfer_id"])
    assert row["status"] == "rejected"
    assert await _cash(first) == Decimal("0.00")
    assert await db.fetchval(
        "SELECT count(*) FROM cash_movements WHERE kind = 'deposit' AND note IS NOT NULL"
    ) == 0


async def test_a_worker_cannot_answer_a_request_made_of_the_owner(client):
    """It was not theirs to give, and their drawer is not the one being asked for."""
    _, _, _, asker, _, _, giver_tg = await _ready()
    asked = await requests_service.ask(
        asker, requests_service.OWNER, Decimal("5000"), "idem-mq-01"
    )

    with pytest.raises(BotError) as raised:
        await requests_service.decide(giver_tg, asked["request"]["id"], True)

    assert raised.value.code == "unknown_item"


async def test_a_stranger_cannot_answer_anything(client):
    _, _, second, asker, _, _, _ = await _ready()
    asked = await requests_service.ask(
        asker, str(second), Decimal("5000"), "idem-mq-01"
    )

    with pytest.raises(BotError) as raised:
        await requests_service.decide(123_456_789, asked["request"]["id"], True)

    assert raised.value.code == "unknown_worker"


# -- telling people ------------------------------------------------------------

async def test_the_shop_being_asked_gets_the_question_with_its_buttons(client):
    """The person who has to act did not start the action, and a message that only
    says "go and look somewhere" gets looked at and forgotten."""
    sent = []

    async def fake_send(chat_id, text, reply_markup=None):
        sent.append((chat_id, text, reply_markup))

    _, _, second, asker, _, _, giver_tg = await _ready()
    with mock.patch.object(telegram, "send_message", fake_send):
        asked = await requests_service.ask(
            asker, str(second), Decimal("5000"), "idem-mq-01"
        )

    assert [chat_id for chat_id, _, _ in sent] == [giver_tg]
    _, body, markup = sent[0]
    assert "5,000" in body
    assert "Խանութ 1" in body, "the shop asking"
    assert "Անի" in body, "and who is asking, so there is somebody to ring"
    request_id = asked["request"]["id"]
    assert [
        button["callback_data"] for row in markup["inline_keyboard"] for button in row
    ] == [f"mq:{request_id}:y", f"mq:{request_id}:n"]


async def test_a_request_of_the_owner_goes_to_the_owners_own_chat(client):
    sent = []

    async def fake_send(chat_id, text, reply_markup=None):
        sent.append((chat_id, text))

    _, _, _, asker, _, _, _ = await _ready()
    with mock.patch.object(telegram, "send_message", fake_send):
        await requests_service.ask(
            asker, requests_service.OWNER, Decimal("5000"), "idem-mq-01"
        )

    assert [chat_id for chat_id, _ in sent] == [OWNER_TG]


async def test_the_asking_shop_is_told_the_answer(client):
    """They asked because the drawer could not cover something. A refusal they never
    hear about is a shop waiting for money that is not coming."""
    sent = []

    async def fake_send(chat_id, text, reply_markup=None):
        sent.append(text)

    _, _, second, asker, _, asker_tg, giver_tg = await _ready()
    asked = await requests_service.ask(
        asker, str(second), Decimal("5000"), "idem-mq-01"
    )
    with mock.patch.object(telegram, "send_message", fake_send):
        await requests_service.decide(giver_tg, asked["request"]["id"], False)

    assert any("5,000" in body and "Խանութ 2" in body for body in sent)


async def test_an_undeliverable_nudge_does_not_undo_the_request(client):
    async def fake_send(chat_id, text, reply_markup=None):
        raise telegram.Undeliverable("chat not found", blocked=True)

    _, _, second, asker, _, _, _ = await _ready()
    with mock.patch.object(telegram, "send_message", fake_send):
        result = await requests_service.ask(
            asker, str(second), Decimal("5000"), "idem-mq-01"
        )

    assert result["request"]["status"] == "pending"
    assert await db.fetchval("SELECT count(*) FROM money_requests") == 1


# -- over the wire -------------------------------------------------------------

async def test_who_can_be_asked_lists_the_open_shops(client, bot_headers):
    owner_id, first, second = await _two_shops()
    await make_store(owner_id, "Խանութ 3", lat=YEREVAN_LAT + 0.02, lng=YEREVAN_LNG + 0.02)
    _, asker_tg = await _worker_at(owner_id, first, "Անի", "ani")
    await _worker_at(owner_id, second, "Գոռ", "gor")

    body = (await client.get(
        f"{BASE}/cash/requests/who",
        params={"telegram_id": asker_tg},
        headers=bot_headers,
    )).json()

    assert [store["id"] for store in body["stores"]] == [second]


async def test_the_whole_round_trip_over_the_wire(client, bot_headers):
    _, first, second, _, _, asker_tg, giver_tg = await _ready()

    asked = await client.post(
        f"{BASE}/cash/requests",
        json={
            "telegram_id": asker_tg,
            "asked_of": str(second),
            "amount": "5000",
            "idempotency_key": "idem-mq-wire-01",
        },
        headers=bot_headers,
    )
    assert asked.status_code == 201

    waiting = (await client.get(
        f"{BASE}/cash/requests/pending",
        params={"telegram_id": giver_tg},
        headers=bot_headers,
    )).json()
    assert [row["amount"] for row in waiting["incoming"]] == ["5000.00"]
    assert waiting["incoming"][0]["store"] == "Խանութ 1"

    request_id = asked.json()["request"]["id"]
    answered = await client.post(
        f"{BASE}/cash/requests/{request_id}/decide",
        json={"telegram_id": giver_tg, "accept": True},
        headers=bot_headers,
    )

    assert answered.status_code == 200
    assert answered.json()["request"]["status"] == "accepted"
    assert await _cash(second) == Decimal("4000.00")


async def test_the_owner_answers_over_the_wire_without_being_a_worker(client, bot_headers):
    """The one call the bot makes that is not a worker's. The owner has no worker row
    at all, and the endpoint resolves them from the Telegram account that tapped."""
    _, _, _, _, _, asker_tg, _ = await _ready()
    asked = await client.post(
        f"{BASE}/cash/requests",
        json={
            "telegram_id": asker_tg,
            "asked_of": "owner",
            "amount": "5000",
            "idempotency_key": "idem-mq-wire-01",
        },
        headers=bot_headers,
    )

    answered = await client.post(
        f"{BASE}/cash/requests/{asked.json()['request']['id']}/decide",
        json={"telegram_id": OWNER_TG, "accept": True},
        headers=bot_headers,
    )

    assert answered.status_code == 200
    assert answered.json()["request"]["status"] == "accepted"


async def test_a_float_amount_is_refused_outright(client, bot_headers):
    """Money travels as a decimal string. A float has already lost digits by the time
    this service sees it."""
    _, _, second, _, _, asker_tg, _ = await _ready()

    response = await client.post(
        f"{BASE}/cash/requests",
        json={
            "telegram_id": asker_tg,
            "asked_of": str(second),
            "amount": 5000.5,
            "idempotency_key": "idem-mq-wire-01",
        },
        headers=bot_headers,
    )

    assert response.status_code == 422


def test_the_buttons_carry_the_prefix_the_bot_listens_for():
    """Minted here, tapped there — and, uniquely, tapped by the owner. CB_MONEY_REQUEST
    in bot/app/keyboards.py and BTN_MONEY_ASK_YES / BTN_MONEY_ASK_NO in bot/app/texts.py.
    """
    assert texts.MONEY_REQUEST_CALLBACK == "mq"
    assert texts.BTN_MONEY_REQUEST_YES == "✅ Հաստատել"
    assert texts.BTN_MONEY_REQUEST_NO == "❌ Մերժել"
    assert texts.money_request_buttons(7)["inline_keyboard"][0][0]["callback_data"] == (
        "mq:7:y"
    )
