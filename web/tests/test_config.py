"""Pure functions that are easy to get wrong and expensive to get wrong.

Both of these caused a real failed deployment, so they are pinned here.
"""

from __future__ import annotations

from app.config import clean_dsn, normalise_base_url

# -- APP_BASE_URL ------------------------------------------------------------

def test_a_railway_domain_pasted_without_a_scheme_still_works():
    """Railway displays the generated domain bare, and pasting it verbatim is
    the obvious thing to do. Left alone it makes urlsplit produce an empty
    netloc, so the CSRF same-origin check rejects every POST — including login."""
    assert (
        normalise_base_url("storemanager-production-14cb.up.railway.app")
        == "https://storemanager-production-14cb.up.railway.app"
    )


def test_an_explicit_scheme_is_left_alone():
    assert normalise_base_url("https://example.com") == "https://example.com"
    assert normalise_base_url("http://example.com") == "http://example.com"


def test_a_trailing_slash_is_dropped():
    assert normalise_base_url("https://example.com/") == "https://example.com"


def test_localhost_gets_http_not_https():
    assert normalise_base_url("localhost:8000") == "http://localhost:8000"
    assert normalise_base_url("127.0.0.1:8000") == "http://127.0.0.1:8000"


def test_an_empty_value_falls_back_to_local():
    assert normalise_base_url("") == "http://localhost:8000"
    assert normalise_base_url("   ") == "http://localhost:8000"


def test_the_normalised_url_actually_parses_into_an_origin():
    """The property the CSRF check depends on."""
    from urllib.parse import urlsplit

    parsed = urlsplit(normalise_base_url("storemanager-production-14cb.up.railway.app"))
    assert parsed.scheme == "https"
    assert parsed.netloc == "storemanager-production-14cb.up.railway.app"


# -- Neon connection strings -------------------------------------------------

def test_channel_binding_is_stripped():
    """asyncpg raises on channel_binding, and Neon puts it in every URL it hands out."""
    dsn = clean_dsn(
        "postgresql://u:p@ep-x-pooler.eu-central-1.aws.neon.tech/db"
        "?sslmode=require&channel_binding=require"
    )

    assert "channel_binding" not in dsn.url
    assert "sslmode" not in dsn.url
    assert dsn.ssl == "require"


def test_the_pooled_endpoint_turns_off_the_statement_cache():
    """Neon's pooler is PgBouncer in transaction mode and cannot keep prepared
    statements; leaving the cache on produces intermittent
    'prepared statement __asyncpg_stmt_x__ does not exist'."""
    pooled = clean_dsn("postgresql://u:p@ep-x-pooler.eu-central-1.aws.neon.tech/db")
    direct = clean_dsn("postgresql://u:p@ep-x.eu-central-1.aws.neon.tech/db")

    assert pooled.pooled is True and pooled.statement_cache_size == 0
    assert direct.pooled is False and direct.statement_cache_size > 0


def test_sslmode_disable_turns_tls_off_for_local_postgres():
    dsn = clean_dsn("postgresql://u:p@localhost:5432/db?sslmode=disable")

    assert dsn.ssl is False
