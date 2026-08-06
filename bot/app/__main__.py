"""Entrypoint: build the application, register handlers, poll.

Long polling rather than a webhook, because it needs no public URL and no TLS
plumbing. It does mean **exactly one replica**: two processes calling getUpdates
on one token fight, and Telegram answers
``Conflict: terminated by other getUpdates request``.
"""

from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ConversationHandler,
    MessageHandler,
    filters,
)

from app import keyboards, texts
from app.api import api
from app.config import settings
from app.handlers import common, sell, shift

log = logging.getLogger("vapestore.bot")


def _exact(label: str):
    """A reply-keyboard button is just a text message with a known body."""
    return filters.Text([label])


def build() -> Application:
    application = ApplicationBuilder().token(settings.bot_token).post_shutdown(_shutdown).build()

    # The sell flow owns plain text while it is running, so it has to be
    # registered before the catch-all text handler below.
    sell_flow = ConversationHandler(
        entry_points=[
            MessageHandler(_exact(texts.BTN_SELL), sell.prompt),
            # Typing a product name with no shift-control button pressed starts
            # the flow directly — the fast path for someone who knows the stock.
            MessageHandler(
                filters.TEXT
                & ~filters.COMMAND
                & ~_exact(texts.BTN_OPEN)
                & ~_exact(texts.BTN_UNDO)
                & ~_exact(texts.BTN_STATUS)
                & ~_exact(texts.BTN_END_SHIFT)
                & ~_exact(texts.BTN_CLOSE_STORE),
                sell.search,
            ),
        ],
        states={
            sell.ASK_ITEM: [
                CallbackQueryHandler(sell.choose_item, pattern=f"^{keyboards.CB_ITEM}:"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, sell.search),
            ],
            sell.ASK_QUANTITY: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, sell.choose_quantity),
            ],
            sell.ASK_PAYMENT: [
                CallbackQueryHandler(sell.choose_payment, pattern=f"^{keyboards.CB_PAY}:"),
            ],
        },
        fallbacks=[
            CallbackQueryHandler(sell.cancel, pattern=f"^{keyboards.CB_CANCEL}$"),
            CommandHandler("cancel", sell.cancel),
            MessageHandler(_exact(texts.BTN_CANCEL), sell.cancel),
        ],
        # A cashier who wanders off mid-sale should not be stuck in the flow.
        conversation_timeout=300,
        per_message=False,
    )

    application.add_handler(CommandHandler("start", shift.start))
    application.add_handler(CommandHandler("help", common.help_command))

    application.add_handler(MessageHandler(_exact(texts.BTN_OPEN), shift.ask_location))
    application.add_handler(MessageHandler(filters.LOCATION, shift.handle_location))
    application.add_handler(MessageHandler(_exact(texts.BTN_STATUS), shift.status))
    application.add_handler(MessageHandler(_exact(texts.BTN_UNDO), sell.undo))
    application.add_handler(MessageHandler(_exact(texts.BTN_END_SHIFT), shift.end_shift))
    application.add_handler(
        MessageHandler(_exact(texts.BTN_CLOSE_STORE), shift.confirm_close_store)
    )
    application.add_handler(
        CallbackQueryHandler(shift.close_store, pattern=f"^{keyboards.CB_CLOSE_STORE}$")
    )
    application.add_handler(
        CallbackQueryHandler(sell.cancel, pattern=f"^{keyboards.CB_CANCEL}$")
    )

    application.add_handler(sell_flow)
    application.add_handler(MessageHandler(filters.COMMAND, common.unknown))
    application.add_error_handler(common.on_error)

    return application


async def _shutdown(application: Application) -> None:
    await api.aclose()


def main() -> None:
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    log.info("starting bot against %s", settings.api_base_url)
    build().run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
