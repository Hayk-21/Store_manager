"""The HTTP client.

The property everything else rests on is that a retry reuses the idempotency
key. If it did not, a timeout on a slow connection would sell the same vape
twice and the till would never balance.
"""

from __future__ import annotations

import httpx
import pytest

from app.api import Api, ApiError, ApiUnavailable, new_idempotency_key


def _api_with(handler) -> Api:
    api = Api()
    api._client = httpx.AsyncClient(
        base_url="http://web.test/api/bot/v1",
        transport=httpx.MockTransport(handler),
        headers={"X-Bot-Secret": "test-bot-secret"},
    )
    return api


async def test_the_shared_secret_is_sent_on_every_request():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["secret"] = request.headers.get("x-bot-secret")
        return httpx.Response(200, json={"ok": True, "worker": {}, "session": None})

    await _api_with(handler).me(111)

    assert seen["secret"] == "test-bot-secret"


async def test_a_server_error_is_retried():
    attempts = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(request)
        if len(attempts) < 3:
            return httpx.Response(503, json={"ok": False})
        return httpx.Response(200, json={"ok": True, "session": None})

    result = await _api_with(handler).me(111)

    assert result["ok"] is True
    assert len(attempts) == 3


async def test_a_transport_failure_is_retried():
    attempts = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(request)
        if len(attempts) < 2:
            raise httpx.ConnectError("no route to host")
        return httpx.Response(200, json={"ok": True, "session": None})

    await _api_with(handler).me(111)

    assert len(attempts) == 2


async def test_a_refusal_is_never_retried():
    """"You are out of range" will not become true by asking again."""
    attempts = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(request)
        return httpx.Response(
            422,
            json={"ok": False, "error": {"code": "no_store_in_range",
                                         "message": "Դուք ոչ մի խանութի տարածքում չեք։"}},
        )

    with pytest.raises(ApiError) as caught:
        await _api_with(handler).open_store(111, 40.1, 44.5, 20, "idem-key-0001")

    assert len(attempts) == 1, "a 4xx must be reported, not hammered"
    assert caught.value.code == "no_store_in_range"


async def test_the_idempotency_key_is_identical_across_every_retry():
    """The single most important property in the whole bot."""
    keys = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        keys.append(json.loads(request.content)["idempotency_key"])
        if len(keys) < 3:
            return httpx.Response(500, json={"ok": False})
        return httpx.Response(201, json={"ok": True, "sale": {}, "store_totals": {}})

    await _api_with(handler).sell(111, 7, 2, "cash", "idem-key-stable-01")

    assert len(keys) == 3
    assert len(set(keys)) == 1, f"the key changed between attempts: {keys}"
    assert keys[0] == "idem-key-stable-01"


async def test_giving_up_reports_a_network_problem_not_a_refusal():
    """The worker must be told to try again, not that they did something wrong."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"ok": False})

    with pytest.raises(ApiUnavailable) as caught:
        await _api_with(handler).me(111)

    assert "կապ չկա" in caught.value.human()


async def test_the_servers_armenian_message_is_passed_through_verbatim():
    """The server owns error wording so the two services cannot drift."""
    sentence = "«HQD Cuvie» — պահեստում կա ընդամենը 3 հատ, խնդրված է 5։"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            409,
            json={"ok": False, "error": {"code": "insufficient_stock", "message": sentence,
                                         "details": {"available": 3}}},
        )

    with pytest.raises(ApiError) as caught:
        await _api_with(handler).sell(111, 7, 5, "cash", "idem-key-0001")

    assert caught.value.human() == sentence
    assert caught.value.details["available"] == 3


async def test_an_error_with_no_message_still_says_something_useful():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"ok": False, "error": {"code": "weird"}})

    with pytest.raises(ApiError) as caught:
        await _api_with(handler).me(111)

    assert caught.value.human().strip() != ""


async def test_a_non_json_body_is_not_retried_as_if_it_were_a_blip():
    attempts = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(request)
        return httpx.Response(200, text="<html>gateway</html>")

    with pytest.raises(ApiError):
        await _api_with(handler).me(111)

    assert len(attempts) == 1


async def test_open_store_omits_accuracy_when_the_phone_did_not_report_it():
    """Sending accuracy_m: null would trip the schema's forbid-extra strictness
    for no reason; absent means "unknown"."""
    import json

    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(201, json={"ok": True, "session": {}, "worker": {}})

    await _api_with(handler).open_store(111, 40.1, 44.5, None, "idem-key-0001")

    assert "accuracy_m" not in captured
    assert captured["lat"] == 40.1


async def test_the_bot_never_names_a_store():
    """The server geofences. If the bot could name a store, the two would need
    to be kept in sync — which is exactly the failure this design removes."""
    import json

    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(201, json={"ok": True, "session": {}, "worker": {}})

    await _api_with(handler).open_store(111, 40.1, 44.5, 25, "idem-key-0001")

    assert set(captured) == {"telegram_id", "lat", "lng", "accuracy_m", "idempotency_key"}


async def test_the_profile_name_rides_along_so_the_owner_never_types_one():
    """The owner registers a Telegram id; the name arrives from Telegram."""
    import json

    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            seen["me"] = dict(request.url.params)
        else:
            seen["open"] = json.loads(request.content)
        return httpx.Response(
            201 if request.method == "POST" else 200,
            json={"ok": True, "session": None, "worker": {}},
        )

    api = _api_with(handler)
    await api.me(111, "Անի Հակոբյան")
    await api.open_store(111, 40.1, 44.5, 20, "idem-key-0001", "Անի Հակոբյան")

    assert seen["me"]["telegram_name"] == "Անի Հակոբյան"
    assert seen["open"]["telegram_name"] == "Անի Հակոբյան"


async def test_a_missing_profile_name_is_omitted_rather_than_sent_as_null():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(dict(request.url.params))
        return httpx.Response(200, json={"ok": True, "session": None})

    await _api_with(handler).me(111, None)

    assert "telegram_name" not in seen


def test_every_action_gets_its_own_key():
    assert new_idempotency_key() != new_idempotency_key()
    assert len(new_idempotency_key()) >= 8, "the server rejects keys shorter than 8"
