"""Fixing the books after the fact, and undoing the fixes.

The rule everything here follows: **a completed sale is never edited in place.**

Editing a quantity in place would mean unpicking one stock movement and one
ledger entry and rewriting both consistently, for every field, in every
direction. Get it subtly wrong and the accounts are corrupt without anything
looking wrong. So a correction voids the original — reusing the reversal that
already works — and records a replacement beside it, linked. The books read
"this was recorded, then replaced by that", which is what actually happened, and
there is one piece of reversal logic to keep right instead of one per field.

Because nothing is ever destroyed, every correction is reversible. Each one
writes an ``audit_events`` row inside its own transaction, carrying the few ids
an undo needs. Undo works newest-first: rewinding to a moment is undoing back to
it, one step at a time, each step safe on its own.

Closing a store session snapshots its totals, so any correction to a closed
session recomputes that snapshot from the ledger. The ledger is the source of
truth; the snapshot is a convenience.
"""

from __future__ import annotations

import logging
from decimal import Decimal

from app.db import db
from app.errors import AppError
from app.pricing import resolve_price
from app.repo import audit as audit_repo
from app.repo import money as money_repo
from app.repo import sales as sales_repo

log = logging.getLogger("storemanager.corrections")

CENT = Decimal("0.01")
ZERO = Decimal("0.00")

# Kinds an owner may create by hand. 'sale', 'void' and 'salary' are records of
# something the system did and are never typed in.
OWNER_KINDS = {"withdrawal", "deposit", "adjustment"}

ACTION_LABELS = {
    "void_sale": "Վաճառքի չեղարկում",
    "amend_sale": "Վաճառքի ուղղում",
    "add_sale": "Ավելացված վաճառք",
    "add_movement": "Դրամարկղի գրառում",
    "delete_movement": "Գրառման ջնջում",
    "set_salary": "Աշխատավարձի փոփոխում",
}


# -- shared plumbing ---------------------------------------------------------

async def _resync_snapshot(conn, store_session_id: int) -> None:
    """Bring a closed session's snapshot back in line with its ledger.

    A correction to an open session needs nothing: its totals are read live. A
    closed one has frozen numbers on the row, and leaving those stale would mean
    the report and the ledger disagreed.
    """
    session = await conn.fetchrow(
        "SELECT closed_at FROM store_sessions WHERE id = $1", store_session_id
    )
    if session is None or session["closed_at"] is None:
        return
    totals = await money_repo.totals_on(conn, store_session_id)
    await conn.execute(
        """
        UPDATE store_sessions
           SET cash_at_close = $2, card_at_close = $3, salaries_at_close = $4
         WHERE id = $1
        """,
        store_session_id, totals["cash"], totals["card"], totals["salaries"],
    )


async def _owned_sale(conn, owner_id: int, sale_id: int):
    row = await conn.fetchrow(
        """
        SELECT id, store_id, worker_id, work_session_id, store_session_id,
               payment_method, total, voided_at, superseded_by_sale_id
          FROM sales WHERE id = $1 AND owner_id = $2 FOR UPDATE
        """,
        sale_id, owner_id,
    )
    if row is None:
        raise AppError("not_found", "Վաճառքը չի գտնվել։")
    return row


async def _restore_stock(conn, sale_id: int) -> list:
    """Put the goods back, ascending item order like every other stock path so
    two corrections at once queue rather than deadlock."""
    lines = await sales_repo.lines_for_void(conn, sale_id)
    for line in lines:
        await conn.execute(
            "UPDATE items SET count = count + $2, updated_at = now() WHERE id = $1",
            line["item_id"], line["quantity"],
        )
    return lines


