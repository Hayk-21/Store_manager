"""Keyboards.

Two kinds: the persistent reply keyboard at the bottom of the chat, which
changes depending on whether a shift is open, and inline keyboards for picking
an item or a payment method.
"""

from __future__ import annotations

from decimal import Decimal

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

from app import texts

# Callback data is capped at 64 bytes by Telegram, so these stay terse.
CB_ITEM = "i"
CB_PAY = "p"
CB_KIND = "k"
CB_SUBMIT = "s"
# Ticking "delivery". Sends nothing — it flips a flag and redraws the keyboard,
# and the sale still goes when cash or card is tapped.
CB_DELIVERY = "dl"
# Leaving the wholesale price blank, which means "not sold wholesale".
CB_SKIP = "sk"
# Belongs to the sell conversation alone, so its fallback is the only thing that
# handles it and the conversation actually ends when it fires.
CB_CANCEL = "x"
# Undoing one specific sale, so a button tapped three sales later still reverses
# the receipt it was attached to rather than whatever happens to be last.
CB_UNDO = "u"
# Dismissing a confirmation, which happens outside any conversation. Nothing
# builds one any more, but a keyboard already sitting in somebody's chat still
# can, and a tap that spins forever is worse than one that says "cancelled".
CB_DISMISS = "d"


def off_shift() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [[KeyboardButton(texts.BTN_OPEN)]], resize_keyboard=True
    )


def on_shift() -> ReplyKeyboardMarkup:
    """Selling comes first because it is what the shift is for.

    It records one line immediately. The write-up under «end my shift» is still
    there for whatever was not entered as it happened.
    """
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton(texts.BTN_SELL)],
            [KeyboardButton(texts.BTN_STOCK), KeyboardButton(texts.BTN_DEFECT)],
            [KeyboardButton(texts.BTN_TAKE_CASH), KeyboardButton(texts.BTN_STATUS)],
            [KeyboardButton(texts.BTN_ADD_ITEM)],
            [KeyboardButton(texts.BTN_END_SHIFT)],
        ],
        resize_keyboard=True,
    )


def selling() -> ReplyKeyboardMarkup:
    """While a sale is being entered: one way out, never more than one tap."""
    return ReplyKeyboardMarkup(
        [[KeyboardButton(texts.BTN_CANCEL)]], resize_keyboard=True
    )


