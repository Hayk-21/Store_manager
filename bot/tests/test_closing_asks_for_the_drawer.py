"""The drawer is counted before the shift closes, not offered afterwards.

«Հաստատել» used to be the end of it: the shift closed, the message saying so carried
a button, and counting the till was something the worker could do if they felt like
it on their way out the door. Most did not, and a float nobody counted is the one the
next shift opens against.

So the order changed. The write-up is sent, the server refuses to shut the shop
without a figure, the bot asks for it, and the same close-out goes again with the
number on it. Nothing is written by the refused attempt — which is why the basket
survives it and why the worker can still back out with «Չեղարկել» and go on serving.

Who gets asked is the server's business and not the bot's: a cashier going home at
six while a colleague works until ten has no drawer to settle, and the shop stays
open behind them. The bot cannot know that, so it does not guess — it sends, and
asks only when told to.
"""

from __future__ import annotations

from unittest import mock

from telegram import CallbackQuery, Chat, Message, Update, User
from telegram.ext import ConversationHandler

from app import texts
from app.api import ApiError, ApiUnavailable
from app.handlers import closeout

REFUSED = ApiError(
    "till_count_required",
    "Հերթափոխը դեռ չի փակվել։ Նախ հաշվեք դրամարկղը։",
    status=422,
)


def _summary() -> dict:
    return {
        "session_id": 3,
        "store_id": 1,
        "store_name": "Խանութ 1",
        "started_at": "2026-08-09T09:00:00+04:00",
        "ended_at": "2026-08-09T18:00:00+04:00",
        "duration_minutes": 540,
        "sales": {"receipts": 1, "cash_total": "7000.00",
                  "card_total": "0.00", "total": "7000.00"},
        "salary_deducted": "0.00",
        "salary_unpaid": "0.00",
        "bonus_paid": "0.00",
        "bonus_unpaid": "0.00",
        "salary_halved": False,
        "full_shift_hours": 8,
        "store_closed": True,
        "store_totals_after": {"cash": "7000.00", "card": "0.00"},
    }


def _closed(counted: str = "3000.00", handed: str = "4000.00") -> dict:
    return {
        "ok": True,
        "duplicate": False,
        "summary": _summary(),
        "till_count": {
            "id": 1, "kind": "close", "counted": counted,
            "expected": "7000.00", "handed_over": handed,
        },
    }


class _Context:
    def __init__(self, **data) -> None:
        self.user_data = dict(data)


def _basket() -> list[dict]:
    return [{"item_id": 7, "name": "HQD Cuvie", "quantity": 2,
             "unit_price": "3500.00", "payment_method": "cash"}]


def _tapped() -> Update:
    """«Հաստատել» on the summary."""
    user = User(id=1, first_name="Անի", is_bot=False)
    return Update(
        update_id=1,
        callback_query=CallbackQuery(
            id="1", from_user=user, chat_instance="1", data="cs:go",
            message=Message(message_id=1, date=None,
                            chat=Chat(id=1, type="private"), from_user=user),
        ),
    )


def _typed(text: str) -> Update:
    user = User(id=1, first_name="Անի", is_bot=False)
    return Update(
        update_id=2,
        message=Message(
            message_id=2, date=None, chat=Chat(id=1, type="private"),
            from_user=user, text=text,
        ),
    )


class _Server:
    """The web service, remembering how it was called."""

    def __init__(self, *answers) -> None:
        self.answers = list(answers)
        self.calls: list[dict] = []

    async def __call__(self, telegram_id, lines, key, counted=None):
        self.calls.append({"key": key, "counted": counted, "lines": lines})
        answer = self.answers.pop(0) if self.answers else _closed()
        if isinstance(answer, Exception):
            raise answer
        return answer


async def _run(handler, update, context, server) -> tuple[int, list, _Server]:
    """Run one step, capturing everything sent back."""
    sent: list[tuple[str, object]] = []

    async def capture(self, text, *args, **kwargs):
        sent.append((text, kwargs.get("reply_markup")))
        return Message(message_id=9, date=None, chat=Chat(id=1, type="private"))

    async def nothing(*args, **kwargs):
        return None

    with mock.patch.object(Message, "reply_text", capture), \
         mock.patch.object(CallbackQuery, "answer", nothing), \
         mock.patch.object(CallbackQuery, "edit_message_reply_markup", nothing), \
         mock.patch("app.handlers.closeout.api.close_out", server):
        state = await handler(update, context)
    return state, sent, server


