"""Putting a new product on the shelf, from the counter.

Four short answers and a commit. The one that earns its own tests is the cost
price: it is asked for rather than defaulted to zero, because an item with no
cost reports its whole selling price as profit — a wrong number is worse than a
missing one, since nothing on the statistics page says it is wrong.
"""

from __future__ import annotations

from decimal import Decimal
from unittest import mock

from telegram import CallbackQuery, Chat, Message, Update, User
from telegram.ext import ConversationHandler

from app import texts
from app.api import ApiError, ApiUnavailable
from app.handlers import newitem


def _server_answer(wholesale: str | None = "3000.00") -> dict:
    """Exactly what the web service sends back for ``POST /items``."""
    return {
        "ok": True,
        "item": {"id": 12, "name": "HQD Cuvie", "count": 20,
                 "sell_price": "3500.00", "wholesale_price": wholesale},
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


def _ready(**overrides) -> _Context:
    """Everything answered except the wholesale price."""
    return _Context(**{"new_name": "HQD Cuvie", "new_count": 20,
                       "new_cost": Decimal("1500.00"),
                       "new_price": Decimal("3500.00")} | overrides)


# -- the five answers ---------------------------------------------------------

async def test_the_answers_are_collected_in_order():
    async def fake_reply(self, *args, **kwargs):
        return None

    context = _Context()
    with mock.patch.object(Message, "reply_text", fake_reply):
        assert await newitem.type_name(_typed("HQD Cuvie"), context) == newitem.ASK_COUNT
        assert await newitem.type_count(_typed("20"), context) == newitem.ASK_COST
        assert await newitem.type_cost(_typed("1500"), context) == newitem.ASK_PRICE
        assert await newitem.type_price(_typed("3500"), context) == newitem.ASK_WHOLESALE

    assert context.user_data["new_name"] == "HQD Cuvie"
    assert context.user_data["new_count"] == 20
    assert context.user_data["new_cost"] == Decimal("1500.00")
    assert context.user_data["new_price"] == Decimal("3500.00")


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


async def test_the_retail_price_does_not_commit_on_its_own():
    """It used to be the last answer. Now the wholesale one comes after it, and
    nothing may be written until that question has been put."""
    added = []

    async def fake_add(**kwargs):
        added.append(kwargs)
        return _server_answer()

    async def fake_reply(self, *args, **kwargs):
        return None

    context = _Context(new_name="HQD Cuvie", new_count=20, new_cost=Decimal("1500.00"))
    with (
        mock.patch.object(newitem.api, "add_item", fake_add),
        mock.patch.object(Message, "reply_text", fake_reply),
    ):
        state = await newitem.type_price(_typed("3500"), context)

    assert added == []
    assert state == newitem.ASK_WHOLESALE


async def test_a_wholesale_price_that_is_not_a_number_asks_again():
    replies = []

    async def fake_reply(self, text, *args, **kwargs):
        replies.append(text)

    context = _ready()
    with mock.patch.object(Message, "reply_text", fake_reply):
        state = await newitem.type_wholesale(_typed("չգիտեմ"), context)

    assert state == newitem.ASK_WHOLESALE
    assert replies == [texts.CLOSEOUT_BAD_PRICE]


# -- the commit ---------------------------------------------------------------

async def test_the_whole_product_reaches_the_server():
    sent = {}

    async def fake_add(**kwargs):
        sent.update(kwargs)
        return _server_answer()

    async def fake_reply(self, *args, **kwargs):
        return None

    context = _ready()
    with (
        mock.patch.object(newitem.api, "add_item", fake_add),
        mock.patch.object(Message, "reply_text", fake_reply),
    ):
        state = await newitem.type_wholesale(_typed("3000"), context)

    assert state == ConversationHandler.END
    assert sent["name"] == "HQD Cuvie"
    assert sent["count"] == 20
    assert sent["self_price"] == "1500.00", "money travels as a string, never a float"
    assert sent["sell_price"] == "3500.00"
    assert sent["wholesale_price"] == "3000.00"
    assert context.user_data == {}


async def test_skipping_the_wholesale_price_sends_nothing_rather_than_zero():
    """"We do not sell this one wholesale" is an answer. A zero would read as
    free, and the sell flow would offer it as a price."""
    sent = {}

    async def fake_add(**kwargs):
        sent.update(kwargs)
        return _server_answer(wholesale=None)

    async def noop(*args, **kwargs):
        return None

    async def fake_reply(self, *args, **kwargs):
        return None

    context = _ready()
    with (
        mock.patch.object(newitem.api, "add_item", fake_add),
        mock.patch.object(CallbackQuery, "answer", noop),
        mock.patch.object(CallbackQuery, "edit_message_reply_markup", noop),
        mock.patch.object(Message, "reply_text", fake_reply),
    ):
        state = await newitem.skip_wholesale(_tap("sk"), context)

    assert state == ConversationHandler.END
    assert sent["wholesale_price"] is None
    assert sent["sell_price"] == "3500.00", "the retail price is still there"


async def test_the_confirmation_names_the_retail_price_and_the_wholesale_one():
    replies = []

    async def fake_add(**kwargs):
        return _server_answer()

    async def fake_reply(self, text, *args, **kwargs):
        replies.append(text)

    context = _ready()
    with (
        mock.patch.object(newitem.api, "add_item", fake_add),
        mock.patch.object(Message, "reply_text", fake_reply),
    ):
        await newitem.type_wholesale(_typed("3000"), context)

    assert "3,500" in replies[-1]
    assert "3,000" in replies[-1]


async def test_a_skipped_wholesale_price_is_not_shown_as_zero():
    replies = []

    async def fake_add(**kwargs):
        return _server_answer(wholesale=None)

    async def noop(*args, **kwargs):
        return None

    async def fake_reply(self, text, *args, **kwargs):
        replies.append(text)

    context = _ready()
    with (
        mock.patch.object(newitem.api, "add_item", fake_add),
        mock.patch.object(CallbackQuery, "answer", noop),
        mock.patch.object(CallbackQuery, "edit_message_reply_markup", noop),
        mock.patch.object(Message, "reply_text", fake_reply),
    ):
        await newitem.skip_wholesale(_tap("sk"), context)

    assert "3,500" in replies[-1]
    assert "մեծածախ" not in replies[-1]


async def test_a_name_already_on_the_list_sends_them_back_to_the_name():
    """Not the whole thing again: four of the five answers are still right."""
    replies = []

    async def fake_add(**kwargs):
        raise ApiError("validation_error", "«HQD Cuvie» արդեն կա այս խանութի ցուցակում։")

    async def fake_reply(self, text, *args, **kwargs):
        replies.append(text)

    context = _ready()
    with (
        mock.patch.object(newitem.api, "add_item", fake_add),
        mock.patch.object(Message, "reply_text", fake_reply),
    ):
        state = await newitem.type_wholesale(_typed("3000"), context)

    assert state == newitem.ASK_NAME
    assert "«HQD Cuvie» արդեն կա այս խանութի ցուցակում։" in replies


async def test_a_network_failure_ends_the_flow():
    async def fake_add(**kwargs):
        raise ApiUnavailable()

    async def fake_reply(self, *args, **kwargs):
        return None

    context = _ready()
    with (
        mock.patch.object(newitem.api, "add_item", fake_add),
        mock.patch.object(Message, "reply_text", fake_reply),
    ):
        state = await newitem.type_wholesale(_typed("3000"), context)

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

    context = _ready()
    with (
        mock.patch.object(newitem.api, "add_item", fake_add),
        mock.patch.object(Message, "reply_text", fake_reply),
    ):
        state = await newitem.type_wholesale(_typed("3000"), context)

    assert state == ConversationHandler.END
    assert replies == [texts.NEW_ITEM_DONE_PLAINLY]
    assert "սխալ" not in replies[0].lower()