async def _take_stock(conn, owner_id: int, store_id: int, lines: list[dict]) -> list[dict]:
    """Apply a basket, refusing rather than letting a count go negative.

    A line may name a ``price_kind`` instead of a price: 'wholesale' takes the
    item's wholesale price, 'retail' its shelf price. Resolving it here rather
    than in the route means the owner's form and the cashier's bot cannot end up
    charging different amounts for the same choice of words.
    """
    applied = []
    for line in sorted(lines, key=lambda entry: entry["item_id"]):
        row = await conn.fetchrow(
            """
            UPDATE items SET count = count - $4, updated_at = now()
             WHERE id = $1 AND owner_id = $2 AND store_id = $3
               AND is_active AND count >= $4
            RETURNING name, sell_price, wholesale_price, self_price
            """,
            line["item_id"], owner_id, store_id, line["quantity"],
        )
        if row is None:
            item = await conn.fetchrow(
                "SELECT name, count FROM items WHERE id = $1 AND owner_id = $2",
                line["item_id"], owner_id,
            )
            if item is None:
                raise AppError("not_found", "Ապրանքը չի գտնվել։")
            raise AppError(
                "validation_error",
                f"«{item['name']}» — պահեստում կա ընդամենը {item['count']} հատ։",
            )
        unit_price, kind = resolve_price(row, line.get("unit_price"), line.get("price_kind"))
        applied.append({
            "item_id": line["item_id"],
            "quantity": line["quantity"],
            "unit_price": unit_price,
            "unit_cost": Decimal(row["self_price"]),
            "line_total": (unit_price * line["quantity"]).quantize(CENT),
            "price_kind": kind,
        })
    return applied



async def _retake_stock_for(conn, owner_id: int, sale_id: int, store_id: int) -> None:
    """Take a voided sale's goods back off the shelf, when un-voiding it.

    Can genuinely fail: the stock may have been sold again since. Saying so is
    better than letting a count go negative.
    """
    for line in await sales_repo.lines_for_void(conn, sale_id):
        taken = await conn.fetchval(
            """
            UPDATE items SET count = count - $3, updated_at = now()
             WHERE id = $1 AND owner_id = $2 AND count >= $3
            RETURNING count
            """,
            line["item_id"], owner_id, line["quantity"],
        )
        if taken is None:
            name = await conn.fetchval("SELECT name FROM items WHERE id = $1", line["item_id"])
            raise AppError(
                "validation_error",
                f"Հնարավոր չէ վերականգնել՝ «{name}»-ից պահեստում բավարար քանակ չկա։",
            )


# -- voiding -----------------------------------------------------------------

async def void_sale(
    owner_id: int, user_id: int, sale_id: int, reason: str | None = None
) -> None:
    """Undo a sale: stock back, money reversed, row kept and marked.

    Kept rather than deleted, because a receipt that vanishes is indistinguish-
    able from one that never existed.
    """
    async with db.transaction() as conn:
        sale = await _owned_sale(conn, owner_id, sale_id)
        if sale["voided_at"] is not None:
            raise AppError("validation_error", "Այս վաճառքն արդեն չեղարկված է։")

        await _restore_stock(conn, sale_id)
        await sales_repo.mark_voided(conn, sale_id, sale["worker_id"], reason)
        await money_repo.insert_movement(
            conn,
            owner_id=owner_id, store_id=sale["store_id"],
            store_session_id=sale["store_session_id"],
            method=sale["payment_method"], kind="void",
            amount=-Decimal(sale["total"]), sale_id=sale_id,
            work_session_id=sale["work_session_id"], worker_id=sale["worker_id"],
            note=reason, created_by="owner",
        )
        await _resync_snapshot(conn, sale["store_session_id"])
        await audit_repo.record(
            conn, owner_id, user_id, "void_sale",
            f"Չեղարկվեց վաճառք #{sale_id} — {Decimal(sale['total']):,.0f} ֏",
            store_session_id=sale["store_session_id"],
            payload={"sale_id": sale_id},
        )
    log.info("owner %s voided sale %s", owner_id, sale_id)


# -- amending ----------------------------------------------------------------

