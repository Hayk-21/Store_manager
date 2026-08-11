"""Reading back what the bot itself just printed.

``format.money()`` renders amounts comma-grouped with no decimals — "40,000 ֏" — which
is the US thousands convention. Every prompt that asks for a number was written for the
Armenian one instead — space-thousands, comma-decimal, "1 000,00" — from before the bot
echoed amounts back at all. A worker who reads "40,000 ֏" on screen and types it back
was hitting `.replace(",", ".")`, which turned it into 40.00: forty dram typed as forty
thousand, and no error, because the result was still a valid number.

``parse_money`` has to accept both conventions and never guess wrong between them.
"""

from __future__ import annotations

from decimal import Decimal

from app import format


def test_reads_back_what_money_itself_prints():
    """The bug, reproduced: the bot's own comma-thousands display has to round-trip."""
    assert format.parse_money("40,000") == Decimal("40000.00")
    assert format.parse_money(format.money("40000").replace(" ֏", "")) == Decimal("40000.00")


def test_the_armenian_convention_still_works():
    """Space-thousands, comma-decimal — the format the prompts were written for."""
    assert format.parse_money("1 000,00") == Decimal("1000.00")
    assert format.parse_money("1 000,50") == Decimal("1000.50")


def test_a_plain_number_is_unaffected():
    assert format.parse_money("3500") == Decimal("3500.00")
    assert format.parse_money("3500.50") == Decimal("3500.50")


def test_multiple_thousands_groups():
    assert format.parse_money("1,234,567") == Decimal("1234567.00")


def test_us_style_thousands_and_decimal_together():
    assert format.parse_money("1,000.50") == Decimal("1000.50")


def test_european_style_thousands_and_decimal_together():
    assert format.parse_money("1.000,50") == Decimal("1000.50")


def test_a_short_comma_decimal_is_not_read_as_thousands():
    """Two digits after the comma cannot be a thousands group — money here never has
    more than two decimal places, so this is unambiguously a fraction."""
    assert format.parse_money("5,5") == Decimal("5.50")
    assert format.parse_money("5,50") == Decimal("5.50")


def test_a_non_breaking_space_is_still_a_thousands_separator():
    """A phone keyboard's number row can insert one instead of a plain space."""
    assert format.parse_money("1 000,00") == Decimal("1000.00")


def test_nonsense_is_refused_not_guessed():
    assert format.parse_money("մի քիչ") is None
    assert format.parse_money("") is None
    assert format.parse_money(None) is None


def test_a_negative_number_still_parses_as_a_number():
    """Parsing is not validation — callers decide whether negative is allowed."""
    assert format.parse_money("-500") == Decimal("-500.00")
