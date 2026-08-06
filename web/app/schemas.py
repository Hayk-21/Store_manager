"""Request bodies for the bot API.

``extra="forbid"`` throughout: a typo in a field name should be a loud 422, not
a silently ignored value. Money arrives as a decimal string and is parsed to
Decimal — a float would have already lost precision by the time we saw it.
"""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class BotRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    telegram_id: int = Field(gt=0)


class IdempotentRequest(BotRequest):
    # Required, not optional. It is the only thing standing between a flaky
    # mobile connection and a double sale, so there is no "best effort" mode.
    idempotency_key: str = Field(min_length=8, max_length=128)


class LocationRequest(BotRequest):
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)
    accuracy_m: float | None = Field(default=None, ge=0)


class CheckinRequest(LocationRequest):
    """Read-only probe: tells the worker which store they are near, writes nothing."""


class OpenStoreRequest(LocationRequest):
    idempotency_key: str = Field(min_length=8, max_length=128)


class EndShiftRequest(IdempotentRequest):
    lat: float | None = Field(default=None, ge=-90, le=90)
    lng: float | None = Field(default=None, ge=-180, le=180)


class CloseStoreRequest(IdempotentRequest):
    pass


class SaleLine(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item_id: int = Field(gt=0)
    quantity: int = Field(gt=0, le=10_000)
    # Optional per-line override, for a discount. Absent means the shelf price.
    unit_price: Decimal | None = Field(default=None, ge=0)

    @field_validator("unit_price", mode="before")
    @classmethod
    def _string_money(cls, value):
        """Accept "3500.00" but refuse 3500.0 — a float has already lost digits."""
        if isinstance(value, float):
            raise ValueError("send money as a decimal string, not a float")
        return value


class SaleRequest(IdempotentRequest):
    items: list[SaleLine] = Field(min_length=1, max_length=50)
    payment_method: str

    @field_validator("payment_method")
    @classmethod
    def _known_method(cls, value: str) -> str:
        if value not in {"cash", "card"}:
            raise ValueError("payment_method must be 'cash' or 'card'")
        return value


class VoidRequest(BotRequest):
    reason: str | None = Field(default=None, max_length=300)
