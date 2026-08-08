"""The application assembles, and no button can trap a cashier."""

from __future__ import annotations

from decimal import Decimal
from unittest import mock

import pytest
from telegram import Chat, Location, Message, Update, User
from telegram.ext import ApplicationHandlerStop, ConversationHandler, MessageHandler

from app import format, keyboards, texts
from app.__main__ import BUTTON_LABELS, build
from app.api import ApiError
from app.handlers import common, sell, shift, stock


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


def _flow(entry: str = texts.BTN_END_SHIFT) -> ConversationHandler:
    """The conversation a given button starts. There are two now — selling as it
    happens, and writing the day up — so they have to be told apart by name."""
    return next(
        h for h in build().handlers[0]
        if isinstance(h, ConversationHandler)
        and any(
            getattr(e, "filters", None) is not None and e.filters.check_update(_message(entry))
            for e in h.entry_points
        )
    )


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
        texts.BTN_CO_ABANDON, texts.BTN_CO_SUBMIT,
        texts.BTN_CANCEL, texts.BTN_CASH, texts.BTN_CARD,
        texts.BTN_RETAIL, texts.BTN_WHOLESALE, texts.BTN_OTHER_PRICE,
        texts.BTN_DELIVERY_OFF, texts.BTN_DELIVERY_ON, texts.BTN_SKIP,
        texts.BTN_END_SHIFT,
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


def test_there_are_two_ways_to_record_a_sale_and_both_are_reachable():
    """Selling as it happens, and writing the day up at the end.

    They do not overlap: the first commits one line immediately, the second
    commits whatever was not entered that way. A shop that wants its stock count
    to be right *now* uses the button; one that would rather not touch the bot
    while serving uses the write-up.
    """
    handlers = build().handlers[0]

    assert any(
        getattr(h, "callback", None) is sell.begin for h in handlers
    ), "selling is unreachable outside the flow"
    assert _flow(texts.BTN_SELL) is not _flow(texts.BTN_END_SHIFT)


def test_starting_a_sale_and_then_ending_the_shift_does_not_strand_the_cashier():
    """The trap this closes: the sell conversation keeps its state when another
    flow takes over, and the next number typed would be read as a quantity for a
    sale that no longer exists."""
    flow = _flow(texts.BTN_SELL)

    for label in (texts.BTN_END_SHIFT,):
        assert any(
            getattr(h, "filters", None) is not None and h.filters.check_update(_message(label))
            for h in flow.fallbacks
        ), f"{label!r} would leave a sale half-entered"


def test_neither_flow_eats_the_others_buttons_as_free_text():
    own = {
        texts.BTN_CO_ADD, texts.BTN_CO_REMOVE, texts.BTN_CO_DONE, texts.BTN_CO_ABANDON,
        texts.BTN_CO_SUBMIT, texts.BTN_CANCEL,
        texts.BTN_CASH, texts.BTN_CARD, texts.BTN_RETAIL, texts.BTN_WHOLESALE,
        texts.BTN_OTHER_PRICE, texts.BTN_DELIVERY_OFF, texts.BTN_DELIVERY_ON,
        texts.BTN_SKIP, texts.BTN_END_SHIFT, texts.BTN_SELL,
    }

    for entry in (texts.BTN_SELL, texts.BTN_END_SHIFT):
        for state, handlers in _flow(entry).states.items():
            for handler in handlers:
                filt = getattr(handler, "filters", None)
                if filt is None:
                    continue
                for label in BUTTON_LABELS:
                    if filt.check_update(_message(label)) and label not in own:
                        raise AssertionError(f"{entry}: state {state} would consume {label!r}")


def test_pressing_end_shift_starts_the_write_up_rather_than_ending_anything():
    """Nothing is closed until the cashier has listed the day and confirmed it."""
    flow = _flow()

    assert any(
        h.filters.check_update(_message(texts.BTN_END_SHIFT)) for h in flow.entry_points
    )


def test_there_is_no_close_the_store_button():
    """Closing the shop is not a thing a cashier decides.

    The last one to end their shift closes it, which is the same outcome without
    a button that can end a colleague's day. The write-up is the only way out.
    """
    labels = set(BUTTON_LABELS)

    assert not any("Փակել" in label for label in labels), (
        "a close-the-store button is back on the keyboard"
    )
    assert texts.BTN_END_SHIFT in labels


def test_writing_off_asks_only_which_item_and_how_many():
    """A reason used to be asked for and was dropped. At the counter the answer
    to "what happened" is always some form of "it broke", so the step cost a tap
    and bought nothing the report could act on."""
    from app.handlers import defect

    flow = _flow(texts.BTN_DEFECT)

    assert set(flow.states) == {defect.PICK_ITEM, defect.ASK_QUANTITY}
    assert not hasattr(defect, "ASK_REASON")
    assert not hasattr(keyboards, "defect_reasons")


def test_taking_cash_and_adding_a_product_are_both_on_the_keyboard():
    """Both are things that happen mid-shift with a customer waiting, so both
    have to be one tap from the main keyboard rather than buried."""
    labels = {b.text for row in keyboards.on_shift().keyboard for b in row}

    assert texts.BTN_TAKE_CASH in labels
    assert texts.BTN_ADD_ITEM in labels


def test_the_location_label_has_its_own_handler():
    """Telegram Desktop cannot attach a location and sends the label as text."""
    handlers = build().handlers[0]

    assert any(
        getattr(h, "callback", None) is common.location_from_desktop for h in handlers
    ), "tapping the location button on desktop would go unanswered"


