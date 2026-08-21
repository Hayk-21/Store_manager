"""/reports — organised by store session, because that is the accounting period."""

from __future__ import annotations

import re
from decimal import Decimal

from app.db import db
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


async def _a_completed_shift():
    owner_id = await make_owner("@ownerhandle")
    store_id = await make_store(owner_id, "Խանութ 1", lat=YEREVAN_LAT, lng=YEREVAN_LNG)
    worker_id, _ = await make_worker(owner_id, "Անի", salary_amount="8000.00")
    worker = shifts_service.Worker(
        id=worker_id, owner_id=owner_id, name="Անի", salary_amount=Decimal("8000.00")
    )
    item_id = await make_item(owner_id, store_id, "HQD Cuvie", count=10, sell_price="3500.00")
    await shifts_service.open_store(worker, YEREVAN_LAT, YEREVAN_LNG, 20, "idem-key-open-1", 900)
    # A whole day, so the shift wage is the full figure: a shift under eight
    # hours is paid half, and these numbers are not about that rule.
    await worked_a_full_shift(worker_id)
    await sales_service.record_sale(
        worker, [{"item_id": item_id, "quantity": 3}], "cash", "idem-key-sale-1"
    )
    return owner_id, store_id, worker, item_id


async def test_the_list_shows_one_row_per_time_the_store_was_open(client):
    _, _, worker, _ = await _a_completed_shift()
    await shifts_service.end_shift(worker, None, None, "idem-key-end-01")
    await login(client, "@ownerhandle")

    response = await client.get("/reports")

    assert response.status_code == 200
    assert "Խանութ 1" in response.text
    # Three at 3,500, all of it cash.
    assert "10,500.00" in response.text


async def test_an_open_session_shows_live_numbers_not_a_snapshot(client):
    await _a_completed_shift()
    await login(client, "@ownerhandle")

    response = await client.get("/reports")

    assert "դեռ բաց" in response.text
    assert "10,500.00" in response.text


# -- what a row says ---------------------------------------------------------

async def test_the_row_names_the_worker_rather_than_counting_them(client):
    """One person opens a shop and closes it, so «1» answered nothing."""
    _, _, worker, _ = await _a_completed_shift()
    await shifts_service.end_shift(worker, None, None, "idem-key-end-01")
    await login(client, "@ownerhandle")

    page = await client.get("/reports")

    assert ">Աշխատող</th>" in page.text
    assert ">Աշխատողներ</th>" not in page.text
    assert "Անի" in page.text


async def test_the_row_splits_the_takings_the_way_the_report_does(client):
    """«Վաճառք · Կանխիկ վաճառք · Քարտով վաճառք», not the drawer balances that were
    here — a wage has already come off those, so they never split the takings."""
    _, _, worker, item_id = await _a_completed_shift()
    await sales_service.record_sale(
        worker, [{"item_id": item_id, "quantity": 1}], "card", "idem-key-sale-2"
    )
    await shifts_service.end_shift(worker, None, None, "idem-key-end-01")
    await login(client, "@ownerhandle")

    page = await client.get("/reports")

    assert "Կանխիկ վաճառք" in page.text and "Քարտով վաճառք" in page.text
    assert "14,000.00" in page.text, "four at 3,500"
    assert "10,500.00" in page.text, "three of them cash"
    assert "3,500.00" in page.text, "one on a card"


async def test_the_row_shows_what_was_delivered(client):
    _, store_id, worker, item_id = await _a_completed_shift()
    await sales_service.record_sale(
        worker, [{"item_id": item_id, "quantity": 2}], "cash", "idem-key-sale-2",
        is_delivery=True,
    )
    await shifts_service.end_shift(worker, None, None, "idem-key-end-01")
    await login(client, "@ownerhandle")

    page = await client.get("/reports")

    assert "Առաքում" in page.text
    assert "7,000.00" in page.text, "two at 3,500 went out by delivery"
    assert "1 պատվեր" in page.text


async def test_the_rows_profit_is_the_reports_own_profit(client):
    """The column and the header it opens are one subtraction, not two."""
    _, _, worker, _ = await _a_completed_shift()
    await shifts_service.end_shift(worker, None, None, "idem-key-end-01")
    session_id = await db.fetchval("SELECT id FROM store_sessions")
    await login(client, "@ownerhandle")

    listing = await client.get("/reports")
    detail = await client.get(f"/reports?store_session_id={session_id}")

    # Margin 6,000 (three at 3,500 that cost 1,500) less an 8,000 wage.
    assert "-2,000.00" in listing.text
    assert "-2,000.00" in detail.text


