"""Formatting helpers shared by the handlers."""

from __future__ import annotations

import html
from datetime import datetime
from decimal import Decimal

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
