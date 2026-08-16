"""Owner-initiated money movements.

The bot moves money by selling; this is the other way in — the owner taking the
takings out of the till, or putting a float in.
"""

from __future__ import annotations

import logging
from decimal import Decimal

import asyncpg

from app.db import db
from app.errors import AppError, BotError
from app.repo import money as money_repo
from app.repo import sessions as sessions_repo

log = logging.getLogger("storemanager.money")

KINDS = {"withdrawal", "deposit"}

# The most a cashier may take from the till across one shift — not per tap, or it
# would not be a limit: you take 1,000 four times. Small on purpose: this is for a
# delivery driver or a box of bags, not for banking the takings. Anything larger
# is the owner's decision, made on the website where they can see the whole shop
# rather than at a counter with a customer waiting.
WORKER_WITHDRAWAL_LIMIT = Decimal("1000.00")

# Why the money was taken, as a closed list rather than something typed.
#
# The reason now decides the ceiling, and a ceiling may not hang off free text:
# «ճաշ», «Ճաշի համար» and «ճաշ 🙂» are one thing to a person and three different
# things to a rule. So the bot sends a code and the note written on the row is
# chosen here — one spelling, the same on the report, the same to the sum below.
#
# A limit of ``None`` means the shift allowance does not apply. Paying Haypost for
# a parcel is not petty cash: the amount is whatever the courier charges, and it
# is the shop settling a bill rather than a cashier dipping into the drawer. What
# still applies to both is the drawer itself — you cannot take out notes that are
# not in it.
REASON_LUNCH = "lunch"
REASON_DELIVERY = "delivery"
WITHDRAWAL_REASONS: dict[str, tuple[str, Decimal | None]] = {
    REASON_LUNCH: ("Ճաշ", WORKER_WITHDRAWAL_LIMIT),
    REASON_DELIVERY: ("Հայփոստ (առաքման վճար)", None),
}

# The notes that do not eat the allowance, so that taking 6,000 for a parcel at
# noon does not leave the cashier unable to buy lunch.
UNCAPPED_NOTES = tuple(
    note for note, limit in WITHDRAWAL_REASONS.values() if limit is None
)


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
    if not (note or "").strip():
        # The schema has demanded this of every owner-typed row since 018
        # (``typed_movements_explain_themselves``); this one only ever got away
        # without it by labelling itself 'system'. It is the same rule the cashier's
        # own withdrawals follow, for the same reason: an amount with no reason *is*
        # the shortfall, just with a number attached.
        raise AppError("validation_error", "Գրեք, թե ինչի համար է այս գումարը։")
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
            # Typed on the website, which is what 'owner' means — the column's own
            # comment says so, and ``corrections.add_movement`` already labelled its
            # rows that way. This one defaulted to 'system' and so was
            # indistinguishable from the float the shop carries in each morning,
            # which is the one deposit nobody typed.
            created_by="owner",
        )
    log.info("owner %s recorded %s of %s (%s) at store %s", owner_id, kind, amount, method, store_id)


def _reason(reason: str | None, purpose: str) -> tuple[str, Decimal | None]:
    """What to write on the row, and what ceiling it answers to.

    An unrecognised code — or none at all — is an older bot, which asked the
    cashier to type the reason and knew nothing about categories. It keeps the
    typed text and the original allowance: when the two services deploy
    separately, the safe direction to err in is the one with a limit.
    """
    if reason in WITHDRAWAL_REASONS:
        return WITHDRAWAL_REASONS[reason]
    return (purpose or "").strip()[:300], WORKER_WITHDRAWAL_LIMIT


