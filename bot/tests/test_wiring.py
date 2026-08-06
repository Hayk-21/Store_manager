"""The application actually assembles, and the sell flow's own logic holds."""

from __future__ import annotations

from decimal import Decimal

from telegram.ext import ConversationHandler

from app import format, keyboards, texts
from app.__main__ import BUTTON_LABELS, build
from app.handlers import sell, shift


def test_the_application_builds_with_every_handler_registered():
    """A typo in a handler name or a bad pattern only shows up here."""
    application = build()
    handlers = application.handlers[0]

    assert any(isinstance(h, ConversationHandler) for h in handlers), "the sell flow is missing"
    assert len(handlers) >= 10


def test_the_sell_flow_is_registered_after_the_shift_buttons():
    """Order matters: the conversation swallows plain text, so the reply-keyboard
    buttons have to be matched first or "Ավարտել իմ հերթափոխը" would be treated
    as an item name."""
    handlers = build().handlers[0]
    conversation_at = next(
        i for i, h in enumerate(handlers) if isinstance(h, ConversationHandler)
    )
    button_positions = [
        i
        for i, h in enumerate(handlers)
        if getattr(h, "callback", None)
        in (shift.end_shift, shift.confirm_close_store, shift.status, shift.ask_location, sell.undo)
    ]

    assert len(button_positions) == 5, "a reply-keyboard button lost its handler"
    assert max(button_positions) < conversation_at


def test_no_button_label_is_ever_treated_as_a_product_name():
    """The bug from the field: tapping "📍 Ուղարկել տեղորոշումը" on Telegram
    Desktop sends the *label* as text, because desktop cannot attach a location.
    That fell through to the sell flow, which searched for a product called
    "📍 Ուղարկել տեղորոշումը" and answered "you are not on shift" — true, and
    completely baffling to the cashier.

    Every label the bot puts on a button has to be excluded from that filter.
    """
    from app.__main__ import BUTTON_LABELS

    on_screen = {
        value
        for name, value in vars(texts).items()
        if name.startswith("BTN_") and isinstance(value, str)
    }

    assert on_screen <= set(BUTTON_LABELS), (
        "a button label is missing from BUTTON_LABELS and would be searched for "
        f"as a product: {on_screen - set(BUTTON_LABELS)}"
    )


def test_the_location_label_has_its_own_handler():
    """Not just excluded from the sell flow — it needs an answer of its own,
    otherwise the tap silently does nothing at all."""
    from app.handlers import common

    handlers = build().handlers[0]

    assert any(
        getattr(h, "callback", None) is common.location_from_desktop for h in handlers
    ), "tapping the location button on desktop would go unanswered"


def test_every_button_can_escape_the_sell_flow():
    """The bug from the shop: a cashier halfway through entering a quantity
    pressed a keyboard button, the label was read as the quantity, and there was
    no way out of the flow at all.

    Two halves to the fix, both checked here: the states must only consume text
    the cashier actually typed, and every button must appear as a fallback.
    """
    from telegram.ext import ConversationHandler

    flow = next(h for h in build().handlers[0] if isinstance(h, ConversationHandler))

    # 1. No state may swallow a button label.
    for state, handlers in flow.states.items():
        for handler in handlers:
            filt = getattr(handler, "filters", None)
            if filt is None:
                continue
            for label in BUTTON_LABELS:
                assert not filt.check_update(_message(label)), (
                    f"state {state} would treat {label!r} as free text"
                )

    # 2. Every button label has a fallback that matches it.
    for label in BUTTON_LABELS:
        assert any(
            getattr(h, "filters", None) is not None and h.filters.check_update(_message(label))
            for h in flow.fallbacks
        ), f"no way out of the sell flow when {label!r} is pressed"


def _message(text: str):
    """The smallest thing a telegram.ext filter will accept."""
    from telegram import Chat, Message, Update, User

    user = User(id=1, first_name="T", is_bot=False)
    message = Message(
        message_id=1,
        date=None,
        chat=Chat(id=1, type="private"),
        from_user=user,
        text=text,
    )
    return Update(update_id=1, message=message)


def test_the_sell_flows_cancel_button_is_not_shadowed():
    """It used to share callback data with the close-store confirmation, whose
    handler is registered first — so tapping cancel printed "cancelled" while
    the conversation quietly stayed open."""
    assert keyboards.CB_CANCEL != keyboards.CB_DISMISS

    handlers = build().handlers[0]
    from telegram.ext import ConversationHandler

    conversation_at = next(
        i for i, h in enumerate(handlers) if isinstance(h, ConversationHandler)
    )
    for index, handler in enumerate(handlers[:conversation_at]):
        pattern = getattr(handler, "pattern", None)
        if pattern is not None:
            assert keyboards.CB_CANCEL not in pattern.pattern, (
                f"handler {index} claims the sell flow's cancel before it can"
            )


def test_item_buttons_carry_the_id_not_the_name():
    """What gets sold is an id from a button; typing only ever searches."""
    markup = keyboards.item_choices(
        [{"id": 42, "name": "HQD Cuvie Plus", "count": 7, "sell_price": "3500.00"}]
    )

    button = markup.inline_keyboard[0][0]
    assert button.callback_data == f"{keyboards.CB_ITEM}:42"
    assert "HQD Cuvie" in button.text
    assert "7 հատ" in button.text


def test_an_out_of_stock_item_is_shown_but_marked():
    """Hiding it would make a cashier think the search is broken."""
    markup = keyboards.item_choices(
        [{"id": 1, "name": "Elf Bar", "count": 0, "sell_price": "4000.00"}]
    )

    assert texts.OUT_OF_STOCK_HINT in markup.inline_keyboard[0][0].text


def test_callback_labels_fit_telegrams_limit():
    """Telegram truncates button text and rejects callback_data over 64 bytes."""
    markup = keyboards.item_choices(
        [{"id": 999999, "name": "Ա" * 200, "count": 3, "sell_price": "3500.00"}]
    )

    button = markup.inline_keyboard[0][0]
    assert len(button.text) <= 64
    assert len(button.callback_data.encode()) <= 64


def test_money_renders_the_way_the_website_does():
    assert format.money("7000.00") == "7,000 ֏"
    assert format.money(Decimal("1234567.50")) == "1,234,568 ֏"
    assert format.money(None) == "0 ֏"


def test_durations_read_as_armenian():
    assert format.duration_minutes(748) == "12ժ 28ր"
    assert format.duration_minutes(45) == "45ր"
    assert format.duration_minutes(None) == "—"


def test_the_sold_summary_splits_cash_from_card():
    summary = format.sold_summary(
        {"total": "12000.00", "cash_total": "8000.00", "card_total": "4000.00"}
    )

    assert "12,000 ֏" in summary
    assert "կանխիկ 8,000 ֏" in summary
    assert "քարտ 4,000 ֏" in summary
