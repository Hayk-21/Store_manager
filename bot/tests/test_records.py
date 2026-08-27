"""Correcting a receipt from «Վիճակ».

A cashier's mistakes used to be reversible for about ten seconds — the undo button
under the confirmation — and after that they belonged to the owner. These cover the
way back: the list of the shift's own receipts, choosing one, and cancelling it.

Two properties matter more than the wording:

* a receipt that is already cancelled cannot be cancelled again, and
* the cancel button carries the id of the receipt it was drawn for, so a tap three
  sales later still reverses the right one.
"""

from __future__ import annotations

import contextlib
from unittest import mock

import pytest
from telegram import CallbackQuery, Chat, Message, Update, User

from app import keyboards, texts
from app.api import ApiError
from app.handlers import records


def _receipt(sale_id: int, at: str = "2026-08-27T18:12:00+04:00", **over) -> dict:
    """One row as the web service sends it — see GET /shift/receipts."""
    return {
        "id": sale_id,
        "sold_at": at,
        "total": "7000.00",
        "payment_method": "cash",
        "is_delivery": False,
        "voided": False,
        "lines": "HQD Cuvie ×2",
        **over,
    }


def _answer(receipts: list[dict], total: int | None = None) -> dict:
    return {
        "ok": True,
        "store_name": "Խանութ 1",
        "total_receipts": len(receipts) if total is None else total,
        "receipts": receipts,
    }


class _Context:
    def __init__(self, **data) -> None:
        self.user_data = dict(data)


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


def _message_update() -> Update:
    """«Վիճակ» arrives as a tapped reply-keyboard button, which is ordinary text."""
    user = User(id=1, first_name="Հայկ", is_bot=False)
    return Update(
        update_id=1,
        message=Message(
            message_id=1, date=None, chat=Chat(id=1, type="private"),
            from_user=user, text=texts.BTN_STATUS,
        ),
    )


class _Screen:
    """Collects what the handler put on the screen."""

    def __init__(self) -> None:
        self.replies: list[tuple[str, dict]] = []
        self.answers: list[str | None] = []
        self.edited: list[str] = []
        self.markup_cleared = False

    @contextlib.contextmanager
    def patches(self):
        """Stand in for every way a handler can put something on the screen."""
        screen = self

        async def answer(self, text=None, **kwargs):
            screen.answers.append(text)

        async def reply_text(self, text, **kwargs):
            screen.replies.append((text, kwargs))

        async def edit_text(self, text, **kwargs):
            screen.edited.append(text)

        async def edit_markup(self, reply_markup=None, **kwargs):
            screen.markup_cleared = reply_markup is None

        with (
            mock.patch.object(CallbackQuery, "answer", answer),
            mock.patch.object(CallbackQuery, "edit_message_text", edit_text),
            mock.patch.object(CallbackQuery, "edit_message_reply_markup", edit_markup),
            mock.patch.object(Message, "reply_text", reply_text),
        ):
            yield screen

    @property
    def last(self) -> str:
        return self.replies[-1][0]

    @property
    def last_markup(self):
        return self.replies[-1][1].get("reply_markup")


def _labels(markup) -> list[str]:
    return [b.text for row in markup.inline_keyboard for b in row]


def _datas(markup) -> list[str]:
    return [b.callback_data for row in markup.inline_keyboard for b in row]


# -- the list ----------------------------------------------------------------

async def test_the_list_offers_every_receipt_of_the_shift():
    screen = _Screen()
    with screen.patches(), mock.patch.object(records.api, "shift_receipts",
        mock.AsyncMock(return_value=_answer([_receipt(41), _receipt(42)])),
    ):
        await records.show_list(_tap(keyboards.CB_FIX), _Context())

    assert _datas(screen.last_markup)[:2] == ["fxp:41", "fxp:42"]


