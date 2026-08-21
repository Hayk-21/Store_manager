"""What one worker leaves in the drawer is what the next one finds, everywhere.

The whole point of the casa, checked end to end rather than a piece at a time. Worker A
locks up and says they are leaving 30,000; worker B opens the shop tomorrow. Every figure
B or the owner can see about that drawer has to be 30,000, and every calculation has to
start from it:

* the store's balance;
* the till B's session opens with;
* what the bot reads back to B before their shift ends;
* the owner's share at the end of B's day, which is B's takings *on top of* the 30,000
  less whatever B leaves;
* and tomorrow's float again, from B's own count.

Each of those has its own test elsewhere. This file is about the chain: every link
correct and the chain still wrong is exactly the shape of the bug that had a worker hand
over 82,000 and then be paid out of an empty drawer.
"""

from __future__ import annotations

from decimal import Decimal

from app.db import db
from app.repo import money as money_repo
from app.services import sales as sales_service
from app.services import shifts as shifts_service
from app.services import till as till_service
from tests.factories import (
    YEREVAN_LAT,
    YEREVAN_LNG,
    login,
    make_item,
    make_owner,
    make_store,
    make_worker,
    worked_a_full_shift,
)

BASE = "/api/bot/v1"


async def _a_shop(balance: str = "0.00"):
    """A shop, and what is already in its drawer.

    Most of the chains below start with a worker leaving 30,000 behind, which only a
    drawer that already holds it can do: a day taking 7,000 cannot leave 30,000, and
    the close-out refuses the claim. That used to go through, and it is how a shop's
    float grew out of money nobody had taken — the balance simply became whatever the
    last person typed. So the float is *given* to the shop here, the way a real one is:
    somebody put change in the till before the first day.
    """
    owner_id = await make_owner("@ownerhandle")
    store_id = await make_store(owner_id, "Նուբարաշեն", lat=YEREVAN_LAT, lng=YEREVAN_LNG)
    if Decimal(balance) > 0:
        await db.execute(
            "UPDATE stores SET till_balance = $2 WHERE id = $1", store_id, Decimal(balance)
        )
    item_id = await make_item(
        owner_id, store_id, "HQD Cuvie", count=100,
        self_price="1500.00", sell_price="3500.00",
    )
    return owner_id, store_id, item_id


async def _worker(owner_id: int, name: str, salary: str = "0.00"):
    worker_id, telegram_id = await make_worker(owner_id, name, salary_amount=salary)
    return shifts_service.Worker(
        id=worker_id, owner_id=owner_id, name=name, salary_amount=Decimal(salary)
    ), telegram_id


async def _a_days_work(worker, item_id: int | None, sold: int, key: str, left: str) -> int:
    """Open, sell, count the drawer, lock up. Returns the store session that just closed.

    ``left`` is what the worker says they are leaving behind, and it is not optional in
    the way it reads: the close-out is refused without it once this shift is the one
    shutting the shop. Counting used to be a separate call afterwards, which is how a
    night could pass with nobody having said anything about the drawer.

    It is also refused when it is *more than the drawer holds* — see ``_a_shop``, which
    is why these chains start with a shop that has a float rather than conjuring one on
    the first evening.

    Idempotency keys are padded to the eight characters the schema insists on — the
    constraint is there so a bot cannot send a one-character key, and a test that trips
    over it is the constraint working.
    """
    await shifts_service.open_store(
        worker, YEREVAN_LAT, YEREVAN_LNG, 20, f"idem-open-{key}", 900
    )
    session_id = await db.fetchval(
        "SELECT id FROM store_sessions ORDER BY id DESC LIMIT 1"
    )
    if sold:
        await sales_service.record_sale(
            worker, [{"item_id": item_id, "quantity": sold}], "cash", f"idem-sale-{key}"
        )
    await worked_a_full_shift(worker.id)
    await shifts_service.close_out_shift(
        worker, [], f"idem-close-{key}", counted=Decimal(left)
    )
    return session_id


