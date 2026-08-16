"""Taking money out of the till, from the counter.

The reason is picked first and the amount second, because the reason is what
decides the ceiling: lunch answers to the shift allowance, a courier's fee does
not. Asking it the other way round meant refusing a number and only then
explaining why.

The interesting part is still what happens when the server says no. A refusal
here is almost always a number problem — more than the allowance, or more than is
actually in the drawer — and the cashier is standing there with the money in
their hand. Sending them back to the keyboard beats ending the conversation and
making them start again.
"""

from __future__ import annotations

from decimal import Decimal
from unittest import mock

from telegram import CallbackQuery, Chat, Message, Update, User
from telegram.ext import ConversationHandler

from app import keyboards, texts
from app.api import ApiError, ApiUnavailable
from app.handlers import cash


def _server_answer(duplicate: bool = False) -> dict:
    """Exactly what the web service sends back for ``POST /cash/withdraw``."""
    return {
        "ok": True,
        "duplicate": duplicate,
        "withdrawal": {"id": 9, "amount": "800.00", "purpose": "Ճաշ"},
        "store_totals": {"cash": "19200.00", "card": "4000.00"},
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


def _tapped(data: str) -> Update:
    user = User(id=1, first_name="Հայկ", is_bot=False)
    return Update(
        update_id=1,
        callback_query=CallbackQuery(
            id="1", from_user=user, chat_instance="1", data=data,
            message=Message(
                message_id=1, date=None, chat=Chat(id=1, type="private"),
                from_user=User(id=2, first_name="bot", is_bot=True),
            ),
        ),
    )


def _lunch() -> _Context:
    return _Context(cash_reason="lunch")


def _delivery() -> _Context:
    return _Context(cash_reason="delivery")


# -- the reason ---------------------------------------------------------------

async def test_the_flow_opens_by_asking_what_the_money_is_for():
    """Not "how much". The ceiling depends on the answer, so it comes first."""
    replies = []

    async def fake_reply(self, text, *args, **kwargs):
        replies.append((text, kwargs.get("reply_markup")))

    context = _Context()
    with mock.patch.object(Message, "reply_text", fake_reply):
        state = await cash.begin(_typed(texts.BTN_TAKE_CASH), context)

    assert state == cash.ASK_REASON
    assert replies[0][0] == texts.CASH_ASK_REASON
    assert "cash_reason" not in context.user_data


async def test_both_reasons_are_offered_and_nothing_else():
    labels = [
        button.text
        for row in keyboards.cash_reasons().inline_keyboard
        for button in row
    ]

    assert labels == [texts.BTN_CASH_LUNCH, texts.BTN_CASH_DELIVERY, texts.BTN_CANCEL]


async def test_choosing_lunch_asks_for_the_amount_and_names_the_limit():
    replies = []

    async def fake_reply(self, text, *args, **kwargs):
        replies.append(text)

    async def fake_answer(self, *args, **kwargs):
        return None

    context = _Context()
    with (
        mock.patch.object(CallbackQuery, "answer", fake_answer),
        mock.patch.object(Message, "reply_text", fake_reply),
    ):
        state = await cash.choose_reason(_tapped(f"{keyboards.CB_REASON}:lunch"), context)

    assert state == cash.ASK_AMOUNT
    assert context.user_data["cash_reason"] == "lunch"
    assert "1,000" in replies[0], "the cashier is told the ceiling before typing"


async def test_choosing_the_delivery_fee_names_no_limit_because_there_is_none():
    """The parcel costs what the post office charges. Naming a ceiling that does
    not exist would be worse than naming none."""
    replies = []

    async def fake_reply(self, text, *args, **kwargs):
        replies.append(text)

    async def fake_answer(self, *args, **kwargs):
        return None

    context = _Context()
    with (
        mock.patch.object(CallbackQuery, "answer", fake_answer),
        mock.patch.object(Message, "reply_text", fake_reply),
    ):
        state = await cash.choose_reason(_tapped(f"{keyboards.CB_REASON}:delivery"), context)

    assert state == cash.ASK_AMOUNT
    assert context.user_data["cash_reason"] == "delivery"
    assert "1,000" not in replies[0]


async def test_changing_your_mind_drops_the_key_with_the_reason():
    """A number typed under one reason and sent under another is a different
    withdrawal, and it must not inherit the first one's idempotency key —
    otherwise the server answers with the row it already wrote."""
    async def fake_reply(self, *args, **kwargs):
        return None

    async def fake_answer(self, *args, **kwargs):
        return None

    context = _Context(cash_reason="lunch", cash_key="already-in-hand")
    with (
        mock.patch.object(CallbackQuery, "answer", fake_answer),
        mock.patch.object(Message, "reply_text", fake_reply),
    ):
        await cash.choose_reason(_tapped(f"{keyboards.CB_REASON}:delivery"), context)

    assert context.user_data["cash_reason"] == "delivery"
    assert "cash_key" not in context.user_data


# -- the amount ---------------------------------------------------------------

async def test_a_comma_and_spaces_are_still_a_number():
    """People type «1 000,50» because that is how the amount is written."""
    sent = {}

    async def fake_withdraw(**kwargs):
        sent.update(kwargs)
        return _server_answer()

    async def fake_reply(self, *args, **kwargs):
        return None

    with (
        mock.patch.object(cash.api, "withdraw", fake_withdraw),
        mock.patch.object(Message, "reply_text", fake_reply),
    ):
        await cash.type_amount(_typed("1 000,00"), _lunch())

    assert sent["amount"] == "1000.00"


async def test_something_that_is_not_a_number_asks_again():
    replies = []

    async def fake_reply(self, text, *args, **kwargs):
        replies.append(text)

    context = _lunch()
    with mock.patch.object(Message, "reply_text", fake_reply):
        state = await cash.type_amount(_typed("մի քիչ"), context)

    assert state == cash.ASK_AMOUNT
    assert replies == [texts.CASH_BAD_AMOUNT]


async def test_zero_is_not_an_amount():
    replies = []

    async def fake_reply(self, text, *args, **kwargs):
        replies.append(text)

    with mock.patch.object(Message, "reply_text", fake_reply):
        state = await cash.type_amount(_typed("0"), _lunch())

    assert state == cash.ASK_AMOUNT
    assert replies == [texts.CASH_BAD_AMOUNT]


async def test_more_than_the_lunch_limit_is_refused_without_asking_the_server():
    """The bug from the field: 6,000 was accepted, the cashier answered another
    question, and only then was it refused. Say no at the number."""
    replies = []
    calls = []

    async def fake_withdraw(**kwargs):  # pragma: no cover - must not be reached
        calls.append(kwargs)
        return _server_answer()

    async def fake_reply(self, text, *args, **kwargs):
        replies.append(text)

    with (
        mock.patch.object(cash.api, "withdraw", fake_withdraw),
        mock.patch.object(Message, "reply_text", fake_reply),
    ):
        state = await cash.type_amount(_typed("6000"), _lunch())

    assert state == cash.ASK_AMOUNT
    assert calls == []
    assert len(replies) == 1
    assert "1,000" in replies[0], "and it says what the limit is"


async def test_exactly_the_lunch_limit_is_accepted():
    sent = {}

    async def fake_withdraw(**kwargs):
        sent.update(kwargs)
        return _server_answer()

    async def fake_reply(self, *args, **kwargs):
        return None

    with (
        mock.patch.object(cash.api, "withdraw", fake_withdraw),
        mock.patch.object(Message, "reply_text", fake_reply),
    ):
        state = await cash.type_amount(_typed("1000"), _lunch())

    assert state == ConversationHandler.END
    assert sent["amount"] == "1000.00"


async def test_the_delivery_fee_has_no_ceiling_at_all():
    """6,000 for a parcel is not a cashier dipping into the drawer, it is the
    shop paying a bill. Only an empty till can refuse it, and that is the
    server's call."""
    sent = {}

    async def fake_withdraw(**kwargs):
        sent.update(kwargs)
        return _server_answer()

    async def fake_reply(self, *args, **kwargs):
        return None

    with (
        mock.patch.object(cash.api, "withdraw", fake_withdraw),
        mock.patch.object(Message, "reply_text", fake_reply),
    ):
        state = await cash.type_amount(_typed("6000"), _delivery())

    assert state == ConversationHandler.END
    assert sent["amount"] == "6000.00"


def test_the_bots_limit_matches_the_servers():
    """Two copies of one number. The server is still the arbiter — it also knows
    what has already been taken this shift — but they must not disagree about the
    ceiling, or the bot refuses what the server would allow."""
    # WORKER_WITHDRAWAL_LIMIT in web/app/services/money.py
    on_the_server = Decimal("1000.00")

    assert on_the_server == cash.LIMIT
    assert cash.REASONS["lunch"].limit == on_the_server
    assert cash.REASONS["delivery"].limit is None


def test_the_reason_codes_and_their_wording_match_the_servers():
    """The code decides the ceiling and the text is written on the row. Both are
    agreed with WITHDRAWAL_REASONS in web/app/services/money.py, and a drift in
    either would file a courier's fee as lunch."""
    on_the_server = {
        "lunch": "Ճաշ",
        "delivery": "Հայփոստ (առաքման վճար)",
    }

    assert {code: reason.purpose for code, reason in cash.REASONS.items()} == on_the_server
    assert all(code == reason.code for code, reason in cash.REASONS.items()), (
        "the code sent is the code it is filed under"
    )


# -- the commit ---------------------------------------------------------------

async def test_the_amount_and_the_chosen_reason_reach_the_server():
    sent = {}

    async def fake_withdraw(**kwargs):
        sent.update(kwargs)
        return _server_answer()

    async def fake_reply(self, *args, **kwargs):
        return None

    context = _lunch()
    with (
        mock.patch.object(cash.api, "withdraw", fake_withdraw),
        mock.patch.object(Message, "reply_text", fake_reply),
    ):
        state = await cash.type_amount(_typed("800"), context)

    assert state == ConversationHandler.END
    assert sent["amount"] == "800.00", "money travels as a string, never a float"
    assert sent["reason"] == "lunch"
    assert sent["purpose"] == texts.CASH_PURPOSE_LUNCH, "readable by an older service"
    assert context.user_data == {}


async def test_the_delivery_fee_travels_under_its_own_code():
    sent = {}

    async def fake_withdraw(**kwargs):
        sent.update(kwargs)
        return _server_answer()

    async def fake_reply(self, *args, **kwargs):
        return None

    with (
        mock.patch.object(cash.api, "withdraw", fake_withdraw),
        mock.patch.object(Message, "reply_text", fake_reply),
    ):
        await cash.type_amount(_typed("2500"), _delivery())

    assert sent["reason"] == "delivery"
    assert sent["purpose"] == texts.CASH_PURPOSE_DELIVERY


async def test_a_refusal_leaves_the_cashier_able_to_correct_the_number():
    """«There is only 500 in the till» is a typo, not a dead end."""
    replies = []

    async def fake_withdraw(**kwargs):
        raise ApiError("validation_error", "Դրամարկղում կա 500 ֏։")

    async def fake_reply(self, text, *args, **kwargs):
        replies.append(text)

    context = _lunch()
    with (
        mock.patch.object(cash.api, "withdraw", fake_withdraw),
        mock.patch.object(Message, "reply_text", fake_reply),
    ):
        state = await cash.type_amount(_typed("800"), context)

    assert state == cash.ASK_AMOUNT
    assert "Դրամարկղում կա 500 ֏։" in replies
    assert context.user_data["cash_reason"] == "lunch", "and not asked for again"
    assert "cash_key" not in context.user_data, "a fresh key for the corrected amount"


async def test_a_refusal_asks_again_under_the_reason_already_chosen():
    replies = []

    async def fake_withdraw(**kwargs):
        raise ApiError("validation_error", "Դրամարկղում կա 500 ֏։")

    async def fake_reply(self, text, *args, **kwargs):
        replies.append(text)

    with (
        mock.patch.object(cash.api, "withdraw", fake_withdraw),
        mock.patch.object(Message, "reply_text", fake_reply),
    ):
        await cash.type_amount(_typed("6000"), _delivery())

    assert replies[-1] == texts.CASH_ASK_AMOUNT_DELIVERY


async def test_a_network_failure_ends_the_flow_rather_than_looping():
    replies = []

    async def fake_withdraw(**kwargs):
        raise ApiUnavailable()

    async def fake_reply(self, text, *args, **kwargs):
        replies.append(text)

    context = _lunch()
    with (
        mock.patch.object(cash.api, "withdraw", fake_withdraw),
        mock.patch.object(Message, "reply_text", fake_reply),
    ):
        state = await cash.type_amount(_typed("800"), context)

    assert state == ConversationHandler.END
    assert context.user_data == {}


async def test_money_already_out_of_the_till_is_never_reported_as_a_failure():
    """Everything after the server accepts it is presentation. If the reply
    cannot be rendered, the row still exists and the drawer is still short."""
    replies = []

    async def fake_withdraw(**kwargs):
        return {"ok": True}          # a shape the confirmation cannot render

    async def fake_reply(self, text, *args, **kwargs):
        replies.append(text)

    context = _lunch()
    with (
        mock.patch.object(cash.api, "withdraw", fake_withdraw),
        mock.patch.object(Message, "reply_text", fake_reply),
    ):
        state = await cash.type_amount(_typed("800"), context)

    assert state == ConversationHandler.END
    assert replies == [texts.CASH_DONE_PLAINLY]
    assert "սխալ" not in replies[0].lower()


async def test_a_retry_reuses_the_key_rather_than_minting_a_new_one():
    """The key belongs to the withdrawal, not to the attempt. As long as it is
    still in hand, a second try resolves to the row already written instead of
    emptying the till twice."""
    keys = []

    async def fake_withdraw(**kwargs):
        keys.append(kwargs["key"])
        raise ApiError("internal", "…")

    async def fake_reply(self, *args, **kwargs):
        return None

    context = _lunch()
    with (
        mock.patch.object(cash.api, "withdraw", fake_withdraw),
        mock.patch.object(Message, "reply_text", fake_reply),
    ):
        await cash.type_amount(_typed("800"), context)
        context.user_data.update(cash_reason="lunch", cash_key=keys[0])
        await cash.type_amount(_typed("800"), context)

    assert keys[0] == keys[1]
