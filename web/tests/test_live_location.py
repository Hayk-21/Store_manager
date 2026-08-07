"""Live location: what may open a shift, and what happens to the trail after.

The rule the whole feature rests on: a shift can only be opened from a position
Telegram is streaming from the device. A point dropped on the map and a real
"send my current location" are the same object over the wire, and
``horizontal_accuracy`` does not separate them — a live period does, because
there is no way to aim a moving stream at a chosen point.

Everything after the opening is *recorded, never enforced*. A cashier who walks
to the bank has not ended their shift, and a bot that decided otherwise from a
GPS reading would be wrong often enough to be worse than useless.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.db import db
from app.errors import BotError
from app.repo import tracking as tracking_repo
from app.services import shifts as shifts_service
from tests.factories import (
    YEREVAN_LAT,
    YEREVAN_LNG,
    login,
    make_owner,
    make_store,
    make_worker,
)

BASE = "/api/bot/v1"

# ~700 m north of the shop: outside any sane radius, close enough to be a
# cashier on an errand rather than a spoofed position on another continent.
AWAY_LAT = YEREVAN_LAT + 0.0063


async def _a_worker(radius_m: int = 120):
    owner_id = await make_owner("@ownerhandle")
    store_id = await make_store(
        owner_id, "Խանութ 1", lat=YEREVAN_LAT, lng=YEREVAN_LNG, radius_m=radius_m
    )
    worker_id, telegram_id = await make_worker(owner_id, "Անի", salary_amount="0.00")
    worker = shifts_service.Worker(
        id=worker_id, owner_id=owner_id, name="Անի", salary_amount=Decimal("0.00")
    )
    return owner_id, store_id, worker, telegram_id


async def _open(worker, live_period=900, key="idem-key-open-1"):
    return await shifts_service.open_store(
        worker, YEREVAN_LAT, YEREVAN_LNG, 20, key, live_period
    )


# -- opening requires a live location -----------------------------------------

async def test_a_live_location_opens_a_shift(client):
    _, _, worker, _ = await _a_worker()

    result = await _open(worker, live_period=900)

    assert result["session"]["id"]
    assert await db.fetchval("SELECT start_live_period FROM work_sessions") == 900


@pytest.mark.parametrize("live_period", [None, 0])
async def test_a_plain_location_cannot_open_a_shift(client, live_period):
    """The whole point of the feature."""
    _, _, worker, _ = await _a_worker()

    with pytest.raises(BotError) as caught:
        await _open(worker, live_period=live_period)

    assert caught.value.code == "location_not_live"
    assert caught.value.status == 422
    assert await db.fetchval("SELECT count(*) FROM work_sessions") == 0


async def test_a_suspiciously_short_period_is_refused(client):
    """Below Telegram's own minimum, so it did not come from its interface."""
    _, _, worker, _ = await _a_worker()

    with pytest.raises(BotError) as caught:
        await _open(worker, live_period=60)

    assert caught.value.code == "location_not_live"
    assert caught.value.details["minimum_s"] == 900


@pytest.mark.parametrize("live_period", [900, 3600, 28_800])
async def test_every_span_telegram_offers_is_accepted(client, live_period):
    """15 minutes, 1 hour, 8 hours. The worker picks; any of them will do."""
    _, _, worker, _ = await _a_worker()

    assert await _open(worker, live_period=live_period)


async def test_the_refusal_says_how_to_share_a_live_location(client):
    """It is four taps down a menu most people have never opened. Being told no
    without being told how is a dead end."""
    _, _, worker, _ = await _a_worker()

    with pytest.raises(BotError) as caught:
        await _open(worker, live_period=None)

    message = caught.value.message
    assert "Live Location" in message
    assert "📎" in message


async def test_being_out_of_range_is_still_a_different_answer(client):
    """A live location from the wrong place is a distance problem, not a
    liveness one, and the worker needs to be told which."""
    _, _, worker, _ = await _a_worker()

    with pytest.raises(BotError) as caught:
        await shifts_service.open_store(
            worker, AWAY_LAT, YEREVAN_LNG, 20, "idem-key-open-1", 900
        )

    assert caught.value.code == "no_store_in_range"