async def _reading(session_id: int) -> dict[str, Decimal]:
    """What that evening's count came to, as closing wrote it down."""
    row = await db.fetchrow(
        """
        SELECT counted, expected, handed_over FROM till_counts
         WHERE store_session_id = $1 ORDER BY id DESC LIMIT 1
        """,
        session_id,
    )
    return {key: Decimal(value) for key, value in dict(row).items()}


async def _float(store_id: int) -> Decimal:
    return await db.fetchval("SELECT till_balance FROM stores WHERE id = $1", store_id)


async def _till(session_id: int) -> Decimal:
    return Decimal((await money_repo.totals_for_session(session_id))["cash"])


# -- the chain ----------------------------------------------------------------

async def test_tomorrow_starts_with_exactly_what_last_night_left(client):
    owner_id, store_id, item_id = await _a_shop(balance="30000")
    anahit, _ = await _worker(owner_id, "Անի")
    gor, _ = await _worker(owner_id, "Գոռ")

    # Monday: 7,000 taken, 30,000 left behind.
    await _a_days_work(anahit, item_id, 2, "mon", left="30000")

    # Tuesday: Gor opens up, sells nothing, and leaves what he found.
    tuesday = await _a_days_work(gor, None, 0, "tue", left="30000")

    assert await _float(store_id) == Decimal("30000.00")
    assert await _till(tuesday) == Decimal("30000.00"), "found in the drawer, not zero"


async def test_the_second_day_computes_from_the_first_days_leftovers(client):
    """The owner's share on Tuesday is Monday's float plus Tuesday's takings, less what
    Gor leaves. Not Tuesday's takings alone — the 30,000 was already in the drawer and
    handing it over twice is exactly the sort of thing this checks."""
    owner_id, store_id, item_id = await _a_shop(balance="30000")
    anahit, _ = await _worker(owner_id, "Անի")
    gor, _ = await _worker(owner_id, "Գոռ")

    await _a_days_work(anahit, item_id, 2, "mon", left="30000")

    tuesday = await _a_days_work(gor, item_id, 3, "tue", left="25000")

    reading = await _reading(tuesday)
    assert reading["expected"] == Decimal("40500.00"), "30,000 carried + 10,500 sold"
    assert reading["handed_over"] == Decimal("15500.00"), "less the 25,000 left behind"
    assert await _float(store_id) == Decimal("25000.00"), "and tomorrow starts at 25,000"


async def test_a_worker_who_leaves_nothing_leaves_the_next_one_nothing(client):
    """The honest end of the same rule, and the one worth stating: an empty drawer is a
    real answer and the next session must not invent a float."""
    owner_id, store_id, item_id = await _a_shop()
    anahit, _ = await _worker(owner_id, "Անի")
    gor, _ = await _worker(owner_id, "Գոռ")

    await _a_days_work(anahit, item_id, 2, "mon", left="0")
    tuesday = await _a_days_work(gor, None, 0, "tue", left="0")

    assert await _float(store_id) == Decimal("0.00")
    assert await _till(tuesday) == Decimal("0.00")
    assert await db.fetchval(
        "SELECT count(*) FROM cash_movements WHERE store_session_id = $1", tuesday
    ) == 0, "no deposit of nothing"


