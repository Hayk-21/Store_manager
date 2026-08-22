"""Cash carried from one of an owner's shops to another.

One shop runs out of change while a sister shop is sitting on the day's takings,
so somebody puts an envelope in a taxi. Recording that as a withdrawal at one end
and nothing at the other told the books two half-truths: money left a drawer for
no stated reason, and arrived in a drawer that had no idea it was coming — the
receiving shop's count came up over, and the only explanation was a phone call
nobody wrote down.

**The two ends are not booked at the same moment, and that is the whole design.**
The cash really does leave the sender's drawer when they hand it over, so their
withdrawal is written at once — otherwise their own count tonight is short by the
amount they no longer have. It must not appear in the receiving till until it is
physically there, or the person counting *that* drawer is being held to money
still in somebody's pocket. So the deposit waits for a worker at the destination
to say it arrived.

A rejection is the envelope that did not turn up. The money goes back into the
sending shop's drawer, because nothing was spent and nothing should have left the
books; that needs the sending shop to still be open, and the refusal says so when
it is not.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from html import escape

import asyncpg

from app import texts
from app.db import db
from app.errors import BotError
from app.repo import money as money_repo
from app.repo import money_transfers as transfers_repo
from app.repo import sessions as sessions_repo
from app.repo import workers as workers_repo
from app.services import telegram

log = logging.getLogger("storemanager.money_transfers")

ZERO = Decimal("0.00")

# The same ceiling every other typed amount answers to. A drawer holding more than
# this is a mistyped figure, not a shop.
MAX_AMOUNT = Decimal("10000000.00")


def note_sent(store_name: str) -> str:
    """What the sending shop's ledger row says.

    The wording matters beyond being readable: ``money.UNCAPPED_NOTES`` recognises
    it by its opening, which is what keeps a transfer from eating the cashier's
    lunch allowance. It is not petty cash — it is the drawer moving.
    """
    return f"Փոխանցվեց «{store_name}» խանութ"


def note_received(store_name: str) -> str:
    return f"Ստացվեց «{store_name}» խանութից"


def note_returned(store_name: str) -> str:
    return f"Վերադարձվեց «{store_name}» խանութից"


async def send_by_worker(
    worker, to_store_id: int, amount: Decimal, idem_key: str
) -> dict:
    """Hand cash to another shop. The sender's till drops now; the other rises later.

    The amount is capped by the drawer and by nothing else. It is not petty cash
    and answers to no shift allowance — a cashier sending the day's takings to the
    shop that needs them is moving the owner's money between the owner's own
    tills, not spending it.
    """
    if amount <= ZERO or amount > MAX_AMOUNT:
        raise BotError("validation_error", "Սխալ գումար։")

    replay = await transfers_repo.by_external_id(worker.owner_id, idem_key)
    if replay is not None:
        return _payload(replay, duplicate=True)

    try:
        async with db.transaction() as conn:
            shift = await sessions_repo.lock_open_for_worker(conn, worker.id)
            if shift is None:
                raise BotError("no_open_session")
            if to_store_id == shift["store_id"]:
                raise BotError("validation_error", "Ընտրեք այլ խանութ։")

            # The drawer is shared. Locking only this worker's shift row would
            # serialise their own double-taps and nothing else, so two cashiers
            # could both read the same 5,000 and both send 4,000 of it.
            await sessions_repo.lock_open_for_store(
                conn, worker.owner_id, shift["store_id"]
            )

            # Read, not locked. Whether the destination is open is a courtesy
            # check answered at this instant; the row that actually lands there is
            # written under that shop's own lock when somebody confirms. Taking
            # both locks here would let two shops sending to each other at the same
            # moment wait on one another for ever.
            destination = await conn.fetchrow(
                """
                SELECT s.id, s.name, ss.id AS store_session_id
                  FROM stores s
                  LEFT JOIN store_sessions ss
                         ON ss.store_id = s.id AND ss.closed_at IS NULL
                 WHERE s.id = $1 AND s.owner_id = $2 AND s.is_active
                """,
                to_store_id,
                worker.owner_id,
            )
            if destination is None:
                raise BotError("unknown_item")
            if destination["store_session_id"] is None:
                raise BotError(
                    "validation_error",
                    f"«{destination['name']}» խանութը փակ է։ "
                    f"Գումար ուղարկել կարելի է միայն բաց խանութ։",
                )

            totals = await money_repo.totals_on(conn, shift["store_session_id"])
            available = Decimal(totals["cash"])
            if amount > available:
                # Physics, not permission: you cannot put notes in an envelope
                # that are not in the drawer.
                raise BotError(
                    "validation_error",
                    f"Դրամարկղում կա {available:,.0f} ֏։ Ավելին ուղարկել հնարավոր չէ։",
                )

            await money_repo.insert_movement(
                conn,
                owner_id=worker.owner_id,
                store_id=shift["store_id"],
                store_session_id=shift["store_session_id"],
                method="cash",
                kind="withdrawal",
                amount=-amount,
                work_session_id=shift["id"],
                worker_id=worker.id,
                note=note_sent(destination["name"]),
                created_by="worker",
                external_id=idem_key,
            )
            transfer_id = await transfers_repo.insert(
                conn,
                owner_id=worker.owner_id,
                from_store_id=shift["store_id"],
                to_store_id=to_store_id,
                from_session_id=shift["store_session_id"],
                amount=amount,
                sent_by_worker_id=worker.id,
                external_id=idem_key,
            )
            after = await money_repo.totals_on(conn, shift["store_session_id"])
            left_in_till = Decimal(after["cash"])
    except asyncpg.exceptions.UniqueViolationError:
        original = await transfers_repo.by_external_id(worker.owner_id, idem_key)
        if original is None:  # pragma: no cover - some other constraint
            raise
        return _payload(original, duplicate=True)

    log.info(
        "worker %s sent %s from store %s to store %s",
        worker.id, amount, shift["store_id"], to_store_id,
    )
    row = await transfers_repo.get(worker.owner_id, transfer_id)
    await _tell_the_destination(row)
    payload = _payload(row, duplicate=False)
    payload["store_totals"] = {"cash": f"{left_in_till:.2f}"}
    return payload


async def decide_by_worker(worker, transfer_id: int, accept: bool) -> dict:
    """Say whether the envelope arrived, as somebody standing in the shop it was sent
    to.

    Confirming and crediting the till are one step. A confirmation that did not
    credit it would be a promise, and the drawer would disagree with the screen
    until somebody noticed at closing time.
    """
    async with db.transaction() as conn:
        shift = await sessions_repo.lock_open_for_worker(conn, worker.id)
        if shift is None:
            raise BotError("no_open_session")

        transfer = await transfers_repo.lock_pending(conn, worker.owner_id, transfer_id)
        if transfer is None:
            # Either it never existed, or a colleague answered it first. From here
            # those are the same situation.
            raise BotError("validation_error", "Այս փոխանցումն արդեն պատասխանված է։")
        if transfer["to_store_id"] != shift["store_id"]:
            # Only the shop it was sent to may answer. Reads as missing rather than
            # forbidden, like every other cross-shop lookup.
            raise BotError("unknown_item")

        amount = Decimal(transfer["amount"])
        names = await conn.fetchrow(
            "SELECT (SELECT name FROM stores WHERE id = $1) AS src,"
            "       (SELECT name FROM stores WHERE id = $2) AS dst",
            transfer["from_store_id"],
            transfer["to_store_id"],
        )

        if accept:
            await money_repo.insert_movement(
                conn,
                owner_id=worker.owner_id,
                store_id=shift["store_id"],
                store_session_id=shift["store_session_id"],
                method="cash",
                kind="deposit",
                amount=amount,
                work_session_id=shift["id"],
                worker_id=worker.id,
                note=note_received(names["src"]),
                created_by="worker",
            )
            await transfers_repo.decide(
                conn, transfer_id, "received",
                worker_id=worker.id, to_session_id=shift["store_session_id"],
            )
        else:
            # Back into the drawer it left, which needs that shop to still be
            # trading. Refusing here rather than writing the row against a closed
            # session leaves the transfer pending and answerable later, instead of
            # burying the money in a till that has already been settled.
            source = await sessions_repo.lock_open_for_store(
                conn, worker.owner_id, transfer["from_store_id"]
            )
            if source is None:
                raise BotError(
                    "validation_error",
                    f"«{names['src']}» խանութն արդեն փակ է, գումարը հետ վերադարձնել "
                    f"հնարավոր չէ։ Զանգահարեք ղեկավարին։",
                )
            await money_repo.insert_movement(
                conn,
                owner_id=worker.owner_id,
                store_id=transfer["from_store_id"],
                store_session_id=source["id"],
                method="cash",
                kind="deposit",
                amount=amount,
                note=note_returned(names["dst"]),
                created_by="worker",
            )
            await transfers_repo.decide(
                conn, transfer_id, "rejected", worker_id=worker.id
            )

    # Read after the transaction, never inside it: ``get`` goes to the pool, so a
    # call from in here would run on another connection and report the row as it
    # was before the decision.
    log.info(
        "worker %s %s money transfer %s",
        worker.id, "received" if accept else "rejected", transfer_id,
    )
    row = await transfers_repo.get(worker.owner_id, transfer_id)
    await _tell_the_sender(row)
    return _payload(row, duplicate=False)


async def _tell_the_destination(transfer) -> None:
    """Nudge whoever is on shift at the shop the money is going to.

    They are the only people who can confirm it, and money nobody knows to expect
    is money that sits pending until somebody notices the till is short at the
    other end. The buttons travel with the message rather than living on a screen
    the worker has to go and find: this is the one thing in the bot where the
    people who need to act are not the people who started the action.

    A failure to deliver is logged and swallowed. The transfer is already written,
    and it is still on «Փոխանցումներ» to be answered.
    """
    body = texts.MONEY_TRANSFER_SENT.format(
        amount=f"{Decimal(transfer['amount']):,.0f}",
        store=escape(transfer["from_store_name"]),
        worker=escape(transfer["sent_by_name"] or ""),
    )
    markup = texts.money_transfer_buttons(transfer["id"])
    for chat_id in await workers_repo.telegram_ids_on_shift(transfer["to_store_id"]):
        try:
            await telegram.send_message(chat_id, body, reply_markup=markup)
        except telegram.Undeliverable as exc:
            log.info(
                "could not notify chat %s about money transfer %s: %s",
                chat_id, transfer["id"], exc.reason,
            )


async def _tell_the_sender(transfer) -> None:
    """And tell the shop it came from what happened to it.

    A rejection above all: the money is back in their drawer, which changes the
    figure they are about to count, and finding that out at closing time is
    finding it out too late.
    """
    template = (
        texts.MONEY_TRANSFER_RECEIVED
        if transfer["status"] == "received"
        else texts.MONEY_TRANSFER_RETURNED
    )
    body = template.format(
        amount=f"{Decimal(transfer['amount']):,.0f}",
        store=escape(transfer["to_store_name"]),
    )
    for chat_id in await workers_repo.telegram_ids_on_shift(transfer["from_store_id"]):
        try:
            await telegram.send_message(chat_id, body)
        except telegram.Undeliverable as exc:
            log.info(
                "could not tell chat %s about money transfer %s: %s",
                chat_id, transfer["id"], exc.reason,
            )


def _payload(row, *, duplicate: bool) -> dict:
    return {
        "ok": True,
        "duplicate": duplicate,
        "transfer": {
            "id": row["id"],
            "amount": f"{Decimal(row['amount']):.2f}",
            "status": row["status"],
            "from_store": row["from_store_name"],
            "to_store": row["to_store_name"],
            "sent_by": row["sent_by_name"],
        },
    }
