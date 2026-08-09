"""The messages a worker gets when their shift ends.

Three of them, and the drawer is the last — deliberately. It goes out from
``report_end``, which every way of ending a shift funnels through: the worker
pressing the button, the write-up, and the last one out closing the shop. So
neither the offer to count the till nor its position can depend on which route
they took.

Written because "I still cannot see the button" is not something a screenshot can
settle. It turned out to be true and to be my fault — the prompt was the middle
message and the one after it pushed the button off the screen — and the only way to
know where the code puts it is to run the code.
"""

from __future__ import annotations

from unittest import mock

from telegram import Chat, Message, ReplyKeyboardRemove, Update, User

from app import keyboards, texts
from app.handlers import shift


def _summary(store_closed: bool = True, halved: bool = False) -> dict:
    """What the web service sends back for a finished shift."""
    return {
        "session_id": 3,
        "store_id": 1,
        "store_name": "Խանութ 1",
        "started_at": "2026-08-09T09:00:00+04:00",
        "ended_at": "2026-08-09T18:00:00+04:00",
        "duration_minutes": 540,
        "sales": {"receipts": 1, "cash_total": "500000.00",
                  "card_total": "0.00", "total": "500000.00"},
        "salary_deducted": "8000.00",
        "salary_halved": halved,
        "full_shift_hours": 8,
        "store_closed": store_closed,
        "store_totals_after": {"cash": "492000.00", "card": "0.00"},
    }


def _update() -> Update:
    user = User(id=1, first_name="Հայկ", is_bot=False)
    return Update(
        update_id=1,
        message=Message(
            message_id=1, date=None, chat=Chat(id=1, type="private"),
            from_user=user, text="x",
        ),
    )


async def _sent(summary: dict) -> list[tuple[str, object]]:
    """Every reply the end-of-shift report makes, as (text, keyboard)."""
    out: list[tuple[str, object]] = []

    async def capture(self, text, *args, **kwargs):
        out.append((text, kwargs.get("reply_markup")))

    with mock.patch.object(Message, "reply_text", capture):
        await shift.report_end(_update(), summary)
    return out


async def test_ending_a_shift_offers_to_count_the_drawer():
    sent = await _sent(_summary())

    keyboards_sent = [markup for _, markup in sent]
    tapped = [
        button.callback_data
        for markup in keyboards_sent
        if hasattr(markup, "inline_keyboard")
        for row in markup.inline_keyboard
        for button in row
    ]

    assert f"{keyboards.CB_TILL}:close" in tapped, "no way to record the till"


async def test_the_drawer_is_offered_whether_or_not_the_shop_closed():
    """One worker of two going home still leaves cash in a drawer, and the shift
    that ends is still theirs to account for."""
    for store_closed in (True, False):
        sent = await _sent(_summary(store_closed=store_closed))
        assert any(
            hasattr(markup, "inline_keyboard") for _, markup in sent
        ), f"store_closed={store_closed} got no till button"


async def test_the_drawer_prompt_is_the_last_thing_on_screen():
    """The bug this closes, and it was mine. The prompt was the middle of three
    messages, so the one after it pushed the button up off the top of a phone —
    the worker was asked to count the till and then shown something else, and
    reported the button missing. Nothing may come after it.
    """
    sent = await _sent(_summary())

    assert len(sent) == 3
    assert isinstance(sent[0][1], ReplyKeyboardRemove), "the summary is first"
    assert texts.BTN_OPEN in str(sent[1][1]), "the welcome carries the reply keyboard"
    assert sent[-1][0] == texts.TILL_HANDOVER_PROMPT
    assert hasattr(sent[-1][1], "inline_keyboard"), "and the button is on it"


async def test_the_prompt_says_the_money_stays_in_the_shop():
    """The reason for asking. Without it the worker reads "how much is left?" as
    "how much are you handing over?" and empties the drawer."""
    sent = await _sent(_summary())

    assert "խանութում" in sent[-1][0]


def test_something_actually_handles_the_drawer_button():
    """A button that appears and does nothing is worse than no button: the worker
    taps it, watches it spin, and has no reason to trust the next one."""
    from telegram import CallbackQuery

    from app.__main__ import build

    user = User(id=1, first_name="T", is_bot=False)
    tap = Update(
        update_id=1,
        callback_query=CallbackQuery(
            id="1", from_user=user, chat_instance="1",
            data=f"{keyboards.CB_TILL}:close",
            message=Message(message_id=1, date=None,
                            chat=Chat(id=1, type="private"), from_user=user),
        ),
    )

    assert any(h.check_update(tap) for h in build().handlers[0]), (
        "the drawer button would spin and do nothing"
    )


async def test_a_halved_wage_is_explained_in_the_summary():
    sent = await _sent(_summary(halved=True))

    assert "կիսով չափ" in sent[0][0]


async def test_a_full_wage_says_nothing_about_halving():
    sent = await _sent(_summary(halved=False))

    assert "կիսով չափ" not in sent[0][0]