async def test_a_night_nobody_counted_carries_the_old_float_unchanged(client):
    """A skipped count is not a count of zero. The money is still in the drawer, and
    the shop's balance is the last thing anybody actually said about it.

    A worker cannot skip it any more — the close-out will not go through without a
    figure. The shop can still be shut over their head, by the owner or by the
    overnight sweep, and that is the night this describes.
    """
    owner_id, store_id, item_id = await _a_shop(balance="30000")
    anahit, _ = await _worker(owner_id, "Անի")
    gor, _ = await _worker(owner_id, "Գոռ")

    await _a_days_work(anahit, item_id, 2, "mon", left="30000")
    await shifts_service.open_store(gor, YEREVAN_LAT, YEREVAN_LNG, 20, "idem-open-tue", 900)
    tuesday = await db.fetchval("SELECT id FROM store_sessions ORDER BY id DESC LIMIT 1")
    await sales_service.record_sale(
        gor, [{"item_id": item_id, "quantity": 1}], "cash", "idem-sale-tue"
    )
    await shifts_service.close_store_session_as_owner(owner_id, tuesday)

    assert await _float(store_id) == Decimal("30000.00")

    wednesday = await _a_days_work(anahit, None, 0, "wed", left="30000")
    assert await _till(wednesday) == Decimal("30000.00")


async def test_three_days_running_never_lose_or_duplicate_the_float(client):
    """The arithmetic that matters, over enough days for a leak to show. Each morning
    starts at last night's figure, and each evening's share is that morning's float plus
    the day's cash less what is left."""
    owner_id, store_id, item_id = await _a_shop()
    anahit, _ = await _worker(owner_id, "Անի")

    d1 = await _a_days_work(anahit, item_id, 2, "d1", left="5000")   # 7,000
    d2 = await _a_days_work(anahit, item_id, 2, "d2", left="4000")   # 5,000 + 7,000
    d3 = await _a_days_work(anahit, item_id, 2, "d3", left="3000")   # 4,000 + 7,000

    first, second, third = await _reading(d1), await _reading(d2), await _reading(d3)
    assert first["handed_over"] == Decimal("2000.00")
    assert second["expected"] == Decimal("12000.00")
    assert second["handed_over"] == Decimal("8000.00")
    assert third["expected"] == Decimal("11000.00")
    assert third["handed_over"] == Decimal("8000.00")
    # Three days took 21,000 in cash. The owner has 18,000 of it and the shop has the
    # 3,000 still in its drawer.
    assert (
        first["handed_over"] + second["handed_over"] + third["handed_over"]
        + await _float(store_id)
    ) == Decimal("21000.00")


async def test_a_wage_comes_out_before_the_owners_share(client):
    """The ordering the whole redesign is about. Anahit is paid 6,000 from a drawer
    holding 30,000 carried plus 7,000 taken, and the owner gets what is left over the
    float — not the wage as well."""
    owner_id, store_id, item_id = await _a_shop(balance="30000")
    anahit, _ = await _worker(owner_id, "Անի", salary="6000.00")
    gor, _ = await _worker(owner_id, "Գոռ")

    await _a_days_work(gor, None, 0, "setup", left="30000")

    monday = await _a_days_work(anahit, item_id, 2, "mon", left="30000")

    reading = await _reading(monday)
    assert reading["expected"] == Decimal("31000.00"), "30,000 + 7,000 − a 6,000 wage"
    assert reading["handed_over"] == Decimal("1000.00")


# -- what each side is shown --------------------------------------------------

async def test_the_bot_shows_the_next_worker_the_float_it_starts_with(client, bot_headers):
    """Gor opens up and reads the shift back before ending it. The drawer he is about to
    count has to be the one Anahit left, not an empty one."""
    owner_id, store_id, item_id = await _a_shop(balance="30000")
    anahit, _ = await _worker(owner_id, "Անի")
    gor, gor_tg = await _worker(owner_id, "Գոռ")

    await _a_days_work(anahit, item_id, 2, "mon", left="30000")
    await shifts_service.open_store(gor, YEREVAN_LAT, YEREVAN_LNG, 20, "idem-open-tue", 900)
    await sales_service.record_sale(
        gor, [{"item_id": item_id, "quantity": 1}], "cash", "idem-sale-tue"
    )

    review = (await client.get(
        f"{BASE}/shift/review", params={"telegram_id": gor_tg}, headers=bot_headers
    )).json()

    assert review["store_float"] == "30000.00", "what the shop keeps"
    assert review["till"]["cash"] == "33500.00", "carried over, plus what he sold"


