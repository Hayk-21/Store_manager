"""Which store is the worker standing in.

The scenarios below are the reason this moved server-side. You cannot construct
"one store 30 m away with a tight radius, another 800 m away with a generous
one" by walking around Yerevan, which is exactly why VAL's version of this bug
survived to production.
"""

from __future__ import annotations

import pytest

from app.errors import BotError
from app.services.geofence import match_store, require_store
from tests.factories import YEREVAN_LAT, YEREVAN_LNG, make_owner, make_store

# ~110 m north, and ~800 m north, of the reference point.
NEARBY_LAT = YEREVAN_LAT + 0.001
FAR_LAT = YEREVAN_LAT + 0.0072


async def test_the_only_store_in_range_wins(client):
    owner_id = await make_owner()
    store_id = await make_store(owner_id, lat=YEREVAN_LAT, lng=YEREVAN_LNG, radius_m=120)

    match = await match_store(owner_id, YEREVAN_LAT, YEREVAN_LNG)

    assert match.matched is not None
    assert match.matched.id == store_id
    assert match.matched.distance_m == 0


async def test_nearest_wins_over_a_farther_store_with_a_generous_radius(client):
    """This is the VAL bug, pinned.

    Sorting by distance and taking the first row that is merely *within its own
    radius* would still pick the close one here — but only because it sorts
    first. The property that matters is that the filter is applied to every
    candidate before the winner is chosen.
    """
    owner_id = await make_owner()
    close = await make_store(owner_id, "Մոտ", lat=YEREVAN_LAT, lng=YEREVAN_LNG, radius_m=120)
    await make_store(owner_id, "Հեռու", lat=FAR_LAT, lng=YEREVAN_LNG, radius_m=1000)

    match = await match_store(owner_id, YEREVAN_LAT, YEREVAN_LNG)

    assert match.matched.id == close
    # Both are genuinely in range; the choice between them is what is being tested.
    assert sum(1 for c in match.candidates if c.within) == 2


async def test_a_nearer_store_outside_its_own_radius_loses_to_a_farther_one_inside(client):
    """Distance alone is not the rule — each store's own radius decides."""
    owner_id = await make_owner()
    # 110 m away but only willing to accept 50 m.
    await make_store(owner_id, "Մոտ բայց խիստ", lat=NEARBY_LAT, lng=YEREVAN_LNG, radius_m=50)
    # 800 m away and willing to accept 1000 m.
    generous = await make_store(owner_id, "Հեռու բայց լայն", lat=FAR_LAT, lng=YEREVAN_LNG,
                                radius_m=1000)

    match = await match_store(owner_id, YEREVAN_LAT, YEREVAN_LNG)

    assert match.matched.id == generous
    assert match.candidates[0].name == "Մոտ բայց խիստ", "the nearest is still reported"
    assert match.candidates[0].within is False


async def test_stores_without_coordinates_are_never_candidates(client):
    owner_id = await make_owner()
    await make_store(owner_id, "Անհասցե", lat=None, lng=None)

    match = await match_store(owner_id, YEREVAN_LAT, YEREVAN_LNG)

    assert match.candidates == []
    assert match.matched is None


async def test_another_owners_store_is_never_a_candidate(client):
    """Standing inside a competitor's shop must not attach you to it."""
    owner_id = await make_owner()
    other_id = await make_owner()
    await make_store(other_id, "Ուրիշի", lat=YEREVAN_LAT, lng=YEREVAN_LNG, radius_m=5000)

    match = await match_store(owner_id, YEREVAN_LAT, YEREVAN_LNG)

    assert match.candidates == []


async def test_inactive_stores_are_never_candidates(client):
    from app.db import db

    owner_id = await make_owner()
    store_id = await make_store(owner_id, lat=YEREVAN_LAT, lng=YEREVAN_LNG)
    await db.execute("UPDATE stores SET is_active = false WHERE id = $1", store_id)

    match = await match_store(owner_id, YEREVAN_LAT, YEREVAN_LNG)

    assert match.matched is None


async def test_out_of_range_says_how_far_away_the_nearest_one_is(client):
    """A dead end is much more useful as an instruction."""
    owner_id = await make_owner()
    await make_store(owner_id, "Հյուսիսային", lat=FAR_LAT, lng=YEREVAN_LNG, radius_m=120)

    with pytest.raises(BotError) as caught:
        await require_store(owner_id, YEREVAN_LAT, YEREVAN_LNG)

    error = caught.value
    assert error.code == "no_store_in_range"
    assert error.status == 422
    assert error.details["nearest"]["name"] == "Հյուսիսային"
    assert 750 < error.details["nearest"]["distance_m"] < 850
    assert "Հյուսիսային" in error.message


async def test_no_coordinates_anywhere_is_a_different_complaint(client):
    """"Nobody set the coordinates" needs the owner; "you are too far" needs the
    worker to walk. Telling them apart is the whole point."""
    owner_id = await make_owner()
    await make_store(owner_id, lat=None, lng=None)

    with pytest.raises(BotError) as caught:
        await require_store(owner_id, YEREVAN_LAT, YEREVAN_LNG)

    assert caught.value.code == "no_stores_located"


async def test_a_hopelessly_vague_reading_is_refused_rather_than_guessed_at(client):
    owner_id = await make_owner()
    await make_store(owner_id, lat=YEREVAN_LAT, lng=YEREVAN_LNG, radius_m=120)

    with pytest.raises(BotError) as caught:
        await require_store(owner_id, YEREVAN_LAT, YEREVAN_LNG, accuracy_m=500)

    assert caught.value.code == "location_too_vague"


async def test_an_ordinary_urban_reading_is_accepted(client):
    """Phone GPS in a street canyon is routinely +/- 30 m. Refusing that would
    reject honest workers standing in the doorway."""
    owner_id = await make_owner()
    await make_store(owner_id, lat=YEREVAN_LAT, lng=YEREVAN_LNG, radius_m=120)

    match = await require_store(owner_id, YEREVAN_LAT, YEREVAN_LNG, accuracy_m=35)

    assert match.matched is not None
