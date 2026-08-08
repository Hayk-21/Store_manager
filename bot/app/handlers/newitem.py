"""Putting a new product on the shelf, from the counter.

A delivery arrives mid-shift and the cashier is the one holding it. Making them
wait for the owner to add it on the website means either the box sits unsellable
or it gets sold as something else, and the second is worse.

Five questions, all short: name, how many, what it cost us, the retail price, and
the wholesale one. The cost price is asked for rather than defaulted to zero — an
item with no cost reports its entire selling price as profit, which is a worse
number to have than no number at all.

The wholesale price can be skipped, because "we do not sell this one wholesale"
is a real answer and the price sheet has a dash there. But it is asked, because
without it the «Մեծածախ» button never appears when selling the item, and the
cashier who just added it is the one who would need it.
"""

from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes, ConversationHandler

from app import format, keyboards, texts
from app.api import ApiError, ApiUnavailable, api

log = logging.getLogger("storemanager.bot.newitem")

ASK_NAME, ASK_COUNT, ASK_COST, ASK_PRICE, ASK_WHOLESALE = range(50, 55)

MAX_COUNT = 1_000_000

_KEYS = ("new_name", "new_count", "new_cost", "new_price")


def _clear(context) -> None:
    for key in _KEYS:
        context.user_data.pop(key, None)


def _money(raw: str) -> Decimal | None:
    """A typed amount, or None when it is not a number."""
    cleaned = (raw or "").strip().replace(",", ".").replace(" ", "")
    try:
        amount = Decimal(cleaned).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return None
    return amount if amount >= 0 else None


async def begin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Reached from the correction screen, whose «Բոլորովին նոր ապրանք» button is
    this flow's entry point.

    That screen's draft is dropped on the way in. The two share one journey — the
    cashier went looking for a product on the shelf list and it was not there — and
    leaving half-finished nudges behind would apply them the next time the list was
    opened, long after anybody remembered making them.
    """
    _clear(context)
    for key in ("rs_items", "rs_deltas", "rs_page", "rs_key"):
        context.user_data.pop(key, None)

    message = update.effective_message
    if update.callback_query is not None:
        await update.callback_query.answer()
        await update.callback_query.edit_message_reply_markup(reply_markup=None)
        message = update.callback_query.message

    await message.reply_text(
        texts.NEW_ITEM_ASK_NAME, parse_mode=ParseMode.HTML,
        reply_markup=keyboards.selling(),
    )
    return ASK_NAME


async def type_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    name = (update.effective_message.text or "").strip()[:200]
    if not name:
        await update.effective_message.reply_text(texts.NEW_ITEM_ASK_NAME_AGAIN)
        return ASK_NAME

    context.user_data["new_name"] = name
    await update.effective_message.reply_text(
        texts.NEW_ITEM_ASK_COUNT.format(item=format.esc(name)), parse_mode=ParseMode.HTML
    )
    return ASK_COUNT


async def type_count(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    raw = (update.effective_message.text or "").strip()
    try:
        count = int(raw)
    except ValueError:
        await update.effective_message.reply_text(texts.BAD_QUANTITY)
        return ASK_COUNT
    if not (0 <= count <= MAX_COUNT):
        await update.effective_message.reply_text(texts.BAD_QUANTITY)
        return ASK_COUNT

    context.user_data["new_count"] = count
    await update.effective_message.reply_text(texts.NEW_ITEM_ASK_COST)
    return ASK_COST


async def type_cost(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    cost = _money(update.effective_message.text or "")
    if cost is None:
        await update.effective_message.reply_text(texts.CLOSEOUT_BAD_PRICE)
        return ASK_COST

    context.user_data["new_cost"] = cost
    await update.effective_message.reply_text(
        texts.NEW_ITEM_ASK_PRICE, parse_mode=ParseMode.HTML
    )
    return ASK_PRICE


async def type_price(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """The retail price — what a walk-in customer pays."""
    price = _money(update.effective_message.text or "")
    if price is None:
        await update.effective_message.reply_text(texts.CLOSEOUT_BAD_PRICE)
        return ASK_PRICE

    context.user_data["new_price"] = price
    await update.effective_message.reply_text(
        texts.NEW_ITEM_ASK_WHOLESALE,
        parse_mode=ParseMode.HTML,
        reply_markup=keyboards.skip(),
    )
    return ASK_WHOLESALE


async def type_wholesale(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    wholesale = _money(update.effective_message.text or "")
    if wholesale is None:
        await update.effective_message.reply_text(texts.CLOSEOUT_BAD_PRICE)
        return ASK_WHOLESALE

    return await _submit(update.effective_message, update.effective_user.id,
                         context, wholesale)


async def skip_wholesale(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Not sold wholesale. Stored as nothing, not as zero — zero reads as free."""
    query = update.callback_query
    await query.answer()
    await query.edit_message_reply_markup(reply_markup=None)
    return await _submit(query.message, update.effective_user.id, context, None)


