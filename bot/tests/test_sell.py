"""Selling from the bot, and the shape of the answer it gets back.

The payload here is copied from what the web service actually returns for
``POST /sale`` — see ``_sale_payload`` in ``web/app/services/sales.py``. The
first version of this flow read ``result["items"]``, which does not exist, and
the failure was the expensive kind: the sale committed, then the bot crashed
rendering the reply and told the cashier it had failed. They would have entered
it again.

So there are two things under test. That the confirmation is built from the real
shape, and that *nothing after the server accepts the sale* can report a failure.
"""

from __future__ import annotations

from decimal import Decimal
from unittest import mock

import pytest
from telegram import CallbackQuery, Chat, Message, Update, User
from telegram.ext import ConversationHandler

from app import texts
from app.api import ApiError
from app.handlers import sell


def _server_answer(duplicate: bool = False) -> dict:
    """Exactly what the web service sends back for a one-line sale."""
    return {
        "ok": True,
        "duplicate": duplicate,
        "sale": {
            "id": 41,
            "store_id": 7,
            "session_id": 12,
            "payment_method": "cash",
            "total": "7000.00",
            "sold_at": "2026-08-08T13:28:36+04:00",
            "voided": False,
            "lines": [
                {
                    "item_id": 3,
                    "name": "HQD Cuvie",
                    "quantity": 2,
                    "unit_price": "3500.00",
                    "line_total": "7000.00",
                    "remaining_count": 15,
                }
            ],
        },
        "store_totals": {"cash": "27000.00", "card": "4000.00"},
    }


class _Context:
    def __init__(self, **data) -> None:
        self.user_data = dict(data)


def _tap(data: str) -> Update:
    user = User(id=1, first_name="Հայկ", is_bot=False)
    message = Message(
        message_id=1, date=None, chat=Chat(id=1, type="private"), from_user=user
    )
    return Update(
        update_id=1,
        callback_query=CallbackQuery(
            id="1", from_user=user, chat_instance="1", data=data, message=message
        ),
    )


# -- the confirmation ---------------------------------------------------------

def test_the_confirmation_is_built_from_the_real_payload():
    """The regression. Reading a key the server does not send crashed *after*
    the sale was committed."""
    body = sell._confirmation(_server_answer(), "cash")

    assert "HQD Cuvie" in body
    assert "7,000" in body
    assert "15" in body, "what is left on the shelf"
    assert texts.BTN_CASH in body


def test_a_replay_says_so_and_still_shows_the_sale():
    body = sell._confirmation(_server_answer(duplicate=True), "cash")

    assert texts.SALE_ALREADY_RECORDED in body
    assert "HQD Cuvie" in body


@pytest.mark.parametrize(
    "broken",
    [
        {"ok": True},                                   # nothing at all
        {"ok": True, "sale": {}},                       # no lines
        {"ok": True, "sale": {"lines": []}},            # empty lines
        {"ok": True, "sale": {"lines": [{}]}, "store_totals": {}},  # empty line
    ],
)
def test_a_confirmation_it_cannot_build_still_reads_as_success(broken):
    """The receipt is already written. A field missing from the response is a
    cosmetic problem, and saying "failed" would be the opposite of the truth."""
    body = sell._confirmation(broken, "cash")

    assert body == texts.SALE_RECORDED_PLAINLY
    assert "սխալ" not in body.lower()


# -- the wholesale button -----------------------------------------------------

def test_an_item_with_a_wholesale_price_offers_it():
    """It went missing once by being absent from the item search payload, so
    every product looked like one nobody sells wholesale."""
    from app import keyboards

    markup = keyboards.suggested_prices(
        {"sell_price": "3500.00", "wholesale_price": "3000.00"}
    )

    labels = [b.text for row in markup.inline_keyboard for b in row]
    assert any(texts.BTN_WHOLESALE in label for label in labels)
    assert any("3,000" in label for label in labels), "the actual wholesale price"


def test_an_item_without_one_does_not_offer_it():
    """Blank means "not sold wholesale", which is an answer, not a gap."""
    from app import keyboards

    markup = keyboards.suggested_prices({"sell_price": "3500.00", "wholesale_price": None})

    labels = [b.text for row in markup.inline_keyboard for b in row]
    assert not any(texts.BTN_WHOLESALE in label for label in labels)


async def test_tapping_wholesale_takes_the_wholesale_price():
    async def noop(*args, **kwargs):
        return None

    async def fake_reply(self, *args, **kwargs):
        return None

    context = _Context(
        sell_item={"id": 3, "name": "HQD Cuvie", "count": 9,
                   "sell_price": "3500.00", "wholesale_price": "3000.00"},
        sell_qty=2,
    )

    with (
        mock.patch.object(CallbackQuery, "answer", noop),
        mock.patch.object(CallbackQuery, "edit_message_reply_markup", noop),
        mock.patch.object(Message, "reply_text", fake_reply),
    ):
        await sell.choose_suggested_price(_tap("k:wholesale"), context)

    assert context.user_data["sell_price"] == Decimal("3000.00")
    assert context.user_data["sell_kind"] == "wholesale"