async def test_the_endpoint_refuses_a_body_with_no_live_period(client, bot_headers):
    _, _, _, telegram_id = await _a_worker()

    response = await client.post(
        f"{BASE}/store/open",
        json={"telegram_id": telegram_id, "lat": YEREVAN_LAT, "lng": YEREVAN_LNG,
              "idempotency_key": "idem-key-open-01"},
        headers=bot_headers,
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "location_not_live"


# -- the trail ----------------------------------------------------------------

async def test_the_opening_position_starts_the_trail(client):
    """So the track begins where the shift did, not at whenever the worker first
    happened to move."""
    _, _, worker, _ = await _a_worker()
    await _open(worker)
    shift_id = await db.fetchval("SELECT id FROM work_sessions")

    track = await tracking_repo.track_for_session(shift_id)

    assert len(track) == 1
    assert track[0]["in_range"] is True
    assert track[0]["distance_m"] == 0


async def test_a_ping_is_recorded_against_the_open_shift(client):
    _, _, worker, _ = await _a_worker()
    await _open(worker)

    result = await shifts_service.record_position(worker, YEREVAN_LAT, YEREVAN_LNG)

    assert result["in_range"] is True
    assert result["distance_m"] == 0
    shift = await db.fetchrow("SELECT ping_count, last_ping_at, last_lat FROM work_sessions")
    assert shift["ping_count"] == 2, "the opening position, and this one"
    assert shift["last_ping_at"] is not None
    assert float(shift["last_lat"]) == pytest.approx(YEREVAN_LAT)


async def test_distance_is_measured_to_the_store_the_shift_belongs_to(client):
    """Not to the nearest one. The question is "how far from your post", and the
    nearest shop to a worker on an errand may be a different branch."""
    owner_id, _, worker, _ = await _a_worker()
    await _open(worker)
    # A second shop, much closer to where the worker has wandered.
    await make_store(owner_id, "Խանութ 2", lat=AWAY_LAT, lng=YEREVAN_LNG, radius_m=120)

    result = await shifts_service.record_position(worker, AWAY_LAT, YEREVAN_LNG)

    assert result["store_name"] == "Խանութ 1"
    assert result["distance_m"] > 600


async def test_wandering_off_is_recorded_and_not_punished(client):
    """A cashier stepping out is something the owner should see, not something
    the system should act on."""
    _, _, worker, _ = await _a_worker()
    await _open(worker)

    result = await shifts_service.record_position(worker, AWAY_LAT, YEREVAN_LNG)

    assert result["in_range"] is False
    shift = await db.fetchrow("SELECT ended_at, left_area_at FROM work_sessions")
    assert shift["ended_at"] is None, "the shift is untouched"
    assert shift["left_area_at"] is not None


async def test_the_moment_they_left_is_the_first_one_not_the_last(client):
    """It answers "when did this start", which a value moving with every later
    reading could not."""
    _, _, worker, _ = await _a_worker()
    await _open(worker)
    await shifts_service.record_position(worker, AWAY_LAT, YEREVAN_LNG)
    first = await db.fetchval("SELECT left_area_at FROM work_sessions")

    await shifts_service.record_position(worker, AWAY_LAT, YEREVAN_LNG)

    assert await db.fetchval("SELECT left_area_at FROM work_sessions") == first


async def test_coming_back_does_not_erase_that_they_left(client):
    _, _, worker, _ = await _a_worker()
    await _open(worker)
    await shifts_service.record_position(worker, AWAY_LAT, YEREVAN_LNG)

    await shifts_service.record_position(worker, YEREVAN_LAT, YEREVAN_LNG)

    assert await db.fetchval("SELECT left_area_at FROM work_sessions") is not None
    assert await db.fetchval("SELECT last_distance_m FROM work_sessions") == 0


async def test_a_ping_with_no_shift_open_is_refused(client):
    """The bot swallows this: it is the normal end of the story when a worker
    closes up but leaves sharing running."""
    _, _, worker, _ = await _a_worker()

    with pytest.raises(BotError) as caught:
        await shifts_service.record_position(worker, YEREVAN_LAT, YEREVAN_LNG)

    assert caught.value.code == "no_open_session"


async def test_the_ping_endpoint_needs_no_idempotency_key(client, bot_headers):
    """It is a stream of observations. A repeated position costs a duplicate row
    and nothing else, and requiring a key would make the common call expensive."""
    _, _, worker, telegram_id = await _a_worker()
    await _open(worker)

    response = await client.post(
        f"{BASE}/location/ping",
        json={"telegram_id": telegram_id, "lat": YEREVAN_LAT, "lng": YEREVAN_LNG},
        headers=bot_headers,
    )

    assert response.status_code == 200
    assert response.json()["in_range"] is True


async def test_the_trail_survives_the_shift_ending(client):
    """It is the record of where somebody was, which does not stop being true."""
    _, _, worker, _ = await _a_worker()
    await _open(worker)
    await shifts_service.record_position(worker, AWAY_LAT, YEREVAN_LNG)
    shift_id = await db.fetchval("SELECT id FROM work_sessions")

    await shifts_service.close_out_shift(worker, [], "idem-key-close-1")

    summary = await tracking_repo.summary_for_session(shift_id)
    assert summary["pings"] == 2
    assert summary["out_of_range"] == 1
    assert summary["furthest_m"] > 600


async def test_another_owners_worker_cannot_ping_into_this_shift(client, bot_headers):
    """The ping resolves the tenant from the telegram id like everything else."""
    _, _, worker, _ = await _a_worker()
    await _open(worker)
    other_owner = await make_owner("@ownerother")
    _, stranger_tg = await make_worker(other_owner, "Բ")

    response = await client.post(
        f"{BASE}/location/ping",
        json={"telegram_id": stranger_tg, "lat": YEREVAN_LAT, "lng": YEREVAN_LNG},
        headers=bot_headers,
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "no_open_session"
    assert await db.fetchval("SELECT count(*) FROM location_pings") == 1


# -- what the owner sees ------------------------------------------------------

async def test_the_store_page_shows_where_each_worker_is(client):
    _, store_id, worker, _ = await _a_worker()
    await _open(worker)
    await login(client, "@ownerhandle")

    page = await client.get(f"/stores/{store_id}")

    assert "Անի" in page.text
    assert "📍" in page.text


async def test_a_worker_outside_the_radius_is_flagged_on_the_page(client):
    _, store_id, worker, _ = await _a_worker()
    await _open(worker)
    await shifts_service.record_position(worker, AWAY_LAT, YEREVAN_LNG)
    await login(client, "@ownerhandle")

    page = await client.get(f"/stores/{store_id}")

    assert "խանութից դուրս" in page.text