async def test_the_owner_sees_the_float_on_a_shut_shop(client):
    """It is there whether or not anybody is standing at it, which is the difference
    between the float and the till."""
    owner_id, store_id, item_id = await _a_shop(balance="30000")
    anahit, _ = await _worker(owner_id, "Անի")

    await _a_days_work(anahit, item_id, 2, "mon", left="30000")
    await login(client, "@ownerhandle")

    page = await client.get(f"/stores/{store_id}")

    assert "Խանութի դրամարկղը" in page.text
    assert "30,000.00" in page.text
    assert "Անի" in page.text, "and who last said so"


async def test_an_owners_correction_is_what_tomorrow_opens_with(client):
    """The last word on the drawer, whoever said it. An owner who recounts a shop in
    person and fixes the figure has to be the one the next shift starts from."""
    owner_id, store_id, item_id = await _a_shop(balance="30000")
    anahit, _ = await _worker(owner_id, "Անի")
    gor, _ = await _worker(owner_id, "Գոռ")

    await _a_days_work(anahit, item_id, 2, "mon", left="30000")
    await till_service.set_by_owner(owner_id, store_id, Decimal("12000"), "վերահաշվարկ")

    tuesday = await _a_days_work(gor, None, 0, "tue", left="12000")

    assert await _till(tuesday) == Decimal("12000.00")


async def test_two_shops_do_not_share_a_float(client):
    """Obvious, and worth a test: the balance is a column on the store, and a query
    that forgot the store id would pass every other test in this file."""
    owner_id, store_id, item_id = await _a_shop(balance="30000")
    other = await make_store(owner_id, "Կենտրոն", lat=YEREVAN_LAT, lng=YEREVAN_LNG)
    anahit, _ = await _worker(owner_id, "Անի")

    await _a_days_work(anahit, item_id, 2, "mon", left="30000")

    assert await _float(store_id) == Decimal("30000.00")
    assert await _float(other) == Decimal("0.00"), "the other shop is untouched"


# -- and what it looks like from either side ----------------------------------

async def test_the_arriving_worker_is_told_what_is_in_the_drawer(client, bot_headers):
    """Opening up says how much was left behind.

    The moment the float becomes this worker's responsibility is the moment they
    unlock the door, and it is the only moment at which they can still check it. Told
    at closing time that the till had been 1,000 heavier all along, there is nothing
    left to count.
    """
    owner_id, store_id, item_id = await _a_shop()
    anahit, _ = await _worker(owner_id, "Անի")
    gor, _ = await _worker(owner_id, "Գոռ")

    await _a_days_work(anahit, item_id, 2, "mon", left="1000")

    opened = await shifts_service.open_store(
        gor, YEREVAN_LAT, YEREVAN_LNG, 20, "idem-open-tue", 900
    )

    assert opened["session"]["till"] == {"cash": "1000.00", "carried_in": "1000.00"}


async def test_a_retried_opening_says_the_same_thing(client):
    """The idempotent replay answers from a different branch, and a payload that grew a
    field on one path only is how a worker gets told about the drawer once in three
    tries on a bad connection."""
    owner_id, store_id, item_id = await _a_shop()
    anahit, _ = await _worker(owner_id, "Անի")
    gor, _ = await _worker(owner_id, "Գոռ")

    await _a_days_work(anahit, item_id, 2, "mon", left="1000")

    first = await shifts_service.open_store(
        gor, YEREVAN_LAT, YEREVAN_LNG, 20, "idem-open-tue", 900
    )
    again = await shifts_service.open_store(
        gor, YEREVAN_LAT, YEREVAN_LNG, 20, "idem-open-tue", 900
    )

    assert again["duplicate"] is True
    assert again["session"]["till"] == first["session"]["till"]


