"""Turning form strings into the types the database wants.

Every parser raises ``AppError('validation_error', <Armenian sentence>)`` on bad
input, so a route never has to write its own error handling for a stray comma.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation

from app.errors import AppError

MAX_MONEY = Decimal("9999999999.99")


def text(value: str | None, field: str, *, max_length: int, required: bool = True) -> str | None:
    cleaned = (value or "").strip()
    if not cleaned:
        if required:
            raise AppError("validation_error", f"«{field}» դաշտը պարտադիր է։")
        return None
    if len(cleaned) > max_length:
        raise AppError(
            "validation_error", f"«{field}» դաշտը չպետք է գերազանցի {max_length} նիշը։"
        )
    return cleaned


def money(value: str | None, field: str, *, default: Decimal | None = None) -> Decimal:
    raw = (value or "").strip().replace(",", ".").replace(" ", "")
    if not raw:
        if default is not None:
            return default
        raise AppError("validation_error", f"«{field}» դաշտը պարտադիր է։")
    try:
        amount = Decimal(raw).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        raise AppError("validation_error", f"«{field}» դաշտը թիվ չէ։") from None
    if amount < 0:
        raise AppError("validation_error", f"«{field}» դաշտը չի կարող բացասական լինել։")
    if amount > MAX_MONEY:
        raise AppError("validation_error", f"«{field}» դաշտը չափազանց մեծ է։")
    return amount


def optional_money(value: str | None, field: str) -> Decimal | None:
    """A price that may legitimately be left blank.

    The owner's price sheet has a dash where a model is not sold wholesale, and
    "we do not sell this one wholesale" is a real answer. Storing 0 for it would
    read as "free".
    """
    if not (value or "").strip():
        return None
    return money(value, field)


def whole(
    value: str | None,
    field: str,
    *,
    default: int | None = None,
    minimum: int = 0,
    maximum: int = 1_000_000,
) -> int:
    raw = (value or "").strip()
    if not raw:
        if default is not None:
            return default
        raise AppError("validation_error", f"«{field}» դաշտը պարտադիր է։")
    try:
        number = int(raw)
    except ValueError:
        raise AppError("validation_error", f"«{field}» դաշտը ամբողջ թիվ չէ։") from None
    if not (minimum <= number <= maximum):
        raise AppError(
            "validation_error", f"«{field}» դաշտը պետք է լինի {minimum}-ից {maximum}։"
        )
    return number


def optional_id(value: str | None) -> int | None:
    """A row id from a filter dropdown whose "all" option has no value.

    Declaring such a query parameter as ``int | None`` looks right and is not: a
    ``<select>`` whose blank option is chosen still submits ``?store_id=``, and an
    empty string is not an integer, so the whole page answers 422 — which is what
    the owner saw when they changed the period with «Բոլորը» selected. Blank means
    "no filter", so it is parsed here rather than being rejected.

    A non-numeric value is also read as no filter. There is nothing to tell the
    owner about a URL they did not type, and refusing it only hides the page.
    """
    raw = (value or "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def day(value: str | None, field: str) -> date:
    """A ``YYYY-MM-DD`` from a date input, defaulting to today when blank."""
    raw = (value or "").strip()
    if not raw:
        from app.config import settings

        return settings.local_day()
    try:
        return date.fromisoformat(raw)
    except ValueError:
        raise AppError("validation_error", f"«{field}» դաշտը ամսաթիվ չէ։") from None


def coordinate(value: str | None, field: str, *, limit: float) -> float | None:
    raw = (value or "").strip().replace(",", ".")
    if not raw:
        return None
    try:
        number = float(raw)
    except ValueError:
        raise AppError("validation_error", f"«{field}» դաշտը թիվ չէ։") from None
    if not (-limit <= number <= limit):
        raise AppError(
            "validation_error", f"«{field}» դաշտը պետք է լինի -{limit}-ից {limit}։"
        )
    return number


def coordinate_pair(lat_raw: str | None, lng_raw: str | None) -> tuple[float | None, float | None]:
    """Latitude and longitude arrive and leave together — half a coordinate is
    not a location, and the schema refuses one anyway."""
    lat = coordinate(lat_raw, "Լայնություն", limit=90.0)
    lng = coordinate(lng_raw, "Երկայնություն", limit=180.0)
    if (lat is None) != (lng is None):
        raise AppError(
            "validation_error",
            "Լայնությունը և երկայնությունը պետք է լրացվեն երկուսը միասին։",
        )
    return lat, lng
