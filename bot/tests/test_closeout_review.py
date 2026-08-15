"""What the worker is shown before they end their shift.

The screen that comes up on «🚪 Ավարտել իմ հերթափոխը». It used to be the sales
alone, which is not a review of a shift: a cashier who wrote off a broken pod, put
eight units back on the shelf and took 500 for a delivery driver saw none of it
back, and this is the last moment any of it can be corrected without the owner.

Two things had gone wrong here and both were about wording rather than money, which
is exactly the kind of thing a screenshot catches and a test should have:

* the confirm step said «Ցուցակը դատարկ է։ Այսօր վաճառք չի՞ եղել։» after a day of
  receipts, because "the list" meant the basket and the day was already in the
  books;
* nothing showed the drawer, so the number the worker was about to be asked for was
  a guess.
"""

from __future__ import annotations

from unittest import mock

from telegram import Chat, Message, Update, User

from app import texts
from app.handlers import closeout


def _shift(**overrides) -> dict:
    """Exactly what the web service sends back for ``GET /shift/review``."""
    payload = {
        "ok": True,
        "store_name": "Նուբարաշեն",
        "started_at": "2026-08-09T13:25:00+04:00",
        "sold": [{"name": "HQD Cuvie", "quantity": 2, "total": "6552.00"}],
        "totals": {"receipts": 1, "cash": "0.00", "card": "6552.00",
                   "total": "6552.00"},
        "written_off": [],
        "stock_fixed": [],
        "taken_out": [],
        "transfers": [],
        "till": {"cash": "1000.00", "card": "6552.00"},
        "store_float": "1000.00",
        "salary": {"due": "3500.00", "full": "7000.00", "full_shift_hours": 8},
    }
    payload.update(overrides)
    return payload


class _Context:
    def __init__(self, **data) -> None:
        self.user_data = dict(data)


def _update() -> Update:
    user = User(id=1, first_name="Անի", is_bot=False)
    return Update(
        update_id=1,
        message=Message(
            message_id=1, date=None, chat=Chat(id=1, type="private"),
            from_user=user, text=texts.BTN_END_SHIFT,
        ),
    )


async def _shown(payload, context=None) -> list[str]:
    """Every message ``begin`` sends, in order."""
    out: list[str] = []

    async def capture(self, text, *args, **kwargs):
        out.append(text)

    async def answer(telegram_id):
        if isinstance(payload, Exception):
            raise payload
        return payload

    with mock.patch.object(Message, "reply_text", capture), \
         mock.patch("app.handlers.closeout.api.review_shift", answer):
        await closeout.begin(_update(), context or _Context())
    return out


# -- the sections -------------------------------------------------------------

async def test_the_sales_already_in_the_books_are_listed():
    review = (await _shown(_shift()))[0]

    assert "HQD Cuvie" in review
    assert "6,552" in review


async def test_a_shift_with_no_sale_recorded_says_so():
    """The mistake this screen exists to catch. A whole day with nothing rung up is
    either a very quiet day or a cashier who never pressed anything, and only they
    can tell which."""
    review = (await _shown(_shift(sold=[], totals={
        "receipts": 0, "cash": "0.00", "card": "0.00", "total": "0.00"})))[0]

    assert texts.REVIEW_SOLD_NOTHING.strip() in review


async def test_breakage_is_listed_with_its_reason():
    review = (await _shown(_shift(written_off=[
        {"name": "Elf Bar", "quantity": 1, "reason": "ընկավ"},
    ])))[0]

    assert "Elf Bar" in review
    assert "ընկավ" in review


async def test_breakage_never_shows_what_it_cost():
    """What the shop paid for a vape is the owner's business. The loss is on the
    owner's report; the cashier needs to know it was recorded."""
    review = (await _shown(_shift(written_off=[
        {"name": "Elf Bar", "quantity": 1, "reason": "ընկավ"},
    ])))[0]

    assert "1,500" not in review, "a cost figure has no business on this screen"


async def test_shelf_corrections_are_listed_with_their_direction():
    review = (await _shown(_shift(stock_fixed=[
        {"name": "HQD Cuvie", "delta": 8, "count_after": 42},
        {"name": "Elf Bar", "delta": -3, "count_after": 5},
    ])))[0]

    assert "+8" in review, "an unsigned correction does not say which way it went"
    assert "-3" in review
    assert "42" in review


async def test_cash_taken_out_is_listed_with_what_it_was_for():
    review = (await _shown(_shift(taken_out=[
        {"amount": "500.00", "purpose": "առաքիչին"},
    ])))[0]

    assert "500" in review
    assert "առաքիչին" in review


