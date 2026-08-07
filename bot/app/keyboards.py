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
# Belongs to the sell conversation alone, so its fallback is the only thing that
# handles it and the conversation actually ends when it fires.
CB_CANCEL = "x"
CB_CLOSE_STORE = "c"
# Dismissing the close-store confirmation, which happens outside any conversation.
CB_DISMISS = "d"


def off_shift() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [[KeyboardButton(texts.BTN_OPEN)]], resize_keyboard=True
    )


def on_shift() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton(texts.BTN_SELL), KeyboardButton(texts.BTN_UNDO)],
            [KeyboardButton(texts.BTN_STOCK), KeyboardButton(texts.BTN_STATUS)],
            [KeyboardButton(texts.BTN_END_SHIFT), KeyboardButton(texts.BTN_CLOSE_STORE)],
        ],
        resize_keyboard=True,
    )


def request_location() -> ReplyKeyboardMarkup:
    """``request_location`` only works on phones — which is the point: the
    coordinate has to come from where the worker actually is."""
    return ReplyKeyboardMarkup(
        [[KeyboardButton(texts.BTN_SEND_LOCATION, request_location=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def item_choices(items: list[dict]) -> InlineKeyboardMarkup:
    """One button per match. Out-of-stock items are shown but marked, so a
    cashier does not conclude the search is broken."""
    rows = []
    for item in items:
        count = item["count"]
        price = Decimal(item["sell_price"])
        suffix = texts.OUT_OF_STOCK_HINT if count <= 0 else f"· {count} հատ"
        label = f"{item['name']} — {price:,.0f} ֏ {suffix}"
        rows.append([InlineKeyboardButton(label[:64], callback_data=f"{CB_ITEM}:{item['id']}")])
    rows.append([InlineKeyboardButton(texts.BTN_CANCEL, callback_data=CB_CANCEL)])
    return InlineKeyboardMarkup(rows)


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


def payment_methods() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(texts.BTN_CASH, callback_data=f"{CB_PAY}:cash"),
                InlineKeyboardButton(texts.BTN_CARD, callback_data=f"{CB_PAY}:card"),
            ],
            [InlineKeyboardButton(texts.BTN_CANCEL, callback_data=CB_CANCEL)],
        ]
    )


def confirm_close_store() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(texts.BTN_CONFIRM_CLOSE, callback_data=CB_CLOSE_STORE)],
            [InlineKeyboardButton(texts.BTN_CANCEL, callback_data=CB_DISMISS)],
        ]
    )
