"""Correcting the shelf, one product at a time, with two buttons each.

The count on the screen drifts from the count on the shelf: a box arrives and
nobody enters it, a customer brings one back, two of something were miscounted at
the last close. The cashier is the only person standing in front of both numbers,
so they are the one who can fix it.

The whole list, with a − and a + against each product, because that is the shape
of the job: you walk along the shelf and nudge the ones that are wrong. Nothing is
written until «Հաստատել» — until then the numbers on screen are a draft, so a
mistap costs another tap rather than a wrong stock count.

Adding a product that has never existed here is a different job with different
questions, and it keeps its own flow. There is a button for it at the bottom.
"""

from __future__ import annotations

import logging

from telegram import Update
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import ContextTypes, ConversationHandler

from app import format, keyboards, texts
from app.api import ApiError, ApiUnavailable, api, new_idempotency_key
from app.handlers import newitem

log = logging.getLogger("storemanager.bot.restock")

# Its own range, clear of every other flow's.
PICK = 60

# How many products fit on one screen. Telegram allows more rows than this, but a
# keyboard taller than the phone means scrolling past the buttons that matter —
# «Հաստատել» among them.
PAGE = 6

_KEYS = ("rs_items", "rs_deltas", "rs_page", "rs_key")


def forget(context) -> None:
    """Drop the draft. Exposed because the new-product steps share this
    conversation and hand over by dropping it."""
    for key in _KEYS:
        context.user_data.pop(key, None)


def _clear(context) -> None:
    """Tidy both halves of this conversation.

    One conversation owns two screens — the shelf list and the steps for a product
    that is not on it — so one cancel has to clear both. Clearing only this half
    once left the other running behind a message that said "cancelled".
    """
    forget(context)
    newitem.forget(context)


def _deltas(context) -> dict[str, int]:
    """Pending changes, keyed by item id as a string.

    A string because ``user_data`` is persisted as JSON by some PTB backends, and
    integer keys do not survive that round trip.
    """
    return context.user_data.setdefault("rs_deltas", {})


async def begin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Load the shop's list and draw the first page."""
    _clear(context)
    try:
        # 100 is the endpoint's ceiling. A shop with more products than that gets
        # the first hundred, which is a real limit worth knowing about rather than
        # a number chosen to look generous — asking for 200 is a 422.
        result = await api.search_items(update.effective_user.id, "", limit=100)
    except (ApiError, ApiUnavailable) as exc:
        off_shift = isinstance(exc, ApiError) and exc.code == "no_open_session"
        await update.effective_message.reply_text(
            exc.human(),
            reply_markup=keyboards.off_shift() if off_shift else keyboards.on_shift(),
        )
        return ConversationHandler.END
    except Exception:  # noqa: BLE001
        log.exception("could not load stock for a correction")
        await update.effective_message.reply_text(
            texts.UNEXPECTED, reply_markup=keyboards.on_shift()
        )
        return ConversationHandler.END

    items = result["items"]
    if not items:
        # Nothing to nudge, but the reason they pressed the button might still be
        # "a delivery arrived", so offer the other door rather than a dead end.
        await update.effective_message.reply_text(
            texts.RESTOCK_NOTHING_YET,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboards.restock_empty(),
        )
        return PICK

    context.user_data["rs_items"] = [
        {"id": item["id"], "name": item["name"], "count": item["count"]}
        for item in items
    ]
    context.user_data["rs_page"] = 0
    await update.effective_message.reply_text(
        texts.RESTOCK_INTRO.format(store=format.esc(result["store_name"])),
        parse_mode=ParseMode.HTML,
        reply_markup=keyboards.selling(),
    )
    await update.effective_message.reply_text(
        _screen(context), parse_mode=ParseMode.HTML, reply_markup=_keyboard(context)
    )
    return PICK


def _page_items(context) -> list[dict]:
    items = context.user_data.get("rs_items") or []
    page = context.user_data.get("rs_page", 0)
    return items[page * PAGE : (page + 1) * PAGE]