async def amend_sale(
    owner_id: int,
    user_id: int,
    sale_id: int,
    lines: list[dict],
    payment_method: str,
    reason: str | None = None,
) -> int:
    """Replace a sale with a corrected one, in a single transaction."""
    if not lines:
        raise AppError("validation_error", "Ապրանք ընտրված չէ։")
    if payment_method not in {"cash", "card"}:
        raise AppError("validation_error", "Անհայտ վճարման ձև։")

    async with db.transaction() as conn:
        sale = await _owned_sale(conn, owner_id, sale_id)
        if sale["voided_at"] is not None:
            raise AppError(
                "validation_error",
                "Այս վաճառքը չեղարկված է։ Ուղղելու փոխարեն ավելացրեք նորը։",
            )

        # Undo first, so the corrected basket is checked against stock that has
        # the original's goods back on the shelf. Otherwise raising a quantity
        # from 1 to 2 could fail for want of stock the sale itself was holding.
        await _restore_stock(conn, sale_id)
        await money_repo.insert_movement(
            conn,
            owner_id=owner_id, store_id=sale["store_id"],
            store_session_id=sale["store_session_id"],
            method=sale["payment_method"], kind="void",
            amount=-Decimal(sale["total"]), sale_id=sale_id,
            work_session_id=sale["work_session_id"], worker_id=sale["worker_id"],
            note=reason, created_by="owner",
        )

        applied = await _take_stock(conn, owner_id, sale["store_id"], lines)
        total = sum((line["line_total"] for line in applied), ZERO)
        suffix = await conn.fetchval(
            "SELECT count(*) FROM sales WHERE external_id LIKE $1", f"amend-{sale_id}-%"
        )

        replacement = await sales_repo.insert_sale(
            conn,
            owner_id=owner_id, store_id=sale["store_id"], worker_id=sale["worker_id"],
            work_session_id=sale["work_session_id"],
            store_session_id=sale["store_session_id"],
            payment_method=payment_method, total=total,
            external_id=f"amend-{sale_id}-{suffix}",
        )
        await sales_repo.insert_lines(conn, owner_id, replacement, applied)
        await money_repo.insert_movement(
            conn,
            owner_id=owner_id, store_id=sale["store_id"],
            store_session_id=sale["store_session_id"],
            method=payment_method, kind="sale", amount=total, sale_id=replacement,
            work_session_id=sale["work_session_id"], worker_id=sale["worker_id"],
            note=reason, created_by="owner",
        )
        await conn.execute(
            """
            UPDATE sales SET voided_at = now(), voided_by_worker_id = $2,
                             void_reason = $3, superseded_by_sale_id = $4
             WHERE id = $1
            """,
            sale_id, sale["worker_id"], reason, replacement,
        )
        await _resync_snapshot(conn, sale["store_session_id"])
        await audit_repo.record(
            conn, owner_id, user_id, "amend_sale",
            f"Ուղղվեց վաճառք #{sale_id}՝ {Decimal(sale['total']):,.0f} → {total:,.0f} ֏",
            store_session_id=sale["store_session_id"],
            payload={"original": sale_id, "replacement": replacement},
        )
    log.info("owner %s amended sale %s into %s", owner_id, sale_id, replacement)
    return replacement


# -- adding what was missed --------------------------------------------------