async def test_stock_moved_between_shops_is_listed_with_its_direction():
    """An arrival was somebody else's decision, but it changed the shelf this worker is
    about to be held to."""
    review = (await _shown(_shift(transfers=[
        {"name": "HQD Cuvie", "quantity": 8, "incoming": True,
         "other_store": "Կենտրոն"},
        {"name": "Elf Bar", "quantity": 2, "incoming": False,
         "other_store": "Արաբկիր"},
    ])))[0]

    assert "⬅️" in review and "Կենտրոն" in review, "came in"
    assert "➡️" in review and "Արաբկիր" in review, "went out"


async def test_the_drawer_is_shown_before_the_worker_is_asked_to_count_it():
    """Otherwise the count that follows a few messages later is a guess."""
    review = (await _shown(_shift(till={"cash": "47000.00", "card": "6552.00"})))[0]

    assert "47,000" in review


async def test_the_wage_the_shift_will_pay_is_shown():
    review = (await _shown(_shift()))[0]

    assert "3,500" in review


async def test_a_short_shift_is_told_why_its_wage_is_half():
    """Before it is paid, not after. A worker who sees the reason while the shift is
    still open can decide to stay the extra hour."""
    review = (await _shown(_shift()))[0]

    assert "կիսով չափ" in review


async def test_a_full_shift_says_nothing_about_halving():
    review = (await _shown(_shift(
        salary={"due": "7000.00", "full": "7000.00", "full_shift_hours": 8})))[0]

    assert "կիսով չափ" not in review


async def test_a_wage_of_nothing_is_left_out_entirely():
    """A monthly wage costs the till nothing when a shift ends. Printing «0 ֏» beside
    "your wage" reads as not being paid."""
    review = (await _shown(_shift(
        salary={"due": "0.00", "full": "0.00", "full_shift_hours": 8})))[0]

    assert "աշխատավարձ" not in review.lower()


async def test_empty_sections_are_not_shown_at_all():
    """A heading with nothing under it reads as something that failed to load."""
    review = (await _shown(_shift()))[0]

    assert "Խոտան" not in review
    assert "Պահեստի ուղղումներ" not in review
    assert "Վերցված" not in review
    assert "Փոխանցումներ" not in review


async def test_a_populated_shift_shows_every_section():
    review = (await _shown(_shift(
        written_off=[{"name": "Elf Bar", "quantity": 1, "reason": "ընկավ"}],
        stock_fixed=[{"name": "HQD Cuvie", "delta": 8, "count_after": 42}],
        taken_out=[{"amount": "500.00", "purpose": "առաքիչին"}],
        transfers=[{"name": "Elf Bar", "quantity": 2, "incoming": True,
                    "other_store": "Կենտրոն"}],
    )))[0]

    for heading in ("Գրանցված վաճառք", "Խոտան", "Պահեստի ուղղումներ",
                    "Վերցված դրամարկղից", "Փոխանցումներ", "Դրամարկղը հիմա"):
        assert heading in review, f"«{heading}» is missing from the review"


async def test_a_missing_reason_reads_as_missing_rather_than_as_none():
    review = (await _shown(_shift(
        taken_out=[{"amount": "500.00", "purpose": None}])))[0]

    assert "None" not in review
    assert texts.REVIEW_NO_REASON in review


async def test_a_very_busy_shift_still_fits_in_one_message():
    """Telegram *rejects* a message over 4096 characters instead of trimming it, so an
    unbounded list does not make a long screen — it makes an empty one, on exactly the
    shifts that most need reading back."""
    review = (await _shown(_shift(
        sold=[{"name": f"Ապրանք {i}", "quantity": 3, "total": "9999.00"}
              for i in range(80)],
        written_off=[{"name": f"Ապրանք {i}", "quantity": 1, "reason": "ընկավ"}
                     for i in range(40)],
        stock_fixed=[{"name": f"Ապրանք {i}", "delta": -2, "count_after": 7}
                     for i in range(40)],
        taken_out=[{"amount": "100.00", "purpose": "մանրադրամ"} for _ in range(40)],
        transfers=[{"name": f"Ապրանք {i}", "quantity": 1, "incoming": True,
                    "other_store": "Կենտրոն"} for i in range(40)],
    )))[0]

    assert len(review) <= 4096, "Telegram would refuse this and show nothing"


async def test_what_was_cut_off_is_said_out_loud():
    """A list that quietly stops looks like a complete list, and this one is read to
    check a day against."""
    review = (await _shown(_shift(
        sold=[{"name": f"Ապրանք {i}", "quantity": 1, "total": "100.00"}
              for i in range(30)],
    )))[0]

    assert "5" in review
    assert texts.REVIEW_AND_MORE.format(more=5) in review