# -- being asked --------------------------------------------------------------

async def test_confirming_the_write_up_asks_for_the_drawer():
    context = _Context(closeout_basket=_basket(), co_key="keykeykey")

    state, sent, _ = await _run(
        closeout.submit, _tapped(), context, _Server(REFUSED)
    )

    assert state == closeout.ASK_TILL
    assert texts.TILL_ASK_BEFORE_CLOSE in [text for text, _ in sent]


async def test_the_first_attempt_carries_no_figure():
    """It cannot: whether this close shuts the shop depends on who else is still on,
    which is the server's to know. So the bot asks by being refused."""
    context = _Context(closeout_basket=_basket(), co_key="keykeykey")

    _, _, server = await _run(closeout.submit, _tapped(), context, _Server(REFUSED))

    assert server.calls[0]["counted"] is None


async def test_the_question_says_the_shift_is_not_closed_yet():
    """The worker pressed «Հաստատել» expecting to be finished. A question with no
    reason attached reads as the bot having lost the write-up."""
    assert "չի փակվի" in texts.TILL_ASK_BEFORE_CLOSE
    assert "մնում է խանութում" in texts.TILL_ASK_BEFORE_CLOSE, (
        "money that stays, not money handed over"
    )


async def test_only_cancel_is_offered_while_the_number_is_expected():
    """A stray tap on «Վաճառել» must not be read as an amount."""
    context = _Context(closeout_basket=_basket(), co_key="keykeykey")

    _, sent, _ = await _run(closeout.submit, _tapped(), context, _Server(REFUSED))

    keyboard = str(sent[-1][1])
    assert texts.BTN_CANCEL in keyboard
    assert texts.BTN_SELL not in keyboard


# -- answering ----------------------------------------------------------------

async def test_the_number_closes_the_shift_under_the_same_key():
    """The refused attempt wrote nothing, so this is the same close-out rather than a
    second one — and a retry that minted a new key would be a day declared twice."""
    context = _Context(closeout_basket=_basket(), co_key="keykeykey")
    server = _Server(REFUSED, _closed())
    await _run(closeout.submit, _tapped(), context, server)

    state, sent, _ = await _run(closeout.type_till, _typed("3000"), context, server)

    assert state == ConversationHandler.END
    assert server.calls[1]["counted"] == "3000.00"
    assert server.calls[1]["key"] == server.calls[0]["key"]
    assert server.calls[1]["lines"] == server.calls[0]["lines"], "the same day, too"


async def test_what_the_count_came_to_is_reported_back():
    """It has already been taken, so there is nothing left to offer — only to say what
    stays in the shop and what the worker is carrying to the owner."""
    context = _Context(closeout_basket=_basket(), co_key="keykeykey")
    server = _Server(_closed(counted="3000.00", handed="4000.00"))

    _, sent, _ = await _run(closeout.type_till, _typed("3000"), context, server)

    said = "\n".join(text for text, _ in sent)
    assert "3,000" in said, "what stays in the shop"
    assert "4,000" in said, "and what goes to the owner"


async def test_the_drawer_button_is_not_offered_once_it_has_been_answered():
    """Asking again, after the fact, is how the count got skipped in the first place."""
    context = _Context(closeout_basket=_basket(), co_key="keykeykey")

    _, sent, _ = await _run(closeout.type_till, _typed("3000"), context, _Server(_closed()))

    assert not any(
        hasattr(markup, "inline_keyboard") for _, markup in sent
    ), "nothing left to tap"
    assert texts.TILL_HANDOVER_PROMPT not in [text for text, _ in sent]


async def test_a_number_that_is_not_a_number_is_asked_for_again():
    context = _Context(closeout_basket=_basket(), co_key="keykeykey")
    server = _Server()

    state, sent, _ = await _run(closeout.type_till, _typed("շատ"), context, server)

    assert state == closeout.ASK_TILL
    assert sent[-1][0] == texts.TILL_BAD_AMOUNT
    assert server.calls == [], "and nothing was sent to the server"