async def add_sale(
    owner_id: int,
    user_id: int,
    store_session_id: int,
    worker_id: int,
    lines: list[dict],
    payment_method: str,
    note: str | None = None,
) -> int:
    """Record a sale the write-up left out.

    Attached to a shift of that session: every sale belongs to somebody, and an
    unattributed one would break the per-worker figures.
    """
    if not lines:
        raise AppError("validation_error", "Ապրանք ընտրված չէ։")
    if payment_method not in {"cash", "card"}:
        raise AppError("validation_error", "Անհայտ վճարման ձև։")

    async with db.transaction() as conn:
        session = await conn.fetchrow(
            "SELECT id, store_id FROM store_sessions WHERE id = $1 AND owner_id = $2 FOR UPDATE",
            store_session_id, owner_id,
        )
        if session is None:
            raise AppError("not_found", "Հերթափոխը չի գտնվել։")
        shift = await conn.fetchrow(
            """
            SELECT id FROM work_sessions
             WHERE store_session_id = $1 AND worker_id = $2 AND owner_id = $3
            """,
            store_session_id, worker_id, owner_id,
        )
        if shift is None:
            raise AppError("validation_error", "Այս աշխատողն այդ հերթափոխին չի աշխատել։")

        applied = await _take_stock(conn, owner_id, session["store_id"], lines)
        total = sum((line["line_total"] for line in applied), ZERO)
        suffix = await conn.fetchval(
            "SELECT count(*) FROM sales WHERE external_id LIKE $1",
            f"added-{store_session_id}-%",
        )

        sale_id = await sales_repo.insert_sale(
            conn,
            owner_id=owner_id, store_id=session["store_id"], worker_id=worker_id,
            work_session_id=shift["id"], store_session_id=store_session_id,
            payment_method=payment_method, total=total,
            external_id=f"added-{store_session_id}-{suffix}",
        )
        await sales_repo.insert_lines(conn, owner_id, sale_id, applied)
        await money_repo.insert_movement(
            conn,
            owner_id=owner_id, store_id=session["store_id"],
            store_session_id=store_session_id, method=payment_method, kind="sale",
            amount=total, sale_id=sale_id, work_session_id=shift["id"],
            worker_id=worker_id, note=note, created_by="owner",
        )
        await _resync_snapshot(conn, store_session_id)
        await audit_repo.record(
            conn, owner_id, user_id, "add_sale",
            f"Ավելացվեց վաճառք #{sale_id} — {total:,.0f} ֏",
            store_session_id=store_session_id,
            payload={"sale_id": sale_id},
        )
    log.info("owner %s added sale %s to session %s", owner_id, sale_id, store_session_id)
    return sale_id


# -- money that is not a sale ------------------------------------------------

async def add_movement(
    owner_id: int,
    user_id: int,
    store_session_id: int,
    kind: str,
    method: str,
    amount: Decimal,
    purpose: str,
) -> int:
    """Money in or out of a session's till that was not a sale or a salary.

    The purpose is required and is the point of the entry: "paid the influencer"
    cannot be deduced from an amount and a direction.
    """
    if kind not in OWNER_KINDS:
        raise AppError("validation_error", "Անհայտ գործողություն։")
    if method not in {"cash", "card"}:
        raise AppError("validation_error", "Անհայտ վճարման ձև։")
    if amount <= ZERO:
        raise AppError("validation_error", "Գումարը պետք է լինի զրոյից մեծ։")
    if not (purpose or "").strip():
        raise AppError("validation_error", "Գրեք նպատակը։")

    signed = -amount if kind == "withdrawal" else amount

    async with db.transaction() as conn:
        session = await conn.fetchrow(
            "SELECT id, store_id FROM store_sessions WHERE id = $1 AND owner_id = $2 FOR UPDATE",
            store_session_id, owner_id,
        )
        if session is None:
            raise AppError("not_found", "Հերթափոխը չի գտնվել։")

        movement_id = await money_repo.insert_movement(
            conn,
            owner_id=owner_id, store_id=session["store_id"],
            store_session_id=store_session_id, method=method, kind=kind,
            amount=signed, note=purpose.strip(), created_by="owner",
        )
        await _resync_snapshot(conn, store_session_id)
        await audit_repo.record(
            conn, owner_id, user_id, "add_movement",
            f"{ACTION_LABELS['add_movement']}՝ {signed:,.0f} ֏ — {purpose.strip()[:60]}",
            store_session_id=store_session_id,
            payload={"movement_id": movement_id},
        )
    log.info("owner %s recorded a %s of %s: %s", owner_id, kind, amount, purpose[:40])
    return movement_id


