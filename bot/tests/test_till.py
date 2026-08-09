"""Counting the drawer at the end of a shift, from the counter.

One number, and what the bot says back matters more than the number does. The worker
is about to hand cash to somebody, so the amount going to the owner has to be stated
outright rather than left as a subtraction they do in their head at the door.

There is no opening count any more. Asking at the start of a shift meant asking a
worker to answer for a drawer somebody else had filled.
"""

from __future__ import annotations

from decimal import Decimal
from unittest import mock

from telegram import CallbackQuery, Chat, Message, Update, User
from telegram.ext import ConversationHandler

from app import keyboards, texts
from app.api import ApiError, ApiUnavailable
from app.handlers import till


def _server_answer(counted="30000.00", expected="130000.00") -> dict:
    """Exactly what the web service sends back for ``POST /shift/till``."""
    handed = Decimal(expected) - Decimal(counted)
    return {
        "ok": True,
        "duplicate": False,
        "count": {
            "id": 4,
            "kind": "close",
            "counted": counted,
            "expected": expected,
            "handed_over": f"{handed:.2f}",
            "difference": f"{Decimal(counted) - Decimal(expected):.2f}",
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
        state = await till.begin(_tap(keyboards.CB_TILL), context)

    assert state == till.ASK_AMOUNT
    assert edits == [None], "the button was removed"


async def test_the_drawer_can_be_counted_from_the_main_keyboard():
    """The point of that button: mid-shift, before locking up, while the notes are in
    hand. Waiting for the shift-end prompt means doing it from memory."""
    replies = []

    async def fake_reply(self, text, *args, **kwargs):
        replies.append((text, kwargs.get("reply_markup")))

    context = _Context()
    with mock.patch.object(Message, "reply_text", fake_reply):
        state = await till.begin_from_menu(_typed(texts.BTN_TILL_COUNT), context)

    assert state == till.ASK_AMOUNT
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


async def test_both_ways_in_ask_the_same_question():
    """One count, so one question. There is no opening variant to get wrong."""
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
        await till.begin(_tap(keyboards.CB_TILL), _Context())
        await till.begin_from_menu(_typed(texts.BTN_TILL_COUNT), _Context())

    assert replies == [texts.TILL_ASK_CLOSE, texts.TILL_ASK_CLOSE]


# -- the number --------------------------------------------------------------

async def test_something_that_is_not_a_number_asks_again():
    replies = []

    async def fake_reply(self, text, *args, **kwargs):
        replies.append(text)

    context = _Context()
    with mock.patch.object(Message, "reply_text", fake_reply):
        state = await till.type_amount(_typed("շատ"), context)

    assert state == till.ASK_AMOUNT
    assert replies == [texts.TILL_BAD_AMOUNT]


async def test_an_empty_drawer_is_a_real_answer():
    """Nought is a count: a shop that banks everything nightly leaves nothing, and
    refusing it would leave that shop unable to answer."""
    sent = {}

    async def fake_count(**kwargs):
        sent.update(kwargs)
        return _server_answer(counted="0.00", expected="130000.00")

    async def fake_reply(self, *args, **kwargs):
        return None

    context = _Context()
    with (
        mock.patch.object(till.api, "count_till", fake_count),
        mock.patch.object(Message, "reply_text", fake_reply),
    ):
        state = await till.type_amount(_typed("0"), context)

    assert state == ConversationHandler.END
    assert sent["counted"] == "0.00"


async def test_the_amount_travels_as_a_string():
    sent = {}

    async def fake_count(**kwargs):
        sent.update(kwargs)
        return _server_answer()

    async def fake_reply(self, *args, **kwargs):
        return None

    context = _Context()
    with (
        mock.patch.object(till.api, "count_till", fake_count),
        mock.patch.object(Message, "reply_text", fake_reply),
    ):
        await till.type_amount(_typed("30 000"), context)

    assert sent["counted"] == "30000.00", "money is never a float"
    assert "kind" not in sent, "there is only one kind of count now"


# -- what it says back -------------------------------------------------------

async def test_it_states_what_goes_to_the_owner():
    """The figure the worker is about to act on. They are holding that money, so
    making them subtract two others at the door is how the wrong amount gets
    handed over."""
    replies = []

    async def fake_count(**kwargs):
        return _server_answer(counted="30000.00", expected="130000.00")

    async def fake_reply(self, text, *args, **kwargs):
        replies.append(text)

    with (
        mock.patch.object(till.api, "count_till", fake_count),
        mock.patch.object(Message, "reply_text", fake_reply),
    ):
        await till.type_amount(_typed("30000"), _Context())

    assert "30,000" in replies[-1], "what stays in the shop"
    assert "100,000" in replies[-1], "and what the owner gets"


async def test_a_drawer_that_holds_everything_hands_nothing_over():
    replies = []

    async def fake_count(**kwargs):
        return _server_answer(counted="130000.00", expected="130000.00")

    async def fake_reply(self, text, *args, **kwargs):
        replies.append(text)

    with (
        mock.patch.object(till.api, "count_till", fake_count),
        mock.patch.object(Message, "reply_text", fake_reply),
    ):
        await till.type_amount(_typed("130000"), _Context())

    assert texts.TILL_NOTHING_TO_HAND.strip() in replies[-1]


async def test_a_matching_count_says_nothing_about_a_gap():
    replies = []

    async def fake_count(**kwargs):
        return _server_answer(counted="30000.00", expected="130000.00")

    async def fake_reply(self, text, *args, **kwargs):
        replies.append(text)

    with (
        mock.patch.object(till.api, "count_till", fake_count),
        mock.patch.object(Message, "reply_text", fake_reply),
    ):
        await till.type_amount(_typed("30000"), _Context())

    assert "Համակարգի հաշվարկով" not in replies[-1]


async def test_a_drawer_holding_more_than_expected_is_not_called_an_error():
    """It runs over as easily as short — usually an unrecorded sale — and the worker
    standing at it is the only one who can still say which."""
    replies = []

    async def fake_count(**kwargs):
        # 4,000 in a drawer the books thought held 3,500: the extra stays put.
        return _server_answer(counted="4000.00", expected="3500.00")

    async def fake_reply(self, text, *args, **kwargs):
        replies.append(text)

    with (
        mock.patch.object(till.api, "count_till", fake_count),
        mock.patch.object(Message, "reply_text", fake_reply),
    ):
        await till.type_amount(_typed("4000"), _Context())

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
        state = await till.type_amount(_typed("30000"), _Context())

    assert state == ConversationHandler.END
    assert replies == [texts.TILL_DONE_PLAINLY]


# -- which keyboard is left behind -------------------------------------------

async def test_the_keyboard_menu_hands_back_the_off_shift_one():
    """Both ways in are post-shift now, so there is only one right answer.

    The button used to sit on the working keyboard, and that is what let a worker count
    up at 21:07, hand the owner everything above the float, and be paid at 21:10 out of
    a drawer that no longer had it — the shop closed showing cash of -4,500.
    """
    markups = []

    async def fake_count(**kwargs):
        return _server_answer()

    async def fake_reply(self, text, *args, **kwargs):
        markups.append(kwargs.get("reply_markup"))

    context = _Context()
    with (
        mock.patch.object(till.api, "count_till", fake_count),
        mock.patch.object(Message, "reply_text", fake_reply),
    ):
        await till.begin_from_menu(_typed(texts.BTN_TILL_COUNT), context)
        await till.type_amount(_typed("30000"), context)

    labels = {b.text for row in markups[-1].keyboard for b in row}
    assert texts.BTN_SELL not in labels, "the shift is over"
    assert labels == {texts.BTN_OPEN, texts.BTN_TILL_COUNT}


def test_counting_is_not_offered_while_the_shop_is_trading():
    """The keyboard is the guard people actually meet; the server refuses it too."""
    assert texts.BTN_TILL_COUNT not in {
        button.text for row in keyboards.on_shift().keyboard for button in row
    }, "counting mid-shift hands over the change the shift still needs"
    assert texts.BTN_TILL_COUNT in {
        button.text for row in keyboards.off_shift().keyboard for button in row
    }, "but a worker who scrolled past the prompt can still get back to it"


async def test_a_count_after_the_shift_ended_does_not_hand_it_back():
    """Offering the working menu there would be six buttons that all answer "you are
    not on shift"."""
    markups = []

    async def fake_count(**kwargs):
        return _server_answer()

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
        await till.begin(_tap(keyboards.CB_TILL), context)
        await till.type_amount(_typed("30000"), context)

    labels = {b.text for row in markups[-1].keyboard for b in row}
    assert labels == {texts.BTN_OPEN, texts.BTN_TILL_COUNT}
    assert texts.BTN_SELL not in labels


# -- failures ----------------------------------------------------------------

async def test_a_refusal_lets_them_correct_the_number():
    async def fake_count(**kwargs):
        raise ApiError("validation_error", "Սխալ գումար։")

    async def fake_reply(self, *args, **kwargs):
        return None

    context = _Context()
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

    context = _Context()
    with (
        mock.patch.object(till.api, "count_till", fake_count),
        mock.patch.object(Message, "reply_text", fake_reply),
    ):
        state = await till.type_amount(_typed("30000"), context)

    assert state == ConversationHandler.END
    assert context.user_data == {}


async def test_a_retry_reuses_the_key():
    keys = []

    async def fake_count(**kwargs):
        keys.append(kwargs["key"])
        raise ApiError("internal", "…")

    async def fake_reply(self, *args, **kwargs):
        return None

    context = _Context()
    with (
        mock.patch.object(till.api, "count_till", fake_count),
        mock.patch.object(Message, "reply_text", fake_reply),
    ):
        await till.type_amount(_typed("30000"), context)
        context.user_data["till_key"] = keys[0]
        await till.type_amount(_typed("30000"), context)

    assert keys[0] == keys[1]


async def test_the_count_can_be_skipped_and_the_float_stands():
    """A worker locking up with a queue behind them should not be held there by a
    number — and the shop's balance simply stays whatever it already was."""
    replies = []

    async def fake_reply(self, text, *args, **kwargs):
        replies.append(text)

    context = _Context(till_key="abc", till_back="on_shift")
    with mock.patch.object(Message, "reply_text", fake_reply):
        state = await till.cancel(_typed(texts.BTN_CANCEL), context)

    assert state == ConversationHandler.END
    assert context.user_data == {}
    assert "մնում է նույնը" in replies[-1]
