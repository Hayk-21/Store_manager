"""Taking money out of the till.

Cashiers take cash out — paying a delivery, buying bags, sending somebody for
change. It leaves whether or not the system knows about it, so the choice is not
between allowing it and forbidding it. It is between an unexplained shortfall at
close and a row saying where the money went.

Which is why the purpose is asked for and required. An amount with no reason is
the shortfall, just with a number attached.
"""

from __future__ import annotations

import logging
from decimal import Decimal

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes, ConversationHandler

from app import format, keyboards, texts
from app.api import ApiError, ApiUnavailable, api, new_idempotency_key

log = logging.getLogger("storemanager.bot.cash")

ASK_AMOUNT, ASK_PURPOSE = range(40, 42)

# Mirrors WORKER_WITHDRAWAL_LIMIT in web/app/services/money.py. Duplicated on
# purpose: the server is still the arbiter — it also knows what has already been
# taken this shift, which the bot does not — but being told "no" only after
# typing a reason is a wasted question. This catches the obvious refusal at the
# step where the number was typed.
LIMIT = Decimal("1000.00")

_KEYS = ("cash_amount", "cash_key")


def _clear(context) -> None:
    for key in _KEYS:
        context.user_data.pop(key, None)


async def begin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    _clear(context)
    await update.effective_message.reply_text(
        texts.CASH_ASK_AMOUNT, parse_mode=ParseMode.HTML,
        reply_markup=keyboards.selling(),
    )
    return ASK_AMOUNT


async def type_amount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    amount = format.parse_money(update.effective_message.text)
    if amount is None:
        await update.effective_message.reply_text(texts.CASH_BAD_AMOUNT)
        return ASK_AMOUNT
    if amount <= 0:
        await update.effective_message.reply_text(texts.CASH_BAD_AMOUNT)
        return ASK_AMOUNT
    if amount > LIMIT:
        await update.effective_message.reply_text(
            texts.CASH_OVER_LIMIT.format(limit=format.money(LIMIT)),
            parse_mode=ParseMode.HTML,
        )
        return ASK_AMOUNT

    context.user_data["cash_amount"] = amount
    await update.effective_message.reply_text(
        texts.CASH_ASK_PURPOSE.format(amount=format.money(amount)),
        parse_mode=ParseMode.HTML,
    )
    return ASK_PURPOSE


async def type_purpose(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """The commit. Whether the till can cover it is the server's call."""
    purpose = (update.effective_message.text or "").strip()[:300]
    if not purpose:
        await update.effective_message.reply_text(texts.CASH_ASK_PURPOSE_AGAIN)
        return ASK_PURPOSE

    amount = context.user_data.get("cash_amount")
    if amount is None:  # pragma: no cover
        return ConversationHandler.END

    key = context.user_data.get("cash_key") or new_idempotency_key()
    context.user_data["cash_key"] = key

    try:
        result = await api.withdraw(
            telegram_id=update.effective_user.id,
            amount=str(amount),
            purpose=purpose,
            key=key,
        )
    except (ApiError, ApiUnavailable) as exc:
        # "There is only X in the till" lands here, and the worker should stay
        # in the flow to correct the number rather than start again.
        await update.effective_message.reply_text(exc.human())
        if isinstance(exc, ApiError) and exc.code == "validation_error":
            context.user_data.pop("cash_key", None)
            await update.effective_message.reply_text(texts.CASH_ASK_AMOUNT,
                                                      parse_mode=ParseMode.HTML)
            return ASK_AMOUNT
        _clear(context)
        return ConversationHandler.END
    except Exception:  # noqa: BLE001
        log.exception("could not record a withdrawal")
        _clear(context)
        await update.effective_message.reply_text(
            texts.UNEXPECTED, reply_markup=keyboards.on_shift()
        )
        return ConversationHandler.END

    # The money is out of the till as far as the books are concerned. Nothing
    # below may report a failure.
    _clear(context)
    try:
        taken, totals = result["withdrawal"], result["store_totals"]
        body = texts.CASH_DONE.format(
            amount=format.money(taken["amount"]),
            purpose=format.esc(taken["purpose"]),
            cash=format.money(totals["cash"]),
        )
    except (KeyError, TypeError):
        log.exception("could not render a withdrawal confirmation from %r", result)
        body = texts.CASH_DONE_PLAINLY

    await update.effective_message.reply_text(
        body, parse_mode=ParseMode.HTML, reply_markup=keyboards.on_shift()
    )
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    _clear(context)
    message = update.effective_message
    if update.callback_query is not None:
        await update.callback_query.answer()
    await message.reply_text(texts.CANCELLED, reply_markup=keyboards.on_shift())
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