async def withdraw_by_worker(
    worker, amount: Decimal, purpose: str, idem_key: str, reason: str | None = None
) -> dict:
    """A cashier taking cash out of the till, from the bot.

    It happens whether or not the system knows — paying a delivery, buying bags,
    sending somebody for change — so the choice is between an unexplained
    shortfall at close and a row saying where the money went. The purpose is
    required for exactly that reason: an amount with no reason *is* the
    shortfall, just with a number attached.

    ``reason`` is the category the cashier picked, and it is what decides whether
    the shift allowance applies at all — see ``WITHDRAWAL_REASONS``.

    Cash only. A worker cannot take money off a card.
    """
    note, limit = _reason(reason, purpose)
    if amount <= 0:
        raise BotError("validation_error", "Գումարը պետք է լինի զրոյից մեծ։")
    if limit is not None and amount > limit:
        raise BotError(
            "validation_error",
            f"Հերթափոխի ընթացքում կարելի է վերցնել առավելագույնը "
            f"{limit:,.0f} ֏։ Ավելիի համար դիմեք ղեկավարին։",
            details={"limit": str(limit)},
        )
    if not note:
        raise BotError("validation_error", "Գրեք, թե ինչի համար եք վերցնում։")

    replay = await money_repo.by_external_id(worker.owner_id, idem_key)
    if replay is not None:
        return _withdrawal_payload(replay, duplicate=True)

    try:
        async with db.transaction() as conn:
            shift = await sessions_repo.lock_open_for_worker(conn, worker.id)
            if shift is None:
                raise BotError("no_open_session")

            # The drawer is shared, and the check below reads its total. Locking
            # only the calling worker's own shift row serialises that worker's own
            # double-taps but not two different cashiers on the same store session
            # — both could read the same "5,000 in the till" and both approve a
            # 4,000 withdrawal against it, overdrawing the till by 3,000 with
            # neither request individually over any limit. Locking the session
            # itself makes the second withdrawal wait for the first to commit and
            # then read what is actually left.
            await sessions_repo.lock_open_for_store(conn, worker.owner_id, shift["store_id"])

            # The cap is on the shift, not on the tap. A per-withdrawal limit is
            # not a limit at all — you take 1,000 four times. The shift row is
            # locked above, so this worker's withdrawals are serialised against
            # each other and two taps cannot both read the same remainder.
            #
            # Skipped entirely for a reason that has no ceiling, and the sum it
            # compares against leaves those rows out too: a courier's fee is not
            # spent lunch money.
            if limit is not None:
                already = Decimal(
                    await money_repo.withdrawn_by_worker_on(
                        conn, shift["id"], uncapped_notes=UNCAPPED_NOTES
                    )
                )
                remaining = limit - already
                if amount > remaining:
                    raise BotError(
                        "validation_error",
                        f"Այս հերթափոխին արդեն վերցվել է {already:,.0f} ֏։ "
                        f"Կարող եք վերցնել ևս {max(remaining, Decimal(0)):,.0f} ֏։",
                        details={"limit": str(limit), "already": str(already)},
                    )

            totals = await money_repo.totals_on(conn, shift["store_session_id"])
            available = Decimal(totals["cash"])
            if amount > available:
                # Not a rule about permission — a rule about physics. You cannot
                # take out more notes than are in the drawer, so a number bigger
                # than the till is a typo.
                raise BotError(
                    "validation_error",
                    f"Դրամարկղում կա {available:,.0f} ֏։ Ավելին հանել հնարավոր չէ։",
                )

            movement_id = await money_repo.insert_movement(
                conn,
                owner_id=worker.owner_id,
                store_id=shift["store_id"],
                store_session_id=shift["store_session_id"],
                method="cash",
                kind="withdrawal",
                amount=-amount,
                work_session_id=shift["id"],
                worker_id=worker.id,
                note=note,
                created_by="worker",
                external_id=idem_key,
            )
            after = await money_repo.totals_on(conn, shift["store_session_id"])
    except asyncpg.exceptions.UniqueViolationError:
        original = await money_repo.by_external_id(worker.owner_id, idem_key)
        if original is None:  # pragma: no cover - some other constraint
            raise
        return _withdrawal_payload(original, duplicate=True)

    log.info(
        "worker %s took %s from the till of store %s: %s",
        worker.id, amount, shift["store_id"], note[:40],
    )
    return {
        "ok": True,
        "duplicate": False,
        "withdrawal": {
            "id": movement_id,
            "amount": f"{amount:.2f}",
            "purpose": note,
        },
        "store_totals": {"cash": f"{after['cash']:.2f}", "card": f"{after['card']:.2f}"},
    }


def _withdrawal_payload(row, *, duplicate: bool) -> dict:
    return {
        "ok": True,
        "duplicate": duplicate,
        "withdrawal": {
            "id": row["id"],
            "amount": f"{abs(Decimal(row['amount'])):.2f}",
            "purpose": row["note"] or "",
        },
        "store_totals": {"cash": f"{row['cash']:.2f}", "card": f"{row['card']:.2f}"},
    }
