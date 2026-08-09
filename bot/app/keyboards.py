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
# The − and + against a product on the correction screen, and paging through it.
CB_NUDGE = "n"
CB_PAGE = "pg"
# Its own code rather than reusing CB_SUBMIT: the write-up's confirm is a
# different decision, and two flows sharing a callback prefix is how a tap ends up
# in the wrong conversation.
CB_APPLY = "ap"
# Leaving the correction screen to add a product that never existed here.
CB_NEW_ITEM = "ni"
# Answering one transfer request, asking for a box, and choosing which shop to ask.
# The answer buttons live outside any conversation: they sit on a message that may
# be tapped an hour after it arrived.
# Counting the drawer. Lives on a message rather than the keyboard, and may be
# tapped minutes later, so it is handled outside every flow.
CB_TILL = "till"
CB_TRANSFER = "t"
CB_TRANSFER_NEW = "tn"
CB_TRANSFER_STORE = "ts"
# A button that is really a label. Telegram cannot put text between two buttons on
# one row, so the count in the middle is a button that answers and does nothing.
CB_NOOP = "-"
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


def _labels_of(markup) -> set[str]:
    return {button.text for row in markup.keyboard for button in row}


def main_menu_labels() -> set[str]:
    """The buttons a worker can reach at any moment.

    These are the ones every conversation has to let go of: whatever a cashier is
    half way through, one of these is on their screen and tapping it means "I am
    doing something else now".

    The write-up's own menu is deliberately not here. Those buttons only exist
    while the write-up is on screen, and they belong to it.
    """
    return (
        _labels_of(off_shift())
        | _labels_of(on_shift())
        | _labels_of(request_location())
    )


def reply_keyboard_labels() -> set[str]:
    """Every label that can arrive as a plain text message.

    A reply-keyboard tap is indistinguishable from typing, so each of these has to
    be handled deliberately by whatever flow is open — and an inline button's label
    never arrives this way, so it does not.

    Collected by asking the builders rather than by keeping a second list beside
    them: the failure worth preventing is a new button appearing on the keyboard
    and nothing noticing it needs a way out.
    """
    labels = main_menu_labels() | _labels_of(selling())
    for empty in (True, False):
        labels |= _labels_of(closeout_menu(empty))
    return labels


def on_shift() -> ReplyKeyboardMarkup:
    """Selling comes first because it is what the shift is for.

    It records one line immediately. The write-up under «end my shift» is still
    there for whatever was not entered as it happened.
    """
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton(texts.BTN_SELL)],
            [KeyboardButton(texts.BTN_STOCK), KeyboardButton(texts.BTN_DEFECT)],
            [KeyboardButton(texts.BTN_TAKE_CASH), KeyboardButton(texts.BTN_TILL_COUNT)],
            [KeyboardButton(texts.BTN_ADD_ITEM), KeyboardButton(texts.BTN_TRANSFERS)],
            [KeyboardButton(texts.BTN_STATUS)],
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


def item_choices(
    items: list[dict], allow_empty: bool = False, show_price: bool = True
) -> InlineKeyboardMarkup:
    """One button per match. Out-of-stock items are shown but marked, so a
    cashier does not conclude the search is broken.

    ``allow_empty`` is for the end-of-shift write-up, where an item showing zero
    may still be something that was genuinely sold — the count is only as good
    as the last close-out, and refusing it would make the discrepancy
    unrecordable.

    ``show_price`` is off when picking an item for something that is not a sale.
    A cashier ringing up a customer has to see the price — they are charging it —
    but writing off a broken vape needs only its name and how many, and what the
    shop's prices are is the owner's business, not information to hand out with
    every list.
    """
    rows = []
    for item in items:
        count = item["count"]
        suffix = texts.OUT_OF_STOCK_HINT if count <= 0 else f"· {count} հատ"
        if show_price:
            label = f"{item['name']} — {Decimal(item['sell_price']):,.0f} ֏ {suffix}"
        else:
            label = f"{item['name']} {suffix}"
        rows.append([InlineKeyboardButton(label[:64], callback_data=f"{CB_ITEM}:{item['id']}")])
    rows.append([InlineKeyboardButton(texts.BTN_CANCEL, callback_data=CB_CANCEL)])
    return InlineKeyboardMarkup(rows)