async def test_a_list_that_fits_says_nothing_about_more():
    review = (await _shown(_shift(
        sold=[{"name": f"Ապրանք {i}", "quantity": 1, "total": "100.00"}
              for i in range(closeout.MAX_ROWS)],
    )))[0]

    assert "ևս" not in review


async def test_the_write_up_still_opens_if_the_review_cannot_be_read():
    """A courtesy on the way in. The write-up is what has to work — a worker must
    never be unable to end their shift because a summary query failed."""
    from app.api import ApiUnavailable

    shown = await _shown(ApiUnavailable("no route to the server"))

    assert texts.CLOSEOUT_START in shown[-1]


# -- confirming what was typed ------------------------------------------------

def test_confirming_nothing_new_over_a_recorded_day_does_not_deny_the_day():
    """The bug, verbatim from the screenshot: a shift showing 6,552 of sales was
    asked «Այսօր վաճառք չի՞ եղել։» — reading, to the person who rang them up, as the
    bot having lost them."""
    summary = closeout._summary([], {"receipts": 1, "total": "6552.00"})

    assert "6,552" in summary
    assert texts.CLOSEOUT_EMPTY_BASKET not in summary


def test_confirming_nothing_new_on_a_genuinely_empty_day_still_asks():
    """Here the question is the right one: a whole shift with nothing recorded is
    worth querying before it is settled."""
    summary = closeout._summary([], {"receipts": 0, "total": "0.00"})

    assert summary == texts.CLOSEOUT_EMPTY_BASKET


def test_confirming_nothing_new_with_no_review_at_hand_still_asks():
    """The review call is best-effort, so the basket may be all the bot knows."""
    assert closeout._summary([], None) == texts.CLOSEOUT_EMPTY_BASKET


async def test_the_recorded_day_is_remembered_for_the_confirmation():
    """The confirmation is several taps later and cannot ask the server again — by
    then the shift may be mid-write-up."""
    context = _Context()
    await _shown(_shift(), context)

    assert context.user_data["co_recorded"]["receipts"] == 1


# -- an order that arrived by phone ------------------------------------------

def test_a_delivery_is_shown_under_its_own_heading():
    """The worker entered it and the stock left because of it, so the write-up has
    to show it — but nobody sold it over the counter, and «Գրանցված վաճառք» is the
    figure their bonus is measured against."""
    shift = _shift(
        delivered=[{"name": "Vanter 30000", "quantity": 4, "total": "20000.00"}],
        delivery_totals={"receipts": 1, "cash": "0.00", "card": "20000.00",
                         "total": "20000.00"},
    )

    body = closeout._shift_so_far(shift)

    assert "Առաքում" in body
    assert "Vanter 30000" in body, "which products left the shelf"
    assert "×4" in body, "and how many"
    assert "չի հաշվվում" in body, "it says the two are not the same thing"
    # The counter figure is untouched by it.
    assert "6,552" in body


def test_a_delivery_never_shows_the_worker_what_it_was_worth():
    """The worker did not sell it and is not measured on it, so what the shop took
    for it is the owner's business. The count is there so they can tell the orders
    they entered actually landed; the amount is not."""
    shift = _shift(
        delivered=[{"name": "Vanter 30000", "quantity": 4, "total": "20000.00"}],
        delivery_totals={"receipts": 1, "cash": "0.00", "card": "20000.00",
                         "total": "20000.00"},
    )

    body = closeout._shift_so_far(shift)

    assert "20,000" not in body
    assert "Պատվեր՝ 1" in body


def test_no_delivery_no_heading():
    """A heading with nothing under it reads as a thing that failed to load."""
    body = closeout._shift_so_far(_shift())

    assert "Առաքում" not in body


def test_an_older_server_that_does_not_send_the_field_still_renders():
    """The bot and the web service deploy separately, and one can be ahead."""
    shift = _shift()
    shift.pop("sold")

    body = closeout._shift_so_far(shift)

    assert texts.REVIEW_SOLD_NOTHING.strip() in body


# -- what the till section may and may not say --------------------------------

def test_the_till_section_never_reports_the_shops_card_income():
    """«Դրամարկղը հիմա — Կանխիկ՝ X · Քարտ՝ Y» had Y as the whole session's card
    takings: every delivery, and every colleague's sales, printed under this worker's
    own review of their own day. The drawer is the drawer; card money was never in
    it, and what this worker sold on card is in «Գրանցված վաճառք» a few lines up."""
    body = closeout._shift_so_far(
        _shift(till={"cash": "47000.00", "card": "88000.00"})
    )

    assert "47,000" in body, "the drawer"
    assert "88,000" not in body, "not the shop's card income"


