"""Asking another shop for a box, and answering when yours is asked.

The cashier's side of a two-shop conversation. Nothing here moves stock — the
server does that, and only on approval — so what these tests hold in place is that
the bot never claims otherwise, and that the request it sends names the right shop.
"""

from __future__ import annotations

from unittest import mock

from telegram import CallbackQuery, Chat, Message, Update, User
from telegram.ext import ConversationHandler

from app import keyboards, texts
from app.api import ApiError, ApiUnavailable
from app.handlers import transfer


def _asked() -> dict:
    """What the web service sends back for ``POST /transfers``."""
    return {
        "ok": True,
        "duplicate": False,
        "transfer": {
            "id": 7,
            "item_name": "HQD Cuvie",
            "quantity": 4,
            "status": "pending",
            "from_store": "Խանութ 1",
            "to_store": "Խանութ 2",
        },
    }


def _decided(status: str) -> dict:
    answer = _asked()
    answer["transfer"]["status"] = status
    return answer


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


def _ready() -> _Context:
    """A source shop and an item chosen; only the quantity is left."""
    return _Context(
        tr_store=1,
        tr_store_name="Խանութ 1",
        tr_item={"id": 3, "name": "HQD Cuvie", "count": 10},
    )


# -- asking ------------------------------------------------------------------

async def test_the_request_names_the_source_shop_and_the_item():
    """The destination is never sent — it is the shop the shift is open in, so
    nobody can route a box to a third place."""
    sent = {}

    async def fake_request(**kwargs):
        sent.update(kwargs)
        return _asked()

    async def fake_reply(self, *args, **kwargs):
        return None

    context = _ready()
    with (
        mock.patch.object(transfer.api, "request_transfer", fake_request),
        mock.patch.object(Message, "reply_text", fake_reply),
    ):
        state = await transfer.choose_quantity(_typed("4"), context)

    assert state == ConversationHandler.END
    assert sent["from_store_id"] == 1
    assert sent["item_id"] == 3
    assert sent["quantity"] == 4
    assert "to_store_id" not in sent
    assert context.user_data == {}


async def test_the_confirmation_says_the_box_has_not_arrived_yet():
    """A cashier told "done" would go looking for a box nobody has agreed to send."""
    replies = []

    async def fake_request(**kwargs):
        return _asked()

    async def fake_reply(self, text, *args, **kwargs):
        replies.append(text)

    with (
        mock.patch.object(transfer.api, "request_transfer", fake_request),
        mock.patch.object(Message, "reply_text", fake_reply),
    ):
        await transfer.choose_quantity(_typed("4"), _ready())

    assert "հաստատի" in replies[-1], "it says somebody there has to confirm"


async def test_asking_for_more_than_the_other_shop_has_is_caught_here():
    """Checked before sending as a courtesy; the server checks again as the rule."""
    replies = []
    sent = []

    async def fake_request(**kwargs):
        sent.append(kwargs)
        return _asked()

    async def fake_reply(self, text, *args, **kwargs):
        replies.append(text)

    context = _ready()
    with (
        mock.patch.object(transfer.api, "request_transfer", fake_request),
        mock.patch.object(Message, "reply_text", fake_reply),
    ):
        state = await transfer.choose_quantity(_typed("40"), context)

    assert state == transfer.ASK_QUANTITY
    assert sent == []
    assert "10" in replies[0]


async def test_a_quantity_that_is_not_a_number_asks_again():
    replies = []

    async def fake_reply(self, text, *args, **kwargs):
        replies.append(text)

    context = _ready()
    with mock.patch.object(Message, "reply_text", fake_reply):
        state = await transfer.choose_quantity(_typed("մի քանի"), context)

    assert state == transfer.ASK_QUANTITY
    assert replies == [texts.BAD_QUANTITY]


async def test_a_stale_count_sends_them_back_to_the_number_not_to_the_start():
    """The box can be sold between asking and sending. Another go at the quantity
    beats starting from the shop list again."""
    async def fake_request(**kwargs):
        raise ApiError("insufficient_stock", "«HQD Cuvie» — այդ խանութում կա 2 հատ։")

    async def fake_reply(self, *args, **kwargs):
        return None

    context = _ready()
    with (
        mock.patch.object(transfer.api, "request_transfer", fake_request),
        mock.patch.object(Message, "reply_text", fake_reply),
    ):
        state = await transfer.choose_quantity(_typed("4"), context)

    assert state == transfer.ASK_QUANTITY
    assert context.user_data["tr_item"]["id"] == 3, "the item choice survives"
    assert "tr_key" not in context.user_data


async def test_a_network_failure_ends_the_flow():
    async def fake_request(**kwargs):
        raise ApiUnavailable()

    async def fake_reply(self, *args, **kwargs):
        return None

    context = _ready()
    with (
        mock.patch.object(transfer.api, "request_transfer", fake_request),
        mock.patch.object(Message, "reply_text", fake_reply),
    ):
        state = await transfer.choose_quantity(_typed("4"), context)

    assert state == ConversationHandler.END
    assert context.user_data == {}