async def test_the_drawer_is_answered_on_the_report_and_not_in_the_list(client):
    """«Նախորդից մնացած» and «Մնաց խանութում» were columns here, and the pair of them is
    what pushed this table past the width of a screen — eleven columns about selling and
    two about the drawer. A list read by scrolling sideways is a list nobody reads, and
    the drawer is a question the report a row opens already answers, in boxes that can
    also correct it."""
    _, _, worker, _ = await _a_completed_shift()
    session_id = await db.fetchval("SELECT id FROM store_sessions")
    await shifts_service.end_shift(worker, None, None, "idem-key-end-01")
    await till_service.declare_close(worker, Decimal("1500"), "idem-till-1")
    await login(client, "@ownerhandle")

    listing = await client.get("/reports")
    detail = await client.get(f"/reports?store_session_id={session_id}")

    assert "Մնաց խանութում" not in listing.text
    assert "Նախորդից մնացած" not in listing.text
    assert "Մնաց խանութում" in detail.text
    assert "1,500.00" in detail.text


async def test_an_uncounted_drawer_leaves_the_float_not_nothing(client):
    """A worker who goes home without counting has not emptied the shop: the float
    stays where it is, because nothing but a count moves it. Only the owner's
    force-close and the overnight sweep can now end an evening that way."""
    owner_id = await make_owner("@ownerhandle")
    store_id = await make_store(owner_id, "Խանութ 1", lat=YEREVAN_LAT, lng=YEREVAN_LNG)
    await db.execute("UPDATE stores SET till_balance = 4000 WHERE id = $1", store_id)
    worker_id, _ = await make_worker(owner_id, "Անի", salary_amount="0.00")
    worker = shifts_service.Worker(
        id=worker_id, owner_id=owner_id, name="Անի", salary_amount=Decimal("0.00")
    )
    await shifts_service.open_store(worker, YEREVAN_LAT, YEREVAN_LNG, 20, "idem-open-1", 900)
    session_id = await db.fetchval("SELECT id FROM store_sessions")
    await shifts_service.end_shift(worker, None, None, "idem-end-1")
    await login(client, "@ownerhandle")

    page = await client.get(f"/reports?store_session_id={session_id}")

    assert "4,000.00" in page.text, "the float it opened with, not nothing"
    assert "ոչ ոք չի հաշվել" in page.text, "and the page says the figure was worked out"


async def test_an_open_sessions_wage_is_not_zero_in_the_list(client):
    """It is read from the ledger, not from the snapshot taken at closing time —
    which is written when the shop shuts, and is nothing until then."""
    owner_id, store_id, first, _ = await _a_completed_shift()
    second_id, _ = await make_worker(owner_id, "Բաբկեն", salary_amount="5000.00")
    second = shifts_service.Worker(
        id=second_id, owner_id=owner_id, name="Բաբկեն",
        salary_amount=Decimal("5000.00"),
    )
    await shifts_service.open_store(second, YEREVAN_LAT, YEREVAN_LNG, 20, "idem-open-2", 900)
    # The first one goes home; the shop stays open behind them, so nothing has
    # been snapshotted yet.
    await shifts_service.end_shift(first, None, None, "idem-key-end-01")
    await login(client, "@ownerhandle")

    page = await client.get("/reports")

    assert "դեռ բաց" in page.text
    assert "8,000.00" in page.text, "the wage the drawer has already paid"
    assert "Անի · Բաբկեն" in page.text or "Բաբկեն · Անի" in page.text


async def test_the_detail_view_shows_shifts_receipts_and_the_ledger(client):
    _, _, worker, _ = await _a_completed_shift()
    await shifts_service.end_shift(worker, None, None, "idem-key-end-01")
    session_id = await db.fetchval("SELECT id FROM store_sessions")
    await login(client, "@ownerhandle")

    response = await client.get(f"/reports?store_session_id={session_id}")

    assert response.status_code == 200
    assert "Անի" in response.text
    assert "HQD Cuvie" in response.text
    assert "Աշխատավարձ" in response.text
    assert "8,000.00" in response.text


