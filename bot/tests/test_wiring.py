"""The application assembles, and no button can trap a cashier."""

from __future__ import annotations

from decimal import Decimal

from telegram import Chat, Message, Update, User
from telegram.ext import ConversationHandler

from app import format, keyboards, texts
from app.__main__ import BUTTON_LABELS, build
from app.handlers import common, shift, stock


def _message(text: str) -> Update:
    """The smallest thing a telegram.ext filter will accept."""
    user = User(id=1, first_name="T", is_bot=False)
    return Update(
        update_id=1,
        message=Message(
            message_id=1, date=None, chat=Chat(id=1, type="private"),
            from_user=user, text=text,
        ),
    )


def _flow() -> ConversationHandler:
    return next(h for h in build().handlers[0] if isinstance(h, ConversationHandler))


def test_the_application_builds_with_every_handler_registered():
    """A typo in a handler name or a bad pattern only shows up here."""
    handlers = build().handlers[0]

    assert any(isinstance(h, ConversationHandler) for h in handlers), "the write-up is missing"
    assert len(handlers) >= 8


def test_no_button_label_is_ever_treated_as_free_text():
    """The bug from the field: tapping a button sends its *label* as text. If a
    state consumes that, the label is read as a product name or a price and the
    cashier cannot get out of the flow at all."""
    on_screen = {
        value
        for name, value in vars(texts).items()
        if name.startswith("BTN_") and isinstance(value, str)
    }

    assert on_screen <= set(BUTTON_LABELS), (
        f"a button label is missing from BUTTON_LABELS: {on_screen - set(BUTTON_LABELS)}"
    )


def test_every_button_can_escape_the_write_up():
    """Two halves: the states must only consume text the cashier actually typed,
    and every button that is not part of the write-up must have a fallback."""
    flow = _flow()
    own = {
        texts.BTN_CO_ADD, texts.BTN_CO_REMOVE, texts.BTN_CO_DONE,
        texts.BTN_CO_ABANDON, texts.BTN_CO_SUBMIT, texts.BTN_CO_SUBMIT_CLOSE,
        texts.BTN_CANCEL, texts.BTN_CASH, texts.BTN_CARD,
        texts.BTN_RETAIL, texts.BTN_WHOLESALE, texts.BTN_CONFIRM_CLOSE,
        texts.BTN_END_SHIFT, texts.BTN_CLOSE_STORE,
    }

    for state, handlers in flow.states.items():
        for handler in handlers:
            filt = getattr(handler, "filters", None)
            if filt is None:
                continue
            for label in BUTTON_LABELS:
                if filt.check_update(_message(label)) and label not in own:
                    raise AssertionError(f"state {state} would consume {label!r} as free text")

    for label in BUTTON_LABELS:
        if label in own:
            continue
        assert any(
            getattr(h, "filters", None) is not None and h.filters.check_update(_message(label))
            for h in flow.fallbacks
        ), f"no way out of the write-up when {label!r} is pressed"


def test_the_write_up_is_the_only_way_a_sale_is_recorded():
    """Sales are declared at the end of the shift, not as they happen. A live
    sell flow left registered would be a second, contradictory path."""
    handlers = build().handlers[0]

    assert not any(
        getattr(h, "callback", None) is not None
        and "sell" in getattr(h.callback, "__module__", "")
        for h in handlers
    ), "a live sell handler is still registered"


def test_pressing_end_shift_starts_the_write_up_rather_than_ending_anything():
    """Nothing is closed until the cashier has listed the day and confirmed it."""
    flow = _flow()

    assert any(
        h.filters.check_update(_message(texts.BTN_END_SHIFT)) for h in flow.entry_points
    )
    assert any(
        h.filters.check_update(_message(texts.BTN_CLOSE_STORE)) for h in flow.entry_points
    )


def test_the_location_label_has_its_own_handler():
    """Telegram Desktop cannot attach a location and sends the label as text."""
    handlers = build().handlers[0]

    assert any(
        getattr(h, "callback", None) is common.location_from_desktop for h in handlers
    ), "tapping the location button on desktop would go unanswered"


def test_the_shift_buttons_are_registered_outside_the_flow_too():
    """They have to work when no write-up is running."""
    handlers = build().handlers[0]
    callbacks = [getattr(h, "callback", None) for h in handlers]

    for expected in (shift.ask_location, shift.handle_location, shift.status, stock.show):
        assert expected in callbacks, f"{expected.__name__} is unreachable outside the flow"


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
    """During a write-up it may still be something that was genuinely sold."""
    markup = keyboards.item_choices(
        [{"id": 1, "name": "Elf Bar", "count": 0, "sell_price": "4000.00"}]
    )

    assert texts.OUT_OF_STOCK_HINT in markup.inline_keyboard[0][0].text


def test_callback_labels_fit_telegrams_limit():
    markup = keyboards.item_choices(
        [{"id": 999999, "name": "Ա" * 200, "count": 3, "sell_price": "3500.00"}]
    )

    button = markup.inline_keyboard[0][0]
    assert len(button.text) <= 64
    assert len(button.callback_data.encode()) <= 64


def test_the_price_keyboard_offers_wholesale_only_when_there_is_one():
    with_wholesale = keyboards.suggested_prices(
        {"sell_price": "4000.00", "wholesale_price": "2500.00"}
    )
    without = keyboards.suggested_prices({"sell_price": "4000.00", "wholesale_price": None})

    assert len(with_wholesale.inline_keyboard) == 3
    assert len(without.inline_keyboard) == 2


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


def test_the_help_text_describes_the_write_up_flow():
    rendered = texts.HELP.format(
        open_button=texts.BTN_OPEN,
        stock_button=texts.BTN_STOCK,
        end_button=texts.BTN_END_SHIFT,
    )

    assert texts.BTN_END_SHIFT in rendered
    assert "{" not in rendered, "a placeholder was left unfilled"
