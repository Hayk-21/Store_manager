"""The application actually assembles, and the sell flow's own logic holds."""

from __future__ import annotations

from decimal import Decimal

from telegram.ext import ConversationHandler

from app import format, keyboards, texts
from app.__main__ import build
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
