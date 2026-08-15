"""The messages a worker gets when their shift ends.

Two of them, and the drawer is the last — deliberately. It goes out from
``report_end``, which every way of ending a shift funnels through: the worker
pressing the button, the write-up, and the last one out closing the shop. So
neither the offer to count the till nor its position can depend on which route
they took.

Written because "I still cannot see the button" is not something a screenshot can
settle. It turned out to be true and to be my fault — the prompt was the middle
message and the one after it pushed the button off the screen — and the only way to
know where the code puts it is to run the code.

There were three. The middle one was the welcome, sent only to restore the reply
keyboard, which greeted a worker who had just finished with «Բարև, ։» and told them
how to open a shop they had closed. The keyboard rides on the summary now.
"""

from __future__ import annotations

from unittest import mock

from telegram import Chat, Message, Update, User

from app import keyboards, texts
from app.handlers import shift


def _summary(
    store_closed: bool = True, halved: bool = False, unpaid: str = "0.00"
) -> dict:
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
        "salary_unpaid": unpaid,
        "bonus_paid": "0.00",
        "bonus_unpaid": "0.00",
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

    assert keyboards.CB_TILL in tapped, "no way to record the till"


async def test_only_whoever_locked_up_is_asked_to_count():
    """This asserted the opposite, on the reasoning that a shift ending is a shift to
    account for. But the drawer belongs to the shop, not to a shift: one of two
    cashiers going home cannot hand the owner the change their colleague needs for the
    next four hours, and the count would be stale on their very next sale.
    """
    closed = await _sent(_summary(store_closed=True))
    assert any(hasattr(markup, "inline_keyboard") for _, markup in closed)

    still_open = await _sent(_summary(store_closed=False))
    assert not any(hasattr(markup, "inline_keyboard") for _, markup in still_open)
    assert len(still_open) == 1, "nothing about the drawer at all"


async def test_the_drawer_prompt_is_the_last_thing_on_screen():
    """The bug this closes, and it was mine. The prompt was the middle of three
    messages, so the one after it pushed the button up off the top of a phone —
    the worker was asked to count the till and then shown something else, and
    reported the button missing. Nothing may come after it.
    """
    sent = await _sent(_summary())

    assert len(sent) == 2
    assert sent[-1][0] == texts.TILL_HANDOVER_PROMPT
    assert hasattr(sent[-1][1], "inline_keyboard"), "and the button is on it"


async def test_the_reply_keyboard_comes_back_without_a_second_hello():
    """It used to ride on a re-sent welcome, which greeted somebody who had just
    gone home and told them how to open the shop they had locked."""
    sent = await _sent(_summary())

    assert texts.BTN_OPEN in str(sent[0][1]), "the summary carries the keyboard"
    assert not any("Բարև" in text for text, _ in sent), "no greeting at the end of a day"


async def test_the_prompt_says_the_money_stays_in_the_shop():
    """The reason for asking. Without it the worker reads "how much is left?" as
    "how much are you handing over?" and empties the drawer."""
    sent = await _sent(_summary())

    assert "թողնում" in sent[-1][0], "it is money they leave, not money they hand over"
    assert "խանութ" in sent[-1][0], "and it stays in the shop"


async def test_a_wage_the_drawer_could_not_cover_is_explained():
    """A shop that took the day on card cannot pay a cash wage out of an empty
    drawer. The worker goes home with less in hand than the figure above, and
    without a sentence saying so that reads as a deduction."""
    sent = await _sent(_summary(unpaid="2500.00"))

    assert "2,500" in sent[0][0]
    assert "ղեկավարը" in sent[0][0], "and who owes it"


async def test_a_fully_paid_wage_says_nothing_about_a_debt():
    sent = await _sent(_summary(unpaid="0.00"))

    assert "ղեկավարը կվճարի" not in sent[0][0]


async def test_a_negative_closing_cash_is_not_shown_at_all():
    """«Կանխիկ՝ -4,500 ֏» is what a worker saw and reported. A drawer cannot hold less
    than nothing, so that figure is a bookkeeping artefact rather than anything they can
    act on, and the honest answer is not a smaller negative number."""
    summary = _summary()
    summary["store_totals_after"] = {"cash": "-4500.00", "card": "16000.00"}

    sent = await _sent(summary)

    assert "-4,500" not in sent[0][0]
    assert "4,500" not in sent[0][0], "not without its sign either"


async def test_the_shops_card_income_is_never_reported_to_the_worker():
    """«Քարտով մուտք» used to close this message.

    It is the whole session's card income — every delivery, and every colleague's
    sales — printed under the name of whoever happened to be last out of the door, at
    the moment they are reading their own day back. What this worker sold on card is
    stated above, under their own name, and that is the only card figure a cashier
    has any use for.
    """
    summary = _summary()
    summary["store_totals_after"] = {"cash": "492000.00", "card": "16000.00"}

    sent = await _sent(summary)

    assert "16,000" not in sent[0][0], "the shop's card takings are not theirs"
    assert "Քարտով մուտք" not in sent[0][0]
    assert "492,000" in sent[0][0], "the drawer still is"


async def test_a_positive_closing_cash_is_still_shown():
    sent = await _sent(_summary())

    assert "492,000" in sent[0][0]


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
            data=keyboards.CB_TILL,
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


# -- deliveries, which are not this worker's sales ---------------------------

async def test_the_summary_counts_deliveries_without_pricing_them():
    """The shop took that money; the worker did not sell it, and their wage and
    bonus do not turn on it. So they are told how many orders went out — enough to
    tell that what they entered landed — and not what it came to."""
    summary = _summary()
    summary["deliveries"] = {"receipts": 3, "cash_total": "0.00",
                             "card_total": "44000.00", "total": "44000.00"}

    body = (await _sent(summary))[0][0]

    assert "Առաքում՝ 3 պատվեր" in body
    assert "44,000" not in body
    assert "չի հաշվվում" in body
    assert "500,000" in body, "their own sales are untouched"


async def test_a_shift_with_no_deliveries_says_nothing_about_them():
    body = (await _sent(_summary()))[0][0]

    assert "Առաքում" not in body
