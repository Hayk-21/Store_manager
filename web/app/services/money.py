"""Owner-initiated money movements.

The bot moves money by selling; this is the other way in — the owner taking the
takings out of the till, or putting a float in.
"""

from __future__ import annotations

import logging
from decimal import Decimal

from app.db import db
from app.errors import AppError
from app.repo import money as money_repo
from app.repo import sessions as sessions_repo

log = logging.getLogger("storemanager.money")

KINDS = {"withdrawal", "deposit"}


async def record_movement(
    owner_id: int,
    store_id: int,
    method: str,
    kind: str,
    amount: Decimal,
    note: str | None = None,
) -> None:
    """Take money out of, or put money into, the store's open till.

    Requires an open store session. When the store is closed its till has already
    been settled and handed over, so there is nothing to withdraw from — writing
    to a closed session would resurrect a total the owner has already banked.
    """
    if kind not in KINDS:
        raise AppError("validation_error", "Անհայտ գործողություն։")
    if method not in {"cash", "card"}:
        raise AppError("validation_error", "Անհայտ վճարման ձև։")
    if amount <= 0:
        raise AppError("validation_error", "Գումարը պետք է լինի զրոյից մեծ։")

    async with db.transaction() as conn:
        session = await sessions_repo.lock_open_for_store(conn, owner_id, store_id)
        if session is None:
            raise AppError(
                "validation_error",
                "Խանութը փակ է։ Գումար հանել կամ ավելացնել կարելի է միայն բաց հերթափոխի ընթացքում։",
            )
        # The CHECK constraints enforce the sign, so a UI bug cannot book a
        # positive withdrawal.
        signed = -amount if kind == "withdrawal" else amount
        await money_repo.insert_movement(
            conn,
            owner_id=owner_id,
            store_id=store_id,
            store_session_id=session["id"],
            method=method,
            kind=kind,
            amount=signed,
            note=note,
        )
    log.info("owner %s recorded %s of %s (%s) at store %s", owner_id, kind, amount, method, store_id)
