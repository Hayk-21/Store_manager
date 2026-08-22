"""Cash leaving the drawer: spent, or sent to another shop.

Cashiers take money out. It leaves whether or not the system knows about it, so
the choice is not between allowing it and forbidding it. It is between an
unexplained shortfall at close and a row saying where the money went.

Which is why the category is asked for first, and picked rather than typed. It
decides the allowance — lunch answers to it, anything else does not — and an
allowance cannot hang off free spelling. Asking it first also means the question
about the amount can say what the limit is instead of refusing a number after the
fact.

Three answers, and the third is not a reason at all:

* **Ճաշ** — the one thing anybody can put a figure on in advance, so it is the one
  thing with a ceiling.
* **Այլ** — everything else. A courier, a plumber, a taxi with stock in it: nobody
  can list these in advance, so the cashier writes what it was for and no
  allowance applies. The category used to name one specific errand, which meant
  every other errand had to be filed as a lie or not at all.
* **Փոխանցել այլ խանութ** — the money is not being spent. It is moving to another
  of the owner's tills, and it only arrives there when somebody at that shop says
  it has.
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

ASK_REASON, ASK_AMOUNT, ASK_PURPOSE, PICK_STORE = range(40, 44)

# Mirrors WORKER_WITHDRAWAL_LIMIT in web/app/services/money.py. Duplicated on
# purpose: the server is still the arbiter — it also knows what has already been
# taken this shift, which the bot does not — but being told "no" only after
# choosing a reason is a wasted question. This catches the obvious refusal at the
# step where the number was typed.
LIMIT = Decimal("1000.00")

# The purpose a whole category is, when the cashier is not asked to write one.
TO_STORE = "to_store"


class _Reason(NamedTuple):
    """One offered category: what to send, what to ask, and what it may cost.

    ``limit`` of ``None`` is not "no rule" — the drawer still has to hold the
    money, and the server enforces that. It is "no allowance".

    ``asks_purpose`` is «Այլ» alone: it is the category whose whole point is that
    the reason cannot be guessed, so it is collected before the amount. Asking
    afterwards would have somebody explaining money that has effectively gone.
    """

    code: str
    purpose: str
    ask: str
    limit: Decimal | None
    asks_purpose: bool = False


# Codes match WITHDRAWAL_REASONS in web/app/services/money.py, and so do the
# purposes; a test holds both pairs together.
REASONS = {
    "lunch": _Reason("lunch", texts.CASH_PURPOSE_LUNCH, texts.CASH_ASK_AMOUNT_LUNCH, LIMIT),
    "other": _Reason(
        "other", texts.CASH_PURPOSE_OTHER, texts.CASH_ASK_PURPOSE, None,
        asks_purpose=True,
    ),
}

_KEYS = ("cash_reason", "cash_key", "cash_purpose", "cash_store", "cash_store_name",
         "cash_store_names", "cash_available")


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
    """Also reachable from the later steps, so changing your mind is one tap.

    The allowance follows the new category, and the key is dropped with it: a
    number typed under the old reason and sent under the new one would be a
    different withdrawal wearing the same idempotency key.
    """
    query = update.callback_query
    await query.answer()
    code = query.data.split(":", 1)[1]

    if code == TO_STORE:
        _clear(context)
        return await _pick_a_store(update, context)

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
    return ASK_PURPOSE if reason.asks_purpose else ASK_AMOUNT


def _ask_the_amount(reason: _Reason, purpose: str) -> str:
    """The question about the number, for a category that has already been picked."""
    if reason.asks_purpose:
        return texts.CASH_ASK_AMOUNT_OTHER.format(purpose=format.esc(purpose))
    return reason.ask


async def type_purpose(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """What the money is for, in the cashier's own words. «Այլ» only."""
    reason = REASONS.get(context.user_data.get("cash_reason") or "")
    if reason is None:  # pragma: no cover - state without its category, ask again
        return await begin(update, context)

    purpose = (update.effective_message.text or "").strip()
    if len(purpose) < 2:
        # One character is a slip of the thumb, not a reason. The row has to be
        # readable by somebody who was not there.
        await update.effective_message.reply_text(texts.CASH_BAD_PURPOSE)
        return ASK_PURPOSE

    context.user_data["cash_purpose"] = purpose[:280]
    await update.effective_message.reply_text(
        _ask_the_amount(reason, purpose[:280]),
        parse_mode=ParseMode.HTML,
        reply_markup=keyboards.selling(),
    )
    return ASK_AMOUNT