def _the_ledger(page: str) -> str:
    """The «Դրամարկղի շարժ» table alone.

    By its heading and not by its name: the words appear higher up the page too, in
    the sentence pointing at this section, and splitting on those lands in the middle
    of the drawer's explanation instead.
    """
    return page.split("<h3>Դրամարկղի շարժ</h3>")[1]


async def test_the_ledger_says_what_was_sold(client):
    """«Վաճառք · Կանխիկ · 10,500 ֏» is an amount with nothing to check it against. Two
    identical rows an hour apart were told apart by the minute they landed and by
    nothing else, and matching one to its receipt meant counting rows in the table
    above and hoping."""
    _, _, worker, _ = await _a_completed_shift()
    await shifts_service.end_shift(worker, None, None, "idem-key-end-01")
    session_id = await db.fetchval("SELECT id FROM store_sessions")
    await login(client, "@ownerhandle")

    page = (await client.get(f"/reports?store_session_id={session_id}")).text

    ledger = _the_ledger(page)
    assert ">Ապրանք</th>" in ledger
    assert "HQD Cuvie ×3" in ledger, "the quantity too, or a 10,500 row is three vapes"


async def test_a_reversal_names_the_goods_that_came_back(client):
    """The void row is the one worth naming: it is negative, it is the row an owner
    stops at, and «Չեղարկում · -10,500 ֏» does not say what was un-sold."""
    _, _, worker, _ = await _a_completed_shift()
    await sales_service.void_last_sale(worker, "սխալ սեղմեցի")
    session_id = await db.fetchval("SELECT id FROM store_sessions")
    await login(client, "@ownerhandle")

    page = (await client.get(f"/reports?store_session_id={session_id}")).text

    assert _the_ledger(page).count("HQD Cuvie ×3") == 2, "the sale and the reversal of it"


async def test_a_wage_row_names_no_goods(client):
    """It bought none. An em dash there reads as goods nobody could name."""
    _, _, worker, _ = await _a_completed_shift()
    await shifts_service.end_shift(worker, None, None, "idem-key-end-01")
    session_id = await db.fetchval("SELECT id FROM store_sessions")
    await login(client, "@ownerhandle")

    page = (await client.get(f"/reports?store_session_id={session_id}")).text

    ledger = _the_ledger(page)
    assert 'data-label="Ապրանք"></td>' in ledger, "the wage row, with the cell left blank"
    assert "—" not in ledger.split('data-label="Ապրանք"')[1].split("</td>")[0]


async def test_the_long_tables_scroll_inside_themselves(client):
    """Fifty shifts, an evening of receipts and every movement of the drawer, each
    growing without a ceiling: reading the ledger meant scrolling past all of it, and
    getting back to the list meant scrolling past all of it again."""
    _, _, worker, _ = await _a_completed_shift()
    await shifts_service.end_shift(worker, None, None, "idem-key-end-01")
    session_id = await db.fetchval("SELECT id FROM store_sessions")
    await login(client, "@ownerhandle")

    page = (await client.get(f"/reports?store_session_id={session_id}")).text

    assert page.count("table-wrap table-scroll") == 3, "the list, the receipts, the ledger"


async def test_taking_money_out_of_the_till_is_not_hidden_behind_a_triangle(client):
    """It was collapsed at the foot of the ledger and got reported as missing — the
    owner could find no way to take cash out of the drawer, and this was it. Same
    lesson as «Ավելացնել մոռացված վաճառք», which was opened for the same reason."""
    _, _, worker, _ = await _a_completed_shift()
    await shifts_service.end_shift(worker, None, None, "idem-key-end-01")
    session_id = await db.fetchval("SELECT id FROM store_sessions")
    await login(client, "@ownerhandle")

    page = await client.get(f"/reports?store_session_id={session_id}")

    form = re.search(r'<details[^>]*id="add-movement"[^>]*>', page.text)
    assert form and "open" in form.group(0), "the form is folded away again"
    assert "Հանել դրամարկղից" in page.text
    assert 'href="#add-movement"' in page.text, "and it is pointed at from the till figures"


async def test_a_voided_receipt_stays_visible_with_who_voided_it(client):
    """A worker undoing their own slip must not be able to hide it."""
    _, _, worker, _ = await _a_completed_shift()
    await sales_service.void_last_sale(worker, "սխալ սեղմեցի")
    session_id = await db.fetchval("SELECT id FROM store_sessions")
    await login(client, "@ownerhandle")

    response = await client.get(f"/reports?store_session_id={session_id}")

    assert "չեղարկվել է" in response.text
    assert "voided" in response.text, "the row is struck through, not removed"
    assert "Չեղարկում" in response.text, "the reversing ledger entry is shown too"


