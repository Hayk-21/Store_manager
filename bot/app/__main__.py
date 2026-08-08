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
from app.handlers import (
    cash,
    closeout,
    common,
    defect,
    newitem,
    restock,
    sell,
    shift,
    stock,
    transfer,
)

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

# A live location arrives once as a message and then as a stream of *edits* to
# that same message, one every few seconds while the worker moves. The two mean
# completely different things -- "open my shift" and "I am still here" -- and
# PTB applies no update-type restriction of its own, so a plain
# ``filters.LOCATION`` would hand every edit to the open-a-shift handler and
# answer "you are already on shift" all afternoon. They are told apart here.
_shared_location = filters.LOCATION & ~filters.UpdateType.EDITED_MESSAGE
_moved = filters.LOCATION & filters.UpdateType.EDITED_MESSAGE


def build() -> Application:
    application = ApplicationBuilder().token(settings.bot_token).post_shutdown(_shutdown).build()

    # Writing the day up. A cashier does not touch the bot while serving; they
    # list what went out once, at the end, and that list is the only way a sale
    # reaches the system.
    closeout_flow = ConversationHandler(
        entry_points=[
            MessageHandler(_exact(texts.BTN_END_SHIFT), closeout.begin),
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
                # The tickbox first, and it stays on this step: it redraws the
                # keyboard and adds nothing to the list.
                CallbackQueryHandler(
                    closeout.toggle_delivery, pattern=f"^{keyboards.CB_DELIVERY}$"
                ),
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
            MessageHandler(_exact(texts.BTN_SELL), closeout.escape(sell.begin)),
            MessageHandler(_exact(texts.BTN_DEFECT), closeout.escape(defect.begin)),
            MessageHandler(_exact(texts.BTN_TAKE_CASH), closeout.escape(cash.begin)),
            MessageHandler(_exact(texts.BTN_STOCK), closeout.escape(stock.show)),
            MessageHandler(_exact(texts.BTN_STATUS), closeout.escape(shift.status)),
            MessageHandler(_exact(texts.BTN_OPEN), closeout.escape(shift.ask_location)),
            MessageHandler(
                _exact(texts.BTN_SEND_LOCATION),
                closeout.escape(common.location_from_desktop),
            ),
            MessageHandler(_shared_location, closeout.escape(shift.handle_location)),
        ],
        # Long: a cashier writing up a busy day gets interrupted by customers.
        conversation_timeout=1800,
        per_message=False,
    )

    # Group -1, ahead of everything: a live location moving is not a thing the
    # worker did, and it must not disturb whatever they are doing. Handled
    # first, then stopped, so the write-up conversation never sees it and never
    # treats it as a way out of the flow.
    application.add_handler(MessageHandler(_moved, shift.handle_live_update), group=-1)

    # Selling as it happens. Registered before the write-up so that while a sale
    # is being entered its states get the text first; each flow's fallbacks hand
    # over cleanly if the cashier taps the other one's button mid-way.
    sell_flow = ConversationHandler(
        entry_points=[MessageHandler(_exact(texts.BTN_SELL), sell.begin)],
        states={
            sell.PICK_ITEM: [
                CallbackQueryHandler(sell.choose_item, pattern=f"^{keyboards.CB_ITEM}:"),
                MessageHandler(_free_text, sell.search),
            ],
            sell.ASK_QUANTITY: [MessageHandler(_free_text, sell.choose_quantity)],
            sell.ASK_PRICE: [
                CallbackQueryHandler(
                    sell.choose_suggested_price, pattern=f"^{keyboards.CB_KIND}:"
                ),
                MessageHandler(_free_text, sell.type_price),
            ],
            sell.ASK_METHOD: [
                # The tickbox first, and it stays on this step: it redraws the
                # keyboard and sends nothing. Cash or card is the commit.
                CallbackQueryHandler(
                    sell.toggle_delivery, pattern=f"^{keyboards.CB_DELIVERY}$"
                ),
                CallbackQueryHandler(sell.choose_method, pattern=f"^{keyboards.CB_PAY}:"),
            ],
        },
        fallbacks=[
            MessageHandler(_exact(texts.BTN_CANCEL), sell.cancel),
            CallbackQueryHandler(sell.cancel, pattern=f"^{keyboards.CB_CANCEL}$"),
            CommandHandler("cancel", sell.cancel),
            CommandHandler("start", sell.escape(shift.start)),
            # Ending the shift mid-sale: drop the sale rather than leave this
            # conversation holding a state that would swallow the write-up's
            # next answer.
            MessageHandler(_exact(texts.BTN_END_SHIFT), sell.interrupted),
            MessageHandler(_exact(texts.BTN_DEFECT), sell.escape(defect.begin)),
            MessageHandler(_exact(texts.BTN_TAKE_CASH), sell.escape(cash.begin)),
            MessageHandler(_exact(texts.BTN_STOCK), sell.escape(stock.show)),
            MessageHandler(_exact(texts.BTN_STATUS), sell.escape(shift.status)),
            MessageHandler(_exact(texts.BTN_OPEN), sell.escape(shift.ask_location)),
            MessageHandler(_shared_location, sell.escape(shift.handle_location)),
        ],
        # Shorter than the write-up's: one sale is entered while the customer is
        # standing there, so an abandoned flow is abandoned.
        conversation_timeout=600,
        per_message=False,
    )

    # Writing off breakage. Its own flow rather than a sale of zero: a damaged
    # vape in the revenue figures would also count towards a worker's bonus,
    # which would pay somebody for breaking things.
    defect_flow = ConversationHandler(
        entry_points=[MessageHandler(_exact(texts.BTN_DEFECT), defect.begin)],
        states={
            defect.PICK_ITEM: [
                CallbackQueryHandler(defect.choose_item, pattern=f"^{keyboards.CB_ITEM}:"),
                MessageHandler(_free_text, defect.search),
            ],
            defect.ASK_QUANTITY: [MessageHandler(_free_text, defect.choose_quantity)],
        },
        fallbacks=[
            MessageHandler(_exact(texts.BTN_CANCEL), defect.cancel),
            CallbackQueryHandler(defect.cancel, pattern=f"^{keyboards.CB_CANCEL}$"),
            CommandHandler("cancel", defect.cancel),
            CommandHandler("start", defect.escape(shift.start)),
            MessageHandler(_exact(texts.BTN_SELL), defect.escape(sell.begin)),
            MessageHandler(_exact(texts.BTN_TAKE_CASH), defect.escape(cash.begin)),
            MessageHandler(_exact(texts.BTN_STOCK), defect.escape(stock.show)),
            MessageHandler(_exact(texts.BTN_STATUS), defect.escape(shift.status)),
            MessageHandler(_exact(texts.BTN_END_SHIFT), defect.cancel),
        ],
        conversation_timeout=600,
        per_message=False,
    )

    # Taking cash out. Two steps, both free text, so it needs the same escapes
    # as the others: a button pressed mid-way must not be read as an amount.
    cash_flow = ConversationHandler(
        entry_points=[MessageHandler(_exact(texts.BTN_TAKE_CASH), cash.begin)],
        states={
            cash.ASK_AMOUNT: [MessageHandler(_free_text, cash.type_amount)],
            cash.ASK_PURPOSE: [MessageHandler(_free_text, cash.type_purpose)],
        },
        fallbacks=[
            MessageHandler(_exact(texts.BTN_CANCEL), cash.cancel),
            CommandHandler("cancel", cash.cancel),
            CommandHandler("start", cash.escape(shift.start)),
            MessageHandler(_exact(texts.BTN_SELL), cash.escape(sell.begin)),
            MessageHandler(_exact(texts.BTN_DEFECT), cash.escape(defect.begin)),
            MessageHandler(_exact(texts.BTN_STOCK), cash.escape(stock.show)),
            MessageHandler(_exact(texts.BTN_STATUS), cash.escape(shift.status)),
            MessageHandler(_exact(texts.BTN_END_SHIFT), cash.cancel),
        ],
        conversation_timeout=600,
        per_message=False,
    )

    application.add_handler(CommandHandler("start", shift.start))
    application.add_handler(CommandHandler("help", common.help_command))
    application.add_handler(sell_flow)
    application.add_handler(defect_flow)
    application.add_handler(cash_flow)

    # Correcting the shelf: the whole list with a − and a + against each product,
    # nothing written until "confirm". Registered before newitem_flow, because the
    # «Բոլորովին նոր ապրանք» button on its screen is that flow's entry point and
    # this one must not swallow the tap.
    restock_flow = ConversationHandler(
        entry_points=[MessageHandler(_exact(texts.BTN_ADD_ITEM), restock.begin)],
        states={
            restock.PICK: [
                CallbackQueryHandler(restock.nudge, pattern=f"^{keyboards.CB_NUDGE}:"),
                CallbackQueryHandler(restock.turn_page, pattern=f"^{keyboards.CB_PAGE}:"),
                CallbackQueryHandler(restock.submit, pattern=f"^{keyboards.CB_APPLY}$"),
                # The count in the middle of each row is a button because Telegram
                # has no other way to put text there. Answered and ignored, so it
                # does not fall through to something else.
                CallbackQueryHandler(restock.noop, pattern=f"^{keyboards.CB_NOOP}$"),
            ],
        },
        fallbacks=[
            MessageHandler(_exact(texts.BTN_CANCEL), restock.cancel),
            CallbackQueryHandler(restock.cancel, pattern=f"^{keyboards.CB_CANCEL}$"),
            CommandHandler("cancel", restock.cancel),
            CommandHandler("start", restock.escape(shift.start)),
            MessageHandler(_exact(texts.BTN_SELL), restock.escape(sell.begin)),
            MessageHandler(_exact(texts.BTN_DEFECT), restock.escape(defect.begin)),
            MessageHandler(_exact(texts.BTN_TAKE_CASH), restock.escape(cash.begin)),
            MessageHandler(_exact(texts.BTN_STOCK), restock.escape(stock.show)),
            MessageHandler(_exact(texts.BTN_STATUS), restock.escape(shift.status)),
            MessageHandler(_exact(texts.BTN_END_SHIFT), restock.cancel),
        ],
        conversation_timeout=900,
        per_message=False,
    )
    application.add_handler(restock_flow)

    # Asking another shop for stock. The item list belongs to the *other* shop, so
    # PICK_ITEM takes free text for searching it — hence the usual escapes.
    transfer_flow = ConversationHandler(
        entry_points=[MessageHandler(_exact(texts.BTN_TRANSFERS), transfer.show)],
        states={
            transfer.PICK_STORE: [
                CallbackQueryHandler(
                    transfer.begin_request, pattern=f"^{keyboards.CB_TRANSFER_NEW}$"
                ),
                CallbackQueryHandler(
                    transfer.choose_store, pattern=f"^{keyboards.CB_TRANSFER_STORE}:"
                ),
                CallbackQueryHandler(transfer.noop, pattern=f"^{keyboards.CB_NOOP}$"),
            ],
            transfer.PICK_ITEM: [
                CallbackQueryHandler(transfer.choose_item, pattern=f"^{keyboards.CB_ITEM}:"),
                MessageHandler(_free_text, transfer.search_item),
            ],
            transfer.ASK_QUANTITY: [
                MessageHandler(_free_text, transfer.choose_quantity)
            ],
        },
        fallbacks=[
            MessageHandler(_exact(texts.BTN_CANCEL), transfer.cancel),
            CallbackQueryHandler(transfer.cancel, pattern=f"^{keyboards.CB_CANCEL}$"),
            CommandHandler("cancel", transfer.cancel),
            CommandHandler("start", transfer.escape(shift.start)),
            MessageHandler(_exact(texts.BTN_SELL), transfer.escape(sell.begin)),
            MessageHandler(_exact(texts.BTN_DEFECT), transfer.escape(defect.begin)),
            MessageHandler(_exact(texts.BTN_TAKE_CASH), transfer.escape(cash.begin)),
            MessageHandler(_exact(texts.BTN_ADD_ITEM), transfer.escape(restock.begin)),
            MessageHandler(_exact(texts.BTN_STOCK), transfer.escape(stock.show)),
            MessageHandler(_exact(texts.BTN_STATUS), transfer.escape(shift.status)),
            MessageHandler(_exact(texts.BTN_END_SHIFT), transfer.cancel),
        ],
        conversation_timeout=900,
        per_message=False,
    )
    application.add_handler(transfer_flow)

    # Adding a product at the counter. Five short answers, all free text, so it
    # needs the same escapes: a button pressed mid-way must not become a name.
    newitem_flow = ConversationHandler(
        entry_points=[
            # Reached from the correction screen: the cashier looked for the
            # product, it was not there, and this is the other door.
            CallbackQueryHandler(newitem.begin, pattern=f"^{keyboards.CB_NEW_ITEM}$"),
        ],
        states={
            newitem.ASK_NAME: [MessageHandler(_free_text, newitem.type_name)],
            newitem.ASK_COUNT: [MessageHandler(_free_text, newitem.type_count)],
            newitem.ASK_COST: [MessageHandler(_free_text, newitem.type_cost)],
            newitem.ASK_PRICE: [MessageHandler(_free_text, newitem.type_price)],
            newitem.ASK_WHOLESALE: [
                # "Not sold wholesale" is an answer, so there is a button for it.
                CallbackQueryHandler(
                    newitem.skip_wholesale, pattern=f"^{keyboards.CB_SKIP}$"
                ),
                MessageHandler(_free_text, newitem.type_wholesale),
            ],
        },
        fallbacks=[
            MessageHandler(_exact(texts.BTN_CANCEL), newitem.cancel),
            CallbackQueryHandler(newitem.cancel, pattern=f"^{keyboards.CB_CANCEL}$"),
            CommandHandler("cancel", newitem.cancel),
            CommandHandler("start", newitem.escape(shift.start)),
            MessageHandler(_exact(texts.BTN_SELL), newitem.escape(sell.begin)),
            MessageHandler(_exact(texts.BTN_DEFECT), newitem.escape(defect.begin)),
            MessageHandler(_exact(texts.BTN_TAKE_CASH), newitem.escape(cash.begin)),
            MessageHandler(_exact(texts.BTN_STOCK), newitem.escape(stock.show)),
            MessageHandler(_exact(texts.BTN_STATUS), newitem.escape(shift.status)),
            MessageHandler(_exact(texts.BTN_END_SHIFT), newitem.cancel),
        ],
        conversation_timeout=600,
        per_message=False,
    )
    application.add_handler(newitem_flow)
    application.add_handler(closeout_flow)

    # Undoing a sale is not part of entering one: the receipt is finished, and
    # the button may be tapped long after the flow ended.
    application.add_handler(
        CallbackQueryHandler(sell.undo, pattern=f"^{keyboards.CB_UNDO}:")
    )
    application.add_handler(MessageHandler(_exact(texts.BTN_DEFECT), defect.begin))
    application.add_handler(MessageHandler(_exact(texts.BTN_TAKE_CASH), cash.begin))
    application.add_handler(MessageHandler(_exact(texts.BTN_ADD_ITEM), restock.begin))
    application.add_handler(MessageHandler(_exact(texts.BTN_TRANSFERS), transfer.show))
    # Approving or rejecting a request, outside any conversation: the buttons sit on
    # a message that may well be tapped an hour after it arrived.
    application.add_handler(
        CallbackQueryHandler(transfer.decide, pattern=f"^{keyboards.CB_TRANSFER}:")
    )

    application.add_handler(MessageHandler(_exact(texts.BTN_OPEN), shift.ask_location))
    application.add_handler(MessageHandler(_shared_location, shift.handle_location))
    # The label arriving as text means the tap produced no location: Telegram
    # Desktop cannot share one. Say so, instead of letting it fall through.
    application.add_handler(
        MessageHandler(_exact(texts.BTN_SEND_LOCATION), common.location_from_desktop)
    )
    application.add_handler(MessageHandler(_exact(texts.BTN_SELL), sell.begin))
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
    # httpx logs every request at INFO, and the URL carries the bot token.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    # APScheduler narrates the conversation-timeout job it arms and disarms on
    # every single message. Two lines per tap drowns the log: a real traceback
    # sits between hundreds of "Added job"/"Removed job" pairs, which is exactly
    # how a crash in the sell flow went unnoticed until a person reported it.
    logging.getLogger("apscheduler").setLevel(logging.WARNING)
    log.info("starting bot against %s", settings.api_base_url)
    build().run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
