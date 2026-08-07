"""Narrow screens.

Below 900px each wide table becomes a list of cards and the column heading moves
beside the value, taken from the cell's ``data-label``. A cell that forgets one
renders as a value with no idea what it means — which is invisible on a desktop
and obvious on the phone the owner actually uses.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

TEMPLATES = Path(__file__).resolve().parents[1] / "app" / "templates"
CSS = Path(__file__).resolve().parents[1] / "app" / "static" / "app.css"

CARD_TABLES = ["_items_table.html", "workers.html", "reports.html"]

# <td ...> up to the closing angle bracket, ignoring any '>' inside a Jinja tag.
CELL = re.compile(r"<td\b((?:[^>{]|\{[%{].*?[%}]\})*)>", re.DOTALL)


def _source(name: str) -> str:
    """Template text with Jinja comments removed — they talk *about* markup."""
    text = (TEMPLATES / name).read_text(encoding="utf-8")
    return re.sub(r"\{#.*?#\}", "", text, flags=re.DOTALL)


def _cells(name: str) -> list[str]:
    return CELL.findall(_source(name))


@pytest.mark.parametrize("template", CARD_TABLES)
def test_every_cell_of_a_card_table_carries_a_label(template):
    missing = [
        attrs.strip()
        for attrs in _cells(template)
        # An explicitly empty label is a deliberate "this cell needs no heading",
        # and empty-cell means "hide me entirely on a phone".
        if "data-label" not in attrs and "empty-cell" not in attrs
    ]

    assert not missing, (
        f"{template}: {len(missing)} cell(s) would render unlabelled on a phone: {missing[:3]}"
    )


@pytest.mark.parametrize("template", CARD_TABLES)
def test_wide_tables_opt_into_the_card_layout(template):
    for opening in re.findall(r"<table\b[^>]*>", _source(template)):
        assert "cards" in opening, f"{template}: {opening} will not collapse on a phone"


def test_the_stylesheet_actually_defines_the_card_layout():
    css = CSS.read_text(encoding="utf-8")

    assert "@media (max-width: 900px)" in css
    assert "table.cards thead { display: none; }" in css
    assert "content: attr(data-label)" in css


def test_inputs_are_large_enough_that_ios_does_not_zoom():
    """Anything under 16px makes Safari zoom the page on focus, which leaves the
    layout scrolled sideways with no obvious way back."""
    css = CSS.read_text(encoding="utf-8")

    narrow = css.split("@media (max-width: 900px)", 1)[1]
    assert "input, select, textarea, button { font-size: 16px; }" in narrow


def test_the_viewport_is_declared():
    base = (TEMPLATES / "base.html").read_text(encoding="utf-8")

    assert 'name="viewport"' in base
    assert "width=device-width" in base
