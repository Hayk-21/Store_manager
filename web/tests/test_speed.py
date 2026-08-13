"""The things that make the site fast, held in place.

Every one of these is invisible when it works and invisible when it breaks — a
page still renders, correctly, just slowly. So each is asserted rather than left
to be noticed, and each says what it costs when it is gone.

The measurements behind them are in PERFORMANCE.md.
"""

from __future__ import annotations

import re
from pathlib import Path

from app.config import settings
from tests.factories import login, make_owner, make_store

TEMPLATES = Path(__file__).resolve().parents[1] / "app" / "templates"


# -- compression -------------------------------------------------------------

async def test_a_page_is_compressed_when_the_browser_can_take_it(client):
    """A month of statistics is 685 KB of HTML and gzips to 25 KB, because the page
    is the same edit form five hundred times over. On a phone that is the better part
    of two seconds of downloading per view — more than the server and the database
    cost put together."""
    owner_id = await make_owner("@ownerhandle")
    await make_store(owner_id, "Խանութ 1")
    await login(client, "@ownerhandle")

    response = await client.get("/statistics", headers={"Accept-Encoding": "gzip"})

    assert response.status_code == 200
    assert response.headers.get("content-encoding") == "gzip"


async def test_a_browser_that_cannot_take_it_gets_plain_html(client):
    owner_id = await make_owner("@ownerhandle")
    await make_store(owner_id, "Խանութ 1")
    await login(client, "@ownerhandle")

    response = await client.get("/statistics", headers={"Accept-Encoding": "identity"})

    assert response.status_code == 200
    assert "content-encoding" not in response.headers
    assert "Վիճակագրություն" in response.text


# -- static assets -----------------------------------------------------------

async def test_a_fingerprinted_asset_is_cached_for_good(client):
    """The URL carries a hash of the file's contents, so a cached copy can never be
    stale — but the mount sent only an ETag, so every navigation spent a round-trip
    being told nothing had changed. Two of those on most pages, five on the map."""
    response = await client.get("/static/app.css?v=deadbeef01")

    assert response.status_code == 200
    assert "immutable" in response.headers["cache-control"]
    assert f"max-age={365 * 24 * 3600}" in response.headers["cache-control"]


async def test_an_unstamped_url_is_never_promised_forever(client):
    """Leaflet's stylesheet asks for its marker images by plain name. Promising a
    year on a URL that does not change when the file does is how a shop ends up
    serving an asset nobody can replace."""
    response = await client.get("/static/app.css")

    assert response.status_code == 200
    assert "immutable" not in response.headers["cache-control"]


def test_every_asset_the_templates_ask_for_is_fingerprinted():
    """The guarantee above holds only while every reference goes through
    ``static()``. A hand-written ``/static/…`` would be served unstamped — correct,
    but re-validated on every page, which is the cost this was meant to remove."""
    for name in ("base.html", "stores.html", "store_detail.html"):
        source = (TEMPLATES / name).read_text(encoding="utf-8")
        assert '"/static/' not in source, f"{name} hard-codes a static URL"


# -- the footer poll ---------------------------------------------------------

def test_the_footer_polls_slowly_and_only_when_it_is_being_looked_at():
    """Three round-trips including a write, on every open tab, forever. At ten
    seconds a tab left open overnight kept writing to the database all night, and
    every poll competed for the same small pool as the page the owner was waiting
    for."""
    source = (TEMPLATES / "base.html").read_text(encoding="utf-8")
    trigger = re.search(r'hx-trigger="([^"]*)"[^>]*>\s*<div class="footer-inner"',
                        source, re.DOTALL)
    trigger = trigger.group(1) if trigger else source

    assert "every 10s" not in source
    assert "every 30s" in trigger
    assert "visibilityState" in trigger, "a backgrounded tab must stop polling"


# -- the pool ----------------------------------------------------------------

def test_the_pool_opens_its_connections_before_anybody_asks():
    """It shrank to one connection after five idle minutes, so the next overlap of
    two requests paid for a TLS handshake and authentication against Neon in the
    middle of somebody's page load."""
    assert settings.db_pool_min >= 5
    assert settings.db_pool_max >= settings.db_pool_min
