"""What is on the shelf, for the store this worker is standing in."""

from __future__ import annotations

import logging

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from app import format, keyboards, texts
from app.api import ApiError, ApiUnavailable, api

log = logging.getLogger("storemanager.bot.stock")

# Telegram refuses a message over 4096 characters and a full catalogue gets
# there, so a long list is sent in pieces rather than losing its tail.
CHUNK_CHARS = 3500


async def show(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        result = await api.search_items(update.effective_user.id, "", limit=100)
    except Exception as exc:  # noqa: BLE001
        if isinstance(exc, (ApiError, ApiUnavailable)):
            off_shift = isinstance(exc, ApiError) and exc.code == "no_open_session"
            await update.effective_message.reply_text(
                exc.human(),
                reply_markup=keyboards.off_shift() if off_shift else keyboards.on_shift(),
            )
        else:
            log.exception("unhandled error while listing stock")
            await update.effective_message.reply_text(texts.UNEXPECTED)
        return

    items = result["items"]
    if not items:
        await update.effective_message.reply_text(
            texts.STOCK_EMPTY, reply_markup=keyboards.on_shift()
        )
        return

    in_stock = [i for i in items if i["count"] > 0]
    empty = [i for i in items if i["count"] <= 0]

    lines = [
        texts.STOCK_HEADER.format(
            store=format.esc(result["store_name"]),
            lines=len(items),
            units=sum(i["count"] for i in items),
        )
    ]
    lines += [
        texts.STOCK_ROW.format(
            name=format.esc(entry["name"]),
            count=entry["count"],
            price=format.money(entry["sell_price"]),
        )
        for entry in in_stock
    ]
    if empty:
        lines.append(texts.STOCK_EMPTY_HEADER)
        lines += [f"• {format.esc(entry['name'])}" for entry in empty]

    await _send_in_chunks(update.effective_message, lines)


async def _send_in_chunks(message, lines: list[str]) -> None:
    chunk: list[str] = []
    size = 0
    for line in lines:
        if size + len(line) > CHUNK_CHARS and chunk:
            await message.reply_text("\n".join(chunk), parse_mode=ParseMode.HTML)
            chunk, size = [], 0
        chunk.append(line)
        size += len(line) + 1
    await message.reply_text(
        "\n".join(chunk), parse_mode=ParseMode.HTML, reply_markup=keyboards.on_shift()
    )
