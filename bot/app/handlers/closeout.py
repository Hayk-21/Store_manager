"""Writing the day up at the end of a shift.

The cashier serves customers without touching the bot. When the shift ends they
list what went out: item, quantity, price, cash or card. The price is prefilled
from the shelf but can be changed — a discount, a haggle, a wholesale run — which
is the whole reason this is typed rather than assumed.

Nothing is committed until the summary is confirmed. The basket lives in
``user_data`` up to that point, so backing out costs nothing.
"""

from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes, ConversationHandler

from app import format, keyboards, texts
from app.api import ApiError, ApiUnavailable, api, new_idempotency_key

log = logging.getLogger("storemanager.bot.closeout")

PICK_ITEM, ASK_QUANTITY, ASK_PRICE, ASK_METHOD, CONFIRM = range(10, 15)

MAX_QUANTITY = 10_000
MAX_LINES = 50
BASKET = "closeout_basket"


def _basket(context) -> list[dict]:
    return context.user_data.setdefault(BASKET, [])


def _clear(context) -> None:
    for key in (BASKET, "co_item", "co_qty", "co_price", "co_key", "co_candidates"):
        context.user_data.pop(key, None)


def _kind_suffix(kind: str | None) -> str:
    """Say why a price is not the shelf price, when it is not.

    Retail lines get nothing: the common case should not be labelled, or the
    label stops carrying information.
    """
    if kind == "wholesale":
        return texts.KIND_WHOLESALE
    if kind == "custom":
        return texts.KIND_CUSTOM
    return ""


def _summary(basket: list[dict]) -> str:
    if not basket:
        return texts.CLOSEOUT_EMPTY_BASKET

    cash = sum(Decimal(line["unit_price"]) * line["quantity"]
               for line in basket if line["payment_method"] == "cash")
    card = sum(Decimal(line["unit_price"]) * line["quantity"]
               for line in basket if line["payment_method"] == "card")

    rows = [
        texts.CLOSEOUT_ROW.format(
            index=index + 1,
            name=format.esc(line["name"]),
            quantity=line["quantity"],
            price=format.money(line["unit_price"]),
            total=format.money(Decimal(line["unit_price"]) * line["quantity"]),
            method=texts.BTN_CASH if line["payment_method"] == "cash" else texts.BTN_CARD,
            kind=_kind_suffix(line.get("price_kind"))
                 + (texts.KIND_DELIVERY if line.get("is_delivery") else ""),
        )
        for index, line in enumerate(basket)
    ]
    return texts.CLOSEOUT_SUMMARY.format(
        rows="\n".join(rows),
        cash=format.money(cash),
        card=format.money(card),
        total=format.money(cash + card),
    )


async def _fail(update: Update, exc: Exception) -> int:
    message = update.effective_message
    if isinstance(exc, (ApiError, ApiUnavailable)):
        await message.reply_text(exc.human(), reply_markup=keyboards.on_shift())
    else:
        log.exception("unhandled error while closing out")
        await message.reply_text(texts.UNEXPECTED, reply_markup=keyboards.on_shift())
    return ConversationHandler.END


# -- entry -------------------------------------------------------------------

async def begin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """"End my shift" pressed. Ask what was sold before ending anything."""
    _clear(context)
    # One key for the whole close-out, minted now and reused for every retry, so
    # a flaky connection at the very end cannot double the day's takings.
    context.user_data["co_key"] = new_idempotency_key()
    await update.effective_message.reply_text(
        texts.CLOSEOUT_START, parse_mode=ParseMode.HTML,
        reply_markup=keyboards.closeout_menu(empty=True),
    )
    return PICK_ITEM