async def test_a_negative_drawer_is_asked_for_again():
    context = _Context(closeout_basket=_basket(), co_key="keykeykey")
    server = _Server()

    state, _, _ = await _run(closeout.type_till, _typed("-500"), context, server)

    assert state == closeout.ASK_TILL
    assert server.calls == []


async def test_a_figure_the_server_will_not_take_keeps_the_write_up():
    """A mistyped nought comes back as a refusal, and the whole day's list must not go
    with it — the worker has typed it once and is standing at the door."""
    context = _Context(closeout_basket=_basket(), co_key="keykeykey")
    refusal = ApiError("validation_error", "Սխալ գումար։", status=422)

    state, sent, _ = await _run(
        closeout.type_till, _typed("99999999999"), context, _Server(refusal)
    )

    assert state == closeout.ASK_TILL
    assert "Սխալ գումար։" in [text for text, _ in sent]
    assert context.user_data["closeout_basket"] == _basket(), "still in hand"


# -- the shop that stays open -------------------------------------------------

async def test_a_shift_that_leaves_the_shop_open_is_never_asked():
    """The server does not refuse, so there is no question — the colleague still
    serving customers will be the one to settle the drawer."""
    context = _Context(closeout_basket=_basket(), co_key="keykeykey")
    summary = _summary() | {"store_closed": False}
    server = _Server({"ok": True, "duplicate": False, "summary": summary})

    state, sent, _ = await _run(closeout.submit, _tapped(), context, server)

    assert state == ConversationHandler.END
    assert texts.TILL_ASK_BEFORE_CLOSE not in [text for text, _ in sent]
    assert server.calls[0]["counted"] is None


async def test_an_older_service_that_never_asks_still_offers_the_button():
    """Both halves deploy separately, so for a minute the bot is newer than the
    service. It closes the shop without asking, and the worker is offered the count
    the way they used to be rather than being left with no way to record it."""
    context = _Context(closeout_basket=_basket(), co_key="keykeykey")
    without = {"ok": True, "duplicate": False, "summary": _summary()}

    _, sent, _ = await _run(closeout.submit, _tapped(), context, _Server(without))

    assert texts.TILL_HANDOVER_PROMPT in [text for text, _ in sent]
    assert any(hasattr(markup, "inline_keyboard") for _, markup in sent)


def test_the_amount_step_is_wired_to_something():
    """A conversation state with no handler is a worker typing a number into a bot
    that has stopped listening — with their whole day still unwritten."""
    from app.__main__ import build

    flow = next(
        handler for handler in build().handlers[0]
        if getattr(handler, "states", None) and closeout.PICK_ITEM in handler.states
    )
    typed = _typed("3000")

    assert closeout.ASK_TILL in flow.states
    assert any(h.check_update(typed) for h in flow.states[closeout.ASK_TILL])


def test_backing_out_of_the_drawer_question_abandons_the_write_up():
    """«Չեղարկել» is the only button on that keyboard. Nothing was written, so the
    shift is still open and can be closed properly — but it must not be a way of
    closing it without counting."""
    from app.__main__ import build

    flow = next(
        handler for handler in build().handlers[0]
        if getattr(handler, "states", None) and closeout.PICK_ITEM in handler.states
    )

    assert any(
        h.check_update(_typed(texts.BTN_CANCEL)) for h in flow.fallbacks
    ), "the cancel button would do nothing at all"


async def test_a_connection_lost_at_the_last_step_keeps_the_day():
    """The worst moment for the network to go: a whole day typed up, the number given,
    and the worker at the door. Nothing was written, so typing it again finishes the
    same close-out."""
    context = _Context(closeout_basket=_basket(), co_key="keykeykey")
    server = _Server(ApiUnavailable(), _closed())

    state, _, _ = await _run(closeout.type_till, _typed("3000"), context, server)
    assert state == closeout.ASK_TILL
    assert context.user_data["closeout_basket"] == _basket()

    state, _, _ = await _run(closeout.type_till, _typed("3000"), context, server)
    assert state == ConversationHandler.END
    assert server.calls[1]["key"] == server.calls[0]["key"]
