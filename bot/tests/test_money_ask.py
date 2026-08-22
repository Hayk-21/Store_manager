"""Asking another shop, or the owner, for cash.

The drawer runs dry from the inside and the wage is what exposes it. The money is
not missing — it is in a sister shop's drawer or the owner's pocket — and this is
how a cashier says so from behind the counter.

The unusual part is answering. Those buttons arrive on a message the *web* service
pushes, and whoever taps them may be a worker at the shop being asked or the owner
themselves. The bot deliberately does not tell them apart: it forwards the tap, and
the server resolves it from the Telegram account. So the tests here are about the
bot forwarding faithfully and rendering whichever answer comes back.
"""

from __future__ import annotations

from unittest import mock

from telegram import CallbackQuery, Chat, Message, Update, User
from telegram.ext import ConversationHandler

from app import keyboards, texts
from app.api import ApiError, ApiUnavailable
from app.handlers import money_ask


class _Context:
    def __init__(self, **data) -> None:
        self.user_data = dict(data)


def _typed(text: str) -> Update:
    user = User(id=1, first_name="Անի", is_bot=False)
    return Update(
        update_id=1,
        message=Message(
            message_id=1, date=None, chat=Chat(id=1, type="private"),
            from_user=user, text=text,
        ),
    )


def _tapped(data: str) -> Update:
    user = User(id=1, first_name="Անի", is_bot=False)
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


def _targets() -> dict:
    return {"ok": True, "stores": [{"id": 4, "name": "Կենտրոն"}]}


def _asked(status: str = "pending", of_owner: bool = False) -> dict:
    return {
        "ok": True,
        "duplicate": False,
        "request": {
            "id": 3,
            "amount": "5000.00",
            "status": status,
            "asked_of": None if of_owner else "Կենտրոն",
            "asked_the_owner": of_owner,
            "to_store": "Աբովյան",
            "requested_by": "Անի",
        },
    }


async def _run(handler, update, context, **patches):
    """Run one step, capturing everything sent back."""
    sent: list[tuple[str, object]] = []

    async def capture(self, text, *args, **kwargs):
        sent.append((text, kwargs.get("reply_markup")))
        return Message(message_id=9, date=None, chat=Chat(id=1, type="private"))

    async def nothing(*args, **kwargs):
        return None

    with mock.patch.object(Message, "reply_text", capture), \
         mock.patch.object(CallbackQuery, "answer", nothing), \
         mock.patch.object(CallbackQuery, "edit_message_reply_markup", nothing):
        for name, fake in patches.items():
            mock.patch.object(money_ask.api, name, fake).start()
        try:
            state = await handler(update, context)
        finally:
            mock.patch.stopall()
    return state, sent


# -- asking --------------------------------------------------------------------

async def test_the_owner_is_always_on_the_list():
    """They have no shop to be open or shut, and they are the answer when every other
    till is as empty as this one."""
    async def fake_targets(telegram_id):
        return _targets()

    context = _Context()
    state, sent = await _run(
        money_ask.begin, _tapped(keyboards.CB_MONEY_ASK_NEW), context,
        money_request_targets=fake_targets,
    )

    assert state == money_ask.PICK_WHO
    labels = [b.text for row in sent[-1][1].inline_keyboard for b in row]
    assert labels == ["Կենտրոն", texts.BTN_MONEY_ASK_OWNER, texts.BTN_CANCEL]


async def test_a_shop_with_no_open_neighbour_can_still_ask_the_owner():
    """Unlike sending, the list is never empty — which is the whole point of the
    owner being on it."""
    async def fake_targets(telegram_id):
        return {"ok": True, "stores": []}

    context = _Context()
    state, sent = await _run(
        money_ask.begin, _tapped(keyboards.CB_MONEY_ASK_NEW), context,
        money_request_targets=fake_targets,
    )

    assert state == money_ask.PICK_WHO
    labels = [b.text for row in sent[-1][1].inline_keyboard for b in row]
    assert texts.BTN_MONEY_ASK_OWNER in labels


async def test_choosing_a_shop_asks_how_much():
    context = _Context(ask_names={"4": "Կենտրոն"})

    state, sent = await _run(
        money_ask.choose_who, _tapped(f"{keyboards.CB_MONEY_ASK_WHO}:4"), context
    )

    assert state == money_ask.ASK_AMOUNT
    assert context.user_data["ask_who"] == "4"
    assert "Կենտրոն" in sent[-1][0]


async def test_choosing_the_owner_travels_as_a_word_not_a_number():
    """There is no store id that could ever mean "the owner", so it is not smuggled
    in as one."""
    context = _Context(ask_names={})

    await _run(
        money_ask.choose_who, _tapped(f"{keyboards.CB_MONEY_ASK_WHO}:owner"), context
    )

    assert context.user_data["ask_who"] == "owner"


async def test_the_amount_reaches_the_server_with_who_was_asked():
    sent_args = {}

    async def fake_ask(**kwargs):
        sent_args.update(kwargs)
        return _asked()

    context = _Context(ask_who="4", ask_who_name="Կենտրոն")
    state, sent = await _run(
        money_ask.type_amount, _typed("5 000"), context, ask_for_money=fake_ask
    )

    assert state == ConversationHandler.END
    assert sent_args["asked_of"] == "4"
    assert sent_args["amount"] == "5000.00", "money travels as a string, never a float"
    assert "Կենտրոն" in sent[-1][0]
    assert context.user_data == {}