async def test_a_shift_with_no_sales_says_so_rather_than_drawing_an_empty_list():
    screen = _Screen()
    with screen.patches(), mock.patch.object(records.api, "shift_receipts", mock.AsyncMock(return_value=_answer([])),
    ):
        await records.show_list(_tap(keyboards.CB_FIX), _Context())

    assert screen.last == texts.FIX_NOTHING


async def test_a_truncated_list_says_how_many_there_really_are():
    """«The one I want is not here» and «there are none» need different answers."""
    screen = _Screen()
    rows = [_receipt(n) for n in range(100, 120)]
    with screen.patches(), mock.patch.object(records.api, "shift_receipts",
        mock.AsyncMock(return_value=_answer(rows, total=31)),
    ):
        await records.show_list(_tap(keyboards.CB_FIX), _Context())

    assert "20" in screen.last and "31" in screen.last


async def test_a_cancelled_receipt_is_shown_but_cannot_be_cancelled_again():
    """Dropping it would leave the cashier unable to tell a receipt they already
    cancelled from one that was never there."""
    screen = _Screen()
    with screen.patches(), mock.patch.object(records.api, "shift_receipts",
        mock.AsyncMock(return_value=_answer([_receipt(41, voided=True), _receipt(42)])),
    ):
        await records.show_list(_tap(keyboards.CB_FIX), _Context())

    datas = _datas(screen.last_markup)
    assert "fxp:41" not in datas, "a cancelled receipt is not a button that cancels"
    assert keyboards.CB_FIX_NOOP in datas
    assert "fxp:42" in datas


def test_the_dead_row_is_not_the_conversation_noop():
    """CB_NOOP is only handled inside the restock and transfer flows. This list lives
    outside every flow, so a tap on it would spin until Telegram gave up."""
    assert keyboards.CB_FIX_NOOP != keyboards.CB_NOOP


# -- one receipt -------------------------------------------------------------

async def test_choosing_one_reads_it_back_before_anything_happens():
    screen = _Screen()
    with screen.patches(), mock.patch.object(records.api, "shift_receipts",
        mock.AsyncMock(return_value=_answer([_receipt(41)])),
    ):
        await records.pick(_tap("fxp:41"), _Context())

    assert "HQD Cuvie ×2" in screen.last
    assert "7,000" in screen.last
    assert "18:12" in screen.last
    assert "fxv:41" in _datas(screen.last_markup), "and the button names that receipt"


async def test_a_receipt_that_has_gone_says_so_instead_of_failing():
    """The list is a snapshot: the shift can end between drawing it and tapping it."""
    screen = _Screen()
    with screen.patches(), mock.patch.object(records.api, "shift_receipts",
        mock.AsyncMock(return_value=_answer([_receipt(42)])),
    ):
        await records.pick(_tap("fxp:41"), _Context())

    assert texts.BTN_STATUS in screen.last
    assert screen.last_markup is None


async def test_one_already_cancelled_is_refused_at_the_second_screen_too():
    screen = _Screen()
    with screen.patches(), mock.patch.object(records.api, "shift_receipts",
        mock.AsyncMock(return_value=_answer([_receipt(41, voided=True)])),
    ):
        await records.pick(_tap("fxp:41"), _Context())

    assert screen.last == texts.FIX_ALREADY_VOIDED


# -- cancelling --------------------------------------------------------------

def _void_answer() -> dict:
    """What POST /sale/void sends back — see void_last_sale in web/app/services."""
    return {
        "ok": True,
        "voided": {"sale_id": 41, "total": "7000.00", "payment_method": "cash",
                   "is_delivery": False, "sold_at": "2026-08-27T18:12:00+04:00",
                   "restored": [{"item_id": 3, "quantity": 2}]},
        "store_totals": {"cash": "20000.00", "card": "4000.00"},
        "sold_totals": {"cash": "0.00", "card": "0.00"},
    }


