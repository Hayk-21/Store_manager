"""What the worker is told about money that was in the drawer before they arrived.

A shop keeps a float overnight: whatever the last person to lock up counted and said
they were leaving. It is deposited into the next session's till the moment somebody
opens up, so it is in every cash figure the bot shows from the first minute of the
shift — and until now nothing said so. A cashier who sold 5,000 in cash and was shown
6,000 in the drawer had no way to tell that 1,000 of it was never theirs, and the
natural reading of the difference is "I must have miscounted my own sales".

Two screens say it now: the greeting when the shop is opened, and «Վիճակ». Both take
their figures straight off the API, so both have to survive a server that is older
than the bot and does not send them at all.
"""

from __future__ import annotations

from app import format, texts

# -- opening up ---------------------------------------------------------------

def test_the_morning_drawer_is_all_float():
    """The ordinary case: first shift of the day, nothing sold yet, and every dram
    in the till was left there by somebody else."""
    line = format.drawer_line({"cash": "1000.00", "carried_in": "1000.00"})
    assert "1,000" in line
    assert "նախորդ հերթափոխից" in line


def test_joining_a_running_shop_says_which_part_is_the_float():
    """A worker arriving at two in the afternoon finds a drawer that has been trading
    since morning. The float is still the float; it is just no longer all of it."""
    line = format.drawer_line({"cash": "46000.00", "carried_in": "1000.00"})
    assert "46,000" in line
    assert "1,000" in line
    assert "որից" in line


def test_an_empty_drawer_is_not_worth_a_line():
    """«Դրամարկղում՝ 0 ֏» on every opening is a sentence that says only that the
    feature exists."""
    assert format.drawer_line({"cash": "0.00", "carried_in": "0.00"}) == ""


def test_a_shop_that_kept_nothing_overnight_says_nothing():
    """No float means nobody left anything for this worker to be answerable for. The
    cash in the drawer is then the session's own takings, which «Վիճակ» already
    reports."""
    assert format.drawer_line({"cash": "46000.00", "carried_in": "0.00"}) == ""


def test_an_older_server_sends_no_drawer_at_all():
    """A bot deployed ahead of its server must open the shift, not crash on it."""
    assert format.drawer_line(None) == ""
    assert format.drawer_line({}) == ""


def test_the_greeting_still_renders_with_the_drawer_in_it():
    body = texts.SHIFT_OPENED.format(
        store="Նուբարաշեն",
        distance=12,
        minutes=15,
        drawer=format.drawer_line({"cash": "1000.00", "carried_in": "1000.00"}),
    )
    assert "1,000" in body
    assert "Դուք հերթափոխի մեջ եք" in body


def test_the_greeting_reads_the_same_as_before_on_an_empty_drawer():
    """The slot is empty, not blank-looking: no stray line, no double gap."""
    body = texts.SHIFT_OPENED.format(
        store="Նուբարաշեն", distance=12, minutes=15, drawer=""
    )
    assert "\n\n\n" not in body


# -- «Վիճակ» -------------------------------------------------------------------

def test_the_status_says_how_much_of_the_till_was_already_there():
    """The user's own example: 1,000 left behind, 5,000 sold for cash, 6,000 in the
    drawer — and the worker can see which is which."""
    line = format.carried_line({"cash": "6000.00", "carried_in": "1000.00"})
    assert "1,000" in line
    assert "նախորդ հերթափոխից" in line


def test_nothing_carried_in_says_nothing():
    assert format.carried_line({"cash": "6000.00", "carried_in": "0.00"}) == ""


def test_a_float_that_is_the_whole_drawer_is_not_a_part_of_it():
    """«Որից 1,000» under «Դրամարկղում կանխիկ՝ 1,000» is a sentence about nothing.
    The shift has taken no cash yet, and the figure above already said so."""
    assert format.carried_line({"cash": "1000.00", "carried_in": "1000.00"}) == ""


def test_the_status_survives_an_older_server():
    assert format.carried_line({"cash": "6000.00"}) == ""
    assert format.carried_line(None) == ""


def test_a_junk_figure_is_read_as_nothing_rather_than_thrown():
    """Whatever went wrong upstream, the worker still gets their status screen."""
    assert format.carried_line({"cash": "6000.00", "carried_in": "—"}) == ""


def test_the_status_renders_with_the_line_in_it():
    body = texts.STATUS.format(
        store="Նուբարաշեն",
        since="09:00",
        duration="5ժ 00ր",
        receipts=3,
        sold="15,000 ֏",
        deliveries="",
        cash=format.money("6000.00"),
        carried=format.carried_line({"cash": "6000.00", "carried_in": "1000.00"}),
    )
    assert "6,000" in body
    assert "1,000" in body
    # The drawer line stays under the drawer figure, not under the worker's sales.
    assert body.index("Դրամարկղում կանխիկ") < body.index("նախորդ հերթափոխից")