async def test_nothing_bounds_the_amount_here():
    """What a shop needs is not what this shop has. The only drawer that can refuse it
    is the one being asked, at the moment they answer."""
    sent_args = {}

    async def fake_ask(**kwargs):
        sent_args.update(kwargs)
        return _asked()

    context = _Context(ask_who="owner", ask_who_name=texts.BTN_MONEY_ASK_OWNER)
    await _run(money_ask.type_amount, _typed("250000"), context, ask_for_money=fake_ask)

    assert sent_args["amount"] == "250000.00"


async def test_something_that_is_not_a_number_asks_again():
    context = _Context(ask_who="4", ask_who_name="Կենտրոն")

    state, sent = await _run(money_ask.type_amount, _typed("մի քիչ"), context)

    assert state == money_ask.ASK_AMOUNT
    assert sent[-1][0] == texts.CASH_BAD_AMOUNT


async def test_a_refusal_leaves_the_worker_able_to_try_again():
    """The shop shut while this was being typed, or the number was wrong. Another go
    beats starting from the list."""
    async def fake_ask(**kwargs):
        raise ApiError("validation_error", "Այդ խանութը փակ է։")

    context = _Context(ask_who="4", ask_who_name="Կենտրոն")
    state, sent = await _run(
        money_ask.type_amount, _typed("5000"), context, ask_for_money=fake_ask
    )

    assert state == money_ask.ASK_AMOUNT
    assert "Այդ խանութը փակ է։" in [text for text, _ in sent]
    assert "ask_key" not in context.user_data, "a fresh key for the next try"


async def test_a_network_failure_ends_the_flow_rather_than_looping():
    async def fake_ask(**kwargs):
        raise ApiUnavailable()

    context = _Context(ask_who="4", ask_who_name="Կենտրոն")
    state, _ = await _run(
        money_ask.type_amount, _typed("5000"), context, ask_for_money=fake_ask
    )

    assert state == ConversationHandler.END
    assert context.user_data == {}


# -- answering one -------------------------------------------------------------

async def test_accepting_forwards_the_verdict_and_says_what_it_cost():
    calls = []

    async def fake_decide(telegram_id, request_id, accept):
        calls.append((request_id, accept))
        return _asked(status="accepted")

    state, sent = await _run(
        money_ask.decide, _tapped(f"{keyboards.CB_MONEY_REQUEST}:3:y"), _Context(),
        decide_money_request=fake_decide,
    )

    assert state == ConversationHandler.END
    assert calls == [(3, True)]
    assert "5,000" in sent[-1][0]
    assert "Աբովյան" in sent[-1][0], "the shop it is going to"
    assert "դրամարկղից" in sent[-1][0], "and that it came out of this drawer"


async def test_the_owner_accepting_is_not_told_their_drawer_was_touched():
    """They have not got one. The sentence that explains what confirming costs a shop
    would simply be wrong for them."""
    async def fake_decide(telegram_id, request_id, accept):
        return _asked(status="accepted", of_owner=True)

    _, sent = await _run(
        money_ask.decide, _tapped(f"{keyboards.CB_MONEY_REQUEST}:3:y"), _Context(),
        decide_money_request=fake_decide,
    )

    assert "5,000" in sent[-1][0]
    assert "ձեր դրամարկղից" not in sent[-1][0]


async def test_refusing_travels_as_a_refusal():
    calls = []

    async def fake_decide(telegram_id, request_id, accept):
        calls.append((request_id, accept))
        return _asked(status="rejected")

    _, sent = await _run(
        money_ask.decide, _tapped(f"{keyboards.CB_MONEY_REQUEST}:3:n"), _Context(),
        decide_money_request=fake_decide,
    )

    assert calls == [(3, False)]
    assert "5,000" in sent[-1][0]


async def test_an_answer_that_cannot_be_rendered_is_never_reported_as_a_failure():
    """Everything after the server accepts it is presentation. The money has already
    moved, or already not."""
    async def fake_decide(telegram_id, request_id, accept):
        return {"ok": True}          # a shape the confirmation cannot render

    _, sent = await _run(
        money_ask.decide, _tapped(f"{keyboards.CB_MONEY_REQUEST}:3:y"), _Context(),
        decide_money_request=fake_decide,
    )

    assert sent[-1][0] == texts.MONEY_ASK_DECIDED_PLAINLY


# -- the screen it lives on ----------------------------------------------------

async def test_nothing_waiting_adds_nothing_to_the_transfers_screen():
    async def fake_pending(telegram_id):
        return {"ok": True, "incoming": []}

    _, sent = await _run(
        money_ask.show_waiting, _typed("x"), _Context(),
        pending_money_requests=fake_pending,
    )

    assert sent == []


async def test_requests_waiting_are_listed_with_their_own_buttons():
    """The way back to a request whose pushed message was missed."""
    async def fake_pending(telegram_id):
        return {
            "ok": True,
            "incoming": [
                {"id": 3, "amount": "5000.00", "store": "Աբովյան", "worker": "Անի"}
            ],
        }

    _, sent = await _run(
        money_ask.show_waiting, _typed("x"), _Context(),
        pending_money_requests=fake_pending,
    )

    text, markup = sent[-1]
    assert "5,000" in text and "Աբովյան" in text
    assert [b.callback_data for row in markup.inline_keyboard for b in row] == [
        f"{keyboards.CB_MONEY_REQUEST}:3:y",
        f"{keyboards.CB_MONEY_REQUEST}:3:n",
    ]


def test_the_buttons_and_prefix_match_the_servers():
    """The web service mints these onto the message it pushes — MONEY_REQUEST_CALLBACK
    and BTN_MONEY_REQUEST_* in web/app/texts.py — and the bot listens for them here."""
    assert keyboards.CB_MONEY_REQUEST == "mq"
    assert texts.BTN_MONEY_ASK_YES == "✅ Հաստատել"
    assert texts.BTN_MONEY_ASK_NO == "❌ Մերժել"
