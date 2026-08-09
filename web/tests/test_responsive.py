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

CARD_TABLES = ["_items_table.html", "workers.html", "reports.html", "expenses.html"]

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
        assert "cards" in opening or "stack" in opening, (
            f"{template}: {opening} will not collapse on a phone"
        )


def test_the_stylesheet_actually_defines_the_card_layout():
    css = CSS.read_text(encoding="utf-8")

    assert "@media (max-width: 1200px)" in css
    assert "table.cards thead { display: none; }" in css
    assert "table.stack thead { display: none; }" in css, "always-stacked tables"
    assert "content: attr(data-label)" in css


def test_inputs_are_large_enough_that_ios_does_not_zoom():
    """Anything under 16px makes Safari zoom the page on focus, which leaves the
    layout scrolled sideways with no obvious way back."""
    css = CSS.read_text(encoding="utf-8")

    narrow = css.split("@media (max-width: 1200px)", 1)[1]
    assert "input, select, textarea, button { font-size: 16px; }" in narrow


def test_the_viewport_is_declared():
    base = (TEMPLATES / "base.html").read_text(encoding="utf-8")

    assert 'name="viewport"' in base
    assert "width=device-width" in base


def test_the_page_is_wide_enough_for_its_widest_table():
    """The report list is nine columns and only folds into cards below 1200px, so
    the page must be able to be wider than that — it was capped at 1100px, which
    guaranteed a sideways scrollbar on every desktop with room to spare."""
    css = CSS.read_text(encoding="utf-8")
    caps = [int(n) for n in re.findall(r"^main \{[^}]*?max-width: (\d+)px", css, re.M | re.S)]

    assert caps, "main has no width cap at all"
    assert min(caps) > 1200, f"main is capped at {min(caps)}px, narrower than the card breakpoint"


def test_nothing_can_scroll_the_whole_page_sideways():
    """A table may overflow its own wrapper — that is what the wrapper is for. The
    body may not, because then the layout slides and the fixed footer comes away
    from the edge of the screen."""
    css = CSS.read_text(encoding="utf-8")

    assert "html, body { max-width: 100%; overflow-x: hidden; }" in css


def test_an_always_stacked_card_flows_its_fields_rather_than_listing_them():
    """One field per row cost about 370px per expense on a screen with room for
    seven side by side. They flow now: as many across as fit, wrapping when not."""
    css = CSS.read_text(encoding="utf-8")
    rule = css.split("table.stack tr {", 1)[1].split("}", 1)[0]

    assert "display: grid" in rule
    assert "auto-fit" in rule, "a fixed column count would need a breakpoint per screen"


def test_every_asset_is_fingerprinted_so_a_change_reaches_the_browser():
    """A stylesheet served under a fixed URL stays in the browser's cache, so a
    layout fix does not arrive — the owner sees the old layout and reasonably
    concludes nothing was done. That happened, which is why this is a test and not
    a note: any new /static reference has to go through ``static()`` too.
    """
    for name in ("base.html", "stores.html", "store_detail.html"):
        source = (TEMPLATES / name).read_text(encoding="utf-8")
        raw = re.findall(r'(?:href|src)="/static/[^"]+"', source)
        assert not raw, f"{name}: {raw} would be cached past a deploy"


def test_a_changed_stylesheet_gets_a_different_url():
    from app.templating import STATIC_DIR, static

    before = static("app.css")
    digest = before.split("v=", 1)[1]

    assert len(digest) >= 8, "too short to be a content hash"
    # The hash is of the contents, so an unchanged file keeps its URL and stays
    # cached — the point is only that a *changed* one cannot.
    assert static("app.css") == before
    assert digest not in (STATIC_DIR / "app.css").read_text(encoding="utf-8")


def test_a_missing_asset_does_not_break_the_page():
    """A 404 that names the file beats an exception while rendering the template."""
    from app.templating import static

    assert static("no-such-file.css") == "/static/no-such-file.css"


def test_the_footer_is_short_enough_to_be_worth_its_place():
    """It is on every page. Five lines per store was 92px of every screen spent
    repeating what the store page says in full."""
    css = CSS.read_text(encoding="utf-8")
    height = int(re.search(r"--footer-h: (\d+)px", css).group(1))

    assert height <= 64, f"the footer claims {height}px of every page"
