"""Formatting helpers shared by the handlers."""

from __future__ import annotations

import html
from datetime import datetime
from decimal import Decimal, InvalidOperation

CURRENCY = "֏"


def esc(value) -> str:
    """Make a name safe to drop into an HTML message.

    Telegram parses these messages as HTML, and it *rejects the whole message*
    on a stray entity rather than showing it literally. An item called
    "Blue Razz & Ice" — an entirely ordinary name in this shop — would make the
    send fail and the sale appear to hang.
    """
    return html.escape(str(value if value is not None else ""), quote=False)


def money(value) -> str:
    """The server sends decimal strings; render them the same way the website does."""
    if value is None:
        return f"0 {CURRENCY}"
    amount = value if isinstance(value, Decimal) else Decimal(str(value))
    # No decimal places: dram has no subunit in practice, and a cashier reading
    # a phone screen mid-transaction does not need two more digits.
    return f"{amount:,.0f} {CURRENCY}"


def parse_money(raw: str | None) -> Decimal | None:
    """A typed amount, accepting either grouping convention — and never guessing
    wrong between them.

    A worker sees amounts the bot itself prints as "40,000 ֏" — comma-thousands, no
    decimals, from ``money()`` below — and reasonably types them back the same way.
    But every prompt that asks for a number was built for the Armenian convention,
    space-thousands and comma-decimal ("1 000,00"), from before the bot echoed
    amounts back at all. A bare ``.replace(",", ".")`` cannot honor both: it turned
    a worker's "40,000" into 40.00 — forty dram typed as forty thousand — and never
    raised an error, because the result was still a valid number.

    The two conventions agree on everything except what a comma means, and that is
    resolved by what follows it: a comma immediately followed by exactly three
    digits, with digits before it, is a thousands separator — the shape ``money()``
    prints and the shape on a price tag. Any other comma is a decimal point. That
    split is safe here specifically because a fractional dram is never a real
    amount, so no comma is legitimately followed by three decimal places.
    """
    if raw is None:
        return None
    text = raw.strip().replace(" ", " ").replace(" ", "")
    if not text:
        return None
    if "," in text and "." in text:
        # Both present: whichever comes last is the decimal point.
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    elif "," in text:
        head, _, tail = text.rpartition(",")
        if len(tail) == 3 and head and any(ch.isdigit() for ch in head):
            text = text.replace(",", "")
        else:
            text = text.replace(",", ".")
    try:
        return Decimal(text).quantize(Decimal("0.01"))
    except InvalidOperation:
        return None


def clock(iso: str | None) -> str:
    if not iso:
        return "—"
    return datetime.fromisoformat(iso).strftime("%H:%M")


def duration_since(iso: str | None, until: str | None = None) -> str:
    if not iso:
        return "—"
    start = datetime.fromisoformat(iso)
    end = datetime.fromisoformat(until) if until else datetime.now(start.tzinfo)
    minutes = max(0, int((end - start).total_seconds() // 60))
    hours, mins = divmod(minutes, 60)
    return f"{hours}ժ {mins:02d}ր" if hours else f"{mins}ր"


def duration_minutes(total: int | None) -> str:
    if total is None:
        return "—"
    hours, mins = divmod(max(0, total), 60)
    return f"{hours}ժ {mins:02d}ր" if hours else f"{mins}ր"


def sold_summary(sales: dict) -> str:
    """"12,000 ֏ (կանխիկ 8,000 · քարտ 4,000)"."""
    total = money(sales["total"])
    cash, card = money(sales["cash_total"]), money(sales["card_total"])
    return f"{total} (կանխիկ {cash} · քարտ {card})"
