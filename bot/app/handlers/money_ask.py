"""Asking another shop, or the owner, for cash.

The drawer runs dry from the inside, and the wage is what exposes it: the till pays
as far as it reaches and the rest becomes a debt, so a worker locking up on 2,000
with a 5,000 wage due goes home short *and* the shop opens tomorrow with nothing to
give change from. The money is not missing — it is in a sister shop's drawer, or in
the owner's pocket — and until now there was no way to say so from behind the
counter.

It lives on «🔄 Փոխանցումներ», which is already the screen for business between
shops, and it belongs to that conversation rather than starting one of its own: two
conversations alive on one screen is how a tap ends up answered by the wrong flow.

**Answering is the unusual part.** The buttons arrive on a message the *web* service
pushes, and the person who taps them may be a worker at the shop being asked or the
owner themselves. The bot does not decide which — it forwards the tap, and the server
resolves it from the Telegram account, which is the one thing an answer cannot lie
about.
"""

from __future__ import annotations

import logging

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes, ConversationHandler

from app import format, keyboards, texts
from app.api import ApiError, ApiUnavailable, api, new_idempotency_key

log = logging.getLogger("storemanager.bot.money_ask")

# Its own numbers, immediately after the transfer flow's — they share a conversation,
# so a collision would put two different questions in one state.
PICK_WHO, ASK_AMOUNT = range(74, 76)

OWNER = "owner"

_KEYS = ("ask_who", "ask_who_name", "ask_key")


def _clear(context) -> None:
    for key in _KEYS:
        context.user_data.pop(key, None)


async def begin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """«Գումար խնդրել» tapped. Who is being asked comes first."""
    _clear(context)
    query = update.callback_query
    if query is not None:
        await query.answer()

    try:
        result = await api.money_request_targets(update.effective_user.id)
    except (ApiError, ApiUnavailable) as exc:
        await update.effective_message.reply_text(
            exc.human(), reply_markup=keyboards.on_shift()
        )
        return ConversationHandler.END
    except Exception:  # noqa: BLE001
        log.exception("could not list who could be asked for money")
        await update.effective_message.reply_text(
            texts.UNEXPECTED, reply_markup=keyboards.on_shift()
        )
        return ConversationHandler.END

    stores = result.get("stores") or []
    # Never empty, unlike sending: the owner is always askable, which is the whole
    # point of them being on the list. A shop with no open sister shop still has
    # somebody to ask.
    context.user_data["ask_names"] = {
        str(store["id"]): store["name"] for store in stores
    }
    if query is not None:
        await query.edit_message_reply_markup(reply_markup=None)
    await update.effective_message.reply_text(
        texts.MONEY_ASK_PICK_WHO, parse_mode=ParseMode.HTML,
        reply_markup=keyboards.money_ask_targets(stores),
    )
    return PICK_WHO


