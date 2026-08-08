"""Putting a new product on the shelf, from the counter.

Four short answers and a commit. The one that earns its own tests is the cost
price: it is asked for rather than defaulted to zero, because an item with no
cost reports its whole selling price as profit — a wrong number is worse than a
missing one, since nothing on the statistics page says it is wrong.
"""

from __future__ import annotations

from decimal import Decimal
from unittest import mock

from telegram import Chat, Message, Update, User
from telegram.ext import ConversationHandler

from app import texts
from app.api import ApiError, ApiUnavailable
from app.handlers import newitem


def _server_answer() -> dict:
    """Exactly what the web service sends back for ``POST /items``."""
    return {
        "ok": True,
        "item": {"id": 12, "name": "HQD Cuvie", "count": 20, "sell_price": "3500.00"},
    }


class _Context:
    def __init__(self, **data) -> None:
        self.user_data = dict(data)


def _typed(text: str) -> Update:
    user = User(id=1, first_name="Հայկ", is_bot=False)
    return Update(
        update_id=1,
        message=Message(
            message_id=1, date=None, chat=Chat(id=1, type="private"),
            from_user=user, text=text,
        ),
    )


# -- the four answers ---------------------------------------------------------

async def test_the_answers_are_collected_in_order():
    async def fake_reply(self, *args, **kwargs):
        return None

    context = _Context()
    with mock.patch.object(Message, "reply_text", fake_reply):
        assert await newitem.type_name(_typed("HQD Cuvie"), context) == newitem.ASK_COUNT
        assert await newitem.type_count(_typed("20"), context) == newitem.ASK_COST
        assert await newitem.type_cost(_typed("1500"), context) == newitem.ASK_PRICE

    assert context.user_data["new_name"] == "HQD Cuvie"
    assert context.user_data["new_count"] == 20
    assert context.user_data["new_cost"] == Decimal("1500.00")


async def test_a_blank_name_asks_again():
    replies = []

    async def fake_reply(self, text, *args, **kwargs):
        replies.append(text)

    context = _Context()
    with mock.patch.object(Message, "reply_text", fake_reply):
        state = await newitem.type_name(_typed("   "), context)

    assert state == newitem.ASK_NAME
    assert replies == [texts.NEW_ITEM_ASK_NAME_AGAIN]


async def test_a_count_that_is_not_a_number_asks_again():
    replies = []

    async def fake_reply(self, text, *args, **kwargs):
        replies.append(text)

    context = _Context(new_name="HQD Cuvie")
    with mock.patch.object(Message, "reply_text", fake_reply):
        state = await newitem.type_count(_typed("մի քանի"), context)

    assert state == newitem.ASK_COUNT
    assert replies == [texts.BAD_QUANTITY]


async def test_a_negative_count_asks_again():
    replies = []

    async def fake_reply(self, text, *args, **kwargs):
        replies.append(text)

    context = _Context(new_name="HQD Cuvie")
    with mock.patch.object(Message, "reply_text", fake_reply):
        state = await newitem.type_count(_typed("-3"), context)

    assert state == newitem.ASK_COUNT
    assert replies == [texts.BAD_QUANTITY]


async def test_a_cost_that_is_not_a_number_asks_again():
    """Guessing zero here would put the item's whole price in the profit
    column, and nothing downstream would say so."""
    replies = []

    async def fake_reply(self, text, *args, **kwargs):
        replies.append(text)

    context = _Context(new_name="HQD Cuvie", new_count=20)
    with mock.patch.object(Message, "reply_text", fake_reply):
        state = await newitem.type_cost(_typed("չգիտեմ"), context)

    assert state == newitem.ASK_COST
    assert replies == [texts.CLOSEOUT_BAD_PRICE]
    assert "new_cost" not in context.user_data


# -- the commit ---------------------------------------------------------------

async def test_the_whole_product_reaches_the_server():
    sent = {}

    async def fake_add(**kwargs):
        sent.update(kwargs)
        return _server_answer()

    async def fake_reply(self, *args, **kwargs):
        return None

    context = _Context(new_name="HQD Cuvie", new_count=20, new_cost=Decimal("1500.00"))
    with (
        mock.patch.object(newitem.api, "add_item", fake_add),
        mock.patch.object(Message, "reply_text", fake_reply),
    ):
        state = await newitem.type_price(_typed("3500"), context)

    assert state == ConversationHandler.END
    assert sent["name"] == "HQD Cuvie"
    assert sent["count"] == 20
    assert sent["self_price"] == "1500.00", "money travels as a string, never a float"
    assert sent["sell_price"] == "3500.00"
    assert context.user_data == {}


async def test_a_name_already_on_the_list_sends_them_back_to_the_name():
    """Not the whole thing again: three of the four answers are still right."""
    replies = []

    async def fake_add(**kwargs):
        raise ApiError("validation_error", "«HQD Cuvie» արդեն կա այս խանութի ցուցակում։")

    async def fake_reply(self, text, *args, **kwargs):
        replies.append(text)

    context = _Context(new_name="HQD Cuvie", new_count=20, new_cost=Decimal("1500.00"))
    with (
        mock.patch.object(newitem.api, "add_item", fake_add),
        mock.patch.object(Message, "reply_text", fake_reply),
    ):
        state = await newitem.type_price(_typed("3500"), context)

    assert state == newitem.ASK_NAME
    assert "«HQD Cuvie» արդեն կա այս խանութի ցուցակում։" in replies


async def test_a_network_failure_ends_the_flow():
    async def fake_add(**kwargs):
        raise ApiUnavailable()

    async def fake_reply(self, *args, **kwargs):
        return None

    context = _Context(new_name="HQD Cuvie", new_count=20, new_cost=Decimal("1500.00"))
    with (
        mock.patch.object(newitem.api, "add_item", fake_add),
        mock.patch.object(Message, "reply_text", fake_reply),
    ):
        state = await newitem.type_price(_typed("3500"), context)

    assert state == ConversationHandler.END
    assert context.user_data == {}


async def test_a_saved_item_is_never_reported_as_a_failure():
    """The product is on the shelf. A field missing from the reply is cosmetic,
    and "failed" would send the cashier off to add it a second time."""
    replies = []

    async def fake_add(**kwargs):
        return {"ok": True}          # a shape the confirmation cannot render

    async def fake_reply(self, text, *args, **kwargs):
        replies.append(text)

    context = _Context(new_name="HQD Cuvie", new_count=20, new_cost=Decimal("1500.00"))
    with (
        mock.patch.object(newitem.api, "add_item", fake_add),
        mock.patch.object(Message, "reply_text", fake_reply),
    ):
        state = await newitem.type_price(_typed("3500"), context)

    assert state == ConversationHandler.END
    assert replies == [texts.NEW_ITEM_DONE_PLAINLY]
    assert "սխալ" not in replies[0].lower()
