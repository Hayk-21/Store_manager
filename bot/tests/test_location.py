"""What the bot will accept as "I am standing here".

Telegram tells us less than it looks. A point dropped on the map and a genuine
"send my current location" arrive as the same object — ``horizontal_accuracy``
seemed to separate them but is simply absent on many real readings, which is how
an earlier version locked honest workers out of their own shift.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app import texts
from app.config import settings
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


@pytest.fixture(autouse=True)
def lenient(monkeypatch):
    """The shipped default: trust the reading, check what can be checked."""
    monkeypatch.setattr(settings, "require_live_location", False)


# -- the default: a working shop ---------------------------------------------

def test_a_reading_with_accuracy_is_accepted():
    assert _reject_faked_location(FakeMessage(accuracy=18)) is None


def test_a_reading_WITHOUT_accuracy_is_accepted():
    """The regression. Many Telegram clients omit accuracy on a genuine
    location, and refusing those meant a worker standing in their own shop was
    told their position was hand-placed."""
    assert _reject_faked_location(FakeMessage(accuracy=None)) is None


def test_a_live_location_is_accepted():
    assert _reject_faked_location(FakeMessage(accuracy=12, live_period=900)) is None


# -- what is still refused, and can be told apart reliably --------------------

def test_a_forwarded_location_is_refused():
    """It is somebody else's position, from whenever they sent it."""
    assert _reject_faked_location(FakeMessage(accuracy=18, forwarded=True)) == (
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


# -- strict mode, for a shop that stops trusting its staff --------------------

def test_strict_mode_demands_a_live_location(monkeypatch):
    """The one thing that cannot be aimed at a chosen point through the normal
    interface. Off by default because it costs the worker real effort."""
    monkeypatch.setattr(settings, "require_live_location", True)

    assert _reject_faked_location(FakeMessage(accuracy=18)) == texts.LOCATION_NOT_LIVE
    assert _reject_faked_location(FakeMessage(accuracy=None)) == texts.LOCATION_NOT_LIVE
    assert _reject_faked_location(FakeMessage(live_period=900)) is None


def test_strict_mode_still_refuses_a_forwarded_live_location(monkeypatch):
    monkeypatch.setattr(settings, "require_live_location", True)

    assert _reject_faked_location(FakeMessage(live_period=900, forwarded=True)) == (
        texts.LOCATION_FORWARDED
    )


def test_strict_mode_explains_where_the_button_is():
    """"Share a live location" is not obvious; the message has to say how."""
    assert "live" in texts.LOCATION_NOT_LIVE.lower()
    assert "📎" in texts.LOCATION_NOT_LIVE


def test_every_complaint_survives_being_formatted():
    """They are sent through .format(button=...); a stray brace would raise."""
    for complaint in (
        texts.LOCATION_NOT_LIVE, texts.LOCATION_FORWARDED, texts.LOCATION_STALE,
    ):
        rendered = complaint.format(button=texts.BTN_SEND_LOCATION)
        assert "{" not in rendered
