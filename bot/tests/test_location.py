"""What the bot will accept as "I am standing here".

Telegram tells us less than it looks. A point dropped on the map and a genuine
"send my current location" arrive as the same object — ``horizontal_accuracy``
seemed to separate them but is simply absent on many real readings, which is how
an earlier version locked honest workers out of their own shift.

A *live* location is the one exception, and it is now the requirement: Telegram
streams it from the device and keeps editing the message as it moves, so there
is no way to aim it at a chosen point.
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


# -- what is accepted ---------------------------------------------------------

def test_a_live_location_is_accepted():
    assert _reject_faked_location(FakeMessage(accuracy=12, live_period=900)) is None


def test_a_live_location_without_accuracy_is_accepted():
    """The old regression, still guarded. Many clients omit accuracy on a
    genuine reading, and refusing those told a worker standing in their own shop
    that their position was hand-placed."""
    assert _reject_faked_location(FakeMessage(accuracy=None, live_period=900)) is None


@pytest.mark.parametrize("period", [900, 3600, 28_800])
def test_every_span_telegram_offers_is_accepted(period):
    """15 minutes, 1 hour, 8 hours — the worker picks, we take any of them."""
    assert _reject_faked_location(FakeMessage(live_period=period)) is None


@pytest.mark.parametrize("age", [0, 5, 60, MAX_LOCATION_AGE_S - 1])
def test_a_slow_connection_is_still_accepted(age):
    """The window has to be generous enough for a phone on a bad signal."""
    assert _reject_faked_location(FakeMessage(live_period=900, age_s=age)) is None


# -- what is refused ----------------------------------------------------------

def test_a_plain_location_is_refused():
    """The whole point: a static point can be dropped anywhere on the map."""
    assert _reject_faked_location(FakeMessage(accuracy=18)) == texts.LOCATION_NOT_LIVE
    assert _reject_faked_location(FakeMessage(accuracy=None)) == texts.LOCATION_NOT_LIVE


def test_a_live_period_of_zero_is_not_live():
    """Absent and zero mean the same thing here, and a falsy check covers both."""
    assert _reject_faked_location(FakeMessage(live_period=0)) == texts.LOCATION_NOT_LIVE


def test_a_forwarded_live_location_is_refused():
    """It is somebody else's position, and forwarding is how one worker would
    cover for another."""
    assert _reject_faked_location(FakeMessage(live_period=900, forwarded=True)) == (
        texts.LOCATION_FORWARDED
    )


@pytest.mark.parametrize("age", [MAX_LOCATION_AGE_S + 1, 3600, 86_400])
def test_an_old_location_is_refused(age):
    """Replaying this morning's genuine reading is still not where you are now."""
    assert _reject_faked_location(FakeMessage(live_period=900, age_s=age)) == (
        texts.LOCATION_STALE
    )


# -- the wording, which is the only interface the worker has ------------------

def test_the_refusal_explains_how_to_share_a_live_location():
    """"Share a live location" is four taps down a menu most people have never
    opened. Being told no without being told how is a dead end."""
    assert "Live Location" in texts.LOCATION_NOT_LIVE
    assert "📎" in texts.LOCATION_NOT_LIVE
    assert "Share My Live Location" in texts.LOCATION_NOT_LIVE


def test_the_prompt_says_it_needs_a_phone():
    """Telegram Desktop has no live location at all, and a worker trying from a
    laptop would otherwise just fail repeatedly."""
    assert "Share My Live Location" in texts.ASK_LOCATION
    assert "հեռախոս" in texts.ASK_LOCATION


def test_no_complaint_carries_a_stray_placeholder():
    """They are sent verbatim now; a leftover {button} would print as itself."""
    for complaint in (
        texts.LOCATION_NOT_LIVE, texts.LOCATION_FORWARDED, texts.LOCATION_STALE,
        texts.LOCATION_ONLY_FROM_PHONE, texts.ASK_LOCATION,
    ):
        assert "{" not in complaint
