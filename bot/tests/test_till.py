"""Counting the drawer, from the counter.

One number, and the only thing worth testing hard is what the bot says back. A
worker who counted 3,000 when the books expected 3,500 needs to be told the gap in
plain words — and not called wrong, because a drawer runs over as easily as short
and they are the only person still able to explain either.
"""

from __future__ import annotations

from decimal import Decimal
from unittest import mock

from telegram import CallbackQuery, Chat, Message, Update, User
from telegram.ext import ConversationHandler

from app import keyboards, texts
from app.api import ApiError, ApiUnavailable
from app.handlers import till


def _server_answer(counted="3500.00", expected="3500.00") -> dict:
    """Exactly what the web service sends back for ``POST /shift/till``."""
    difference = Decimal(counted) - Decimal(expected)
    return {
        "ok": True,
        "duplicate": False,
        "count": {
            "id": 4,
            "kind": "close",
            "counted": counted,
            "expected": expected,
            "difference": f"{difference:.2f}",
        },
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


# -- getting in --------------------------------------------------------------

async def test_the_button_asks_for_the_amount_and_takes_itself_away():
    """A second tap would open a second count under a different key, and the server
    would refuse it — better that the button is gone once used."""
    edits = []

    async def noop(*args, **kwargs):
        return None

    async def record_edit(self, *args, **kwargs):
        edits.append(kwargs.get("reply_markup", "unset"))

    async def fake_reply(self, *args, **kwargs):
        return None

    context = _Context()
    with (
        mock.patch.object(CallbackQuery, "answer", noop),
        mock.patch.object(CallbackQuery, "edit_message_reply_markup", record_edit),
        mock.patch.object(Message, "reply_text", fake_reply),
    ):
        state = await till.begin(_tap(f"{keyboards.CB_TILL}:close"), context)

    assert state == till.ASK_AMOUNT
    assert context.user_data["till_kind"] == "close"
    assert edits == [None], "the button was removed"


async def test_the_two_ends_ask_different_questions():
    replies = []

    async def noop(*args, **kwargs):
        return None

    async def fake_reply(self, text, *args, **kwargs):
        replies.append(text)

    with (
        mock.patch.object(CallbackQuery, "answer", noop),
        mock.patch.object(CallbackQuery, "edit_message_reply_markup", noop),
        mock.patch.object(Message, "reply_text", fake_reply),
    ):
        await till.begin(_tap(f"{keyboards.CB_TILL}:close"), _Context())
        await till.begin(_tap(f"{keyboards.CB_TILL}:open"), _Context())

    assert replies == [texts.TILL_ASK_CLOSE, texts.TILL_ASK_OPEN]


async def test_the_drawer_can_be_counted_from_the_main_keyboard():
    """The point of the button: mid-shift, before locking up, while the notes are in
    hand. Waiting for the shift-end prompt means doing it from memory."""
    replies = []

    async def fake_reply(self, text, *args, **kwargs):
        replies.append((text, kwargs.get("reply_markup")))

    context = _Context()
    with mock.patch.object(Message, "reply_text", fake_reply):
        state = await till.begin_from_menu(_typed(texts.BTN_TILL_COUNT), context)

    assert state == till.ASK_AMOUNT
    assert context.user_data["till_kind"] == "close", "it is what carries over"
    assert replies[0][0] == texts.TILL_ASK_CLOSE


async def test_counting_from_the_menu_leaves_only_cancel_on_the_keyboard():
    """A number is expected next. A stray tap on «Վաճառել» must not be read as one —
    and the reply keyboard is the thing most likely to be tapped by accident."""
    markups = []

    async def fake_reply(self, text, *args, **kwargs):
        markups.append(kwargs.get("reply_markup"))

    with mock.patch.object(Message, "reply_text", fake_reply):
        await till.begin_from_menu(_typed(texts.BTN_TILL_COUNT), _Context())

    labels = {b.text for row in markups[0].keyboard for b in row}
    assert labels == {texts.BTN_CANCEL}


async def test_a_mid_shift_count_is_what_the_next_shift_starts_with():
    """Filed as a closing count, so counting early is never wrong — a later one
    replaces it, because the most recent is the one that carries over."""
    sent = {}

    async def fake_count(**kwargs):
        sent.update(kwargs)
        return _server_answer(counted="40000.00", expected="40000.00")

    async def fake_reply(self, *args, **kwargs):
        return None

    context = _Context()
    with (
        mock.patch.object(till.api, "count_till", fake_count),
        mock.patch.object(Message, "reply_text", fake_reply),
    ):
        await till.begin_from_menu(_typed(texts.BTN_TILL_COUNT), context)
        await till.type_amount(_typed("40000"), context)

    assert sent["kind"] == "close"


async def test_a_count_mid_shift_hands_the_working_menu_back():
    """Still on shift, so the keyboard has to come back — and which keyboard is taken
    from how the flow was entered, not asked of the server."""
    markups = []

    async def fake_count(**kwargs):
        return _server_answer(counted="40000.00", expected="40000.00")

    async def fake_reply(self, text, *args, **kwargs):
        markups.append(kwargs.get("reply_markup"))

    context = _Context()
    with (
        mock.patch.object(till.api, "count_till", fake_count),
        mock.patch.object(Message, "reply_text", fake_reply),
    ):
        await till.begin_from_menu(_typed(texts.BTN_TILL_COUNT), context)
        await till.type_amount(_typed("40000"), context)

    labels = {b.text for row in markups[-1].keyboard for b in row}
    assert texts.BTN_SELL in labels, "the worker is still on shift"
    assert texts.BTN_TILL_COUNT in labels, "and can count again"


async def test_a_count_after_the_shift_ended_does_not_hand_it_back():
    """Offering the working menu there would be six buttons that all answer "you are
    not on shift"."""
    markups = []

    async def fake_count(**kwargs):
        return _server_answer(counted="40000.00", expected="40000.00")

    async def noop(*args, **kwargs):
        return None

    async def fake_reply(self, text, *args, **kwargs):
        markups.append(kwargs.get("reply_markup"))

    context = _Context()
    with (
        mock.patch.object(till.api, "count_till", fake_count),
        mock.patch.object(CallbackQuery, "answer", noop),
        mock.patch.object(CallbackQuery, "edit_message_reply_markup", noop),
        mock.patch.object(Message, "reply_text", fake_reply),
    ):
        await till.begin(_tap(f"{keyboards.CB_TILL}:close"), context)
        await till.type_amount(_typed("40000"), context)

    labels = {b.text for row in markups[-1].keyboard for b in row}
    assert labels == {texts.BTN_OPEN}


async def test_an_opening_count_keeps_the_worker_at_work():
    """Counting at the start of a shift is not the end of one."""
    markups = []

    async def fake_count(**kwargs):
        return _server_answer(counted="40000.00", expected="40000.00")

    async def noop(*args, **kwargs):
        return None

    async def fake_reply(self, text, *args, **kwargs):
        markups.append(kwargs.get("reply_markup"))

    context = _Context()
    with (
        mock.patch.object(till.api, "count_till", fake_count),
        mock.patch.object(CallbackQuery, "answer", noop),
        mock.patch.object(CallbackQuery, "edit_message_reply_markup", noop),
        mock.patch.object(Message, "reply_text", fake_reply),
    ):
        await till.begin(_tap(f"{keyboards.CB_TILL}:open"), context)
        await till.type_amount(_typed("40000"), context)

    labels = {b.text for row in markups[-1].keyboard for b in row}
    assert texts.BTN_SELL in labels


# -- the number --------------------------------------------------------------

async def test_something_that_is_not_a_number_asks_again():
    replies = []

    async def fake_reply(self, text, *args, **kwargs):
        replies.append(text)

    context = _Context(till_kind="close")
    with mock.patch.object(Message, "reply_text", fake_reply):
        state = await till.type_amount(_typed("շատ"), context)

    assert state == till.ASK_AMOUNT
    assert replies == [texts.TILL_BAD_AMOUNT]


async def test_an_empty_drawer_is_a_real_answer():
    """Nought is a count. Refusing it would leave the one shop that banks
    everything nightly unable to answer the question."""
    sent = {}

    async def fake_count(**kwargs):
        sent.update(kwargs)
        return _server_answer(counted="0.00", expected="0.00")

    async def fake_reply(self, *args, **kwargs):
        return None

    context = _Context(till_kind="close")
    with (
        mock.patch.object(till.api, "count_till", fake_count),
        mock.patch.object(Message, "reply_text", fake_reply),
    ):
        state = await till.type_amount(_typed("0"), context)

    assert state == ConversationHandler.END
    assert sent["counted"] == "0.00"


async def test_the_amount_travels_as_a_string_with_its_kind():
    sent = {}

    async def fake_count(**kwargs):
        sent.update(kwargs)
        return _server_answer(counted="40000.00", expected="40000.00")

    async def fake_reply(self, *args, **kwargs):
        return None

    context = _Context(till_kind="close")
    with (
        mock.patch.object(till.api, "count_till", fake_count),
        mock.patch.object(Message, "reply_text", fake_reply),
    ):
        await till.type_amount(_typed("40 000"), context)

    assert sent["counted"] == "40000.00", "money is never a float"
    assert sent["kind"] == "close"


# -- what it says back -------------------------------------------------------

async def test_a_matching_drawer_says_so():
    replies = []

    async def fake_count(**kwargs):
        return _server_answer(counted="3500.00", expected="3500.00")

    async def fake_reply(self, text, *args, **kwargs):
        replies.append(text)

    with (
        mock.patch.object(till.api, "count_till", fake_count),
        mock.patch.object(Message, "reply_text", fake_reply),
    ):
        await till.type_amount(_typed("3500"), _Context(till_kind="close"))

    assert texts.TILL_MATCHES in replies[-1]


async def test_a_short_drawer_names_the_gap():
    replies = []

    async def fake_count(**kwargs):
        return _server_answer(counted="3000.00", expected="3500.00")

    async def fake_reply(self, text, *args, **kwargs):
        replies.append(text)

    with (
        mock.patch.object(till.api, "count_till", fake_count),
        mock.patch.object(Message, "reply_text", fake_reply),
    ):
        await till.type_amount(_typed("3000"), _Context(till_kind="close"))

    assert "500" in replies[-1]
    assert "3,500" in replies[-1], "and what the books expected"
    # Shown as a positive shortfall, not as "-500", which reads like a second sum.
    assert "-500" not in replies[-1]


async def test_a_heavy_drawer_is_not_reported_as_an_error():
    """It runs over as easily as short — usually an unrecorded sale — and the worker
    is the only one who can still say which."""
    replies = []

    async def fake_count(**kwargs):
        return _server_answer(counted="4000.00", expected="3500.00")

    async def fake_reply(self, text, *args, **kwargs):
        replies.append(text)

    with (
        mock.patch.object(till.api, "count_till", fake_count),
        mock.patch.object(Message, "reply_text", fake_reply),
    ):
        await till.type_amount(_typed("4000"), _Context(till_kind="close"))

    assert "500" in replies[-1]
    assert "սխալ" not in replies[-1].lower()


async def test_a_recorded_count_is_never_reported_as_a_failure():
    replies = []

    async def fake_count(**kwargs):
        return {"ok": True}          # a shape the confirmation cannot render

    async def fake_reply(self, text, *args, **kwargs):
        replies.append(text)

    with (
        mock.patch.object(till.api, "count_till", fake_count),
        mock.patch.object(Message, "reply_text", fake_reply),
    ):
        state = await till.type_amount(_typed("3500"), _Context(till_kind="close"))

    assert state == ConversationHandler.END
    assert replies == [texts.TILL_DONE_PLAINLY]


# -- failures ----------------------------------------------------------------

async def test_a_refusal_lets_them_correct_the_number():
    async def fake_count(**kwargs):
        raise ApiError("validation_error", "Սխալ գումար։")

    async def fake_reply(self, *args, **kwargs):
        return None

    context = _Context(till_kind="close")
    with (
        mock.patch.object(till.api, "count_till", fake_count),
        mock.patch.object(Message, "reply_text", fake_reply),
    ):
        state = await till.type_amount(_typed("99999999999"), context)

    assert state == till.ASK_AMOUNT
    assert "till_key" not in context.user_data


async def test_a_network_failure_ends_the_flow():
    async def fake_count(**kwargs):
        raise ApiUnavailable()

    async def fake_reply(self, *args, **kwargs):
        return None

    context = _Context(till_kind="close")
    with (
        mock.patch.object(till.api, "count_till", fake_count),
        mock.patch.object(Message, "reply_text", fake_reply),
    ):
        state = await till.type_amount(_typed("3500"), context)

    assert state == ConversationHandler.END
    assert context.user_data == {}


async def test_a_retry_reuses_the_key():
    keys = []

    async def fake_count(**kwargs):
        keys.append(kwargs["key"])
        raise ApiError("internal", "…")

    async def fake_reply(self, *args, **kwargs):
        return None

    context = _Context(till_kind="close")
    with (
        mock.patch.object(till.api, "count_till", fake_count),
        mock.patch.object(Message, "reply_text", fake_reply),
    ):
        await till.type_amount(_typed("3500"), context)
        context.user_data.update(till_kind="close", till_key=keys[0])
        await till.type_amount(_typed("3500"), context)

    assert keys[0] == keys[1]


async def test_the_count_can_be_skipped():
    """A worker locking up with a queue behind them should not be held there by a
    number they can give the owner tomorrow."""
    async def fake_reply(self, *args, **kwargs):
        return None

    context = _Context(till_kind="close", till_key="abc")
    with mock.patch.object(Message, "reply_text", fake_reply):
        state = await till.cancel(_typed(texts.BTN_CANCEL), context)

    assert state == ConversationHandler.END
    assert context.user_data == {}
