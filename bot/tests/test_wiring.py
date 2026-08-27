"""The application assembles, and no button can trap a cashier."""

from __future__ import annotations

from decimal import Decimal
from unittest import mock

import pytest
from telegram import CallbackQuery, Chat, Location, Message, Update, User
from telegram.ext import (
    ApplicationHandlerStop,
    CommandHandler,
    ConversationHandler,
    MessageHandler,
)

from app import format, keyboards, texts
from app.__main__ import BUTTON_LABELS, build
from app.api import ApiError
from app.handlers import common, sell, shift, stock
from app.keyboards import main_menu_labels, reply_keyboard_labels


class _Ctx:
    def __init__(self, **data) -> None:
        self.user_data = dict(data)


def _tap(data: str) -> Update:
    user = User(id=1, first_name="T", is_bot=False)
    message = Message(
        message_id=1, date=None, chat=Chat(id=1, type="private"), from_user=user
    )
    return Update(
        update_id=1,
        callback_query=CallbackQuery(
            id="1", from_user=user, chat_instance="1", data=data, message=message
        ),
    )


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


def _text_fallbacks(flow):
    """The fallbacks that answer a *reply-keyboard tap*, which arrives as text.

    CommandHandler is excluded, and that exclusion is the whole point of this
    helper. ``CommandHandler.filters`` defaults to ``filters.ALL``, so asking one
    whether it handles a given message answers yes to everything — and a check
    written as "does any fallback match this label" silently passed for every
    label the moment a bare ``CommandHandler`` was in the list. It reported a way
    out of the write-up for buttons that had none.
    """
    return [
        h for h in flow.fallbacks
        if isinstance(h, MessageHandler) and getattr(h, "filters", None) is not None
    ]


def test_every_button_can_escape_the_write_up():
    """Two halves: the states must only consume text the cashier actually typed,
    and every button that is not part of the write-up must have a fallback.

    The trap: the write-up holds a basket. A button that leaves it without ending
    it leaves the conversation alive in a state that takes free text, so the next
    product name the cashier types anywhere is quietly added to a list they think
    they have left.
    """
    flow = _flow()
    own = {
        texts.BTN_CO_ADD, texts.BTN_CO_REMOVE, texts.BTN_CO_DONE,
        texts.BTN_CO_ABANDON, texts.BTN_CO_SUBMIT,
        texts.BTN_CANCEL, texts.BTN_END_SHIFT,
    }

    for state, handlers in flow.states.items():
        for handler in handlers:
            filt = getattr(handler, "filters", None)
            if filt is None:
                continue
            for label in BUTTON_LABELS:
                if filt.check_update(_message(label)) and label not in own:
                    raise AssertionError(f"state {state} would consume {label!r} as free text")

    fallbacks = _text_fallbacks(flow)
    for label in reply_keyboard_labels():
        if label in own:
            continue
        assert any(h.filters.check_update(_message(label)) for h in fallbacks), (
            f"no way out of the write-up when {label!r} is pressed"
        )


def test_every_flow_lets_go_of_every_other_flows_button():
    """Not just the write-up: any conversation holding half-entered state must be
    left cleanly when the cashier taps something else, or that state is still
    there waiting to swallow the next thing they type."""
    flows = [
        h for h in build().handlers[0] if isinstance(h, ConversationHandler)
    ]
    assert len(flows) >= 6, "a flow went missing"

    for flow in flows:
        entries = {
            label for label in main_menu_labels()
            for h in flow.entry_points
            if getattr(h, "filters", None) is not None
            and h.filters.check_update(_message(label))
        }
        fallbacks = _text_fallbacks(flow)
        for label in main_menu_labels():
            if label in entries:
                continue
            handled = any(h.filters.check_update(_message(label)) for h in fallbacks)
            state_takes_it = any(
                getattr(h, "filters", None) is not None
                and h.filters.check_update(_message(label))
                for handlers in flow.states.values()
                for h in handlers
            )
            assert handled or state_takes_it, (
                f"{flow.name or flow}: pressing {label!r} leaves this flow holding its state"
            )


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


