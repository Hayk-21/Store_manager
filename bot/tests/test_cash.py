"""Taking money out of the till, from the counter.

The flow is two answers and a commit, and the interesting part is what happens
when the server says no. A refusal here is almost always a number problem — more
than the limit, or more than is actually in the drawer — and the cashier is
standing there with the reason in their hand. Sending them back to the keyboard
beats ending the conversation and making them start again.
"""

from __future__ import annotations

from decimal import Decimal
from unittest import mock

from telegram import Chat, Message, Update, User
from telegram.ext import ConversationHandler

from app import texts
from app.api import ApiError, ApiUnavailable
from app.handlers import cash


def _server_answer(duplicate: bool = False) -> dict:
    """Exactly what the web service sends back for ``POST /cash/withdraw``."""
    return {
        "ok": True,
        "duplicate": duplicate,
        "withdrawal": {"id": 9, "amount": "800.00", "purpose": "առաքիչին"},
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


# -- the amount ---------------------------------------------------------------

async def test_a_typed_amount_moves_on_to_the_purpose():
    async def fake_reply(self, *args, **kwargs):
        return None

    context = _Context()
    with mock.patch.object(Message, "reply_text", fake_reply):
        state = await cash.type_amount(_typed("800"), context)

    assert state == cash.ASK_PURPOSE
    assert context.user_data["cash_amount"] == Decimal("800.00")


async def test_a_comma_and_spaces_are_still_a_number():
    """People type «1 000,50» because that is how the amount is written."""
    async def fake_reply(self, *args, **kwargs):
        return None

    context = _Context()
    with mock.patch.object(Message, "reply_text", fake_reply):
        await cash.type_amount(_typed("1 000,00"), context)

    assert context.user_data["cash_amount"] == Decimal("1000.00")


async def test_something_that_is_not_a_number_asks_again():
    replies = []

    async def fake_reply(self, text, *args, **kwargs):
        replies.append(text)

    context = _Context()
    with mock.patch.object(Message, "reply_text", fake_reply):
        state = await cash.type_amount(_typed("մի քիչ"), context)

    assert state == cash.ASK_AMOUNT
    assert replies == [texts.CASH_BAD_AMOUNT]
    assert "cash_amount" not in context.user_data


async def test_zero_is_not_an_amount():
    replies = []

    async def fake_reply(self, text, *args, **kwargs):
        replies.append(text)

    context = _Context()
    with mock.patch.object(Message, "reply_text", fake_reply):
        state = await cash.type_amount(_typed("0"), context)

    assert state == cash.ASK_AMOUNT
    assert replies == [texts.CASH_BAD_AMOUNT]


async def test_more_than_the_limit_is_refused_before_the_reason_is_asked():
    """The bug from the field: 6,000 was accepted, the cashier typed a reason,
    and only then was it refused. Say no at the number."""
    replies = []

    async def fake_reply(self, text, *args, **kwargs):
        replies.append(text)

    context = _Context()
    with mock.patch.object(Message, "reply_text", fake_reply):
        state = await cash.type_amount(_typed("6000"), context)

    assert state == cash.ASK_AMOUNT, "not moved on to the purpose"
    assert "cash_amount" not in context.user_data
    assert len(replies) == 1
    assert "1,000" in replies[0], "and it says what the limit is"


async def test_exactly_the_limit_is_accepted():
    async def fake_reply(self, *args, **kwargs):
        return None

    context = _Context()
    with mock.patch.object(Message, "reply_text", fake_reply):
        state = await cash.type_amount(_typed("1000"), context)

    assert state == cash.ASK_PURPOSE
    assert context.user_data["cash_amount"] == Decimal("1000.00")


def test_the_bots_limit_matches_the_servers():
    """Two copies of one number. The server is still the arbiter — it also knows
    what has already been taken this shift — but they must not disagree about the
    ceiling, or the bot refuses what the server would allow."""
    # WORKER_WITHDRAWAL_LIMIT in web/app/services/money.py
    on_the_server = Decimal("1000.00")

    assert on_the_server == cash.LIMIT


# -- the purpose, and the commit ----------------------------------------------

async def test_a_blank_purpose_is_asked_for_again():
    """The reason is the entire point of the row. Without it this is a shortfall
    with a number attached, which is what it exists to replace."""
    replies = []

    async def fake_reply(self, text, *args, **kwargs):
        replies.append(text)

    context = _Context(cash_amount=Decimal("800.00"))
    with mock.patch.object(Message, "reply_text", fake_reply):
        state = await cash.type_purpose(_typed("   "), context)

    assert state == cash.ASK_PURPOSE
    assert replies == [texts.CASH_ASK_PURPOSE_AGAIN]


async def test_the_amount_and_the_purpose_reach_the_server():
    sent = {}

    async def fake_withdraw(**kwargs):
        sent.update(kwargs)
        return _server_answer()

    async def fake_reply(self, *args, **kwargs):
        return None

    context = _Context(cash_amount=Decimal("800.00"))
    with (
        mock.patch.object(cash.api, "withdraw", fake_withdraw),
        mock.patch.object(Message, "reply_text", fake_reply),
    ):
        state = await cash.type_purpose(_typed("առաքիչին"), context)

    assert state == ConversationHandler.END
    assert sent["amount"] == "800.00", "money travels as a string, never a float"
    assert sent["purpose"] == "առաքիչին"
    assert context.user_data == {}


async def test_a_refusal_leaves_the_cashier_able_to_correct_the_number():
    """«There is only 500 in the till» is a typo, not a dead end."""
    replies = []

    async def fake_withdraw(**kwargs):
        raise ApiError("validation_error", "Դրամարկղում կա 500 ֏։")

    async def fake_reply(self, text, *args, **kwargs):
        replies.append(text)

    context = _Context(cash_amount=Decimal("800.00"))
    with (
        mock.patch.object(cash.api, "withdraw", fake_withdraw),
        mock.patch.object(Message, "reply_text", fake_reply),
    ):
        state = await cash.type_purpose(_typed("առաքիչին"), context)

    assert state == cash.ASK_AMOUNT
    assert "Դրամարկղում կա 500 ֏։" in replies
    assert "cash_key" not in context.user_data, "a fresh key for the corrected amount"


async def test_a_network_failure_ends_the_flow_rather_than_looping():
    replies = []

    async def fake_withdraw(**kwargs):
        raise ApiUnavailable()

    async def fake_reply(self, text, *args, **kwargs):
        replies.append(text)

    context = _Context(cash_amount=Decimal("800.00"))
    with (
        mock.patch.object(cash.api, "withdraw", fake_withdraw),
        mock.patch.object(Message, "reply_text", fake_reply),
    ):
        state = await cash.type_purpose(_typed("առաքիչին"), context)

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

    context = _Context(cash_amount=Decimal("800.00"))
    with (
        mock.patch.object(cash.api, "withdraw", fake_withdraw),
        mock.patch.object(Message, "reply_text", fake_reply),
    ):
        state = await cash.type_purpose(_typed("առաքիչին"), context)

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

    context = _Context(cash_amount=Decimal("800.00"))
    with (
        mock.patch.object(cash.api, "withdraw", fake_withdraw),
        mock.patch.object(Message, "reply_text", fake_reply),
    ):
        await cash.type_purpose(_typed("առաքիչին"), context)
        context.user_data.update(cash_amount=Decimal("800.00"), cash_key=keys[0])
        await cash.type_purpose(_typed("առաքիչին"), context)

    assert keys[0] == keys[1]