def _location(*, edited: bool, live_period: int | None = 900) -> Update:
    """A shared live location, or one of the edits Telegram sends as it moves."""
    user = User(id=1, first_name="T", is_bot=False)
    message = Message(
        message_id=1, date=None, chat=Chat(id=1, type="private"),
        from_user=user,
        location=Location(latitude=40.1772, longitude=44.5032, live_period=live_period),
    )
    if edited:
        return Update(update_id=1, edited_message=message)
    return Update(update_id=1, message=message)


def test_a_moving_live_location_never_reaches_the_open_a_shift_handler():
    """The subtle one. Telegram sends a live location once as a message and then
    as a stream of *edits* to it, and python-telegram-bot applies no update-type
    restriction of its own — so a bare ``filters.LOCATION`` hands every edit to
    the handler that opens a shift, which answers "you are already on shift"
    every few seconds for as long as the worker keeps walking."""
    handlers = build().handlers[0]
    opens = [h for h in handlers if getattr(h, "callback", None) is shift.handle_location]

    assert opens, "nothing opens a shift from a location"
    for handler in opens:
        assert handler.filters.check_update(_location(edited=False)), "a share must open"
        assert not handler.filters.check_update(_location(edited=True)), (
            "an edit is movement, not a request to open a shift"
        )


def test_movement_is_handled_before_anything_else_and_stops_there():
    """It runs in an earlier group than the write-up conversation, so a location
    updating mid-write-up cannot be read as the cashier leaving the flow."""
    app = build()
    groups = {
        group: [getattr(h, "callback", None) for h in handlers]
        for group, handlers in app.handlers.items()
    }
    live_groups = [g for g, callbacks in groups.items() if shift.handle_live_update in callbacks]

    assert live_groups, "nothing records a moving live location"
    assert min(live_groups) < 0, "movement must be handled ahead of the conversation"


def test_the_write_ups_location_escape_only_fires_on_a_fresh_share():
    """Its fallback exists so a worker can re-open a store mid-write-up. A
    location that merely moved is not that, and must not end the flow."""
    flow = _flow()
    location_fallbacks = [
        h for h in flow.fallbacks
        if isinstance(h, MessageHandler) and h.filters.check_update(_location(edited=False))
    ]

    assert location_fallbacks, "a fresh location must still be a way out of the write-up"
    for handler in location_fallbacks:
        assert not handler.filters.check_update(_location(edited=True)), (
            "a live location moving would abandon the cashier's write-up"
        )


async def test_recording_movement_stops_the_update_going_any_further():
    """The guarantee the group ordering alone cannot give. Without this the
    update falls through to the write-up conversation, whose fallbacks would
    read it as the cashier leaving the flow — every few seconds, all shift."""
    calls = []

    async def fake_ping(**kwargs):
        calls.append(kwargs)
        return {"ok": True, "distance_m": 8, "in_range": True}

    with (
        mock.patch.object(shift.api, "ping", fake_ping),
        pytest.raises(ApplicationHandlerStop),
    ):
        await shift.handle_live_update(_location(edited=True), None)

    assert calls and calls[0]["lat"] == 40.1772


async def test_a_failed_ping_still_stops_and_still_says_nothing():
    """A missed reading is a gap in a trail, not something the worker did or can
    fix. It must not produce a message, and must not let the update through."""
    replies = []

    async def fake_reply(self, *args, **kwargs):
        replies.append(args)

    async def boom(**kwargs):
        raise ApiError("internal", "…")

    with (
        mock.patch.object(shift.api, "ping", boom),
        mock.patch.object(Message, "reply_text", fake_reply),
        pytest.raises(ApplicationHandlerStop),
    ):
        await shift.handle_live_update(_location(edited=True), None)

    assert replies == [], "a worker who gets told off for walking will stop sharing"


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


def test_wholesale_is_offered_even_when_the_product_has_no_wholesale_price():
    """It used to be hidden in that case, which left a cashier selling a box at a
    trade price with no way to say so — typing the number filed the line as a
    haggle and every wholesale figure read low. Now the button is always there and
    asks for the price when there is none on the product."""
    with_wholesale = keyboards.suggested_prices(
        {"sell_price": "4000.00", "wholesale_price": "2500.00"}
    )
    without = keyboards.suggested_prices({"sell_price": "4000.00", "wholesale_price": None})

    def data(markup):
        return [b.callback_data for row in markup.inline_keyboard for b in row]

    assert f"{keyboards.CB_KIND}:wholesale" in data(with_wholesale)
    assert f"{keyboards.CB_KIND}:wholesale" in data(without)
    # Same shape either way now; only the label on that one row differs.
    assert len(with_wholesale.inline_keyboard) == len(without.inline_keyboard)


def test_the_wholesale_button_says_which_kind_it_is():
    """With a price it shows the price; without one it says it will ask."""
    def label(wholesale):
        markup = keyboards.suggested_prices(
            {"sell_price": "4000.00", "wholesale_price": wholesale}
        )
        return next(
            b.text for row in markup.inline_keyboard for b in row
            if b.callback_data == f"{keyboards.CB_KIND}:wholesale"
        )

    assert "2,500" in label("2500.00")
    assert label(None) == texts.BTN_WHOLESALE_NO_PRICE


def test_a_price_can_always_be_typed_instead():
    """It was always possible and only the prose said so, which is not the same
    as being offered."""
    markup = keyboards.suggested_prices({"sell_price": "4000.00", "wholesale_price": None})

    labels = [b.text for row in markup.inline_keyboard for b in row]
    assert texts.BTN_OTHER_PRICE in labels


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
