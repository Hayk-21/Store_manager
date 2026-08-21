"""Server-owned, display-ready Armenian wording.

The bot prints ``error.message`` verbatim rather than translating a code, so the
wording lives here and cannot drift between the two services. The cost is that
changing a sentence is a web deploy; the benefit is that the bot can show a
sensible message for a code it has never heard of.
"""

from __future__ import annotations

ERROR_MESSAGES: dict[str, str] = {
    # generic
    "unauthorized": "Հարցումը չհաստատվեց։",
    "forbidden": "Գործողությունն արգելված է։",
    "not_found": "Չի գտնվել։",
    "validation_error": "Ուղարկված տվյալները սխալ են։",
    "internal": "Համակարգային սխալ։ Փորձեք մի փոքր ուշ։",
    # who is asking
    "unknown_worker": "Դուք գրանցված չեք համակարգում։ Դիմեք ղեկավարին։",
    "worker_inactive": "Ձեր հաշիվն ապաակտիվացված է։ Դիմեք ղեկավարին։",
    # where they are
    "no_store_in_range": "Դուք ոչ մի խանութի տարածքում չեք։ Մոտեցեք խանութին և կրկին փորձեք։",
    "no_stores_located": "Ոչ մի խանութի կոորդինատները նշված չեն։ Դիմեք ղեկավարին։",
    "location_too_vague": (
        "Տեղորոշումը բավականաչափ ճշգրիտ չէ։ Դուրս եկեք բաց տարածք և կրկին ուղարկեք։"
    ),
    # Deliberately a set of instructions rather than a complaint: "live location"
    # is a menu item most people have never used, and being told no without
    # being told how is a dead end.
    "location_not_live": (
        "Անհրաժեշտ է կենդանի տեղորոշում (Live Location)։\n\n"
        "Հեռախոսում՝ 📎 → «Location» → «Share My Live Location» → "
        "ընտրեք 15 րոպե և ուղարկեք։\n\n"
        "Սովորական տեղորոշումը կարելի է ձեռքով նշել քարտեզին, դրա համար այն "
        "չի ընդունվում։ Համակարգչի Telegram-ը կենդանի տեղորոշում ուղարկել չի կարող։"
    ),
    # shift state
    "session_already_open": "Դուք արդեն հերթափոխի մեջ եք։",
    "no_open_session": "Դուք հերթափոխի մեջ չեք։ Նախ բացեք խանութը։",
    "store_not_open": "Խանութը բաց չէ։",
    # The drawer is the shop's, not a shift's. Counting it while the shop is still
    # trading hands the owner the change the next few hours need, and the count is
    # stale on the very next sale.
    "store_still_open": (
        "Խանութը դեռ բաց է։ Դրամարկղը հաշվում է վերջինը դուրս եկողը՝ "
        "հերթափոխն ավարտելուց հետո։"
    ),
    # The refusal that makes the count part of closing rather than a favour asked
    # afterwards. Written as an instruction, because the bot answering it is the one
    # that asks for the figure — and a bot too old to know the code prints this
    # sentence verbatim, so it has to make sense on its own.
    "till_count_required": (
        "Հերթափոխը դեռ չի փակվել։ Նախ հաշվեք դրամարկղը և գրեք, "
        "թե որքան կանխիկ եք թողնում խանութում։"
    ),
    # Always overridden with the names of whoever is still working, but a
    # sensible sentence has to exist for the code on its own.
    "others_on_shift": (
        "Խանութը փակել չի կարելի՝ ուրիշ աշխատողներ դեռ հերթափոխի մեջ են։ "
        "Ավարտեք ձեր հերթափոխը, իսկ խանութը կփակի վերջինը դուրս եկողը։"
    ),
    # selling
    "unknown_item": "Այդպիսի ապրանք այս խանութում չկա։",
    "insufficient_stock": "Պահեստում բավարար քանակ չկա։",
    "empty_basket": "Ապրանք ընտրված չէ։",
    "nothing_to_void": "Չեղարկելու վաճառք չկա։",
}


# -- pushed to a worker's chat ------------------------------------------------

# A shop asking this shop for stock. Sent to whoever is on shift there, because a
# request nobody knows about is another shop waiting for a box that is not coming.
# No buttons: the message is a prompt, and the answering is done under
# «Փոխանցումներ» in the bot, which is also where it can be found later.
TRANSFER_REQUESTED = (
    "📦 <b>Ապրանքի հարցում</b>\n\n"
    "«{store}» խանութը խնդրում է <b>{quantity}</b> հատ «{item}»։\n\n"
    "Բոտում սեղմեք «🔄 Փոխանցումներ»՝ հաստատելու կամ մերժելու համար։"
)


def insufficient_stock_message(name: str, requested: int, available: int) -> str:
    return f"«{name}» — պահեստում կա ընդամենը {available} հատ, խնդրված է {requested}։"


def till_over_cash_message(available) -> str:
    """The drawer cannot be left with more than it holds.

    Sent when a worker's closing count is above the cash the books say is in the
    till. What they leave becomes the shop's float and the rest is what the owner is
    owed, so a figure above the till reads as "the owner gets nothing tonight" and
    quietly carries the difference into tomorrow's opening balance.

    The ceiling is stated as a number, because the answer to "how much may I write"
    is a number. What it is made of is described in words rather than added up term
    by term: a wage or a withdrawal can be booked against the card side of the same
    session, so a printed sum would not always tie out to the ceiling beside it.
    """
    return (
        f"Այդքան կանխիկ դրամարկղում չկա։ Ըստ գրքերի դրամարկղում կա "
        f"{available:,.0f} ֏՝ նախորդ հերթափոխից մնացածը գումարած օրվա կանխիկ "
        f"վաճառքը, հանած աշխատավարձը և դրամարկղից վերցվածը։\n\n"
        f"Գրեք {available:,.0f} ֏ կամ ավելի քիչ։ Եթե դրամարկղում իրականում ավելի "
        f"շատ կանխիկ կա, զանգահարեք ղեկավարին։"
    )


def no_store_in_range_message(nearest_name: str | None, distance_m: int | None) -> str:
    """Telling the worker how far off they are turns a dead end into an instruction."""
    if nearest_name is None or distance_m is None:
        return ERROR_MESSAGES["no_store_in_range"]
    return (
        f"Դուք «{nearest_name}»-ից {distance_m} մ հեռու եք։ "
        f"Մոտեցեք խանութին և կրկին փորձեք։"
    )