async def prompt_item(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """"Add an item" pressed — but typing the name straight away works too."""
    await update.effective_message.reply_text(texts.CLOSEOUT_ASK_ITEM)
    return PICK_ITEM


async def search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = (update.effective_message.text or "").strip()
    if not query:
        await update.effective_message.reply_text(texts.CLOSEOUT_ASK_ITEM)
        return PICK_ITEM

    try:
        result = await api.search_items(update.effective_user.id, query)
    except Exception as exc:  # noqa: BLE001
        return await _fail(update, exc)

    items = result["items"]
    if not items:
        await update.effective_message.reply_text(texts.NOTHING_FOUND.format(query=query))
        return PICK_ITEM

    context.user_data["co_candidates"] = {str(i["id"]): i for i in items}
    await update.effective_message.reply_text(
        texts.CHOOSE_ITEM, reply_markup=keyboards.item_choices(items, allow_empty=True)
    )
    return PICK_ITEM


async def choose_item(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    item = (context.user_data.get("co_candidates") or {}).get(query.data.split(":", 1)[1])
    if item is None:
        await query.edit_message_text(texts.CANCELLED)
        return PICK_ITEM

    context.user_data["co_item"] = item
    await query.edit_message_reply_markup(reply_markup=None)
    await query.message.reply_text(
        texts.CLOSEOUT_ASK_QUANTITY.format(item=item["name"], available=item["count"])
    )
    return ASK_QUANTITY


async def choose_quantity(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    raw = (update.effective_message.text or "").strip()
    try:
        quantity = int(raw)
    except ValueError:
        await update.effective_message.reply_text(texts.BAD_QUANTITY)
        return ASK_QUANTITY
    if not (0 < quantity <= MAX_QUANTITY):
        await update.effective_message.reply_text(texts.BAD_QUANTITY)
        return ASK_QUANTITY

    item = context.user_data.get("co_item")
    if item is None:  # pragma: no cover
        return ConversationHandler.END

    # Checked against what the shelf says, counting what is already in this
    # basket -- three of something with two left is wrong even across two lines.
    already = sum(
        line["quantity"] for line in _basket(context) if line["item_id"] == item["id"]
    )
    if already + quantity > item["count"]:
        await update.effective_message.reply_text(
            texts.NOT_ENOUGH_STOCK.format(
                item=item["name"],
                available=max(0, item["count"] - already),
                requested=quantity,
            )
        )
        return ASK_QUANTITY

    context.user_data["co_qty"] = quantity
    await update.effective_message.reply_text(
        texts.CLOSEOUT_ASK_PRICE.format(
            item=format.esc(item["name"]),
            quantity=quantity,
            suggested=format.money(item["sell_price"]),
        ),
        parse_mode=ParseMode.HTML,
        reply_markup=keyboards.suggested_prices(item),
    )
    return ASK_PRICE


async def choose_suggested_price(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    kind = query.data.split(":", 1)[1]
    item = context.user_data.get("co_item")
    if item is None:  # pragma: no cover
        return ConversationHandler.END

    if kind == "other":
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text(texts.ASK_OTHER_PRICE)
        return ASK_PRICE

    price = item.get("wholesale_price") if kind == "wholesale" else item.get("sell_price")
    if price is None:  # pragma: no cover - the button is only offered when set
        price = item["sell_price"]
    context.user_data["co_price"] = Decimal(price)
    # Which list it came from, kept beside the amount. The server records it, so
    # "how much do we sell wholesale" stops being a guess about low prices.
    context.user_data["co_price_kind"] = kind
    await query.edit_message_reply_markup(reply_markup=None)
    return await _ask_method(query.message, context)


async def type_price(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """The point of the whole step: whatever the customer actually paid."""
    raw = (update.effective_message.text or "").strip().replace(",", ".").replace(" ", "")
    try:
        price = Decimal(raw).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        await update.effective_message.reply_text(texts.CLOSEOUT_BAD_PRICE)
        return ASK_PRICE
    if price < 0:
        await update.effective_message.reply_text(texts.CLOSEOUT_BAD_PRICE)
        return ASK_PRICE

    context.user_data["co_price"] = price
    # Typed by hand. The server downgrades this to the list price's own kind if
    # the number turns out to match it exactly, so leaving a prefilled wholesale
    # price alone is not filed as a haggle.
    context.user_data["co_price_kind"] = "custom"
    return await _ask_method(update.effective_message, context)


async def _ask_method(message, context) -> int:
    item = context.user_data["co_item"]
    total = context.user_data["co_price"] * context.user_data["co_qty"]
    await message.reply_text(
        texts.ASK_PAYMENT.format(
            item=format.esc(item["name"]),
            quantity=context.user_data["co_qty"],
            total=format.money(total),
        ),
        parse_mode=ParseMode.HTML,
        reply_markup=keyboards.payment_methods(),
    )
    return ASK_METHOD


async def choose_method(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    # "p:cash" or "p:cash:d" -- the marker rides along with the method rather
    # than needing a toggle with a state of its own.
    parts = query.data.split(":")
    method, is_delivery = parts[1], len(parts) > 2 and parts[2] == "d"
    item = context.user_data.get("co_item")
    if item is None:  # pragma: no cover
        return ConversationHandler.END

    basket = _basket(context)
    if len(basket) >= MAX_LINES:
        await query.message.reply_text(texts.CLOSEOUT_TOO_MANY.format(limit=MAX_LINES))
        return PICK_ITEM

    basket.append(
        {
            "item_id": item["id"],
            "name": item["name"],
            "quantity": context.user_data["co_qty"],
"unit_price": str(context.user_data["co_price"]),
            "price_kind": context.user_data.get("co_price_kind", "retail"),
            "payment_method": method,
            "is_delivery": is_delivery,
        }
    )
    for key in ("co_item", "co_qty", "co_price", "co_price_kind", "co_candidates"):
        context.user_data.pop(key, None)

    await query.edit_message_reply_markup(reply_markup=None)
    await query.message.reply_text(
        _summary(basket), parse_mode=ParseMode.HTML,
        reply_markup=keyboards.closeout_menu(empty=False),
    )
    return PICK_ITEM


# -- finishing ---------------------------------------------------------------

async def review(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Show the whole day back before anything is written."""
    basket = _basket(context)
    await update.effective_message.reply_text(
        _summary(basket) + "\n\n" + texts.CLOSEOUT_CONFIRM_PROMPT,
        parse_mode=ParseMode.HTML,
        reply_markup=keyboards.closeout_confirm(),
    )
    return CONFIRM


async def drop_last(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Nothing is committed yet, so undo is just popping the list."""
    basket = _basket(context)
    removed = basket.pop() if basket else None
    message = (
        texts.CLOSEOUT_REMOVED.format(name=format.esc(removed["name"]))
        if removed else texts.CLOSEOUT_NOTHING_TO_REMOVE
    )
    await update.effective_message.reply_text(
        message + "\n\n" + _summary(basket), parse_mode=ParseMode.HTML,
        reply_markup=keyboards.closeout_menu(empty=not basket),
    )
    return PICK_ITEM


async def submit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    basket = _basket(context)
    key = context.user_data.get("co_key") or new_idempotency_key()
    context.user_data["co_key"] = key

    await query.edit_message_reply_markup(reply_markup=None)

    try:
        result = await api.close_out(
            update.effective_user.id,
            [
                {
                    "item_id": line["item_id"],
                    "quantity": line["quantity"],
                    "unit_price": line["unit_price"],
                    "price_kind": line.get("price_kind", "retail"),
                    "payment_method": line["payment_method"],
                    "is_delivery": line.get("is_delivery", False),
                }
                for line in basket
            ],
            key,
        )
    except Exception as exc:  # noqa: BLE001
        return await _fail(update, exc)

    _clear(context)
    from app.handlers.shift import report_end

    await report_end(update, result["summary"])
    return ConversationHandler.END


def escape(handler):
    """Wrap a normal handler so it also ends the write-up.

    Every reply-keyboard button that is not part of the write-up goes through
    this. Without it the label would be consumed as a product name or a price
    and the cashier would have no way out — which is exactly how the previous
    flow trapped somebody.
    """

    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        _clear(context)
        await handler(update, context)
        return ConversationHandler.END

    wrapped.__name__ = f"escape_{getattr(handler, '__name__', 'handler')}"
    wrapped.__wrapped__ = handler
    return wrapped


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Back out. The shift stays open and nothing was recorded."""
    query = update.callback_query
    if query is not None:
        await query.answer()
        await query.edit_message_reply_markup(reply_markup=None)
    _clear(context)
    await update.effective_message.reply_text(
        texts.CLOSEOUT_ABANDONED, reply_markup=keyboards.on_shift()
    )
    return ConversationHandler.END