def test_one_conversation_owns_the_shelf_and_the_new_product():
    """They were two, and it cost a cashier a product they had cancelled.

    Both flows claimed the inline «Չեղարկել». The correction screen was offered the
    tap first, answered "cancelled", and left the new-product steps running — so the
    next number typed anywhere completed the product. One conversation cannot race
    itself, which is why these states live together.
    """
    from app.handlers import newitem

    flow = _flow(texts.BTN_ADD_ITEM)

    assert newitem.ASK_NAME in flow.states, "the new-product steps live elsewhere again"
    assert newitem.ASK_WHOLESALE in flow.states

    claimants = [
        f for f in build().handlers[0]
        if isinstance(f, ConversationHandler)
        and any(h.check_update(_tap(keyboards.CB_CANCEL)) for h in f.fallbacks)
        and (newitem.ASK_NAME in f.states or restock_state(f))
    ]
    assert len(claimants) == 1, "two flows would fight over the same Cancel again"


def restock_state(flow) -> bool:
    from app.handlers import restock

    return restock.PICK in flow.states


def test_cancelling_the_new_product_clears_the_shelf_draft_too():
    """One cancel, both halves. Clearing only one left the other's state behind for
    the next thing the cashier typed."""
    from app.handlers import restock

    context = _Ctx(
        rs_items=[{"id": 3, "name": "HQD Cuvie", "count": 1}],
        rs_deltas={"3": 4},
        new_name="Waka",
        new_count=2,
        new_cost=Decimal("1000"),
        new_price=Decimal("2000"),
    )

    restock._clear(context)

    assert context.user_data == {}


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


def test_a_worker_whose_conversation_vanished_always_gets_an_answer():
    """From the field, and it looked exactly like the bot being down.

    A deploy left two containers polling one token for a few seconds. Conversation
    state lives in the process, so the one that asked "what is the money for?" and
    the one that received the answer were not the same container. The second had no
    conversation, nothing claimed the text, and the cashier got silence — then
    tapped «Չեղարկել», which only exists inside a flow's fallbacks, and got silence
    again. There was no way back to the menu but /start.

    Anything typed, and that button, must land somewhere.
    """
    plain = [h for h in build().handlers[0] if not isinstance(h, ConversationHandler)]

    for probe, what in (
        (_message("Hh"), "a typed word"),
        (_message("777"), "a typed number"),
        (_message(texts.BTN_CANCEL), "the cancel button"),
        (_message("/nonsense"), "an unknown command"),
    ):
        assert any(
            getattr(h, "filters", None) is not None and h.check_update(probe)
            for h in plain
        ), f"{what} goes unanswered when no conversation is open"


def _claimed_by_something(app, update) -> bool:
    """Would *any* handler take this update, in group order?

    Commands are deliberately not probed with this: ``CommandHandler.check_update``
    reaches for the bot behind the message, which a hand-built Update has not got.
    Reply-keyboard labels arrive as ordinary text and are the case that matters.
    """
    for handlers in app.handlers.values():
        for handler in handlers:
            check = handler.check_update(update)
            if check is not None and check is not False:
                return True
    return False


def test_no_button_is_ever_answered_with_silence_while_a_flow_is_open():
    """The bug from the field, and the worst shape a bug can take here.

    «Ավարտել իմ հերթափոխը» opens the write-up. Pressed *again* it matched nothing:
    not the states, which take only free text and so exclude every button label; not
    the fallbacks, because this flow is where that button leads and `_DOORS` therefore
    leaves it out. The update fell past every handler in the application and the bot
    said nothing whatsoever — indistinguishable, from behind a counter at the end of
    a shift, from the bot being down.

    So this asserts the property the module docstring already claims: with any flow
    open in any of its states, every label on a reply keyboard lands somewhere.
    """
    app = build()
    flows = [h for h in app.handlers[0] if isinstance(h, ConversationHandler)]
    key = (1, 1)

    for flow in flows:
        for state in flow.states:
            for label in reply_keyboard_labels():
                for other in flows:
                    other._conversations.pop(key, None)
                flow._conversations[key] = state
                assert _claimed_by_something(app, _message(label)), (
                    f"{flow.name or flow} in state {state}: pressing {label!r} "
                    f"is answered by nothing at all"
                )
    for flow in flows:
        flow._conversations.pop(key, None)