def test_the_worker_still_sees_their_own_card_sales():
    """Removing the shop's figure must not remove theirs. It is the number their day
    is measured on, and it is the one they check against their receipts."""
    body = closeout._shift_so_far(
        _shift(
            totals={"receipts": 1, "cash": "0.00", "card": "6552.00", "total": "6552.00"},
            till={"cash": "47000.00", "card": "88000.00"},
        )
    )

    assert "6,552" in body


def test_a_delivery_paid_by_card_reaches_neither_figure():
    """The whole complaint, in one shift. A 3,000 card delivery is the shop's takings
    and not this cashier's selling — so it is in neither «Գրանցված վաճառք» nor the
    drawer line, and the only trace of it is the product and the count."""
    body = closeout._shift_so_far(
        _shift(
            totals={"receipts": 1, "cash": "0.00", "card": "6552.00", "total": "6552.00"},
            delivered=[{"name": "Vanter 30000", "quantity": 1, "total": "3000.00"}],
            delivery_totals={"receipts": 1, "cash": "0.00", "card": "3000.00",
                             "total": "3000.00"},
            till={"cash": "1000.00", "card": "9552.00"},
        )
    )

    assert "3,000" not in body, "the delivery's money is nowhere on the screen"
    assert "9,552" not in body, "nor the total it is part of"
    assert "6,552" in body, "their own card sales are"
    assert "Vanter 30000" in body, "and the stock that left is"


# -- the write-up the worker types --------------------------------------------

def _line(name: str, price: str, quantity: int, method: str, **extra) -> dict:
    line = {"name": name, "unit_price": price, "quantity": quantity,
            "payment_method": method}
    line.update(extra)
    return line


def test_the_write_up_totals_are_the_counter_alone():
    """A worker typing up four phone orders was shown a day they did not have. The
    three totals under the list are what they sold across the counter; the delivery
    lines are in the list, because the stock left and the books need them, but they
    are not this worker's selling and were never their bonus."""
    summary = closeout._summary([
        _line("HQD Cuvie", "3500.00", 2, "cash"),
        _line("Vanter 30000", "3000.00", 1, "card", is_delivery=True),
    ])

    assert "7,000" in summary, "the counter sale"
    assert "Ընդամենը՝ <b>7,000 ֏</b>" in summary, "and it is the whole total"
    assert "10,000" not in summary, "not the two added together"


def test_a_delivery_line_still_shows_what_was_typed():
    """The row keeps its own amount. This is the screen where a worker checks what
    they just entered before confirming it, and a line whose figure is hidden is a
    line they cannot check."""
    summary = closeout._summary([
        _line("Vanter 30000", "3000.00", 1, "card", is_delivery=True),
    ])

    assert "3,000" in summary, "the line they typed"
    assert "առաքում" in summary, "labelled as what it is"
    assert "Ընդամենը՝ <b>0 ֏</b>" in summary, "and none of it is their sale"


def test_a_list_with_a_delivery_in_it_says_why_the_totals_are_smaller():
    """Otherwise the arithmetic looks broken: four lines above, and a total that does
    not add up to them."""
    summary = closeout._summary([
        _line("HQD Cuvie", "3500.00", 2, "cash"),
        _line("Vanter 30000", "3000.00", 1, "card", is_delivery=True),
    ])

    assert texts.CLOSEOUT_SUMMARY_DELIVERY.strip() in summary


def test_an_ordinary_list_says_nothing_about_delivery():
    """A note about deliveries on a list with none is a sentence about nothing."""
    summary = closeout._summary([_line("HQD Cuvie", "3500.00", 2, "cash")])

    assert "առաքում" not in summary
    assert texts.CLOSEOUT_SUMMARY_DELIVERY.strip() not in summary


def test_the_split_by_payment_ignores_deliveries_too():
    """Both halves, not just the total. A card delivery landing in «Քարտ» is exactly
    the figure that was reported: the worker sold nothing on card and was shown
    3,000."""
    summary = closeout._summary([
        _line("HQD Cuvie", "3500.00", 1, "cash"),
        _line("Vanter 30000", "3000.00", 1, "card", is_delivery=True),
    ])

    assert "Քարտ՝ <b>0 ֏</b>" in summary
    assert "Կանխիկ՝ <b>3,500 ֏</b>" in summary