async def test_the_drawer_is_the_float_plus_what_this_worker_sold(client, bot_headers):
    """The owner's own example, end to end.

    Anahit leaves 1,000. Gor opens up, sells one 5,000 vape for cash, and asks what is
    in the drawer: 6,000 — of which 1,000 is not his. Both figures go to the bot, so
    «Վիճակ» can say which is which instead of leaving him to assume the difference is
    his own miscount.
    """
    owner_id, store_id, _ = await _a_shop(balance="1000")
    vape = await make_item(
        owner_id, store_id, "Vanter", count=10,
        self_price="2000.00", sell_price="5000.00",
    )
    anahit, _ = await _worker(owner_id, "Անի")
    gor, gor_tg = await _worker(owner_id, "Գոռ")

    await _a_days_work(anahit, None, 0, "mon", left="1000")
    await shifts_service.open_store(gor, YEREVAN_LAT, YEREVAN_LNG, 20, "idem-open-tue", 900)
    await sales_service.record_sale(
        gor, [{"item_id": vape, "quantity": 1}], "cash", "idem-sale-tue"
    )

    me = (await client.get(
        f"{BASE}/me", params={"telegram_id": gor_tg}, headers=bot_headers
    )).json()

    assert me["session"]["store_totals"]["cash"] == "6000.00"
    assert me["session"]["store_totals"]["carried_in"] == "1000.00"
    # And his own selling stays his own: the float is the shop's, not a sale.
    assert me["session"]["sales"]["total"] == "5000.00"


async def test_the_report_shows_both_ends_of_the_drawer(client):
    """What the shop opened on and what it closed on, as two figures the owner can
    subtract. One of them was invisible: «Մնաց խանութում» said what this worker left,
    and nothing on the page said what they had found."""
    owner_id, store_id, item_id = await _a_shop(balance="30000")
    anahit, _ = await _worker(owner_id, "Անի")
    gor, _ = await _worker(owner_id, "Գոռ")

    await _a_days_work(anahit, item_id, 2, "mon", left="30000")
    tuesday = await _a_days_work(gor, item_id, 1, "tue", left="25000")
    await login(client, "@ownerhandle")

    page = (await client.get(f"/reports?store_session_id={tuesday}")).text

    assert "Նախորդից մնացած" in page
    assert "30,000.00" in page, "what Anahit left"
    assert "25,000.00" in page, "what Gor left"
    # 30,000 carried + 3,500 sold − 25,000 left behind.
    assert "8,500.00" in page, "and what the owner is owed"


async def test_the_list_carries_the_same_pair(client):
    """The row and the report it opens answer the drawer question the same way."""
    owner_id, store_id, item_id = await _a_shop(balance="30000")
    anahit, _ = await _worker(owner_id, "Անի")
    gor, _ = await _worker(owner_id, "Գոռ")

    await _a_days_work(anahit, item_id, 2, "mon", left="30000")
    await _a_days_work(gor, item_id, 1, "tue", left="25000")
    await login(client, "@ownerhandle")

    page = (await client.get("/reports")).text

    assert "Նախորդից մնացած" in page
    assert page.count("30,000.00") >= 1
    assert page.count("25,000.00") >= 1


async def test_the_float_is_inside_what_the_owner_is_owed(client):
    """The requirement stated plainly: the money left behind is part of the till, and
    the owner's share is the till less what stays. A float excluded from it would have
    the shop hand over the same 30,000 every single morning."""
    owner_id, store_id, item_id = await _a_shop(balance="30000")
    anahit, _ = await _worker(owner_id, "Անի")
    gor, _ = await _worker(owner_id, "Գոռ")

    await _a_days_work(anahit, item_id, 2, "mon", left="30000")
    # 30,000 carried in + 3,500 sold, and Gor takes nothing home in the drawer.
    tuesday = await _a_days_work(gor, item_id, 1, "tue", left="0")

    reading = await _reading(tuesday)
    assert reading["expected"] == Decimal("33500.00")
    assert reading["handed_over"] == Decimal("33500.00")
    assert await _float(store_id) == Decimal("0.00")