async def _submit(message, telegram_id: int, context, wholesale) -> int:
    """The commit, from either the typed price or the skip button."""
    name = context.user_data.get("new_name")
    count = context.user_data.get("new_count")
    cost = context.user_data.get("new_cost")
    price = context.user_data.get("new_price")
    if name is None or count is None or cost is None or price is None:  # pragma: no cover
        return ConversationHandler.END

    try:
        result = await api.add_item(
            telegram_id=telegram_id,
            name=name,
            count=count,
            self_price=str(cost),
            sell_price=str(price),
            wholesale_price=None if wholesale is None else str(wholesale),
        )
    except (ApiError, ApiUnavailable) as exc:
        # "That name is already on the list" lands here, and the cashier should
        # get another go at the name rather than start the whole thing again.
        await message.reply_text(exc.human())
        if isinstance(exc, ApiError) and exc.code == "validation_error":
            await message.reply_text(texts.NEW_ITEM_ASK_NAME, parse_mode=ParseMode.HTML)
            return ASK_NAME
        _clear(context)
        return ConversationHandler.END
    except Exception:  # noqa: BLE001
        log.exception("could not add an item")
        _clear(context)
        await message.reply_text(texts.UNEXPECTED, reply_markup=keyboards.on_shift())
        return ConversationHandler.END

    _clear(context)
    try:
        item = result["item"]
        wholesale_line = (
            texts.NEW_ITEM_DONE_WHOLESALE.format(
                price=format.money(item["wholesale_price"])
            )
            if item.get("wholesale_price")
            else ""
        )
        body = texts.NEW_ITEM_DONE.format(
            item=format.esc(item["name"]),
            count=item["count"],
            price=format.money(item["sell_price"]),
            wholesale=wholesale_line,
        )
    except (KeyError, TypeError):
        log.exception("could not render a new-item confirmation from %r", result)
        body = texts.NEW_ITEM_DONE_PLAINLY

    await message.reply_text(
        body, parse_mode=ParseMode.HTML, reply_markup=keyboards.on_shift()
    )
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    _clear(context)
    # Reachable from the reply keyboard and from the inline «Չեղարկել» under the
    # wholesale question, so it has to answer a callback when there is one —
    # otherwise the button spins.
    if update.callback_query is not None:
        await update.callback_query.answer()
        await update.callback_query.edit_message_reply_markup(reply_markup=None)
    await update.effective_message.reply_text(
        texts.CANCELLED, reply_markup=keyboards.on_shift()
    )
    return ConversationHandler.END


def escape(handler):
    """Leave this flow and run something else, like the others."""

    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        _clear(context)
        await handler(update, context)
        return ConversationHandler.END

    wrapped.__name__ = f"escape_{getattr(handler, '__name__', 'handler')}"
    wrapped.__wrapped__ = handler
    return wrapped