async def delete_movement(owner_id: int, user_id: int, movement_id: int) -> None:
    """Remove an owner-entered movement.

    Only those. A 'sale', 'void' or 'salary' row is the record of something that
    happened, and deleting it would leave the sale or shift it belongs to
    describing money no longer in the ledger.
    """
    async with db.transaction() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, kind, method, amount, note, created_by, store_session_id, store_id
              FROM cash_movements WHERE id = $1 AND owner_id = $2 FOR UPDATE
            """,
            movement_id, owner_id,
        )
        if row is None:
            raise AppError("not_found", "Գրառումը չի գտնվել։")
        if row["created_by"] != "owner" or row["kind"] not in OWNER_KINDS:
            raise AppError(
                "validation_error",
                "Այս գրառումը վաճառքի կամ աշխատավարձի արդյունք է։ "
                "Ջնջելու փոխարեն ուղղեք համապատասխան վաճառքը։",
            )

        await conn.execute("DELETE FROM cash_movements WHERE id = $1", movement_id)
        await _resync_snapshot(conn, row["store_session_id"])
        await audit_repo.record(
            conn, owner_id, user_id, "delete_movement",
            f"Ջնջվեց գրառում՝ {Decimal(row['amount']):,.0f} ֏ — {(row['note'] or '')[:60]}",
            store_session_id=row["store_session_id"],
            payload={
                "store_session_id": row["store_session_id"],
                "store_id": row["store_id"],
                "kind": row["kind"],
                "method": row["method"],
                "amount": str(row["amount"]),
                "note": row["note"],
            },
        )
    log.info("owner %s deleted movement %s", owner_id, movement_id)


# -- the shift itself --------------------------------------------------------

async def set_salary(
    owner_id: int, user_id: int, work_session_id: int, salary: Decimal
) -> None:
    """Change what a finished shift was paid.

    The snapshot on the shift row and the ledger entry move together, or the
    report and the till disagree.
    """
    if salary < ZERO:
        raise AppError("validation_error", "Աշխատավարձը չի կարող բացասական լինել։")

    async with db.transaction() as conn:
        shift = await conn.fetchrow(
            """
            SELECT id, store_id, store_session_id, worker_id, ended_at, salary_paid
              FROM work_sessions WHERE id = $1 AND owner_id = $2 FOR UPDATE
            """,
            work_session_id, owner_id,
        )
        if shift is None:
            raise AppError("not_found", "Հերթափոխը չի գտնվել։")
        if shift["ended_at"] is None:
            raise AppError("validation_error", "Հերթափոխը դեռ բաց է։")

        previous = Decimal(shift["salary_paid"] or 0)
        await _replace_salary(conn, owner_id, shift, salary)
        await _resync_snapshot(conn, shift["store_session_id"])
        await audit_repo.record(
            conn, owner_id, user_id, "set_salary",
            f"Աշխատավարձ՝ {previous:,.0f} → {salary:,.0f} ֏",
            store_session_id=shift["store_session_id"],
            payload={"work_session_id": work_session_id, "previous": str(previous)},
        )
    log.info("owner %s set salary of shift %s to %s", owner_id, work_session_id, salary)


async def _replace_salary(conn, owner_id: int, shift, salary: Decimal) -> None:
    await conn.execute(
        "UPDATE work_sessions SET salary_paid = $2 WHERE id = $1", shift["id"], salary
    )
    # one_salary_per_work_session means there is at most one to replace.
    await conn.execute(
        "DELETE FROM cash_movements WHERE work_session_id = $1 AND kind = 'salary'",
        shift["id"],
    )
    if salary > ZERO:
        await money_repo.insert_movement(
            conn,
            owner_id=owner_id, store_id=shift["store_id"],
            store_session_id=shift["store_session_id"], method="cash", kind="salary",
            amount=-salary, work_session_id=shift["id"], worker_id=shift["worker_id"],
            created_by="owner",
        )


# -- undo --------------------------------------------------------------------

async def revert(owner_id: int, user_id: int, event_id: int) -> str:
    """Undo one correction, putting the books back as they were before it.

    Newest-first, and it says so rather than guessing: undoing an amendment
    whose replacement a later correction has already replaced would leave the
    chain pointing at rows that no longer mean what the link says. Rewinding to
    a moment is undoing back to it, one safe step at a time.
    """
    async with db.transaction() as conn:
        event = await audit_repo.lock_for_revert(conn, owner_id, event_id)
        if event is None:
            raise AppError("not_found", "Գրառումը չի գտնվել։")
        if event["reverted_at"] is not None:
            raise AppError("validation_error", "Այս գործողությունն արդեն հետ է շրջվել։")

        newer = await audit_repo.newer_pending_count(conn, owner_id, event_id)
        if newer:
            raise AppError(
                "validation_error",
                f"Նախ հետ շրջեք ավելի ուշ կատարված {newer} փոփոխությունը։",
            )

        payload = event["payload"]
        if isinstance(payload, str):
            import json

            payload = json.loads(payload)

        handler = {
            "void_sale": _revert_void,
            "amend_sale": _revert_amend,
            "add_sale": _revert_add_sale,
            "add_movement": _revert_add_movement,
            "delete_movement": _revert_delete_movement,
            "set_salary": _revert_set_salary,
        }[event["action"]]
        await handler(conn, owner_id, payload)

        if event["store_session_id"] is not None:
            await _resync_snapshot(conn, event["store_session_id"])
        await audit_repo.mark_reverted(conn, event_id, user_id)

    log.info("owner %s reverted event %s (%s)", owner_id, event_id, event["action"])
    return event["summary"]


async def _revert_void(conn, owner_id: int, payload: dict) -> None:
    """Un-void: take the goods back off the shelf and drop the reversal."""
    sale = await _owned_sale(conn, owner_id, payload["sale_id"])
    await _retake_stock_for(conn, owner_id, sale["id"], sale["store_id"])
    await conn.execute(
        """
        UPDATE sales SET voided_at = NULL, voided_by_worker_id = NULL,
                         void_reason = NULL, superseded_by_sale_id = NULL
         WHERE id = $1
        """,
        sale["id"],
    )
    await conn.execute(
        "DELETE FROM cash_movements WHERE sale_id = $1 AND kind = 'void'", sale["id"]
    )


async def _revert_amend(conn, owner_id: int, payload: dict) -> None:
    """Throw the replacement away and bring the original back."""
    replacement = payload["replacement"]
    original = payload["original"]

    # The replacement never should have existed, so it goes entirely.
    await _restore_stock(conn, replacement)
    await conn.execute(
        "DELETE FROM sales WHERE id = $1 AND owner_id = $2", replacement, owner_id
    )
    # sale_items and the ledger row cascade from the sale.
    await _revert_void(conn, owner_id, {"sale_id": original})


async def _revert_add_sale(conn, owner_id: int, payload: dict) -> None:
    sale_id = payload["sale_id"]
    await _owned_sale(conn, owner_id, sale_id)
    await _restore_stock(conn, sale_id)
    await conn.execute("DELETE FROM sales WHERE id = $1 AND owner_id = $2", sale_id, owner_id)


async def _revert_add_movement(conn, owner_id: int, payload: dict) -> None:
    await conn.execute(
        "DELETE FROM cash_movements WHERE id = $1 AND owner_id = $2",
        payload["movement_id"], owner_id,
    )


async def _revert_delete_movement(conn, owner_id: int, payload: dict) -> None:
    """Put a deleted movement back. A new id, but the same money and reason."""
    await money_repo.insert_movement(
        conn,
        owner_id=owner_id,
        store_id=payload["store_id"],
        store_session_id=payload["store_session_id"],
        method=payload["method"],
        kind=payload["kind"],
        amount=Decimal(payload["amount"]),
        note=payload.get("note"),
        created_by="owner",
    )


async def _revert_set_salary(conn, owner_id: int, payload: dict) -> None:
    shift = await conn.fetchrow(
        """
        SELECT id, store_id, store_session_id, worker_id
          FROM work_sessions WHERE id = $1 AND owner_id = $2 FOR UPDATE
        """,
        payload["work_session_id"], owner_id,
    )
    if shift is None:
        raise AppError("not_found", "Հերթափոխը չի գտնվել։")
    await _replace_salary(conn, owner_id, shift, Decimal(payload["previous"]))
