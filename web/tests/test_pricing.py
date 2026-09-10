"""What a line costs, and which price list it belongs to.

One function, ``resolve_price``, shared by the cashier ticking «Մեծածախ» in Telegram
and the owner picking the same word in a form on the website — so the same choice
cannot book a different amount depending on who made it.

The rule it enforces is that **the price list is chosen, not inferred**. It used to
be inferred: any typed amount that differed from the listed price was filed as
'custom', which took the line out of *both* lists. A box haggled down from 3,000 to
2,800 was then neither a retail sale nor a wholesale one, «Մեծածախ» in the reports
counted only the boxes that went at exactly the listed number, and a filter split
into «մանրածախ» and «մեծածախ» could not add up to the takings it was split from.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.pricing import resolve_price

ITEM = {"name": "HQD Cuvie", "sell_price": "3500.00", "wholesale_price": "3000.00"}
NO_TRADE_PRICE = {"name": "HQD Cuvie", "sell_price": "3500.00", "wholesale_price": None}


# -- nothing typed: the list price, under its own name ------------------------

def test_retail_takes_the_shelf_price():
    assert resolve_price(ITEM, None, "retail") == (Decimal("3500.00"), "retail")


def test_wholesale_takes_the_trade_price():
    assert resolve_price(ITEM, None, "wholesale") == (Decimal("3000.00"), "wholesale")


def test_an_unknown_kind_falls_back_to_retail():
    """Not an error: a caller sending nonsense should still book the sale at the
    shelf price rather than fail at the counter."""
    assert resolve_price(ITEM, None, "nonsense") == (Decimal("3500.00"), "retail")
    assert resolve_price(ITEM, None, None) == (Decimal("3500.00"), "retail")


# -- a typed amount: the number changes, the list does not --------------------

def test_haggling_a_wholesale_box_keeps_it_wholesale():
    """The regression this rule exists for. 2,800 off a 3,000 trade price is a
    wholesale sale at 2,800, not a sale belonging to no price list."""
    assert resolve_price(ITEM, "2800.00", "wholesale") == (Decimal("2800.00"), "wholesale")


def test_a_discount_at_the_counter_stays_retail():
    assert resolve_price(ITEM, "3200.00", "retail") == (Decimal("3200.00"), "retail")


def test_typing_the_listed_number_changes_nothing():
    """Leaving a prefilled price alone was never a haggle, and still is not."""
    assert resolve_price(ITEM, "3000.00", "wholesale") == (Decimal("3000.00"), "wholesale")


@pytest.mark.parametrize("kind", ["retail", "wholesale"])
def test_the_two_lists_between_them_account_for_every_typed_price(kind):
    """The property the statistics filter depends on: whatever is typed, the line
    lands in the list that was ticked, so «մանրածախ + մեծածախ» is the whole."""
    _, resolved = resolve_price(ITEM, "1234.00", kind)

    assert resolved == kind


def test_an_explicit_custom_is_still_honoured():
    """Old lines carry it and the owner's amend form on /reports can send it. Re-saving
    one must not quietly re-label what it has always been."""
    assert resolve_price(ITEM, "2800.00", "custom") == (Decimal("2800.00"), "custom")


# -- a product nobody set a trade price for -----------------------------------

def test_wholesale_without_a_trade_price_falls_back_to_the_shelf_price():
    """With no separate number set, the shelf price *is* the price, and the tick
    says only which list the line is filed under. The bot's «Շարունակել» button
    quotes this same fallback, so anything else here would book a different amount
    than the button promised."""
    assert resolve_price(NO_TRADE_PRICE, None, "wholesale") == (
        Decimal("3500.00"), "wholesale"
    )


def test_and_a_typed_amount_still_wins():
    """"Sold this wholesale for 3,000" is a fact about the sale whether or not
    anybody wrote a list price down."""
    assert resolve_price(NO_TRADE_PRICE, "3000.00", "wholesale") == (
        Decimal("3000.00"), "wholesale"
    )
