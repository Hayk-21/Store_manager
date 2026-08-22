"""Cash leaving the drawer, from the counter.

The category is picked first and the amount second, because the category is what
decides the ceiling: lunch answers to the shift allowance, «Այլ» does not. Asking
it the other way round meant refusing a number and only then explaining why.

Three answers. «Ճաշ» is the one thing anybody can put a figure on in advance, so
it is the one thing with a ceiling; «Այլ» is everything else and collects the
reason in the cashier's own words; «Փոխանցել այլ խանութ» is not spending at all —
the money is moving to another of the owner's tills.

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


def _other(purpose: str = "տաքսի") -> _Context:
    """«Այլ», with the reason already typed — the step before the amount."""
    return _Context(cash_reason="other", cash_purpose=purpose)


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


async def test_every_way_money_leaves_the_drawer_is_offered_and_nothing_else():
    labels = [
        button.text
        for row in keyboards.cash_reasons().inline_keyboard
        for button in row
    ]

    assert labels == [
        texts.BTN_CASH_LUNCH,
        texts.BTN_CASH_OTHER,
        texts.BTN_CASH_TO_STORE,
        texts.BTN_CANCEL,
    ]


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


async def test_choosing_other_asks_what_the_money_is_for_first():
    """Nobody can list in advance what money leaves a till for, so «Այլ» collects it
    in the cashier's own words — and before the amount, because a cashier who has
    already typed the number is explaining money that has effectively gone."""
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
        state = await cash.choose_reason(_tapped(f"{keyboards.CB_REASON}:other"), context)

    assert state == cash.ASK_PURPOSE
    assert context.user_data["cash_reason"] == "other"
    assert replies[0] == texts.CASH_ASK_PURPOSE


async def test_the_typed_reason_leads_to_an_amount_with_no_ceiling_named():
    """There is no allowance on «Այլ», and naming a ceiling that does not exist
    would be worse than naming none. The reason is quoted back so the cashier can
    see what they are about to file the number under."""
    replies = []

    async def fake_reply(self, text, *args, **kwargs):
        replies.append(text)

    context = _Context(cash_reason="other")
    with mock.patch.object(Message, "reply_text", fake_reply):
        state = await cash.type_purpose(_typed("տաքսի՝ ապրանքով"), context)

    assert state == cash.ASK_AMOUNT
    assert context.user_data["cash_purpose"] == "տաքսի՝ ապրանքով"
    assert "տաքսի՝ ապրանքով" in replies[0]
    assert "1,000" not in replies[0]


async def test_a_reason_of_one_character_is_not_a_reason():
    """The row has to be readable by somebody who was not there."""
    replies = []

    async def fake_reply(self, text, *args, **kwargs):
        replies.append(text)

    context = _Context(cash_reason="other")
    with mock.patch.object(Message, "reply_text", fake_reply):
        state = await cash.type_purpose(_typed("ա"), context)

    assert state == cash.ASK_PURPOSE
    assert replies == [texts.CASH_BAD_PURPOSE]
    assert "cash_purpose" not in context.user_data


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
        await cash.choose_reason(_tapped(f"{keyboards.CB_REASON}:other"), context)

    assert context.user_data["cash_reason"] == "other"
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


async def test_other_has_no_ceiling_at_all():
    """6,000 for a courier is not a cashier dipping into the drawer, it is the shop
    settling a bill. Only an empty till can refuse it, and that is the server's
    call."""
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
        state = await cash.type_amount(_typed("6000"), _other("առաքիչին"))

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
    assert cash.REASONS["other"].limit is None


def test_the_reason_codes_and_their_wording_match_the_servers():
    """The code decides the ceiling and the text is written on the row. Both are
    agreed with WITHDRAWAL_REASONS and REASON_OTHER in web/app/services/money.py,
    and a drift in either would file a courier's fee as lunch."""
    on_the_server = {
        "lunch": "Ճաշ",
        "other": "Այլ",
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


async def test_what_the_cashier_typed_is_what_reaches_the_row():
    """«Այլ» is the one category whose purpose is not a constant. The code still
    travels — it is what tells the server no allowance applies — and the words
    travel with it, because they are the whole reason the category exists."""
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
        await cash.type_amount(_typed("2500"), _other("ջուր և բաժակներ"))

    assert sent["reason"] == "other"
    assert sent["purpose"] == "ջուր և բաժակներ"


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


async def test_a_refusal_asks_for_the_number_again_and_not_for_the_reason():
    """The category and the words behind it have already been given. Asking for
    them a second time to correct a number would be the bot forgetting."""
    replies = []

    async def fake_withdraw(**kwargs):
        raise ApiError("validation_error", "Դրամարկղում կա 500 ֏։")

    async def fake_reply(self, text, *args, **kwargs):
        replies.append(text)

    context = _other("առաքիչին")
    with (
        mock.patch.object(cash.api, "withdraw", fake_withdraw),
        mock.patch.object(Message, "reply_text", fake_reply),
    ):
        state = await cash.type_amount(_typed("6000"), context)

    assert state == cash.ASK_AMOUNT
    assert replies[-1] != texts.CASH_ASK_PURPOSE
    assert "առաքիչին" in replies[-1], "the reason they already gave, quoted back"
    assert context.user_data["cash_purpose"] == "առաքիչին"


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


# -- sending it to another shop -----------------------------------------------

def _stores() -> dict:
    return {
        "ok": True,
        "available": "9000.00",
        "stores": [{"id": 4, "name": "Կենտրոն"}, {"id": 7, "name": "Մաշտոց"}],
    }


def _sent_back(status: str = "pending") -> dict:
    return {
        "ok": True,
        "duplicate": False,
        "transfer": {
            "id": 3, "amount": "5000.00", "status": status,
            "from_store": "Աբովյան", "to_store": "Կենտրոն", "sent_by": "Անի",
        },
        "store_totals": {"cash": "4000.00"},
    }


async def _pick(context, replies) -> int:
    async def fake_reply(self, text, *args, **kwargs):
        replies.append((text, kwargs.get("reply_markup")))

    async def fake_answer(self, *args, **kwargs):
        return None

    async def fake_stores(telegram_id):
        return _stores()

    with (
        mock.patch.object(cash.api, "money_transfer_stores", fake_stores),
        mock.patch.object(CallbackQuery, "answer", fake_answer),
        mock.patch.object(Message, "reply_text", fake_reply),
    ):
        return await cash.choose_reason(
            _tapped(f"{keyboards.CB_REASON}:to_store"), context
        )


async def test_sending_to_another_shop_asks_which_one():
    replies = []
    context = _Context()

    state = await _pick(context, replies)

    assert state == cash.PICK_STORE
    labels = [b.text for row in replies[-1][1].inline_keyboard for b in row]
    assert labels == ["Կենտրոն", "Մաշտոց", texts.BTN_CANCEL]


async def test_the_amount_question_says_what_the_drawer_holds():
    """The figure is in front of the cashier while they type, rather than arriving
    as a refusal after they have."""
    replies = []
    context = _Context()
    await _pick(context, replies)

    async def fake_reply(self, text, *args, **kwargs):
        replies.append((text, kwargs.get("reply_markup")))

    async def fake_answer(self, *args, **kwargs):
        return None

    async def fake_edit(self, *args, **kwargs):
        return None

    with (
        mock.patch.object(CallbackQuery, "answer", fake_answer),
        mock.patch.object(CallbackQuery, "edit_message_reply_markup", fake_edit),
        mock.patch.object(Message, "reply_text", fake_reply),
    ):
        state = await cash.choose_store(_tapped(f"{keyboards.CB_MONEY_STORE}:4"), context)

    assert state == cash.ASK_AMOUNT
    assert context.user_data["cash_store"] == 4
    assert "Կենտրոն" in replies[-1][0]
    assert "9,000" in replies[-1][0]


async def test_more_than_the_drawer_holds_is_refused_before_the_server_sees_it():
    replies = []
    calls = []

    async def fake_send(**kwargs):  # pragma: no cover - must not be reached
        calls.append(kwargs)
        return _sent_back()

    async def fake_reply(self, text, *args, **kwargs):
        replies.append(text)

    context = _Context(cash_store=4, cash_store_name="Կենտրոն", cash_available="9000.00")
    with (
        mock.patch.object(cash.api, "send_money", fake_send),
        mock.patch.object(Message, "reply_text", fake_reply),
    ):
        state = await cash.amount_step(_typed("12000"), context)

    assert state == cash.ASK_AMOUNT
    assert calls == []
    assert "9,000" in replies[-1]


async def test_the_amount_reaches_the_server_with_the_shop_it_is_going_to():
    sent = {}

    async def fake_send(**kwargs):
        sent.update(kwargs)
        return _sent_back()

    async def fake_reply(self, *args, **kwargs):
        return None

    context = _Context(cash_store=4, cash_store_name="Կենտրոն", cash_available="9000.00")
    with (
        mock.patch.object(cash.api, "send_money", fake_send),
        mock.patch.object(Message, "reply_text", fake_reply),
    ):
        state = await cash.amount_step(_typed("5 000"), context)

    assert state == ConversationHandler.END
    assert sent["to_store_id"] == 4
    assert sent["amount"] == "5000.00", "money travels as a string, never a float"
    assert context.user_data == {}


async def test_the_amount_step_still_serves_a_plain_withdrawal():
    """One state, two questions. Nothing was picked, so it is money being spent."""
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
        await cash.amount_step(_typed("800"), _lunch())

    assert sent["reason"] == "lunch"


async def test_confirming_an_envelope_sends_the_verdict_and_says_so():
    """The buttons arrive on a message pushed by the web service, so this runs
    outside any conversation — an hour after the money was sent, if that is when
    somebody finally has it in hand."""
    replies = []
    calls = []

    async def fake_decide(telegram_id, transfer_id, accept):
        calls.append((transfer_id, accept))
        return _sent_back("received")

    async def fake_reply(self, text, *args, **kwargs):
        replies.append(text)

    async def fake_answer(self, *args, **kwargs):
        return None

    async def fake_edit(self, *args, **kwargs):
        return None

    with (
        mock.patch.object(cash.api, "decide_money_transfer", fake_decide),
        mock.patch.object(CallbackQuery, "answer", fake_answer),
        mock.patch.object(CallbackQuery, "edit_message_reply_markup", fake_edit),
        mock.patch.object(Message, "reply_text", fake_reply),
    ):
        await cash.decide(_tapped(f"{keyboards.CB_MONEY_TRANSFER}:3:y"), _Context())

    assert calls == [(3, True)]
    assert "5,000" in replies[-1]
    assert "Աբովյան" in replies[-1], "the shop it came from"


async def test_denying_an_envelope_travels_as_a_refusal():
    calls = []

    async def fake_decide(telegram_id, transfer_id, accept):
        calls.append((transfer_id, accept))
        return _sent_back("rejected")

    async def fake_reply(self, *args, **kwargs):
        return None

    async def fake_answer(self, *args, **kwargs):
        return None

    async def fake_edit(self, *args, **kwargs):
        return None

    with (
        mock.patch.object(cash.api, "decide_money_transfer", fake_decide),
        mock.patch.object(CallbackQuery, "answer", fake_answer),
        mock.patch.object(CallbackQuery, "edit_message_reply_markup", fake_edit),
        mock.patch.object(Message, "reply_text", fake_reply),
    ):
        await cash.decide(_tapped(f"{keyboards.CB_MONEY_TRANSFER}:3:n"), _Context())

    assert calls == [(3, False)]


async def test_nothing_waiting_adds_nothing_to_the_transfers_screen():
    """An empty section on every visit is noise."""
    replies = []

    async def fake_pending(telegram_id):
        return {"ok": True, "incoming": []}

    async def fake_reply(self, text, *args, **kwargs):
        replies.append(text)

    with (
        mock.patch.object(cash.api, "pending_money_transfers", fake_pending),
        mock.patch.object(Message, "reply_text", fake_reply),
    ):
        await cash.show_waiting(_typed("x"), _Context())

    assert replies == []


async def test_money_waiting_is_listed_with_its_own_buttons():
    """The way back to an envelope whose pushed message was missed."""
    replies = []

    async def fake_pending(telegram_id):
        return {
            "ok": True,
            "incoming": [
                {"id": 3, "amount": "5000.00", "from_store": "Աբովյան", "sent_by": "Անի"}
            ],
        }

    async def fake_reply(self, text, *args, **kwargs):
        replies.append((text, kwargs.get("reply_markup")))

    with (
        mock.patch.object(cash.api, "pending_money_transfers", fake_pending),
        mock.patch.object(Message, "reply_text", fake_reply),
    ):
        await cash.show_waiting(_typed("x"), _Context())

    text, markup = replies[-1]
    assert "5,000" in text and "Աբովյան" in text
    assert [b.callback_data for row in markup.inline_keyboard for b in row] == [
        f"{keyboards.CB_MONEY_TRANSFER}:3:y",
        f"{keyboards.CB_MONEY_TRANSFER}:3:n",
    ]


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
