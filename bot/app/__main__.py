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

log = logging.getLogger("storemanager.bot")


def _exact(label: str):
    """A reply-keyboard button is just a text message with a known body."""
    return filters.Text([label])


# Every label the bot ever puts on a button, collected from texts.py rather than
# listed by hand so a new button cannot be forgotten here.
#
# A reply-keyboard tap arrives as ordinary text. Without this exclusion the sell
# flow treated "📍 Ուղարկել տեղորոշումը" as the name of a product and answered
# "you are not on shift" — true, and baffling. Telegram Desktop cannot attach a
# location at all and sends the label as plain text, so it is not a rare case.
#
# Inline-keyboard labels arrive as callbacks and could not collide, but they are
# swept up too: nobody stocks a product called "💳 Քարտ", and one rule is easier
# to keep true than two.
BUTTON_LABELS = sorted(
    value
    for name, value in vars(texts).items()
    if name.startswith("BTN_") and isinstance(value, str)
)

# Text a cashier actually typed, as opposed to a button they tapped. The sell
# flow only ever consumes this, so a button press is never mistaken for a
# product name or a quantity.
_free_text = filters.TEXT & ~filters.COMMAND & ~filters.Text(BUTTON_LABELS)


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
                filters.TEXT & ~filters.COMMAND & ~filters.Text(BUTTON_LABELS),
                sell.search,
            ),
        ],
        states={
            sell.ASK_ITEM: [
                CallbackQueryHandler(sell.choose_item, pattern=f"^{keyboards.CB_ITEM}:"),
                MessageHandler(_free_text, sell.search),
            ],
            sell.ASK_QUANTITY: [
                MessageHandler(_free_text, sell.choose_quantity),
            ],
            sell.ASK_PRICE_KIND: [
                CallbackQueryHandler(
                    sell.choose_price_kind, pattern=f"^{keyboards.CB_KIND}:"
                ),
            ],
            sell.ASK_PAYMENT: [
                CallbackQueryHandler(sell.choose_payment, pattern=f"^{keyboards.CB_PAY}:"),
            ],
        },
        # Every reply-keyboard button is a way out. The states above only take
        # *free* text, so a button pressed mid-sale lands here instead of being
        # read as a product name or a quantity — which is what left a cashier
        # unable to escape the flow at all.
        fallbacks=[
            CallbackQueryHandler(sell.cancel, pattern=f"^{keyboards.CB_CANCEL}$"),
            CommandHandler("cancel", sell.cancel),
            CommandHandler("start", sell.escape(shift.start)),
            MessageHandler(_exact(texts.BTN_CANCEL), sell.cancel),
            # Pressing "sell" again means start over, not "quantity = 🧾 Վաճառք".
            MessageHandler(_exact(texts.BTN_SELL), sell.restart),
            MessageHandler(_exact(texts.BTN_UNDO), sell.escape(sell.undo)),
            MessageHandler(_exact(texts.BTN_STATUS), sell.escape(shift.status)),
            MessageHandler(_exact(texts.BTN_END_SHIFT), sell.escape(shift.end_shift)),
            MessageHandler(
                _exact(texts.BTN_CLOSE_STORE), sell.escape(shift.confirm_close_store)
            ),
            MessageHandler(_exact(texts.BTN_OPEN), sell.escape(shift.ask_location)),
            MessageHandler(
                _exact(texts.BTN_SEND_LOCATION), sell.escape(common.location_from_desktop)
            ),
            MessageHandler(filters.LOCATION, sell.escape(shift.handle_location)),
        ],
        # A cashier who wanders off mid-sale should not be stuck in the flow.
        conversation_timeout=300,
        per_message=False,
    )

    application.add_handler(CommandHandler("start", shift.start))
    application.add_handler(CommandHandler("help", common.help_command))

    application.add_handler(MessageHandler(_exact(texts.BTN_OPEN), shift.ask_location))
    application.add_handler(MessageHandler(filters.LOCATION, shift.handle_location))
    # The label arriving as text means the tap produced no location: Telegram
    # Desktop cannot share one. Say so, instead of letting it fall through.
    application.add_handler(
        MessageHandler(_exact(texts.BTN_SEND_LOCATION), common.location_from_desktop)
    )
    application.add_handler(MessageHandler(_exact(texts.BTN_STATUS), shift.status))
    application.add_handler(MessageHandler(_exact(texts.BTN_UNDO), sell.undo))
    application.add_handler(MessageHandler(_exact(texts.BTN_END_SHIFT), shift.end_shift))
    application.add_handler(
        MessageHandler(_exact(texts.BTN_CLOSE_STORE), shift.confirm_close_store)
    )
    application.add_handler(
        CallbackQueryHandler(shift.close_store, pattern=f"^{keyboards.CB_CLOSE_STORE}$")
    )
    # Dismissing the close-store confirmation has its own callback data. It used
    # to share CB_CANCEL with the sell flow, and being registered ahead of the
    # conversation it swallowed the sell flow's own cancel button: the cashier
    # saw "cancelled" while the conversation quietly stayed open.
    application.add_handler(
        CallbackQueryHandler(common.dismiss, pattern=f"^{keyboards.CB_DISMISS}$")
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