async def type_amount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """The number, and — once it passes — the commit."""
    reason = REASONS.get(context.user_data.get("cash_reason") or "")
    if reason is None:  # pragma: no cover - state without its category, ask again
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
    purpose = context.user_data.get("cash_purpose") or reason.purpose

    try:
        result = await api.withdraw(
            telegram_id=update.effective_user.id,
            amount=str(amount),
            purpose=purpose,
            reason=reason.code,
            key=key,
        )
    except (ApiError, ApiUnavailable) as exc:
        # "There is only X in the till" lands here, and the worker should stay
        # in the flow to correct the number rather than start again.
        await update.effective_message.reply_text(exc.human())
        if isinstance(exc, ApiError) and exc.code == "validation_error":
            context.user_data.pop("cash_key", None)
            # The amount question again, not the category's opening one: under
            # «Այլ» that would ask what the money is for a second time, and the
            # cashier has already said.
            await update.effective_message.reply_text(
                _ask_the_amount(reason, purpose), parse_mode=ParseMode.HTML
            )
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


# -- sending it to another shop ----------------------------------------------

async def _pick_a_store(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Which shop. Only the open ones, and the server decides which those are."""
    message = update.effective_message
    try:
        result = await api.money_transfer_stores(update.effective_user.id)
    except (ApiError, ApiUnavailable) as exc:
        await message.reply_text(exc.human(), reply_markup=keyboards.on_shift())
        return ConversationHandler.END
    except Exception:  # noqa: BLE001
        log.exception("could not list the shops money could be sent to")
        await message.reply_text(texts.UNEXPECTED, reply_markup=keyboards.on_shift())
        return ConversationHandler.END

    stores = result.get("stores") or []
    if not stores:
        await message.reply_text(
            texts.MONEY_TRANSFER_NO_STORES, reply_markup=keyboards.on_shift()
        )
        return ConversationHandler.END

    context.user_data["cash_available"] = result.get("available") or "0"
    await message.reply_text(
        texts.MONEY_TRANSFER_PICK_STORE, parse_mode=ParseMode.HTML,
        reply_markup=keyboards.money_transfer_stores(stores),
    )
    context.user_data["cash_store_names"] = {
        str(store["id"]): store["name"] for store in stores
    }
    return PICK_STORE


async def choose_store(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    store_id = query.data.split(":", 1)[1]
    names = context.user_data.get("cash_store_names") or {}

    context.user_data["cash_store"] = int(store_id)
    context.user_data["cash_store_name"] = names.get(store_id, "")
    available = context.user_data.get("cash_available") or "0"

    await query.edit_message_reply_markup(reply_markup=None)
    await query.message.reply_text(
        texts.MONEY_TRANSFER_ASK_AMOUNT.format(
            store=format.esc(context.user_data["cash_store_name"]),
            available=format.money(available),
        ),
        parse_mode=ParseMode.HTML,
        reply_markup=keyboards.selling(),
    )
    return ASK_AMOUNT


async def type_transfer_amount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """The number. Checked against the drawer here as a courtesy — the server
    checks it again against the drawer as it stands at the moment of writing, which
    is the one that counts."""
    store_id = context.user_data.get("cash_store")
    if store_id is None:  # pragma: no cover - state without its shop
        return await begin(update, context)

    amount = format.parse_money(update.effective_message.text)
    if amount is None or amount <= 0:
        await update.effective_message.reply_text(texts.CASH_BAD_AMOUNT)
        return ASK_AMOUNT

    available = Decimal(context.user_data.get("cash_available") or "0")
    if amount > available:
        await update.effective_message.reply_text(
            texts.MONEY_TRANSFER_TOO_MUCH.format(available=format.money(available)),
            parse_mode=ParseMode.HTML,
        )
        return ASK_AMOUNT

    key = context.user_data.get("cash_key") or new_idempotency_key()
    context.user_data["cash_key"] = key

    try:
        result = await api.send_money(
            telegram_id=update.effective_user.id,
            to_store_id=store_id,
            amount=str(amount),
            key=key,
        )
    except (ApiError, ApiUnavailable) as exc:
        await update.effective_message.reply_text(exc.human())
        if isinstance(exc, ApiError) and exc.code == "validation_error":
            # The drawer moved, or the other shop shut while this was being typed.
            # Another go at the number beats starting from the shop list again.
            context.user_data.pop("cash_key", None)
            return ASK_AMOUNT
        _clear(context)
        return ConversationHandler.END
    except Exception:  # noqa: BLE001
        log.exception("could not send money to another shop")
        _clear(context)
        await update.effective_message.reply_text(
            texts.UNEXPECTED, reply_markup=keyboards.on_shift()
        )
        return ConversationHandler.END

    # It is out of the till. Nothing below may report a failure.
    _clear(context)
    try:
        sent = result["transfer"]
        body = texts.MONEY_TRANSFER_SENT.format(
            amount=format.money(sent["amount"]),
            store=format.esc(sent["to_store"]),
        )
    except (KeyError, TypeError):
        log.exception("could not render a transfer confirmation from %r", result)
        body = texts.MONEY_TRANSFER_SENT_PLAINLY

    await update.effective_message.reply_text(
        body, parse_mode=ParseMode.HTML, reply_markup=keyboards.on_shift()
    )
    return ConversationHandler.END


async def amount_step(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """One state, two questions. Which one is being answered depends on whether a
    shop was picked, and keeping them in a single state is what lets the category
    buttons stay live on both — a cashier who meant «Ճաշ» can say so without
    starting again."""
    if context.user_data.get("cash_store") is not None:
        return await type_transfer_amount(update, context)
    return await type_amount(update, context)


# -- answering an envelope ---------------------------------------------------

async def decide(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Confirm or deny cash sent from another shop.

    Outside the conversation on purpose: these buttons arrive on a pushed message
    and may be tapped an hour later, long after any flow has timed out.
    """
    query = update.callback_query
    await query.answer()
    _, raw_id, verdict = query.data.split(":")

    try:
        result = await api.decide_money_transfer(
            update.effective_user.id, int(raw_id), verdict == "y"
        )
    except (ApiError, ApiUnavailable) as exc:
        await query.message.reply_text(exc.human())
        return ConversationHandler.END
    except Exception:  # noqa: BLE001
        log.exception("could not answer a money transfer")
        await query.message.reply_text(texts.UNEXPECTED)
        return ConversationHandler.END

    # Decided. Whatever happens below is presentation.
    try:
        transfer = result["transfer"]
        received = transfer["status"] == "received"
        # The other shop either way: the money came from there, and on a denial it
        # is where it has just gone back to.
        body = (
            texts.MONEY_TRANSFER_CONFIRMED if received else texts.MONEY_TRANSFER_DENIED
        ).format(
            amount=format.money(transfer["amount"]),
            store=format.esc(transfer["from_store"]),
        )
    except (KeyError, TypeError):
        log.exception("could not render a transfer decision from %r", result)
        body = texts.MONEY_TRANSFER_DECIDED_PLAINLY

    await query.edit_message_reply_markup(reply_markup=None)
    await query.message.reply_text(body, parse_mode=ParseMode.HTML)
    return ConversationHandler.END


async def show_waiting(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Envelopes this shop has been told to expect and not yet confirmed.

    Shown with the transfers screen, which is where a worker goes when the pushed
    message was missed — scrolled past, or sent to a colleague who has since gone
    home. Silent when there is nothing waiting: an empty section on every visit is
    noise.
    """
    try:
        result = await api.pending_money_transfers(update.effective_user.id)
    except Exception:  # noqa: BLE001
        # A courtesy panel. Its failure must not take the screen it sits on with it.
        log.info("could not list money waiting to be confirmed", exc_info=True)
        return

    incoming = result.get("incoming") or []
    if not incoming:
        return

    await update.effective_message.reply_text(
        texts.MONEY_TRANSFER_WAITING.format(
            rows="\n".join(
                texts.MONEY_TRANSFER_WAITING_ROW.format(
                    amount=format.money(row["amount"]),
                    store=format.esc(row["from_store"]),
                    worker=format.esc(row["sent_by"] or ""),
                )
                for row in incoming
            )
        ),
        parse_mode=ParseMode.HTML,
        reply_markup=keyboards.money_transfers_waiting(incoming),
    )


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
