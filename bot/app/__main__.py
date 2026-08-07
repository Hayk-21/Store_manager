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
from app.handlers import closeout, common, shift, stock

log = logging.getLogger("storemanager.bot")


def _exact(label: str):
    """A reply-keyboard button is just a text message with a known body."""
    return filters.Text([label])


# Every label the bot ever puts on a button, collected from texts.py rather than
# listed by hand so a new button cannot be forgotten here.
#
# A reply-keyboard tap arrives as ordinary text. Without this exclusion the
# write-up flow would treat "📍 Ուղարկել տեղորոշումը" as the name of a product —
# Telegram Desktop cannot attach a location at all and sends the label as plain
# text, so it is not a rare case.
BUTTON_LABELS = sorted(
    value
    for name, value in vars(texts).items()
    if name.startswith("BTN_") and isinstance(value, str)
)

# Text a cashier actually typed, as opposed to a button they tapped. The
# write-up only ever consumes this, so a button press is never mistaken for a
# product name, a quantity or a price.
_free_text = filters.TEXT & ~filters.COMMAND & ~filters.Text(BUTTON_LABELS)


def build() -> Application:
    application = ApplicationBuilder().token(settings.bot_token).post_shutdown(_shutdown).build()

    # Writing the day up. A cashier does not touch the bot while serving; they
    # list what went out once, at the end, and that list is the only way a sale
    # reaches the system.
    closeout_flow = ConversationHandler(
        entry_points=[
            MessageHandler(_exact(texts.BTN_END_SHIFT), closeout.begin),
            MessageHandler(_exact(texts.BTN_CLOSE_STORE), closeout.begin),
        ],
        states={
            closeout.PICK_ITEM: [
                MessageHandler(_exact(texts.BTN_CO_ADD), closeout.prompt_item),
                MessageHandler(_exact(texts.BTN_CO_REMOVE), closeout.drop_last),
                MessageHandler(_exact(texts.BTN_CO_DONE), closeout.review),
                CallbackQueryHandler(closeout.choose_item, pattern=f"^{keyboards.CB_ITEM}:"),
                MessageHandler(_free_text, closeout.search),
            ],
            closeout.ASK_QUANTITY: [
                MessageHandler(_free_text, closeout.choose_quantity),
            ],
            closeout.ASK_PRICE: [
                CallbackQueryHandler(
                    closeout.choose_suggested_price, pattern=f"^{keyboards.CB_KIND}:"
                ),
                MessageHandler(_free_text, closeout.type_price),
            ],
            closeout.ASK_METHOD: [
                CallbackQueryHandler(closeout.choose_method, pattern=f"^{keyboards.CB_PAY}:"),
            ],
            closeout.CONFIRM: [
                CallbackQueryHandler(closeout.submit, pattern=f"^{keyboards.CB_SUBMIT}:"),
                MessageHandler(_exact(texts.BTN_CO_ADD), closeout.prompt_item),
                MessageHandler(_exact(texts.BTN_CO_REMOVE), closeout.drop_last),
            ],
        },
        # Every other button is a way out: the states above take only free text,
        # so a button pressed mid-write-up lands here rather than being read as
        # a product name or a price.
        fallbacks=[
            MessageHandler(_exact(texts.BTN_CO_ABANDON), closeout.cancel),
            CallbackQueryHandler(closeout.cancel, pattern=f"^{keyboards.CB_CANCEL}$"),
            CommandHandler("cancel", closeout.cancel),
            CommandHandler("start", closeout.escape(shift.start)),
            MessageHandler(_exact(texts.BTN_STOCK), closeout.escape(stock.show)),
            MessageHandler(_exact(texts.BTN_STATUS), closeout.escape(shift.status)),
            MessageHandler(_exact(texts.BTN_OPEN), closeout.escape(shift.ask_location)),
            MessageHandler(
                _exact(texts.BTN_SEND_LOCATION),
                closeout.escape(common.location_from_desktop),
            ),
            MessageHandler(filters.LOCATION, closeout.escape(shift.handle_location)),
        ],
        # Long: a cashier writing up a busy day gets interrupted by customers.
        conversation_timeout=1800,
        per_message=False,
    )

    application.add_handler(CommandHandler("start", shift.start))
    application.add_handler(CommandHandler("help", common.help_command))
    application.add_handler(closeout_flow)

    application.add_handler(MessageHandler(_exact(texts.BTN_OPEN), shift.ask_location))
    application.add_handler(MessageHandler(filters.LOCATION, shift.handle_location))
    # The label arriving as text means the tap produced no location: Telegram
    # Desktop cannot share one. Say so, instead of letting it fall through.
    application.add_handler(
        MessageHandler(_exact(texts.BTN_SEND_LOCATION), common.location_from_desktop)
    )
    application.add_handler(MessageHandler(_exact(texts.BTN_STOCK), stock.show))
    application.add_handler(MessageHandler(_exact(texts.BTN_STATUS), shift.status))
    # Dismissing a confirmation, which happens outside any conversation.
    application.add_handler(
        CallbackQueryHandler(common.dismiss, pattern=f"^{keyboards.CB_DISMISS}$")
    )

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