def request_location() -> ReplyKeyboardMarkup:
    """``request_location`` only works on phones — which is the point: the
    coordinate has to come from where the worker actually is."""
    return ReplyKeyboardMarkup(
        [[KeyboardButton(texts.BTN_SEND_LOCATION, request_location=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def item_choices(items: list[dict], allow_empty: bool = False) -> InlineKeyboardMarkup:
    """One button per match. Out-of-stock items are shown but marked, so a
    cashier does not conclude the search is broken.

    ``allow_empty`` is for the end-of-shift write-up, where an item showing zero
    may still be something that was genuinely sold — the count is only as good
    as the last close-out, and refusing it would make the discrepancy
    unrecordable.
    """
    rows = []
    for item in items:
        count = item["count"]
        price = Decimal(item["sell_price"])
        suffix = texts.OUT_OF_STOCK_HINT if count <= 0 else f"· {count} հատ"
        label = f"{item['name']} — {price:,.0f} ֏ {suffix}"
        rows.append([InlineKeyboardButton(label[:64], callback_data=f"{CB_ITEM}:{item['id']}")])
    rows.append([InlineKeyboardButton(texts.BTN_CANCEL, callback_data=CB_CANCEL)])
    return InlineKeyboardMarkup(rows)


def suggested_prices(item: dict) -> InlineKeyboardMarkup:
    """The shelf prices as one tap each, with typing always available.

    Most lines go at one of these, so making the cashier type the usual number
    every time would be the wrong default — but the price is a free field, and
    that is the point of the step.

    «Մեծածախ» appears only for products that have a wholesale price. That is
    also how it went missing once: the item search did not send the field, so
    every item looked like one nobody sells wholesale and the button was
    unreachable for everybody.
    """
    rows = [[InlineKeyboardButton(
        f"{texts.BTN_RETAIL} — {Decimal(item['sell_price']):,.0f} ֏",
        callback_data=f"{CB_KIND}:retail",
    )]]
    if item.get("wholesale_price") is not None:
        rows.append([InlineKeyboardButton(
            f"{texts.BTN_WHOLESALE} — {Decimal(item['wholesale_price']):,.0f} ֏",
            callback_data=f"{CB_KIND}:wholesale",
        )])
    # Typing a price always worked, but only the prose said so, and a cashier
    # looking at two buttons does not read the prose. Now it is a button that
    # asks for the number.
    rows.append([InlineKeyboardButton(texts.BTN_OTHER_PRICE, callback_data=f"{CB_KIND}:other")])
    rows.append([InlineKeyboardButton(texts.BTN_CANCEL, callback_data=CB_CANCEL)])
    return InlineKeyboardMarkup(rows)


def closeout_menu(empty: bool) -> ReplyKeyboardMarkup:
    """The keyboard while writing the day up."""
    rows = [[KeyboardButton(texts.BTN_CO_ADD)]]
    if not empty:
        rows.append([KeyboardButton(texts.BTN_CO_REMOVE), KeyboardButton(texts.BTN_CO_DONE)])
    else:
        rows.append([KeyboardButton(texts.BTN_CO_DONE)])
    rows.append([KeyboardButton(texts.BTN_CO_ABANDON)])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def closeout_confirm() -> InlineKeyboardMarkup:
    """Ending your own shift and shutting the shop are different decisions.

    Both are offered because the last person out wants one tap, not two. The
    server refuses the second when colleagues are still on shift, so the button
    cannot end somebody else's day before they have written up their sales.
    """
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(texts.BTN_CO_SUBMIT, callback_data=f"{CB_SUBMIT}:shift")],
            [InlineKeyboardButton(texts.BTN_CANCEL, callback_data=CB_CANCEL)],
        ]
    )


def price_kinds(retail: Decimal, wholesale: Decimal) -> InlineKeyboardMarkup:
    """Only shown when the item actually has a wholesale price.

    Most sales are over the counter, so making every sale answer this would be a
    tax on the common case. Items with no wholesale price skip it entirely.
    """
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(
                f"{texts.BTN_RETAIL} — {retail:,.0f} ֏", callback_data=f"{CB_KIND}:retail"
            )],
            [InlineKeyboardButton(
                f"{texts.BTN_WHOLESALE} — {wholesale:,.0f} ֏",
                callback_data=f"{CB_KIND}:wholesale",
            )],
            [InlineKeyboardButton(texts.BTN_CANCEL, callback_data=CB_CANCEL)],
        ]
    )


def undo_sale(sale_id: int) -> InlineKeyboardMarkup:
    """One tap to reverse the sale just recorded."""
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(texts.BTN_UNDO_SALE, callback_data=f"{CB_UNDO}:{sale_id}")]]
    )


def skip() -> InlineKeyboardMarkup:
    """For a question whose blank answer means something.

    «Բաց թողնել» rather than expecting a 0 or a dash: "we do not sell this one
    wholesale" is an answer, and a typed 0 would read as "free".
    """
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(texts.BTN_SKIP, callback_data=CB_SKIP)],
            [InlineKeyboardButton(texts.BTN_CANCEL, callback_data=CB_CANCEL)],
        ]
    )


def payment_methods(is_delivery: bool = False) -> InlineKeyboardMarkup:
    """How it was paid, with delivery as a tickbox above it.

    The tickbox commits nothing. Tapping it only redraws this keyboard with the
    box filled in; the sale is sent when cash or card is tapped, whichever way
    the box is set. That is the point of separating them: delivery is not a way
    of paying, it is a fact about the same sale — paid in cash at the door or by
    card in advance — so pairing it with each method gave four buttons that said
    two things.

    It changes no money either way. It records that the goods left the shop
    rather than the counter, which nothing else in the row would tell the owner
    afterwards.
    """
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(
                texts.BTN_DELIVERY_ON if is_delivery else texts.BTN_DELIVERY_OFF,
                callback_data=CB_DELIVERY,
            )],
            [
                InlineKeyboardButton(texts.BTN_CASH, callback_data=f"{CB_PAY}:cash"),
                InlineKeyboardButton(texts.BTN_CARD, callback_data=f"{CB_PAY}:card"),
            ],
            [InlineKeyboardButton(texts.BTN_CANCEL, callback_data=CB_CANCEL)],
        ]
    )


