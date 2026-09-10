"""What a line costs, and why.

One function, in its own module, because two very different callers must agree
on it: the cashier picking «Մեծածախ» in Telegram and the owner picking the same
word in a form on the website. If either resolved it independently the same
choice could book a different amount.
"""

from __future__ import annotations

from decimal import Decimal

CENT = Decimal("0.01")

# 'custom' is still accepted and still read: years of lines carry it, and the
# constraint on sale_items allows it. Nothing produces it any more — see below.
KINDS = ("retail", "wholesale", "custom")


def resolve_price(item, typed, kind: str | None) -> tuple[Decimal, str]:
    """Turn a price list choice, and possibly a typed number, into an amount and a kind.

    **The kind is chosen, not inferred.** The cashier ticks «Մանրածախ» or «Մեծածախ»
    and that is what the line is; «Այլ գին» changes the number underneath it and
    nothing else. A box sold wholesale after haggling is a wholesale sale — that is
    what the shop did — and the amount it went for lives in ``unit_price``, which is
    where the money has always been.

    It used to work the other way: any typed amount that differed from the list price
    was filed as 'custom', which took the line out of *both* price lists. So the
    owner's «Մեծածախ» figure counted only the boxes that went at exactly the listed
    number, every negotiated one silently became a third category, and a filter with
    «մանրածախ» and «մեծածախ» on it could not add up to the takings it was split from.

    ``item`` needs ``name``, ``sell_price`` and ``wholesale_price``.
    """
    kind = kind if kind in KINDS else "retail"
    typed_nothing = typed is None or typed == ""

    # A wholesale tick on a product with no trade price falls back to the shelf
    # price: with no separate number set, the shelf price *is* the price, and the
    # tick then says only which list the line is filed under. It used to refuse
    # instead, which made one tick behave unlike the other on exactly the products
    # least likely to have their prices filled in — and the bot's «Շարունակել»
    # button quotes this same fallback, so refusing here would book a different
    # answer than the button promised.
    listed = Decimal(item["sell_price"])
    if kind == "wholesale" and item["wholesale_price"] is not None:
        listed = Decimal(item["wholesale_price"])

    if typed_nothing:
        return listed, kind

    # The typed number, under the kind that was ticked. A caller that explicitly
    # asks for 'custom' still gets it — the owner's amend form on /reports can send
    # it, and an old line being re-saved should not be re-labelled.
    return Decimal(typed), kind
