"""Request bodies for the bot API.

``extra="forbid"`` throughout: a typo in a field name should be a loud 422, not
a silently ignored value. Money arrives as a decimal string and is parsed to
Decimal — a float would have already lost precision by the time we saw it.
"""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Which price list a line came from. Kept here rather than in the database layer
# because both the bot and the website have to agree on the vocabulary.
PRICE_KINDS = {"retail", "wholesale", "custom"}


class BotRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    telegram_id: int = Field(gt=0)
    # The worker's Telegram profile name, sent on every request. It is how the
    # /workers page fills itself in: the owner registers an id, and the name
    # arrives the first time that person uses the bot. Purely informational —
    # it never affects who the caller is or what they may do.
    telegram_name: str | None = Field(default=None, max_length=200)
    # The @username, without the @. The owner registers this; the first request
    # carrying it binds the registration to the numeric id above, and from then
    # on the id is what identifies the worker.
    telegram_username: str | None = Field(default=None, max_length=32)


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
    # accuracy_m stays optional (inherited). It briefly was not: the absence of
    # accuracy looked like a reliable sign of a hand-placed pin, but plenty of
    # Telegram clients omit it on a perfectly genuine reading, and requiring it
    # locked honest workers out of their own shift. When it *is* present it is
    # still used -- a reading too vague to tell two neighbouring shops apart is
    # refused by the geofence.
    #
    # live_period is the one thing that does separate a real position from a pin
    # dropped on the map: Telegram only sets it when the device is streaming its
    # location for a chosen span. Optional in the schema and required in the
    # service, so a missing value is a refusal with a sentence the worker can act
    # on rather than a 422 full of field paths.
    live_period: int | None = Field(default=None, ge=0)


class LocationPingRequest(LocationRequest):
    """One reading from a live location that is already running.

    No idempotency key: this is a stream of observations, and recording the same
    position twice costs a duplicate row and nothing else. Anything that must not
    happen twice does not go through here.
    """


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
    # Which price list it came from, so a wholesale run recorded as it happens
    # is told apart from a haggle -- exactly as in the end-of-shift write-up.
    price_kind: str = "retail"

    @field_validator("unit_price", mode="before")
    @classmethod
    def _string_money(cls, value):
        """Accept "3500.00" but refuse 3500.0 — a float has already lost digits."""
        if isinstance(value, float):
            raise ValueError("send money as a decimal string, not a float")
        return value

    @field_validator("price_kind")
    @classmethod
    def _known_kind(cls, value: str) -> str:
        if value not in PRICE_KINDS:
            raise ValueError("unknown price_kind")
        return value


class SaleRequest(IdempotentRequest):
    items: list[SaleLine] = Field(min_length=1, max_length=50)
    payment_method: str
    # Orthogonal to the payment method: a delivery can be paid in cash at the
    # door or by card in advance. Changes no money, only what the owner can see.
    is_delivery: bool = False

    @field_validator("payment_method")
    @classmethod
    def _known_method(cls, value: str) -> str:
        if value not in {"cash", "card"}:
            raise ValueError("payment_method must be 'cash' or 'card'")
        return value


class VoidRequest(BotRequest):
    reason: str | None = Field(default=None, max_length=300)
    # Which receipt to reverse. Absent means the worker's most recent one, which
    # is what "undo that" means at the counter; the undo button under a
    # confirmation names its own sale so it cannot reverse a later one.
    sale_id: int | None = Field(default=None, gt=0)


class NewItemRequest(BotRequest):
    """A product the cashier is putting on the shelf.

    Both prices are asked for rather than defaulted: an item with no cost price
    reports its entire selling price as profit, which is worse than not having
    the item at all.
    """

    name: str = Field(min_length=1, max_length=200)
    count: int = Field(ge=0, le=1_000_000)
    self_price: Decimal = Field(ge=0)
    sell_price: Decimal = Field(ge=0)
    # Absent means "not sold wholesale", which is an answer. Zero would read as
    # free, so the bot omits the field rather than sending one.
    wholesale_price: Decimal | None = Field(default=None, ge=0)

    @field_validator("self_price", "sell_price", "wholesale_price", mode="before")
    @classmethod
    def _string_money(cls, value):
        if isinstance(value, float):
            raise ValueError("send money as a decimal string, not a float")
        return value


class AdjustLine(BaseModel):
    """One product's correction. Signed: up for stock found, down for stock gone."""

    item_id: int = Field(gt=0)
    delta: int = Field(ge=-1_000_000, le=1_000_000)


class AdjustStockRequest(IdempotentRequest):
    """A batch of stock corrections from one tap of "confirm".

    One request rather than one per product, because the cashier counted the shelf
    once. Half a batch landing would be worse than none of it: the screen would
    show numbers they believe they have already fixed.
    """

    lines: list[AdjustLine] = Field(min_length=1, max_length=100)
    note: str | None = Field(default=None, max_length=300)