async def test_cancelling_names_the_receipt_that_was_chosen():
    """Not "the last one". A cashier correcting the third of five must not lose the
    fifth."""
    screen = _Screen()
    void = mock.AsyncMock(return_value=_void_answer())
    with screen.patches(), mock.patch.object(records.api, "void_last", void):
        await records.void(_tap("fxv:41"), _Context())

    assert void.await_args.kwargs["sale_id"] == 41
    assert "7,000" in screen.last


async def test_the_buttons_go_before_the_confirmation_arrives():
    """A cancel button left under a cancelled receipt invites a second tap."""
    screen = _Screen()
    with screen.patches(), mock.patch.object(records.api, "void_last", mock.AsyncMock(return_value=_void_answer())
    ):
        await records.void(_tap("fxv:41"), _Context())

    assert screen.markup_cleared


async def test_a_confirmation_it_cannot_render_still_reads_as_success():
    """The receipt is already cancelled. Saying "failed" would be the opposite of
    the truth, and the cashier would go and cancel something else."""
    screen = _Screen()
    with screen.patches(), mock.patch.object(records.api, "void_last", mock.AsyncMock(return_value={"ok": True}),
    ):
        await records.void(_tap("fxv:41"), _Context())

    assert screen.last == texts.VOID_DONE_PLAINLY
    assert "սխալ" not in screen.last.lower()


async def test_a_refusal_from_the_server_is_shown_in_its_own_words():
    """The server owns "that is not your receipt" and "this shift has ended"."""
    screen = _Screen()
    with screen.patches(), mock.patch.object(records.api, "void_last",
        mock.AsyncMock(side_effect=ApiError("nothing_to_void", "Չեղարկելու բան չկա։")),
    ):
        await records.void(_tap("fxv:41"), _Context())

    assert screen.last == "Չեղարկելու բան չկա։"


async def test_closing_does_not_say_the_word_cancelled():
    """On a screen asking «shall I cancel this receipt?», an answer reading
    «Չեղարկվեց» is the one thing it must not say."""
    screen = _Screen()
    with screen.patches(), mock.patch.object(records.api, "void_last", mock.AsyncMock()):
        await records.close(_tap(keyboards.CB_FIX_CLOSE), _Context())

    assert screen.edited == [texts.FIX_CLOSED]
    assert texts.CANCELLED not in screen.edited


# -- the way in --------------------------------------------------------------

def _session(receipts: int) -> dict:
    """What GET /me carries about the open shift, trimmed to what «Վիճակ» reads."""
    return {
        "ok": True,
        "session": {
            "store_name": "Խանութ 1",
            "started_at": "2026-08-27T09:00:00+04:00",
            "sales": {"receipts": receipts, "total": "7000.00",
                      "cash_total": "7000.00", "card_total": "0.00"},
            "store_totals": {"cash": "8000.00", "carried_in": "1000.00"},
        },
    }


async def test_the_status_screen_offers_the_correction_list():
    """The way in the shop owner asked for: press «Վիճակ», and the way to fix a
    record is on that screen rather than somewhere else."""
    from app.handlers import shift

    screen = _Screen()
    with screen.patches(), mock.patch.object(
        shift.api, "me", mock.AsyncMock(return_value=_session(3))
    ):
        await shift.status(_message_update(), _Context())

    assert texts.BTN_FIX_RECORDS in _labels(screen.last_markup)
    assert keyboards.CB_FIX in _datas(screen.last_markup)


async def test_a_shift_with_nothing_sold_yet_is_not_offered_it():
    """Nothing has been recorded, so there is nothing to correct — and a button that
    leads to «you have no receipts» is one a cashier taps once and distrusts after."""
    from app.handlers import shift

    screen = _Screen()
    with screen.patches(), mock.patch.object(
        shift.api, "me", mock.AsyncMock(return_value=_session(0))
    ):
        await shift.status(_message_update(), _Context())

    markup = screen.last_markup
    assert not hasattr(markup, "inline_keyboard"), "the working keyboard comes back"