async def test_tapping_retail_takes_the_shelf_price():
    async def noop(*args, **kwargs):
        return None

    async def fake_reply(self, *args, **kwargs):
        return None

    context = _Context(
        sell_item={"id": 3, "name": "HQD Cuvie", "count": 9,
                   "sell_price": "3500.00", "wholesale_price": "3000.00"},
        sell_qty=1,
    )

    with (
        mock.patch.object(CallbackQuery, "answer", noop),
        mock.patch.object(CallbackQuery, "edit_message_reply_markup", noop),
        mock.patch.object(Message, "reply_text", fake_reply),
    ):
        await sell.choose_suggested_price(_tap("k:retail"), context)

    assert context.user_data["sell_price"] == Decimal("3500.00")
    assert context.user_data["sell_kind"] == "retail"


# -- committing ---------------------------------------------------------------

async def test_a_recorded_sale_is_never_reported_as_a_failure():
    """Everything after the server accepts the sale is presentation. If any of
    it raises, the cashier must still be told the sale went through."""
    replies = []

    async def fake_sell(**kwargs):
        return {"ok": True}          # a shape the confirmation cannot render

    async def fake_reply(self, text, *args, **kwargs):
        replies.append(text)

    async def noop(*args, **kwargs):
        return None

    context = _Context(sell_item={"id": 3, "name": "HQD Cuvie"}, sell_qty=2,
                       sell_price=Decimal("3500.00"), sell_kind="retail")

    with (
        mock.patch.object(sell.api, "sell", fake_sell),
        mock.patch.object(CallbackQuery, "answer", noop),
        mock.patch.object(CallbackQuery, "edit_message_reply_markup", noop),
        mock.patch.object(Message, "reply_text", fake_reply),
    ):
        state = await sell.choose_method(_tap("p:cash"), context)

    assert state == ConversationHandler.END
    assert replies == [texts.SALE_RECORDED_PLAINLY]
    assert context.user_data == {}, "the basket is cleared either way"


async def test_a_refusal_from_the_server_is_reported_verbatim():
    """The other direction: when the sale genuinely did not happen, say so in
    the server's own words rather than inventing a sentence."""
    replies = []

    async def fake_sell(**kwargs):
        raise ApiError("insufficient_stock", "«HQD Cuvie» — պահեստում կա 1 հատ։")

    async def fake_reply(self, text, *args, **kwargs):
        replies.append(text)

    async def noop(*args, **kwargs):
        return None

    context = _Context(sell_item={"id": 3, "name": "HQD Cuvie"}, sell_qty=9,
                       sell_price=Decimal("3500.00"))

    with (
        mock.patch.object(sell.api, "sell", fake_sell),
        mock.patch.object(CallbackQuery, "answer", noop),
        mock.patch.object(CallbackQuery, "edit_message_reply_markup", noop),
        mock.patch.object(Message, "reply_text", fake_reply),
    ):
        state = await sell.choose_method(_tap("p:cash"), context)

    assert state == ConversationHandler.END
    assert replies == ["«HQD Cuvie» — պահեստում կա 1 հատ։"]


async def test_the_price_and_its_kind_reach_the_server():
    """Picking «Մեծածախ» has to arrive as a wholesale line, or the reports
    cannot tell a wholesale run from a discount."""
    sent = {}

    async def fake_sell(**kwargs):
        sent.update(kwargs)
        return _server_answer()

    async def noop(*args, **kwargs):
        return None

    async def fake_reply(self, *args, **kwargs):
        return None

    context = _Context(sell_item={"id": 3, "name": "HQD Cuvie"}, sell_qty=2,
                       sell_price=Decimal("3000.00"), sell_kind="wholesale")

    with (
        mock.patch.object(sell.api, "sell", fake_sell),
        mock.patch.object(CallbackQuery, "answer", noop),
        mock.patch.object(CallbackQuery, "edit_message_reply_markup", noop),
        mock.patch.object(Message, "reply_text", fake_reply),
    ):
        await sell.choose_method(_tap("p:card"), context)

    assert sent["price_kind"] == "wholesale"
    assert sent["unit_price"] == "3000.00"
    assert sent["payment_method"] == "card"
    assert sent["quantity"] == 2


async def test_one_sale_keeps_one_key_across_retries():
    """The idempotency key is minted once per sale, so a retry after a timeout
    resolves to the receipt already written instead of selling twice."""
    keys = []

    async def fake_sell(**kwargs):
        keys.append(kwargs["key"])
        raise ApiError("internal", "…")

    async def noop(*args, **kwargs):
        return None

    async def fake_reply(self, *args, **kwargs):
        return None

    context = _Context(sell_item={"id": 3, "name": "HQD Cuvie"}, sell_qty=1,
                       sell_price=Decimal("3500.00"))

    with (
        mock.patch.object(sell.api, "sell", fake_sell),
        mock.patch.object(CallbackQuery, "answer", noop),
        mock.patch.object(CallbackQuery, "edit_message_reply_markup", noop),
        mock.patch.object(Message, "reply_text", fake_reply),
    ):
        await sell.choose_method(_tap("p:cash"), context)
        # The failure left the key behind so the next attempt reuses it.
        context.user_data.update(
            sell_item={"id": 3, "name": "HQD Cuvie"}, sell_qty=1,
            sell_price=Decimal("3500.00"), sell_key=keys[0],
        )
        await sell.choose_method(_tap("p:cash"), context)

    assert keys[0] == keys[1]