def test_start_reaches_the_open_flow_so_it_can_let_go():
    """Every conversation declares `/start` in its fallbacks so a cashier who is lost
    can get out. Registered ahead of them, the global handler matched first — printed
    the greeting and left the flow exactly where it was, so the one thing everybody
    tries when the bot seems stuck did nothing, while looking like it had.

    Ordering is the whole mechanism, so ordering is what this holds.
    """
    handlers = build().handlers[0]
    global_start = next(
        i for i, h in enumerate(handlers)
        if isinstance(h, CommandHandler) and "start" in h.commands
    )
    last_flow = max(i for i, h in enumerate(handlers) if isinstance(h, ConversationHandler))

    assert global_start > last_flow, (
        "the global /start shadows every flow's own /start fallback"
    )

    for flow in (h for h in handlers if isinstance(h, ConversationHandler)):
        assert any(
            isinstance(h, CommandHandler) and "start" in h.commands for h in flow.fallbacks
        ), f"{flow.name or flow} cannot be left with /start"


def test_the_recovery_handlers_are_last_so_the_flows_get_first_refusal():
    """They are a safety net, not a competitor. Registered before the flows they
    would answer over the top of them and the write-up could never take a product
    name."""
    handlers = build().handlers[0]
    catch_all = next(
        i for i, h in enumerate(handlers)
        if getattr(h, "callback", None) is common.stray_text
    )
    last_flow = max(
        i for i, h in enumerate(handlers) if isinstance(h, ConversationHandler)
    )

    assert catch_all > last_flow


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


def _takes(data: str):
    """The handler that would answer this tap, outside every conversation."""
    from app.handlers import records  # noqa: F401  (imported for the caller's asserts)

    for handler in build().handlers[0]:
        if isinstance(handler, ConversationHandler):
            continue
        check = handler.check_update(_tap(data))
        if check is not None and check is not False:
            return handler
    return None


@pytest.mark.parametrize(
    "data",
    [
        keyboards.CB_FIX,
        f"{keyboards.CB_FIX_PICK}:41",
        f"{keyboards.CB_FIX_VOID}:41",
        keyboards.CB_FIX_CLOSE,
        keyboards.CB_FIX_NOOP,
    ],
)
def test_every_correction_button_has_a_handler_outside_the_flows(data):
    """These sit on a message a cashier comes back to ten minutes later, so none of
    them may depend on a conversation still being open. An unhandled callback spins
    on the phone until Telegram gives up, which reads as the bot being broken."""
    assert _takes(data) is not None, f"{data} goes unanswered"


def test_the_correction_prefixes_do_not_swallow_each_other():
    """«fx», «fxp», «fxv», «fxc» and «fxn» all start with the same two letters, and
    the list handler matching a tap meant for the cancel would redraw the list
    instead of cancelling — or worse, the other way round."""
    from app.handlers import records

    assert _takes(keyboards.CB_FIX).callback is records.show_list
    assert _takes(f"{keyboards.CB_FIX_PICK}:41").callback is records.pick
    assert _takes(f"{keyboards.CB_FIX_VOID}:41").callback is records.void
    assert _takes(keyboards.CB_FIX_CLOSE).callback is records.close
    assert _takes(keyboards.CB_FIX_NOOP).callback is records.already_voided


def test_the_undo_button_still_belongs_to_selling():
    """«u:41» and «fxv:41» both cancel a receipt and must not compete: the first is
    the button under a fresh sale, the second the one on the correction screen."""
    from app.handlers import sell as sell_handlers

    assert _takes(f"{keyboards.CB_UNDO}:41").callback is sell_handlers.undo


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