async def test_a_retry_reuses_the_key():
    keys = []

    async def fake_request(**kwargs):
        keys.append(kwargs["key"])
        raise ApiError("internal", "…")

    async def fake_reply(self, *args, **kwargs):
        return None

    context = _ready()
    with (
        mock.patch.object(transfer.api, "request_transfer", fake_request),
        mock.patch.object(Message, "reply_text", fake_reply),
    ):
        await transfer.choose_quantity(_typed("4"), context)
        context.user_data.update(_ready().user_data)
        context.user_data["tr_key"] = keys[0]
        await transfer.choose_quantity(_typed("4"), context)

    assert keys[0] == keys[1]


# -- answering ---------------------------------------------------------------

async def test_approving_reports_that_the_stock_left():
    replies = []

    async def fake_decide(*args, **kwargs):
        return _decided("approved")

    async def noop(*args, **kwargs):
        return None

    async def fake_reply(self, text, *args, **kwargs):
        replies.append(text)

    with (
        mock.patch.object(transfer.api, "decide_transfer", fake_decide),
        mock.patch.object(CallbackQuery, "answer", noop),
        mock.patch.object(CallbackQuery, "edit_message_reply_markup", noop),
        mock.patch.object(Message, "reply_text", fake_reply),
    ):
        await transfer.decide(_tap("t:7:y"), _Context())

    assert "HQD Cuvie" in replies[-1]
    assert "հանված" in replies[-1], "it says the stock has gone from this shelf"


async def test_rejecting_says_the_shelf_did_not_change():
    replies = []

    async def fake_decide(*args, **kwargs):
        return _decided("rejected")

    async def noop(*args, **kwargs):
        return None

    async def fake_reply(self, text, *args, **kwargs):
        replies.append(text)

    with (
        mock.patch.object(transfer.api, "decide_transfer", fake_decide),
        mock.patch.object(CallbackQuery, "answer", noop),
        mock.patch.object(CallbackQuery, "edit_message_reply_markup", noop),
        mock.patch.object(Message, "reply_text", fake_reply),
    ):
        await transfer.decide(_tap("t:7:n"), _Context())

    assert "չի փոխվել" in replies[-1]


async def test_the_verdict_reaches_the_server_as_sent():
    sent = {}

    async def fake_decide(telegram_id, transfer_id, approve):
        sent.update(
            {"telegram_id": telegram_id, "transfer_id": transfer_id, "approve": approve}
        )
        return _decided("approved" if approve else "rejected")

    async def noop(*args, **kwargs):
        return None

    async def fake_reply(self, *args, **kwargs):
        return None

    with (
        mock.patch.object(transfer.api, "decide_transfer", fake_decide),
        mock.patch.object(CallbackQuery, "answer", noop),
        mock.patch.object(CallbackQuery, "edit_message_reply_markup", noop),
        mock.patch.object(Message, "reply_text", fake_reply),
    ):
        await transfer.decide(_tap("t:7:y"), _Context())
        assert sent == {"telegram_id": 1, "transfer_id": 7, "approve": True}

        await transfer.decide(_tap("t:7:n"), _Context())
        assert sent["approve"] is False


async def test_an_already_answered_request_says_so_rather_than_crashing():
    """Two workers at the shop can be looking at the same message."""
    replies = []

    async def fake_decide(*args, **kwargs):
        raise ApiError("validation_error", "Այս հարցումն արդեն պատասխանված է։")

    async def noop(*args, **kwargs):
        return None

    async def fake_reply(self, text, *args, **kwargs):
        replies.append(text)

    with (
        mock.patch.object(transfer.api, "decide_transfer", fake_decide),
        mock.patch.object(CallbackQuery, "answer", noop),
        mock.patch.object(Message, "reply_text", fake_reply),
    ):
        state = await transfer.decide(_tap("t:7:y"), _Context())

    assert state == ConversationHandler.END
    assert replies == ["Այս հարցումն արդեն պատասխանված է։"]


# -- the keyboard ------------------------------------------------------------

def test_each_waiting_request_gets_its_own_yes_and_no():
    """A shop with three requests can approve one box and refuse another without
    the two getting mixed up."""
    markup = keyboards.transfer_menu([
        {"id": 7, "item_name": "HQD Cuvie", "quantity": 4, "to_store": "Խանութ 2"},
        {"id": 8, "item_name": "Waka 1800", "quantity": 1, "to_store": "Խանութ 3"},
    ])

    data = [b.callback_data for row in markup.inline_keyboard for b in row]
    assert "t:7:y" in data and "t:7:n" in data
    assert "t:8:y" in data and "t:8:n" in data


def test_asking_for_a_box_is_offered_even_with_nothing_waiting():
    markup = keyboards.transfer_menu([])

    data = [b.callback_data for row in markup.inline_keyboard for b in row]
    assert keyboards.CB_TRANSFER_NEW in data