class UndoAdjustRequest(IdempotentRequest):
    """Take back the batch of corrections identified by ``external_id``.

    The batch, not one product: the cashier counted the shelf once and confirmed
    once, so that tap is the thing being undone. Its own idempotency key on top,
    because the undo is itself an action a retry must not apply twice.
    """

    external_id: str = Field(min_length=8, max_length=128)


class TillCountRequest(IdempotentRequest):
    """What the worker is leaving in the drawer at the end of their shift.

    No ``kind``. Counting at the *start* of a shift was dropped: it asked a worker to
    answer for a drawer somebody else had filled, and the answer bound nobody to
    anything. One count, at the end, by the person who was there.
    """

    counted: Decimal = Field(ge=0)

    @field_validator("counted", mode="before")
    @classmethod
    def _string_money(cls, value):
        if isinstance(value, float):
            raise ValueError("send money as a decimal string, not a float")
        return value


class TransferRequest(IdempotentRequest):
    """A cashier asking another of the owner's shops for stock.

    ``item_id`` is the *source* shop's row: the cashier is pointing at a product on
    somebody else's shelf, which is why they had to be shown that shelf to pick
    from. The destination is never sent — it is the shop their shift is open in, so
    nobody can route a box to a third place.
    """

    from_store_id: int = Field(gt=0)
    item_id: int = Field(gt=0)
    quantity: int = Field(gt=0, le=100_000)


class TransferDecision(BotRequest):
    """Approving or rejecting one request, from the shop being asked."""

    approve: bool


class WithdrawRequest(IdempotentRequest):
    """A cashier taking cash out of the till, with the reason they took it."""

    amount: Decimal = Field(gt=0)
    purpose: str = Field(min_length=1, max_length=300)
    # Which of the offered reasons was picked. Optional and unvalidated on
    # purpose: an older bot sends none, a newer one could send a code this
    # service has not heard of, and neither should be a 422 at the counter. The
    # service treats anything it does not recognise as typed text under the
    # ordinary allowance — see ``money._reason``.
    reason: str | None = Field(default=None, max_length=32)

    @field_validator("amount", mode="before")
    @classmethod
    def _string_money(cls, value):
        if isinstance(value, float):
            raise ValueError("send money as a decimal string, not a float")
        return value


class WriteOffRequest(IdempotentRequest):
    """Stock that left without being sold: broken, expired, lost."""

    item_id: int = Field(gt=0)
    quantity: int = Field(gt=0, le=10_000)
    reason: str | None = Field(default=None, max_length=300)


class CloseoutLine(BaseModel):
    """One thing sold during the shift, as the cashier remembers it.

    Price and payment method are per line, not per basket: the same product can
    go out at the shelf price for cash and discounted on a card in the same day.
    """

    model_config = ConfigDict(extra="forbid")

    item_id: int = Field(gt=0)
    quantity: int = Field(gt=0, le=10_000)
    unit_price: Decimal | None = Field(default=None, ge=0)
    payment_method: str
    # Which price list this came from. The amount is already in unit_price, so
    # this records the *reason* — without it a wholesale run and a haggled
    # discount are the same row with a smaller number.
    price_kind: str = "retail"
    # Delivered rather than handed over the counter. Changes no money.
    is_delivery: bool = False

    @field_validator("unit_price", mode="before")
    @classmethod
    def _string_money(cls, value):
        if isinstance(value, float):
            raise ValueError("send money as a decimal string, not a float")
        return value

    @field_validator("payment_method")
    @classmethod
    def _known_method(cls, value: str) -> str:
        if value not in {"cash", "card"}:
            raise ValueError("payment_method must be 'cash' or 'card'")
        return value

    @field_validator("price_kind")
    @classmethod
    def _known_kind(cls, value: str) -> str:
        if value not in PRICE_KINDS:
            raise ValueError("price_kind must be one of " + ", ".join(sorted(PRICE_KINDS)))
        return value


class CloseoutRequest(IdempotentRequest):
    """End the shift and declare what was sold, in one call."""

    lines: list[CloseoutLine] = Field(default_factory=list, max_length=50)
    lat: float | None = Field(default=None, ge=-90, le=90)
    lng: float | None = Field(default=None, ge=-180, le=180)
    close_store: bool = False
    # What is being left in the drawer. Optional here and required by the service,
    # and only when this close actually shuts the shop — which is not something the
    # bot can know in advance, so it asks, is refused, asks the worker, and sends
    # the same call again with the figure.
    counted: Decimal | None = Field(default=None, ge=0)

    @field_validator("counted", mode="before")
    @classmethod
    def _string_money(cls, value):
        if isinstance(value, float):
            raise ValueError("send money as a decimal string, not a float")
        return value
