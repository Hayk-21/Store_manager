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
from app.repo import sessions as sessions_repo

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
    "delete_sale": "Վաճառքի ջնջում",
    "set_salary": "Աշխատավարձի փոփոխում",
    "delete_till_count": "Դրամարկղի հաշվարկի ջնջում",
    "set_movement_amount": "Գրառման գումարի փոփոխում",
}


# -- shared plumbing ---------------------------------------------------------

async def _resync_snapshot(conn, store_session_id: int) -> None:
    """Bring a closed session's snapshot back in line with its ledger.

    A pass-through kept for the name: it follows nearly every correction below, and
    ``_resync_snapshot(conn, …)`` reads as the step it is at ten call sites.
    """
    await sessions_repo.resync_snapshot(conn, store_session_id)


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


async def delete_sale(owner_id: int, user_id: int, sale_id: int) -> None:
    """Remove a sale from the books entirely.

    Voiding is the honest correction — it keeps the receipt and shows it was
    taken back, which is what a shop wants when a customer returned something.
    This is the other thing: a row that should never have been there at all, a
    duplicate or a test entry, and leaving it struck through forever only makes
    the report harder to read.

    Nothing is lost even so. The whole row, its lines and its ledger entries are
    written into the audit payload first, so putting it back is one undo.
    """
    async with db.transaction() as conn:
        sale = await conn.fetchrow(
            """
            SELECT id, store_id, worker_id, work_session_id, store_session_id,
                   payment_method, total, external_id, sold_at, voided_at,
                   voided_by_worker_id, void_reason, superseded_by_sale_id
              FROM sales WHERE id = $1 AND owner_id = $2 FOR UPDATE
            """,
            sale_id, owner_id,
        )
        if sale is None:
            raise AppError("not_found", "Վաճառքը չի գտնվել։")

        lines = await conn.fetch(
            """
            SELECT item_id, quantity, unit_price, unit_cost, line_total, price_kind
              FROM sale_items WHERE sale_id = $1 ORDER BY item_id
            """,
            sale_id,
        )
        movements = await conn.fetch(
            """
            SELECT method, kind, amount, work_session_id, worker_id, note, created_by
              FROM cash_movements WHERE sale_id = $1 ORDER BY id
            """,
            sale_id,
        )

        # Only put the goods back if they are still counted as sold. A voided
        # sale already returned them, and doing it twice would invent stock.
        if sale["voided_at"] is None:
            await _restore_stock(conn, sale_id)

        # sale_items and cash_movements cascade; anything pointing at this sale
        # as its replacement has that link set to NULL.
        await conn.execute("DELETE FROM sales WHERE id = $1 AND owner_id = $2", sale_id, owner_id)
        await _resync_snapshot(conn, sale["store_session_id"])
        await audit_repo.record(
            conn, owner_id, user_id, "delete_sale",
            f"Ջնջվեց վաճառք #{sale_id} — {Decimal(sale['total']):,.0f} ֏",
            store_session_id=sale["store_session_id"],
            payload={
                "sale": {
                    "store_id": sale["store_id"],
                    "worker_id": sale["worker_id"],
                    "work_session_id": sale["work_session_id"],
                    "store_session_id": sale["store_session_id"],
                    "payment_method": sale["payment_method"],
                    "total": str(sale["total"]),
                    "external_id": sale["external_id"],
                    "was_voided": sale["voided_at"] is not None,
                    "voided_by_worker_id": sale["voided_by_worker_id"],
                    "void_reason": sale["void_reason"],
                },
                "lines": [
                    {
                        "item_id": line["item_id"],
                        "quantity": line["quantity"],
                        "unit_price": str(line["unit_price"]),
                        "unit_cost": str(line["unit_cost"]),
                        "line_total": str(line["line_total"]),
                        "price_kind": line["price_kind"],
                    }
                    for line in lines
                ],
                "movements": [
                    {
                        "method": m["method"],
                        "kind": m["kind"],
                        "amount": str(m["amount"]),
                        "work_session_id": m["work_session_id"],
                        "worker_id": m["worker_id"],
                        "note": m["note"],
                        "created_by": m["created_by"],
                    }
                    for m in movements
                ],
            },
        )
    log.info("owner %s deleted sale %s outright", owner_id, sale_id)


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