# -- putting the receipts in an order ----------------------------------------

async def _an_evening_of_three_sales():
    """Sold newest-first as Zeta, Aokit, Muji — so no order matches another."""
    owner_id, store_id, worker, _ = await _a_completed_shift()
    for index, name in enumerate(["Muji 20000", "aokit 50000", "Zeta 10000"]):
        item_id = await make_item(owner_id, store_id, name, count=10, sell_price="1000.00")
        await sales_service.record_sale(
            worker, [{"item_id": item_id, "quantity": 1}], "cash", f"idem-sorted-{index}"
        )
    session_id = await db.fetchval("SELECT id FROM store_sessions")
    return worker, session_id


def _receipts_table(page: str) -> str:
    """Only the receipt rows. The same item names appear again further down the page —
    in the «forgotten sale» picker and in the stock table — and reading order off the
    whole document would be reading those."""
    start = page.index('id="receipts"')
    return page[start:page.index("Ավելացնել մոռացված վաճառք", start)]


def _order_of(page: str, names: list[str]) -> list[str]:
    rows = _receipts_table(page)
    return sorted(names, key=rows.index)


async def _csrf() -> str:
    return await db.fetchval(
        "SELECT csrf_token FROM auth_sessions ORDER BY created_at DESC LIMIT 1"
    )


async def test_receipts_are_newest_first_by_default(client):
    _, session_id = await _an_evening_of_three_sales()
    await login(client, "@ownerhandle")

    page = await client.get(f"/reports?store_session_id={session_id}")

    names = ["Zeta 10000", "aokit 50000", "Muji 20000", "HQD Cuvie"]
    assert _order_of(page.text, names) == names


async def test_receipts_can_be_read_in_dictionary_order(client):
    _, session_id = await _an_evening_of_three_sales()
    await login(client, "@ownerhandle")

    page = await client.get(f"/reports?store_session_id={session_id}&receipts=name")

    names = ["aokit 50000", "HQD Cuvie", "Muji 20000", "Zeta 10000"]
    assert _order_of(page.text, names) == names, "case is not a second alphabet"


async def test_the_chosen_order_is_the_one_shown_as_chosen(client):
    _, session_id = await _an_evening_of_three_sales()
    await login(client, "@ownerhandle")

    page = await client.get(f"/reports?store_session_id={session_id}&receipts=name")

    # Each link carries the other control's current value, so the URL has both in
    # it — choosing an order must not throw away which receipts are being shown.
    assert re.search(r'receipts=name&only=all#receipts"\s+class="is-on"', page.text)
    assert re.search(r'receipts=time&only=all#receipts"\s+class=""', page.text)


async def test_an_order_nobody_offers_reads_as_the_default(client):
    """A sort order is not worth a 400, and it must never reach the SQL."""
    _, session_id = await _an_evening_of_three_sales()
    await login(client, "@ownerhandle")

    page = await client.get(
        f"/reports?store_session_id={session_id}&receipts=id;DROP TABLE sales"
    )

    assert page.status_code == 200
    names = ["Zeta 10000", "aokit 50000", "Muji 20000"]
    assert _order_of(page.text, names) == names
    assert await db.fetchval("SELECT count(*) FROM sales") == 4


async def test_a_correction_leaves_the_table_in_the_order_it_was_in(client):
    """Editing a price with the rows in alphabetical order must not resort them."""
    _, session_id = await _an_evening_of_three_sales()
    await login(client, "@ownerhandle")
    page = await client.get(f"/reports?store_session_id={session_id}&receipts=name")
    item_id = await db.fetchval("SELECT id FROM items WHERE name = 'Zeta 10000'")
    sale_id = await db.fetchval(
        "SELECT sale_id FROM sale_items WHERE item_id = $1", item_id
    )
    back = re.search(rf'action="/sales/{sale_id}/amend\?back=([^"]+)"', page.text).group(1)

    saved = await client.post(
        f"/sales/{sale_id}/amend?back={back}",
        data={
            "csrf_token": await _csrf(),
            "item_id": str(item_id),
            "quantity": "1",
            "unit_price": "1200",
            "price_kind": "retail",
            "payment_method": "cash",
        },
    )
    landed = await client.get(saved.headers["location"])

    names = ["aokit 50000", "HQD Cuvie", "Muji 20000", "Zeta 10000"]
    assert _order_of(landed.text, names) == names
    assert "1,200.00" in landed.text


