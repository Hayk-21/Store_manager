"""The Jinja environment and the filters templates rely on.

All formatting lives here so a number is rendered the same way on every page.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.templating import Jinja2Templates

from app.config import settings

TEMPLATES_DIR = Path(__file__).parent / "templates"


def money(value: Any) -> str:
    """``1234567.5`` -> ``1,234,567.50 ֏``.

    Accepts Decimal, int, str or None. Never floats out of the database — asyncpg
    returns numeric as Decimal — but a str is accepted because the bot API passes
    money around as decimal strings.
    """
    if value is None:
        return f"0.00 {settings.currency}"
    amount = value if isinstance(value, Decimal) else Decimal(str(value))
    return f"{amount:,.2f} {settings.currency}"


def plain_money(value: Any) -> str:
    """The same number without the currency mark, for input fields."""
    if value is None:
        return "0.00"
    amount = value if isinstance(value, Decimal) else Decimal(str(value))
    return f"{amount:.2f}"


def clock(value: datetime | None) -> str:
    """``HH:MM`` in the display timezone."""
    if value is None:
        return "—"
    return value.astimezone(settings.timezone).strftime("%H:%M")


def stamp(value: datetime | None) -> str:
    """``DD.MM.YYYY HH:MM`` in the display timezone."""
    if value is None:
        return "—"
    return value.astimezone(settings.timezone).strftime("%d.%m.%Y %H:%M")


def day(value: Any) -> str:
    if value is None:
        return "—"
    return value.strftime("%d.%m.%Y")


def duration(start: datetime | None, end: datetime | None = None) -> str:
    """``12ժ 30ր`` — hours and minutes, Armenian abbreviations."""
    if start is None:
        return "—"
    finish = end or settings.now()
    minutes = int((finish - start).total_seconds() // 60)
    if minutes < 0:
        minutes = 0
    hours, mins = divmod(minutes, 60)
    return f"{hours}ժ {mins:02d}ր" if hours else f"{mins}ր"


templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
templates.env.filters["money"] = money
templates.env.filters["plain_money"] = plain_money
templates.env.filters["clock"] = clock
templates.env.filters["stamp"] = stamp
templates.env.filters["day"] = day
templates.env.filters["duration"] = duration
templates.env.globals["currency"] = settings.currency


def render(request: Request, name: str, context: dict[str, Any] | None = None, **kwargs: Any):
    """Render a template with the bits every page needs already in scope."""
    ctx: dict[str, Any] = {"request": request}
    ctx.update(context or {})
    return templates.TemplateResponse(request, name, ctx, **kwargs)