def _screen(context) -> str:
    """The draft, as text. Only the changed lines are spelled out.

    Repeating all two hundred products in the message body would bury the four
    the cashier is actually changing, and the keyboard already shows the rest.
    """
    items = context.user_data.get("rs_items") or []
    deltas = _deltas(context)
    pages = max(1, -(-len(items) // PAGE))
    page = context.user_data.get("rs_page", 0)

    changed = [
        texts.RESTOCK_CHANGED_ROW.format(
            name=format.esc(item["name"]),
            before=item["count"],
            after=item["count"] + deltas[str(item["id"])],
            delta=_signed(deltas[str(item["id"])]),
        )
        for item in items
        if deltas.get(str(item["id"]))
    ]

    body = texts.RESTOCK_SCREEN.format(page=page + 1, pages=pages)
    if changed:
        return body + texts.RESTOCK_CHANGED_HEADER + "\n".join(changed)
    return body + texts.RESTOCK_NO_CHANGES


def _signed(delta: int) -> str:
    return f"+{delta}" if delta > 0 else str(delta)


def _keyboard(context):
    items = context.user_data.get("rs_items") or []
    pages = max(1, -(-len(items) // PAGE))
    return keyboards.restock(
        _page_items(context),
        _deltas(context),
        page=context.user_data.get("rs_page", 0),
        pages=pages,
        has_changes=any(_deltas(context).values()),
    )


async def _redraw(query, context) -> int:
    """Update the message in place. The draft lives in one message, not in a
    growing column of them.

    Telegram refuses an edit that would change nothing, and answers with an error
    rather than a shrug. That happens for real: a stale keyboard from an earlier
    page can ask to turn past the last page, and the worker would get "unexpected
    error" for tapping an arrow. Nothing changed, so nothing needs saying.
    """
    try:
        await query.edit_message_text(
            _screen(context), parse_mode=ParseMode.HTML, reply_markup=_keyboard(context)
        )
    except BadRequest as exc:
        if "not modified" not in str(exc).lower():
            raise
    return PICK


async def nudge(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """A − or a + against one product. Writes nothing."""
    query = update.callback_query
    _, raw_id, step = query.data.split(":")
    items = {str(item["id"]): item for item in context.user_data.get("rs_items") or []}
    item = items.get(raw_id)
    if item is None:  # pragma: no cover - the keyboard is built from this list
        await query.answer()
        return PICK

    deltas = _deltas(context)
    proposed = deltas.get(raw_id, 0) + int(step)
    if item["count"] + proposed < 0:
        # The shelf cannot hold less than nothing. Said out loud rather than
        # silently ignored, so a cashier tapping − does not think the button is
        # broken.
        await query.answer(texts.RESTOCK_AT_ZERO, show_alert=False)
        return PICK

    await query.answer()
    if proposed == 0:
        deltas.pop(raw_id, None)
    else:
        deltas[raw_id] = proposed
    return await _redraw(query, context)


async def add_something_new(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """«Բոլորովին նոր ապրանք» — the product is not on the list.

    Same conversation, different screen. Routed through here rather than wiring the
    button straight to the other module so the shelf draft is dropped on the way,
    and so the dependency runs one way only.
    """
    forget(context)
    return await newitem.begin(update, context)


async def noop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """The count in the middle of a row, and the page indicator.

    Both are buttons only because Telegram has no way to draw plain text between
    two buttons on one row. Answered so the client stops spinning, and otherwise
    ignored.
    """
    await update.callback_query.answer()
    return PICK


async def nothing_to_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Something typed on the correction screen.

    There is nothing to type here — the shelf is on the keyboard — but silence
    reads as a dead bot, and the cashier has no way to tell the difference between
    "that does nothing" and "this is broken".
    """
    await update.effective_message.reply_text(texts.RESTOCK_USE_THE_BUTTONS)
    return PICK


async def turn_page(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    items = context.user_data.get("rs_items") or []
    pages = max(1, -(-len(items) // PAGE))
    step = int(query.data.split(":", 1)[1])
    context.user_data["rs_page"] = max(0, min(pages - 1, context.user_data.get("rs_page", 0) + step))
    return await _redraw(query, context)


async def submit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """The commit. One key for this whole correction, reused by every retry."""
    query = update.callback_query
    await query.answer()
    deltas = {
        int(raw_id): delta for raw_id, delta in _deltas(context).items() if delta
    }
    if not deltas:
        # The confirm button is only drawn once something has changed, so this is
        # a stale keyboard. Already answered above — answering twice is an error
        # in its own right.
        return PICK

    key = context.user_data.get("rs_key") or new_idempotency_key()
    context.user_data["rs_key"] = key

    try:
        result = await api.adjust_stock(
            telegram_id=update.effective_user.id,
            lines=[{"item_id": item_id, "delta": delta} for item_id, delta in deltas.items()],
            key=key,
        )
    except (ApiError, ApiUnavailable) as exc:
        # "There are only 3 of those" lands here. The draft is still on screen and
        # still correct apart from that one line, so it stays: throwing away four
        # right answers because the fifth was wrong is not help.
        await query.message.reply_text(exc.human())
        if isinstance(exc, ApiError) and exc.code == "validation_error":
            context.user_data.pop("rs_key", None)
            return PICK
        _clear(context)
        return ConversationHandler.END
    except Exception:  # noqa: BLE001
        log.exception("could not apply a stock correction")
        _clear(context)
        await query.message.reply_text(
            texts.UNEXPECTED, reply_markup=keyboards.on_shift()
        )
        return ConversationHandler.END

    # The shelf is corrected as far as the books are concerned. Nothing below may
    # report a failure.
    _clear(context)
    try:
        rows = result["adjusted"]
        body = texts.RESTOCK_DONE.format(
            rows="\n".join(
                texts.RESTOCK_DONE_ROW.format(
                    name=format.esc(row["name"]),
                    delta=_signed(row["delta"]),
                    count=row["count_after"],
                )
                for row in rows
            )
        )
    except (KeyError, TypeError):
        log.exception("could not render a correction confirmation from %r", result)
        body = texts.RESTOCK_DONE_PLAINLY

    await query.edit_message_reply_markup(reply_markup=None)
    await query.message.reply_text(
        body, parse_mode=ParseMode.HTML, reply_markup=keyboards.on_shift()
    )
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    _clear(context)
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