async def test_another_owners_session_is_not_reachable(client):
    _, _, worker, _ = await _a_completed_shift()
    session_id = await db.fetchval("SELECT id FROM store_sessions")

    other = await make_owner("@ownerother")
    await make_store(other)
    await login(client, "@ownerother")

    listing = await client.get("/reports")
    assert "Խանութ 1" not in listing.text

    detail = await client.get(f"/reports?store_session_id={session_id}")
    assert detail.status_code == 404


# -- showing one kind of receipt ---------------------------------------------

async def _an_evening_of_every_kind():
    """A cash sale, a card sale, and a delivery paid by card."""
    owner_id, store_id, worker, item_id = await _a_completed_shift()
    await sales_service.record_sale(
        worker, [{"item_id": item_id, "quantity": 1}], "card", "idem-card-1"
    )
    await sales_service.record_sale(
        worker, [{"item_id": item_id, "quantity": 1}], "card", "idem-delivery-1",
        is_delivery=True,
    )
    return await db.fetchval("SELECT id FROM store_sessions")


async def test_the_receipts_can_be_narrowed_to_one_kind(client):
    session_id = await _an_evening_of_every_kind()
    await login(client, "@ownerhandle")

    everything = await client.get(f"/reports?store_session_id={session_id}")
    cash = await client.get(f"/reports?store_session_id={session_id}&only=cash")
    card = await client.get(f"/reports?store_session_id={session_id}&only=card")
    delivery = await client.get(f"/reports?store_session_id={session_id}&only=delivery")

    assert _receipts_table(everything.text).count("<tr") == 4, "a header and three sales"
    assert _receipts_table(cash.text).count("<tr") == 2, "the opening cash sale"
    assert _receipts_table(card.text).count("<tr") == 3, "both card sales"
    assert _receipts_table(delivery.text).count("<tr") == 2, "the delivery, card or not"


async def test_the_kinds_overlap_on_purpose(client):
    """A delivery is paid in cash or by card like anything else, so «Առաքում» is not
    a third bucket beside them — it is a different question about the same rows."""
    session_id = await _an_evening_of_every_kind()
    await login(client, "@ownerhandle")

    card = await client.get(f"/reports?store_session_id={session_id}&only=card")

    assert card.text.count("առաքում") >= 1, "the delivery is in the card list too"


async def test_choosing_a_kind_keeps_the_order(client):
    """And the reverse. Two independent controls, each carrying the other."""
    session_id = await _an_evening_of_every_kind()
    await login(client, "@ownerhandle")

    page = await client.get(
        f"/reports?store_session_id={session_id}&receipts=name&only=card"
    )

    assert re.search(r'receipts=name&only=card#receipts"\s+class="is-on"', page.text)
    assert "receipts=name&only=delivery" in page.text, "the other filters keep the order"
    assert "receipts=time&only=card" in page.text, "and the other order keeps the filter"


async def test_a_kind_with_nothing_in_it_says_so(client):
    """Not the same thing as an evening with no sales, and it would read as one."""
    _, _, worker, _ = await _a_completed_shift()
    session_id = await db.fetchval("SELECT id FROM store_sessions")
    await login(client, "@ownerhandle")

    page = await client.get(f"/reports?store_session_id={session_id}&only=delivery")

    assert "Այս ընտրությամբ չեկ չկա" in page.text
    assert "Այս հերթափոխին վաճառք չի եղել" not in page.text


async def test_a_kind_nobody_offers_reads_as_all_of_them(client):
    session_id = await _an_evening_of_every_kind()
    await login(client, "@ownerhandle")

    page = await client.get(
        f"/reports?store_session_id={session_id}&only=1;DROP TABLE sales"
    )

    assert page.status_code == 200
    assert _receipts_table(page.text).count("<tr") == 4
    assert await db.fetchval("SELECT count(*) FROM sales") == 3


async def test_the_header_carries_the_delivery_figure(client):
    """It was only in a sentence underneath, and it is the split an owner asks
    about as often as the payment one."""
    session_id = await _an_evening_of_every_kind()
    await login(client, "@ownerhandle")

    page = await client.get(f"/reports?store_session_id={session_id}")

    assert "Առաքումով վաճառք" in page.text
    assert "3,500.00" in page.text
