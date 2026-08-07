"""What a line costs, and why.

One function, in its own module, because two very different callers must agree
on it: the cashier picking «Մեծածախ» in Telegram and the owner picking the same
word in a form on the website. If either resolved it independently the same
choice could book a different amount.
"""

from __future__ import annotations

from decimal import Decimal

from app.errors import AppError

CENT = Decimal("0.01")

KINDS = ("retail", "wholesale", "custom")


def resolve_price(item, typed, kind: str | None) -> tuple[Decimal, str]:
    """Turn a price list choice, or a typed number, into an amount and a reason.

    A typed price wins over the kind — somebody who wrote 3200 meant 3200 — but
    it is only recorded as 'custom' when it actually differs from the listed
    price it was offered as. Otherwise choosing "wholesale" and leaving the
    prefilled number untouched would be filed as a haggle, and every wholesale
    figure in the reports would read zero.

    ``item`` needs ``name``, ``sell_price`` and ``wholesale_price``.
    """
    kind = kind if kind in KINDS else "retail"

    listed = Decimal(item["sell_price"])
    if kind == "wholesale":
        if item["wholesale_price"] is None:
            # Refusing beats quietly charging retail: the cashier asked for a
            # price the owner never set, and only the owner can fix that.
            raise AppError(
                "validation_error",
                f"«{item['name']}»-ի մեծածախ գինը նշված չէ։ "
                f"Նշեք այն ապրանքի էջում կամ գրեք գինը ձեռքով։",
            )
        listed = Decimal(item["wholesale_price"])

    if typed is None or typed == "":
        return listed, kind

    price = Decimal(typed)
    return (price, kind) if price == listed else (price, "custom")
