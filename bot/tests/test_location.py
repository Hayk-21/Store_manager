"""Only a real device reading opens a shift.

Telegram lets anyone attach any point on the map, so "a location arrived" is not
the same as "this person is standing there". These are the cases that separate
the two.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app import texts
from app.handlers.shift import MAX_LOCATION_AGE_S, _reject_faked_location


class FakeLocation:
    def __init__(self, accuracy=None, live_period=None) -> None:
        self.latitude = 40.1772
        self.longitude = 44.5032
        self.horizontal_accuracy = accuracy
        self.live_period = live_period


class FakeMessage:
    def __init__(self, accuracy=None, live_period=None, forwarded=False, age_s=0) -> None:
        self.location = FakeLocation(accuracy, live_period)
        self.date = datetime.now(UTC) - timedelta(seconds=age_s)
        self.forward_origin = object() if forwarded else None
        self.forward_date = self.date if forwarded else None


def test_a_real_gps_fix_is_accepted():
    assert _reject_faked_location(FakeMessage(accuracy=18)) is None


def test_a_live_location_is_accepted():
    assert _reject_faked_location(FakeMessage(accuracy=12, live_period=900)) is None


def test_a_pin_dropped_on_the_map_is_refused():
    """The whole point. Telegram only fills in accuracy for a device reading, so
    a hand-placed pin has none — which is how somebody 'opens the shop' from
    home."""
    complaint = _reject_faked_location(FakeMessage(accuracy=None))

    assert complaint == texts.LOCATION_NOT_LIVE
    assert "ձեռքով" in complaint, "the worker is told what was wrong with it"


def test_a_forwarded_location_is_refused():
    """It is somebody else's position, from whenever they sent it."""
    assert _reject_faked_location(FakeMessage(accuracy=18, forwarded=True)) == (
        texts.LOCATION_FORWARDED
    )


def test_a_forwarded_pin_is_refused_for_being_forwarded_first():
    assert _reject_faked_location(FakeMessage(accuracy=None, forwarded=True)) == (
        texts.LOCATION_FORWARDED
    )


@pytest.mark.parametrize("age", [MAX_LOCATION_AGE_S + 1, 3600, 86_400])
def test_an_old_location_is_refused(age):
    """Replaying this morning's genuine reading is still not where you are now."""
    assert _reject_faked_location(FakeMessage(accuracy=18, age_s=age)) == (
        texts.LOCATION_STALE
    )


@pytest.mark.parametrize("age", [0, 5, 60, MAX_LOCATION_AGE_S - 1])
def test_a_slow_connection_is_still_accepted(age):
    """The window has to be generous enough for a phone on a bad signal."""
    assert _reject_faked_location(FakeMessage(accuracy=18, age_s=age)) is None


def test_every_complaint_names_the_button_to_press():
    """A refusal that does not say what to do next is just a dead end."""
    for complaint in (
        texts.LOCATION_NOT_LIVE, texts.LOCATION_FORWARDED, texts.LOCATION_STALE,
    ):
        assert "{button}" in complaint
        assert texts.BTN_SEND_LOCATION in complaint.format(button=texts.BTN_SEND_LOCATION)
