"""Deciding which store a worker is standing in.

This runs on the server, against ``stores.lat/lng/radius_m``. The bot forwards a
raw coordinate and nothing else — it holds no store list, no radius and no
names, so there is nothing to keep in sync between the two services.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from app.config import settings
from app.db import db
from app.errors import BotError
from app.repo import stores as stores_repo
from app.texts import no_store_in_range_message

log = logging.getLogger("storemanager.geofence")


@dataclass(frozen=True)
class Candidate:
    id: int
    name: str
    distance_m: int
    radius_m: int

    @property
    def within(self) -> bool:
        return self.distance_m <= self.radius_m

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "distance_m": self.distance_m,
            "radius_m": self.radius_m,
            "within_geofence": self.within,
        }


@dataclass(frozen=True)
class Match:
    matched: Candidate | None
    candidates: list[Candidate]

    @property
    def nearest(self) -> Candidate | None:
        return self.candidates[0] if self.candidates else None


async def match_store(owner_id: int, lat: float, lng: float) -> Match:
    """The nearest store *whose own radius covers the point*.

    The filter has to come before the sort. Sorting first and taking the first
    row that happens to be in range lets a store 800 m away with a 1000 m radius
    beat one 30 m away with a 120 m radius — which is the bug VAL shipped.
    """
    rows = await stores_repo.candidates_near(owner_id, lat, lng)
    candidates = [
        Candidate(
            id=row["id"],
            name=row["name"],
            distance_m=int(round(row["distance_m"])),
            radius_m=row["radius_m"],
        )
        for row in rows
    ]
    inside = sorted(
        (c for c in candidates if c.within), key=lambda c: (c.distance_m, c.id)
    )
    return Match(matched=inside[0] if inside else None, candidates=candidates)


async def distance_to(
    store_lat: float, store_lng: float, lat: float, lng: float
) -> int:
    """Metres between a store and a point, by the same function the geofence uses.

    Goes to Postgres rather than repeating the haversine in Python: one
    definition of distance means a tracked position and a check-in can never
    disagree about how far away the same point is.
    """
    metres = await db.fetchval(
        "SELECT distance_m($1, $2, $3, $4)", store_lat, store_lng, lat, lng
    )
    return int(round(metres))


async def require_store(
    owner_id: int, lat: float, lng: float, accuracy_m: float | None = None
) -> Match:
    """``match_store``, but raising the right BotError when nothing matches.

    The three failure modes are told apart deliberately: "your GPS is vague",
    "you are too far away" and "nobody has set the coordinates yet" need three
    different actions from the person reading the message.
    """
    if accuracy_m is not None and accuracy_m > settings.max_accuracy_m:
        raise BotError(
            "location_too_vague",
            details={"accuracy_m": int(accuracy_m), "max_accuracy_m": settings.max_accuracy_m},
        )

    match = await match_store(owner_id, lat, lng)
    if match.matched is not None:
        return match

    if not match.candidates:
        # Either the owner has no stores at all, or none of them has coordinates.
        raise BotError("no_stores_located")

    nearest = match.nearest
    log.info(
        "owner %s: no store in range, nearest is %s at %s m (radius %s)",
        owner_id, nearest.name, nearest.distance_m, nearest.radius_m,
    )
    raise BotError(
        "no_store_in_range",
        no_store_in_range_message(nearest.name, nearest.distance_m),
        details={
            "nearest": nearest.as_dict(),
            "candidates": [c.as_dict() for c in match.candidates],
        },
    )
