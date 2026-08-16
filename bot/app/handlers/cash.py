"""Taking money out of the till.

Cashiers take cash out — lunch, and paying the post office for a parcel. It
leaves whether or not the system knows about it, so the choice is not between
allowing it and forbidding it. It is between an unexplained shortfall at close
and a row saying where the money went.

Which is why the reason is asked for first, and picked rather than typed. It
decides the ceiling — lunch answers to the shift allowance, a courier's fee does
not — and a ceiling cannot hang off free spelling. Asking it first also means the
question about the amount can say what the limit is instead of refusing a number
after the fact.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import NamedTuple

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes, ConversationHandler

from app import format, keyboards, texts
from app.api import ApiError, ApiUnavailable, api, new_idempotency_key

log = logging.getLogger("storemanager.bot.cash")

ASK_REASON, ASK_AMOUNT = range(40, 42)

# Mirrors WORKER_WITHDRAWAL_LIMIT in web/app/services/money.py. Duplicated on
# purpose: the server is still the arbiter — it also knows what has already been
# taken this shift, which the bot does not — but being told "no" only after
# choosing a reason is a wasted question. This catches the obvious refusal at the
# step where the number was typed.
LIMIT = Decimal("1000.00")


class _Reason(NamedTuple):
    """One offered reason: what to send, what to ask, and what it may cost.

    ``limit`` of ``None`` is not "no rule" — the drawer still has to hold the
    money, and the server enforces that. It is "no allowance": a parcel costs
    what the post office charges.
    """

    code: str
    purpose: str
    ask: str
    limit: Decimal | None


# Codes match WITHDRAWAL_REASONS in web/app/services/money.py, and so do the
# purposes; a test holds both pairs together.
REASONS = {
    "lunch": _Reason("lunch", texts.CASH_PURPOSE_LUNCH, texts.CASH_ASK_AMOUNT_LUNCH, LIMIT),
    "delivery": _Reason(
        "delivery", texts.CASH_PURPOSE_DELIVERY, texts.CASH_ASK_AMOUNT_DELIVERY, None
    ),
}

_KEYS = ("cash_reason", "cash_key")


def _clear(context) -> None:
    for key in _KEYS:
        context.user_data.pop(key, None)


async def begin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    _clear(context)
    await update.effective_message.reply_text(
        texts.CASH_ASK_REASON, parse_mode=ParseMode.HTML,
        reply_markup=keyboards.cash_reasons(),
    )
    return ASK_REASON


async def choose_reason(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Also reachable from the amount step, so changing your mind is one tap.

    The allowance follows the new reason, and the key is dropped with it: a
    number typed under the old reason and sent under the new one would be a
    different withdrawal wearing the same idempotency key.
    """
    query = update.callback_query
    await query.answer()
    code = query.data.split(":", 1)[1]
    reason = REASONS.get(code)
    if reason is None:  # pragma: no cover - a button from a keyboard we no longer send
        await query.message.reply_text(
            texts.CASH_ASK_REASON, parse_mode=ParseMode.HTML,
            reply_markup=keyboards.cash_reasons(),
        )
        return ASK_REASON

    _clear(context)
    context.user_data["cash_reason"] = code
    await query.message.reply_text(
        reason.ask, parse_mode=ParseMode.HTML, reply_markup=keyboards.selling()
    )
    return ASK_AMOUNT


async def type_amount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """The number, and — once it passes — the commit.

    There is no third question. The reason is already known, which is the whole
    of what the old free-text step collected.
    """
    reason = REASONS.get(context.user_data.get("cash_reason") or "")
    if reason is None:  # pragma: no cover - state without its reason, ask again
        return await begin(update, context)

    amount = format.parse_money(update.effective_message.text)
    if amount is None or amount <= 0:
        await update.effective_message.reply_text(texts.CASH_BAD_AMOUNT)
        return ASK_AMOUNT
    if reason.limit is not None and amount > reason.limit:
        await update.effective_message.reply_text(
            texts.CASH_OVER_LIMIT.format(limit=format.money(reason.limit)),
            parse_mode=ParseMode.HTML,
        )
        return ASK_AMOUNT

    return await _commit(update, context, reason, amount)


async def _commit(update, context, reason: _Reason, amount: Decimal) -> int:
    """Whether the till can cover it is the server's call."""
    key = context.user_data.get("cash_key") or new_idempotency_key()
    context.user_data["cash_key"] = key

    try:
        result = await api.withdraw(
            telegram_id=update.effective_user.id,
            amount=str(amount),
            purpose=reason.purpose,
            reason=reason.code,
            key=key,
        )
    except (ApiError, ApiUnavailable) as exc:
        # "There is only X in the till" lands here, and the worker should stay
        # in the flow to correct the number rather than start again.
        await update.effective_message.reply_text(exc.human())
        if isinstance(exc, ApiError) and exc.code == "validation_error":
            context.user_data.pop("cash_key", None)
            await update.effective_message.reply_text(reason.ask,
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
