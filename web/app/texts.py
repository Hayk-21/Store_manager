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
    # shift state
    "session_already_open": "Դուք արդեն հերթափոխի մեջ եք։",
    "no_open_session": "Դուք հերթափոխի մեջ չեք։ Նախ բացեք խանութը։",
    "store_not_open": "Խանութը բաց չէ։",
    # selling
    "unknown_item": "Այդպիսի ապրանք այս խանութում չկա։",
    "insufficient_stock": "Պահեստում բավարար քանակ չկա։",
    "empty_basket": "Ապրանք ընտրված չէ։",
    "nothing_to_void": "Չեղարկելու վաճառք չկա։",
}


def insufficient_stock_message(name: str, requested: int, available: int) -> str:
    return f"«{name}» — պահեստում կա ընդամենը {available} հատ, խնդրված է {requested}։"


def no_store_in_range_message(nearest_name: str | None, distance_m: int | None) -> str:
    """Telling the worker how far off they are turns a dead end into an instruction."""
    if nearest_name is None or distance_m is None:
        return ERROR_MESSAGES["no_store_in_range"]
    return (
        f"Դուք «{nearest_name}»-ից {distance_m} մ հեռու եք։ "
        f"Մոտեցեք խանութին և կրկին փորձեք։"
    )