async def choose_who(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    who = query.data.split(":", 1)[1]

    context.user_data["ask_who"] = who
    names = context.user_data.get("ask_names") or {}
    context.user_data["ask_who_name"] = (
        texts.BTN_MONEY_ASK_OWNER if who == OWNER else names.get(who, "")
    )

    await query.edit_message_reply_markup(reply_markup=None)
    await query.message.reply_text(
        texts.MONEY_ASK_AMOUNT.format(
            who=format.esc(context.user_data["ask_who_name"])
        ),
        parse_mode=ParseMode.HTML,
        reply_markup=keyboards.selling(),
    )
    return ASK_AMOUNT


def _source(who: str, name: str) -> str:
    """Who was asked, in the shape the confirmation needs it."""
    if who == OWNER:
        return texts.MONEY_ASK_FROM_OWNER
    return texts.MONEY_ASK_FROM_STORE.format(store=format.esc(name))


async def type_amount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """How much, and — once it is a number — the request itself.

    Nothing bounds it here. What a shop needs is not what this shop has, and the
    only figure that can refuse it is the drawer of whoever is being asked, at the
    moment they answer.
    """
    who = context.user_data.get("ask_who")
    if who is None:  # pragma: no cover - state without its target
        return await begin(update, context)

    amount = format.parse_money(update.effective_message.text)
    if amount is None or amount <= 0:
        await update.effective_message.reply_text(texts.CASH_BAD_AMOUNT)
        return ASK_AMOUNT

    key = context.user_data.get("ask_key") or new_idempotency_key()
    context.user_data["ask_key"] = key

    try:
        result = await api.ask_for_money(
            telegram_id=update.effective_user.id,
            asked_of=who,
            amount=str(amount),
            key=key,
        )
    except (ApiError, ApiUnavailable) as exc:
        await update.effective_message.reply_text(exc.human())
        if isinstance(exc, ApiError) and exc.code == "validation_error":
            # The shop shut while this was being typed, or the number was wrong.
            # Another go beats starting from the list again.
            context.user_data.pop("ask_key", None)
            return ASK_AMOUNT
        _clear(context)
        return ConversationHandler.END
    except Exception:  # noqa: BLE001
        log.exception("could not ask for money")
        _clear(context)
        await update.effective_message.reply_text(
            texts.UNEXPECTED, reply_markup=keyboards.on_shift()
        )
        return ConversationHandler.END

    name = context.user_data.get("ask_who_name") or ""
    _clear(context)
    try:
        asked = result["request"]
        body = texts.MONEY_ASK_SENT.format(
            amount=format.money(asked["amount"]),
            source=_source(who, name),
        )
    except (KeyError, TypeError):
        log.exception("could not render a money request from %r", result)
        body = texts.MONEY_ASK_SENT_PLAINLY

    await update.effective_message.reply_text(
        body, parse_mode=ParseMode.HTML, reply_markup=keyboards.on_shift()
    )
    return ConversationHandler.END


# -- answering one -----------------------------------------------------------

async def decide(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Yes or no, from whoever was asked.

    Outside every conversation, and the only handler in the bot that may be tapped
    by somebody who is not a worker: the owner answers a request made of them from
    the same message, with the same buttons. Nothing here tells them apart — the
    server resolves the Telegram account and applies whichever authority it belongs
    to, so a mistake about who is tapping is not a mistake this can make.
    """
    query = update.callback_query
    await query.answer()
    _, raw_id, verdict = query.data.split(":")

    try:
        result = await api.decide_money_request(
            update.effective_user.id, int(raw_id), verdict == "y"
        )
    except (ApiError, ApiUnavailable) as exc:
        await query.message.reply_text(exc.human())
        return ConversationHandler.END
    except Exception:  # noqa: BLE001
        log.exception("could not answer a money request")
        await query.message.reply_text(texts.UNEXPECTED)
        return ConversationHandler.END

    # Answered. Whatever happens below is presentation.
    try:
        asked = result["request"]
        if asked["status"] != "accepted":
            template = texts.MONEY_ASK_REFUSED
        elif asked["asked_the_owner"]:
            template = texts.MONEY_ASK_ACCEPTED_BY_OWNER
        else:
            template = texts.MONEY_ASK_ACCEPTED
        body = template.format(
            amount=format.money(asked["amount"]),
            store=format.esc(asked["to_store"]),
        )
    except (KeyError, TypeError):
        log.exception("could not render a money request decision from %r", result)
        body = texts.MONEY_ASK_DECIDED_PLAINLY

    await query.edit_message_reply_markup(reply_markup=None)
    await query.message.reply_text(body, parse_mode=ParseMode.HTML)
    return ConversationHandler.END


async def show_waiting(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """What this shop is being asked for and has not answered.

    On «Փոխանցումներ», beside the money coming the other way. Silent when there is
    nothing waiting: an empty section on every visit is noise on a screen a worker
    opens to do something else.
    """
    try:
        result = await api.pending_money_requests(update.effective_user.id)
    except Exception:  # noqa: BLE001
        # A courtesy panel. Its failure must not take the screen it sits on with it.
        log.info("could not list money requests waiting", exc_info=True)
        return

    incoming = result.get("incoming") or []
    if not incoming:
        return

    await update.effective_message.reply_text(
        texts.MONEY_ASK_WAITING.format(
            rows="\n".join(
                texts.MONEY_ASK_WAITING_ROW.format(
                    amount=format.money(row["amount"]),
                    store=format.esc(row["store"]),
                    worker=format.esc(row["worker"] or ""),
                )
                for row in incoming
            )
        ),
        parse_mode=ParseMode.HTML,
        reply_markup=keyboards.money_asks_waiting(incoming),
    )


def clear(context) -> None:
    """Drop a half-asked request when the screen it lives on is left."""
    _clear(context)
    context.user_data.pop("ask_names", None)
