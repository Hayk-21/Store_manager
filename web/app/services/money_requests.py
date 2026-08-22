"""Asking another shop, or the owner, for cash.

The mirror of ``money_transfers``, and it exists because the drawer runs dry from
the inside. The wage is what exposes it: the till pays as far as it reaches and the
rest becomes a debt, so a worker locking up on 2,000 with a 5,000 wage due goes home
short *and* the shop opens tomorrow with nothing to give change from. The money is
not missing — it is in a sister shop's drawer, or in the owner's pocket — and until
now there was no way to say so from behind the counter.

It is a request rather than a command, for the same reason a stock request is: a
cashier cannot reach into a drawer they are not standing at, and the owner answers
to nobody at all. Whoever is asked says yes or no.

**Saying yes creates a transfer, and the transfer does the rest.** Money asked for
and money sent settle identically at the receiving end — it arrives in somebody's
hand, and the till only rises when they say it has — so the answer hands over to
``money_transfers`` rather than growing a second copy of that machinery here. From
the asking shop's side the two are one thing: an envelope to confirm.

The owner's money has no drawer behind it. Nothing is booked when they accept, and
nothing is given back if it never arrives; what is between them and whoever was
supposed to carry it is not something this can settle.
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
from app.repo import money_requests as requests_repo
from app.repo import money_transfers as transfers_repo
from app.repo import sessions as sessions_repo
from app.repo import users as users_repo
from app.repo import workers as workers_repo
from app.services import money_transfers as transfers_service
from app.services import telegram

log = logging.getLogger("storemanager.money_requests")

ZERO = Decimal("0.00")

# The same ceiling every other typed amount answers to. Asking for more than this is
# a mistyped figure, not a shop that needs change.
MAX_AMOUNT = Decimal("10000000.00")

# Who a shop can ask, as the bot sends it. The owner is not a store id and never can
# be, so it travels as its own word rather than as a magic number.
OWNER = "owner"


async def ask(worker, asked_of: str, amount: Decimal, idem_key: str) -> dict:
    """Ask for cash. Moves nothing — it is a question.

    ``asked_of`` is a store id as a string, or ``"owner"``. Only shops that are open
    are askable: a request nobody is standing in front of is a shop waiting for money
    that is not coming, and there is nobody there to take it out of the drawer.
    """
    if amount <= ZERO or amount > MAX_AMOUNT:
        raise BotError("validation_error", "Սխալ գումար։")

    replay = await requests_repo.by_external_id(worker.owner_id, idem_key)
    if replay is not None:
        return _payload(replay, duplicate=True)

    of_the_owner = asked_of == OWNER
    try:
        asked_of_store_id = None if of_the_owner else int(asked_of)
    except (TypeError, ValueError):
        raise BotError("validation_error", "Ընտրեք, թե ումից եք խնդրում։") from None

    try:
        async with db.transaction() as conn:
            shift = await sessions_repo.lock_open_for_worker(conn, worker.id)
            if shift is None:
                raise BotError("no_open_session")

            if not of_the_owner:
                if asked_of_store_id == shift["store_id"]:
                    raise BotError("validation_error", "Ընտրեք այլ խանութ։")
                # Open *now*, checked without a lock: this is a courtesy, and what
                # actually has to hold is that the drawer covers it at the moment
                # somebody accepts — which is checked there, under that shop's lock.
                open_now = await conn.fetchval(
                    """
                    SELECT 1 FROM stores s
                      JOIN store_sessions ss
                        ON ss.store_id = s.id AND ss.closed_at IS NULL
                     WHERE s.id = $1 AND s.owner_id = $2 AND s.is_active
                    """,
                    asked_of_store_id,
                    worker.owner_id,
                )
                if open_now is None:
                    raise BotError(
                        "validation_error",
                        "Այդ խանութը փակ է։ Գումար խնդրել կարելի է միայն բաց խանութից։",
                    )

            request_id = await requests_repo.insert(
                conn,
                owner_id=worker.owner_id,
                to_store_id=shift["store_id"],
                to_session_id=shift["store_session_id"],
                amount=amount,
                asked_of_store_id=asked_of_store_id,
                asked_the_owner=of_the_owner,
                requested_by_worker_id=worker.id,
                external_id=idem_key,
            )
    except asyncpg.exceptions.UniqueViolationError:
        original = await requests_repo.by_external_id(worker.owner_id, idem_key)
        if original is None:  # pragma: no cover - some other constraint
            raise
        return _payload(original, duplicate=True)

    log.info(
        "worker %s asked %s for %s",
        worker.id, "the owner" if of_the_owner else f"store {asked_of_store_id}", amount,
    )
    row = await requests_repo.get(worker.owner_id, request_id)
    await _tell_whoever_was_asked(row)
    return _payload(row, duplicate=False)


async def decide(telegram_id: int, request_id: int, accept: bool) -> dict:
    """Answer a request, as whoever was asked.

    Who that is decides everything below, and it is worked out from the person who
    tapped rather than from a flag they sent: the owner's own Telegram is bound to
    their account, and a worker's to theirs, so the same button on the same message
    resolves to the right authority without either of them declaring which they are.

    Accepting is what moves the money. It books the giving shop's withdrawal and
    creates the pending transfer the asking shop then confirms — an acceptance that
    moved nothing would be indistinguishable from a refusal at the far end.
    """
    owner = await users_repo.by_telegram_id(telegram_id)
    worker = await workers_repo.by_telegram_id(telegram_id)
    if owner is None and worker is None:
        raise BotError("unknown_worker")

    # The owner is tried first, and only for a request actually made of them.
    # Somebody who is both — an owner who also works a shift — answers with the
    # greater authority when it is theirs to give, and as a cashier otherwise.
    if owner is not None and owner["is_active"]:
        request = await requests_repo.get(owner["id"], request_id)
        if request is not None and request["asked_the_owner"]:
            return await _decide_as_owner(owner["id"], request_id, accept)

    if worker is None:
        raise BotError("unknown_item")
    if not worker["is_active"]:
        raise BotError("worker_inactive")
    return await _decide_as_worker(worker, request_id, accept)


async def _decide_as_owner(owner_id: int, request_id: int, accept: bool) -> dict:
    """The owner answering. Their money comes out of no till, so nothing is booked
    here — only the promise that it is on its way."""
    async with db.transaction() as conn:
        request = await requests_repo.lock_pending(conn, owner_id, request_id)
        if request is None:
            raise BotError("validation_error", "Այս հարցումն արդեն պատասխանված է։")
        if not request["asked_the_owner"]:  # pragma: no cover - guarded by the caller
            raise BotError("unknown_item")

        transfer_id = None
        if accept:
            transfer_id = await transfers_repo.insert(
                conn,
                owner_id=owner_id,
                to_store_id=request["to_store_id"],
                amount=Decimal(request["amount"]),
            )
        await requests_repo.decide(
            conn, request_id,
            "accepted" if accept else "rejected",
            by_owner=True, transfer_id=transfer_id,
        )

    log.info(
        "owner %s %s money request %s",
        owner_id, "accepted" if accept else "rejected", request_id,
    )
    return await _announce(owner_id, request_id, transfer_id)


async def _decide_as_worker(worker, request_id: int, accept: bool) -> dict:
    """A cashier answering for their own shop's drawer.

    They have to be on shift at the shop being asked, and the drawer has to hold it.
    Both are checked here rather than when the request was made: the money is taken
    out at this moment, and the till as it stands now is the only one that counts.

    ``worker`` is the stored row rather than the ``Worker`` the rest of the bot API
    passes around — nothing here needs a salary or a name, and resolving the caller
    is this module's own job because the same tap can arrive from an owner.
    """
    worker_id, owner_id = worker["id"], worker["owner_id"]
    async with db.transaction() as conn:
        shift = await sessions_repo.lock_open_for_worker(conn, worker_id)
        if shift is None:
            raise BotError("no_open_session")

        request = await requests_repo.lock_pending(conn, owner_id, request_id)
        if request is None:
            raise BotError("validation_error", "Այս հարցումն արդեն պատասխանված է։")
        if request["asked_of_store_id"] != shift["store_id"]:
            # Only the shop that was asked may answer. Reads as missing rather than
            # forbidden, like every other cross-shop lookup.
            raise BotError("unknown_item")

        transfer_id = None
        if accept:
            # The drawer is shared, so the session is locked and not just this
            # worker's shift row — otherwise two cashiers could both approve against
            # the same reading and send out more than is in there.
            await sessions_repo.lock_open_for_store(conn, owner_id, shift["store_id"])
            amount = Decimal(request["amount"])
            available = Decimal(
                (await money_repo.totals_on(conn, shift["store_session_id"]))["cash"]
            )
            if amount > available:
                raise BotError(
                    "validation_error",
                    f"Ձեր դրամարկղում կա {available:,.0f} ֏։ Ավելին ուղարկել հնարավոր չէ։",
                )

            asking = await conn.fetchval(
                "SELECT name FROM stores WHERE id = $1", request["to_store_id"]
            )
            await money_repo.insert_movement(
                conn,
                owner_id=owner_id,
                store_id=shift["store_id"],
                store_session_id=shift["store_session_id"],
                method="cash",
                kind="withdrawal",
                amount=-amount,
                work_session_id=shift["id"],
                worker_id=worker_id,
                note=transfers_service.note_sent(asking),
                created_by="worker",
            )
            transfer_id = await transfers_repo.insert(
                conn,
                owner_id=owner_id,
                from_store_id=shift["store_id"],
                from_session_id=shift["store_session_id"],
                to_store_id=request["to_store_id"],
                amount=amount,
                sent_by_worker_id=worker_id,
            )
        await requests_repo.decide(
            conn, request_id,
            "accepted" if accept else "rejected",
            worker_id=worker_id, transfer_id=transfer_id,
        )

    log.info(
        "worker %s %s money request %s",
        worker_id, "accepted" if accept else "rejected", request_id,
    )
    return await _announce(owner_id, request_id, transfer_id)


async def _announce(owner_id: int, request_id: int, transfer_id: int | None) -> dict:
    """Tell the shop that asked, and — when there is money on its way — hand over to
    the transfer's own announcement so the envelope arrives with its buttons on it.

    Read after the transaction, never inside it: these go to the pool, so a call from
    in there would run on another connection and see the rows as they were before.
    """
    request = await requests_repo.get(owner_id, request_id)
    await _tell_the_asking_shop(request)
    if transfer_id is not None:
        await transfers_service.tell_the_destination(
            await transfers_repo.get(owner_id, transfer_id)
        )
    return _payload(request, duplicate=False)


async def _tell_whoever_was_asked(request) -> None:
    """Push the question to the people who can answer it.

    Workers on shift at the shop being asked, or the owner on their own chat. A
    failure to deliver is logged and swallowed — the request is written either way,
    and the owner can find it on the website while a shop finds it under
    «Փոխանցումներ».
    """
    body = (
        texts.MONEY_REQUEST_ASKED_OF_OWNER
        if request["asked_the_owner"]
        else texts.MONEY_REQUEST_ASKED
    ).format(
        store=escape(request["to_store_name"]),
        amount=f"{Decimal(request['amount']):,.0f}",
        worker=escape(request["requested_by_name"] or ""),
    )
    markup = texts.money_request_buttons(request["id"])
    for chat_id in await _who_was_asked(request):
        try:
            await telegram.send_message(chat_id, body, reply_markup=markup)
        except telegram.Undeliverable as exc:
            log.info(
                "could not put money request %s in front of chat %s: %s",
                request["id"], chat_id, exc.reason,
            )


async def _tell_the_asking_shop(request) -> None:
    """And tell the shop what the answer was.

    A refusal above all: they asked because the drawer could not cover something,
    and they need to know to ask somebody else rather than to keep waiting.
    """
    source = (
        texts.MONEY_REQUEST_FROM_OWNER
        if request["asked_the_owner"]
        else texts.MONEY_REQUEST_FROM_STORE.format(
            store=escape(request["asked_of_name"] or "")
        )
    )
    template = (
        texts.MONEY_REQUEST_ACCEPTED
        if request["status"] == "accepted"
        else texts.MONEY_REQUEST_REJECTED
    )
    body = template.format(
        amount=f"{Decimal(request['amount']):,.0f}", source=source
    )
    for chat_id in await workers_repo.telegram_ids_on_shift(request["to_store_id"]):
        try:
            await telegram.send_message(chat_id, body)
        except telegram.Undeliverable as exc:
            log.info(
                "could not tell chat %s about money request %s: %s",
                chat_id, request["id"], exc.reason,
            )


async def _who_was_asked(request) -> list[int]:
    if not request["asked_the_owner"]:
        return await workers_repo.telegram_ids_on_shift(request["asked_of_store_id"])
    # Empty when the owner has never messaged the bot — the binding happens on first
    # contact — and an empty list simply sends nothing, which is right for somebody
    # with no chat to send to.
    owner = await users_repo.by_id(request["owner_id"])
    chat_id = owner["telegram_id"] if owner is not None else None
    return [chat_id] if chat_id else []


def _payload(row, *, duplicate: bool) -> dict:
    return {
        "ok": True,
        "duplicate": duplicate,
        "request": {
            "id": row["id"],
            "amount": f"{Decimal(row['amount']):.2f}",
            "status": row["status"],
            "asked_of": row["asked_of_name"],
            "asked_the_owner": row["asked_the_owner"],
            "to_store": row["to_store_name"],
            "requested_by": row["requested_by_name"],
        },
    }