def suggested_prices(item: dict) -> InlineKeyboardMarkup:
    """The two price lists as one tap each, with typing always available.

    Most lines go at one of these, so making the cashier type the usual number
    every time would be the wrong default — but the price is a free field, and
    that is the point of the step.

    «Մեծածախ» is always offered, even for a product with no wholesale price set.
    It used to be hidden in that case, which meant a cashier selling a box at a
    trade price had no way to say so: typing the number filed the line as a
    haggle, and every wholesale figure in the reports read low. Tapping it with no
    price set now asks for the number instead of vanishing.
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
    else:
        rows.append([InlineKeyboardButton(
            texts.BTN_WHOLESALE_NO_PRICE, callback_data=f"{CB_KIND}:wholesale"
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


def restock(
    items: list[dict],
    deltas: dict[str, int],
    *,
    page: int,
    pages: int,
    has_changes: bool,
) -> InlineKeyboardMarkup:
    """One product per row: − , the name with its running count, and +.

    The middle button is the label, not a control. Telegram has no way to draw
    text between two buttons on the same row, so it is a button that does nothing
    — tapping it is a no-op rather than a surprise.

    «Հաստատել» appears only once something has actually changed. An always-present
    confirm on an unchanged list invites the tap that does nothing and teaches the
    cashier the button is unreliable.
    """
    rows = []
    for item in items:
        delta = deltas.get(str(item["id"]), 0)
        after = item["count"] + delta
        label = f"{item['name'][:24]} · {after}"
        if delta:
            label += f" ({'+' if delta > 0 else ''}{delta})"
        rows.append([
            InlineKeyboardButton("➖", callback_data=f"{CB_NUDGE}:{item['id']}:-1"),
            InlineKeyboardButton(label[:64], callback_data=CB_NOOP),
            InlineKeyboardButton("➕", callback_data=f"{CB_NUDGE}:{item['id']}:1"),
        ])

    if pages > 1:
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("◀️", callback_data=f"{CB_PAGE}:-1"))
        nav.append(InlineKeyboardButton(f"{page + 1}/{pages}", callback_data=CB_NOOP))
        if page < pages - 1:
            nav.append(InlineKeyboardButton("▶️", callback_data=f"{CB_PAGE}:1"))
        rows.append(nav)

    if has_changes:
        rows.append([InlineKeyboardButton(
            texts.BTN_RESTOCK_SUBMIT, callback_data=CB_APPLY
        )])
    rows.append([InlineKeyboardButton(texts.BTN_BRAND_NEW_ITEM, callback_data=CB_NEW_ITEM)])
    rows.append([InlineKeyboardButton(texts.BTN_CANCEL, callback_data=CB_CANCEL)])
    return InlineKeyboardMarkup(rows)


def transfer_menu(incoming: list[dict]) -> InlineKeyboardMarkup:
    """Both directions on one screen: answer what is asked, or ask for something.

    Each pending request gets its own ✅ / ❌ pair rather than a single "answer the
    next one" button, so a shop with three requests can approve one box and refuse
    another without the two getting mixed up.
    """
    rows = []
    for row in incoming:
        rows.append([InlineKeyboardButton(
            f"{row['quantity']} × {row['item_name'][:28]} → {row['to_store'][:14]}"[:64],
            callback_data=CB_NOOP,
        )])
        rows.append([
            InlineKeyboardButton(
                texts.BTN_TRANSFER_APPROVE, callback_data=f"{CB_TRANSFER}:{row['id']}:y"
            ),
            InlineKeyboardButton(
                texts.BTN_TRANSFER_REJECT, callback_data=f"{CB_TRANSFER}:{row['id']}:n"
            ),
        ])
    rows.append([InlineKeyboardButton(texts.BTN_TRANSFER_ASK, callback_data=CB_TRANSFER_NEW)])
    rows.append([InlineKeyboardButton(texts.BTN_CANCEL, callback_data=CB_CANCEL)])
    return InlineKeyboardMarkup(rows)


def transfer_stores(stores: list[dict]) -> InlineKeyboardMarkup:
    """Which shop to ask. The worker's own is not in the list — the server leaves
    it out, because you cannot ask yourself for a box."""
    rows = [
        [InlineKeyboardButton(
            store["name"][:64], callback_data=f"{CB_TRANSFER_STORE}:{store['id']}"
        )]
        for store in stores
    ]
    rows.append([InlineKeyboardButton(texts.BTN_CANCEL, callback_data=CB_CANCEL)])
    return InlineKeyboardMarkup(rows)


def restock_empty() -> InlineKeyboardMarkup:
    """Nothing on the shelf yet, so there is nothing to nudge — but the reason for
    pressing the button may well have been a delivery arriving."""
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(texts.BTN_BRAND_NEW_ITEM, callback_data=CB_NEW_ITEM)],
            [InlineKeyboardButton(texts.BTN_CANCEL, callback_data=CB_CANCEL)],
        ]
    )


def count_the_till() -> InlineKeyboardMarkup:
    """Offered on the message that ends a shift.

    A button rather than another question in the flow: the worker is locking up, and
    that is a moment to be handed one thing to do rather than held in a conversation.
    """
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(texts.BTN_TILL_CLOSE, callback_data=CB_TILL)]]
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


