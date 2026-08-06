"""The sell flow, driven step by step with fakes.

These are the cases that actually went wrong in the shop: an out-of-stock item
that let the cashier walk all the way to the payment buttons before refusing,
and names that break a Telegram HTML message.
"""

from __future__ import annotations

import pytest

from app import format, texts
from app.handlers import sell


class FakeMessage:
    def __init__(self, text: str = "") -> None:
        self.text = text
        self.replies: list[dict] = []

    async def reply_text(self, text, **kwargs):
        self.replies.append({"text": text, **kwargs})
        return FakeMessage()


class FakeQuery:
    def __init__(self, data: str) -> None:
        self.data = data
        self.message = FakeMessage()
        self.answers: list[dict] = []
        self.edits: list[str] = []
        self.markup_cleared = False

    async def answer(self, text=None, show_alert=False):
        self.answers.append({"text": text, "show_alert": show_alert})

    async def edit_message_text(self, text, **kwargs):
        self.edits.append(text)

    async def edit_message_reply_markup(self, reply_markup=None):
        self.markup_cleared = reply_markup is None


class FakeUpdate:
    def __init__(self, text: str = "", query: FakeQuery | None = None) -> None:
        self.effective_message = FakeMessage(text)
        self.callback_query = query


class FakeContext:
    def __init__(self, **user_data) -> None:
        self.user_data = dict(user_data)


def item(count: int, name: str = "HQD Cuvie", price: str = "2000.00") -> dict:
    return {"id": 7, "name": name, "count": count, "sell_price": price}


# -- out of stock ------------------------------------------------------------

async def test_an_out_of_stock_item_is_refused_at_the_moment_it_is_tapped():
    """The reported bug: it used to ask for a quantity, then a payment method,
    and only then say there were none."""
    query = FakeQuery("i:7")
    context = FakeContext(candidates={"7": item(count=0, name="asdasd")})

    state = await sell.choose_item(FakeUpdate(query=query), context)

    assert state == sell.ASK_ITEM, "stay on the list so another item can be picked"
    assert query.answers[0]["show_alert"] is True
    assert "asdasd" in query.answers[0]["text"]
    assert query.message.replies == [], "no quantity was asked for"
    assert "item" not in context.user_data


async def test_an_item_in_stock_is_accepted():
    query = FakeQuery("i:7")
    context = FakeContext(candidates={"7": item(count=3)})

    state = await sell.choose_item(FakeUpdate(query=query), context)

    assert state == sell.ASK_QUANTITY
    assert context.user_data["item"]["id"] == 7
    assert "3" in query.message.replies[0]["text"], "the cashier is told how many there are"


# -- quantity ----------------------------------------------------------------

async def test_asking_for_more_than_there_is_says_so_before_payment():
    update = FakeUpdate("5")
    context = FakeContext(item=item(count=2))

    state = await sell.choose_quantity(update, context)

    assert state == sell.ASK_QUANTITY, "ask again rather than moving on"
    reply = update.effective_message.replies[0]["text"]
    assert "2" in reply and "5" in reply
    assert "quantity" not in context.user_data


# "٣" is deliberately absent: Python's int() accepts Arabic-Indic digits and
# reads it as 3, which is the right answer rather than a bug.
@pytest.mark.parametrize("bad", ["", "abc", "0", "-3", "1.5", "2 հատ", "  "])
async def test_a_quantity_that_is_not_a_positive_number_is_refused(bad):
    update = FakeUpdate(bad)
    context = FakeContext(item=item(count=10))

    state = await sell.choose_quantity(update, context)

    assert state == sell.ASK_QUANTITY
    assert "quantity" not in context.user_data


async def test_an_absurd_quantity_is_refused_here_rather_than_by_the_server():
    update = FakeUpdate(str(sell.MAX_QUANTITY + 1))
    context = FakeContext(item=item(count=99_999_999))

    state = await sell.choose_quantity(update, context)

    assert state == sell.ASK_QUANTITY
    assert str(sell.MAX_QUANTITY) in update.effective_message.replies[0]["text"]


async def test_a_good_quantity_moves_on_to_payment():
    update = FakeUpdate("2")
    context = FakeContext(item=item(count=10, price="2000.00"))

    state = await sell.choose_quantity(update, context)

    assert state == sell.ASK_PAYMENT
    assert context.user_data["quantity"] == 2
    assert context.user_data["sale_key"], "the idempotency key is minted before money moves"
    assert "4,000" in update.effective_message.replies[0]["text"], "the total is shown"


async def test_exactly_all_the_stock_is_allowed():
    update = FakeUpdate("3")
    context = FakeContext(item=item(count=3))

    assert await sell.choose_quantity(update, context) == sell.ASK_PAYMENT


# -- names that would break a Telegram message -------------------------------

@pytest.mark.parametrize(
    "name",
    ["Blue Razz & Ice", "<b>bold</b>", "5 < 6", 'quote " mark', "M&M's"],
)
async def test_a_name_with_html_in_it_does_not_break_the_message(name):
    """Telegram parses these as HTML and rejects the whole message on a stray
    entity, so an ordinary name like "Blue Razz & Ice" would make the sale
    appear to hang."""
    update = FakeUpdate("1")
    context = FakeContext(item=item(count=5, name=name))

    await sell.choose_quantity(update, context)

    sent = update.effective_message.replies[0]["text"]
    # The name appears in its escaped form...
    assert format.esc(name) in sent
    # ...and never raw, which is what Telegram would choke on. (The template's
    # own <b> around the total is fine and expected; only the name matters.)
    if format.esc(name) != name:
        assert name not in sent


def test_escaping_leaves_ordinary_names_alone():
    assert format.esc("HQD Cuvie Plus") == "HQD Cuvie Plus"
    assert format.esc("Էլֆ Բար") == "Էլֆ Բար"
    assert format.esc(None) == ""


def test_escaping_neutralises_markup():
    assert format.esc("<b>x</b>") == "&lt;b&gt;x&lt;/b&gt;"
    assert format.esc("A & B") == "A &amp; B"


# -- the alert text has to survive a Telegram alert ---------------------------

def test_the_out_of_stock_alert_fits_telegrams_limit():
    """Alerts are capped at 200 characters and are shown as plain text."""
    message = texts.OUT_OF_STOCK_ALERT.format(item="X" * 40)

    assert len(message) <= 200
    assert "<" not in message