async def set_movement_amount(
    owner_id: int, user_id: int, movement_id: int, amount: Decimal
) -> int:
    """Change what a hand-entered ledger row was for how much. Returns its session.

    Only the kinds an owner may create by hand. A 'sale' or 'salary' row is one half of
    something else — a receipt, a shift — and editing it alone would leave the two
    disagreeing forever; both have their own editors that move the pair together.

    A wrong figure here is a wrong figure in the drawer, and the only fix was deleting
    the row and typing it again, which loses the reason with it.
    """
    if amount <= ZERO:
        raise AppError("validation_error", "Գումարը պետք է մեծ լինի զրոյից։")

    async with db.transaction() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, kind, amount, note, store_session_id
              FROM cash_movements WHERE id = $1 AND owner_id = $2 FOR UPDATE
            """,
            movement_id, owner_id,
        )
        if row is None:
            raise AppError("not_found", "Գրառումը չի գտնվել։")
        if row["kind"] not in OWNER_KINDS:
            raise AppError(
                "validation_error",
                "Այս գրառումը վաճառքի կամ հերթափոխի մասն է և առանձին չի փոփոխվում։",
            )

        # Withdrawals are stored negative and deposits positive, and the form asks for
        # a plain amount. Keeping the sign from the row means the kind decides it.
        signed = -amount if Decimal(row["amount"]) < ZERO else amount
        await conn.execute(
            "UPDATE cash_movements SET amount = $2 WHERE id = $1", movement_id, signed
        )
        await _resync_snapshot(conn, row["store_session_id"])
        await audit_repo.record(
            conn, owner_id, user_id, "set_movement_amount",
            f"Գրառում՝ {Decimal(row['amount']):,.0f} → {signed:,.0f} ֏ — "
            f"{(row['note'] or '')[:60]}",
            store_session_id=row["store_session_id"],
            payload={
                "movement_id": movement_id,
                "previous": str(row["amount"]),
            },
        )
    log.info("owner %s set movement %s to %s", owner_id, movement_id, amount)
    return row["store_session_id"]


async def _revert_set_movement_amount(conn, owner_id: int, payload: dict) -> None:
    await conn.execute(
        "UPDATE cash_movements SET amount = $2 WHERE id = $1 AND owner_id = $3",
        payload["movement_id"], Decimal(payload["previous"]), owner_id,
    )


async def delete_movement(owner_id: int, user_id: int, movement_id: int) -> None:
    """Remove a ledger row.

    Every row can go, but not all of them through this door. A 'sale' or 'void'
    row is one half of a receipt: deleting it alone would leave the receipt
    describing money the ledger no longer holds, and the two would disagree
    forever. A 'salary' row is one half of a shift, and the same applies.

    So those two are refused *and told where to go instead* — delete the receipt,
    or set the wage to zero. Both of those exist, both remove the ledger row as
    part of doing the whole job, and both are undoable. Refusing without saying
    that would just look like the feature is missing.
    """
    async with db.transaction() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, kind, method, amount, note, created_by, store_session_id,
                   store_id, sale_id, work_session_id
              FROM cash_movements WHERE id = $1 AND owner_id = $2 FOR UPDATE
            """,
            movement_id, owner_id,
        )
        if row is None:
            raise AppError("not_found", "Գրառումը չի գտնվել։")
        if row["kind"] in {"sale", "void"}:
            raise AppError(
                "validation_error",
                f"Այս գրառումը #{row['sale_id']} վաճառքի մասն է։ "
                f"Ջնջեք հենց վաճառքը՝ «Չեկեր» բաժնում, և գրառումը կհեռանա նրա հետ։",
            )
        if row["kind"] == "salary":
            raise AppError(
                "validation_error",
                "Այս գրառումն աշխատավարձ է։ Հեռացնելու համար «Հերթափոխեր» "
                "բաժնում այդ հերթափոխի աշխատավարձը դարձրեք 0։",
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
                "created_by": row["created_by"],
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
    # The owner's figure is taken at face value, debt and all: they are saying what
    # this shift was paid, and if the till was short on the night they have settled
    # it by the time they are editing the row.
    await conn.execute(
        "UPDATE work_sessions SET salary_paid = $2, salary_unpaid = 0 WHERE id = $1",
        shift["id"], salary,
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

        handler = _REVERTERS.get(event["action"])
        # A dead undo button rather than a 500. The page offers the newest event
        # whatever it is, so an action added without a reverter used to crash here —
        # and this dispatcher is the one place that knows the difference.
        if handler is None:
            raise AppError(
                "validation_error",
                "Այս գործողությունը հետ շրջել հնարավոր չէ։",
            )
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


async def _revert_delete_sale(conn, owner_id: int, payload: dict) -> None:
    """Put a deleted sale back, with its lines and its ledger entries.

    A new id — the old one is gone for good — but the same money, the same
    goods and the same attribution. The external_id comes back too, so the
    idempotency key that first created it still resolves here.
    """
    sale = payload["sale"]

    # Take the stock again first: if it has been sold since, saying so beats
    # restoring a sale the shelf can no longer account for.
    if not sale["was_voided"]:
        for line in sorted(payload["lines"], key=lambda entry: entry["item_id"]):
            taken = await conn.fetchval(
                """
                UPDATE items SET count = count - $3, updated_at = now()
                 WHERE id = $1 AND owner_id = $2 AND count >= $3
                RETURNING count
                """,
                line["item_id"], owner_id, line["quantity"],
            )
            if taken is None:
                name = await conn.fetchval(
                    "SELECT name FROM items WHERE id = $1", line["item_id"]
                )
                raise AppError(
                    "validation_error",
                    f"Հնարավոր չէ վերականգնել՝ «{name}»-ից պահեստում բավարար քանակ չկա։",
                )

    sale_id = await sales_repo.insert_sale(
        conn,
        owner_id=owner_id,
        store_id=sale["store_id"],
        worker_id=sale["worker_id"],
        work_session_id=sale["work_session_id"],
        store_session_id=sale["store_session_id"],
        payment_method=sale["payment_method"],
        total=Decimal(sale["total"]),
        external_id=sale["external_id"],
    )
    await sales_repo.insert_lines(
        conn,
        owner_id,
        sale_id,
        [
            {
                "item_id": line["item_id"],
                "quantity": line["quantity"],
                "unit_price": Decimal(line["unit_price"]),
                "unit_cost": Decimal(line["unit_cost"]),
                "line_total": Decimal(line["line_total"]),
                "price_kind": line["price_kind"],
            }
            for line in payload["lines"]
        ],
    )
    if sale["was_voided"]:
        await conn.execute(
            """
            UPDATE sales SET voided_at = now(), voided_by_worker_id = $2, void_reason = $3
             WHERE id = $1
            """,
            sale_id, sale["voided_by_worker_id"], sale["void_reason"],
        )

    for movement in payload["movements"]:
        await money_repo.insert_movement(
            conn,
            owner_id=owner_id,
            store_id=sale["store_id"],
            store_session_id=sale["store_session_id"],
            method=movement["method"],
            kind=movement["kind"],
            amount=Decimal(movement["amount"]),
            sale_id=sale_id,
            work_session_id=movement["work_session_id"],
            worker_id=movement["worker_id"],
            note=movement["note"],
            created_by=movement["created_by"],
        )


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
        created_by=payload.get("created_by", "owner"),
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


async def _revert_delete_till_count(conn, owner_id: int, payload: dict) -> None:
    # Imported here: till imports nothing from this module, but keeping the reverter
    # beside its siblings means the dispatcher below stays the one list of them.
    from app.services import till as till_service

    await till_service.restore_count(conn, owner_id, payload)


# The one list of what can be undone. Below the handlers so every name resolves, and
# looked up rather than indexed — an action with no reverter is a dead undo button,
# which is bad, but a 500 is worse.
_REVERTERS = {
    "void_sale": _revert_void,
    "amend_sale": _revert_amend,
    "add_sale": _revert_add_sale,
    "add_movement": _revert_add_movement,
    "delete_movement": _revert_delete_movement,
    "delete_sale": _revert_delete_sale,
    "set_salary": _revert_set_salary,
    "delete_till_count": _revert_delete_till_count,
    "set_movement_amount": _revert_set_movement_amount,
}
