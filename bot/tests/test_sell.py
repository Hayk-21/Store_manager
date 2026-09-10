"""Selling from the bot, and the shape of the answer it gets back.

The payload here is copied from what the web service actually returns for
``POST /sale`` — see ``_sale_payload`` in ``web/app/services/sales.py``. The
first version of this flow read ``result["items"]``, which does not exist, and
the failure was the expensive kind: the sale committed, then the bot crashed
rendering the reply and told the cashier it had failed. They would have entered
it again.

So there are two things under test. That the confirmation is built from the real
shape, and that *nothing after the server accepts the sale* can report a failure.
"""

from __future__ import annotations

from decimal import Decimal
from unittest import mock

import pytest
from telegram import CallbackQuery, Chat, Message, Update, User
from telegram.ext import ConversationHandler

from app import texts
from app.api import ApiError
from app.handlers import sell


def _server_answer(duplicate: bool = False) -> dict:
    """Exactly what the web service sends back for a one-line sale."""
    return {
        "ok": True,
        "duplicate": duplicate,
        "sale": {
            "id": 41,
            "store_id": 7,
            "session_id": 12,
            "payment_method": "cash",
            "total": "7000.00",
            "sold_at": "2026-08-08T13:28:36+04:00",
            "voided": False,
            "lines": [
                {
                    "item_id": 3,
                    "name": "HQD Cuvie",
                    "quantity": 2,
                    "unit_price": "3500.00",
                    "line_total": "7000.00",
                    "remaining_count": 15,
                }
            ],
        },
        # The drawer and the shop's card income. Sent, and never shown to a worker:
        # a delivery paid by card is in here and is not their sale.
        "store_totals": {"cash": "27000.00", "card": "4000.00"},
        # What this worker has sold across the counter, which is the only pair of
        # figures the confirmation may print.
        "sold_totals": {"cash": "7000.00", "card": "0.00"},
    }


class _Context:
    def __init__(self, **data) -> None:
        self.user_data = dict(data)


def _typed(text: str) -> Update:
    """A price the cashier wrote out, which arrives as an ordinary message."""
    user = User(id=1, first_name="Հայկ", is_bot=False)
    return Update(
        update_id=1,
        message=Message(
            message_id=1, date=None, chat=Chat(id=1, type="private"),
            from_user=user, text=text,
        ),
    )


def _tap(data: str) -> Update:
    user = User(id=1, first_name="Հայկ", is_bot=False)
    message = Message(
        message_id=1, date=None, chat=Chat(id=1, type="private"), from_user=user
    )
    return Update(
        update_id=1,
        callback_query=CallbackQuery(
            id="1", from_user=user, chat_instance="1", data=data, message=message
        ),
    )


# -- the confirmation ---------------------------------------------------------

def test_the_confirmation_is_built_from_the_real_payload():
    """The regression. Reading a key the server does not send crashed *after*
    the sale was committed."""
    body = sell._confirmation(_server_answer(), "cash")

    assert "HQD Cuvie" in body
    assert "7,000" in body
    assert "15" in body, "what is left on the shelf"
    assert texts.BTN_CASH in body


def test_a_replay_says_so_and_still_shows_the_sale():
    body = sell._confirmation(_server_answer(duplicate=True), "cash")

    assert texts.SALE_ALREADY_RECORDED in body
    assert "HQD Cuvie" in body


@pytest.mark.parametrize(
    "broken",
    [
        {"ok": True},                                   # nothing at all
        {"ok": True, "sale": {}},                       # no lines
        {"ok": True, "sale": {"lines": []}},            # empty lines
        {"ok": True, "sale": {"lines": [{}]}, "store_totals": {}},  # empty line
    ],
)
def test_a_confirmation_it_cannot_build_still_reads_as_success(broken):
    """The receipt is already written. A field missing from the response is a
    cosmetic problem, and saying "failed" would be the opposite of the truth."""
    body = sell._confirmation(broken, "cash")

    assert body == texts.SALE_RECORDED_PLAINLY
    assert "սխալ" not in body.lower()


# -- the price list, as a tickbox ---------------------------------------------
#
# The price list and the price used to be one decision: tapping «Մեծածախ» charged
# the wholesale price and moved straight on, and typing any other number filed the
# line as 'custom' — out of *both* lists. So a box haggled down was neither a retail
# sale nor a wholesale one, and «Մեծածախ» in the reports counted only the boxes that
# went at exactly the listed number.
#
# Now the box is ticked first and commits nothing, the way «Առաքում» does a screen
# later. «Այլ գին» changes the number under the tick and leaves the tick alone.

ITEM = {"id": 3, "name": "HQD Cuvie", "count": 9,
        "sell_price": "3500.00", "wholesale_price": "3000.00"}
NO_TRADE_PRICE = {"id": 3, "name": "HQD Cuvie", "count": 9,
                  "sell_price": "3500.00", "wholesale_price": None}


def _price_screen(item=ITEM, kind="retail", quantity=1, typed=None):
    from app import keyboards

    markup = keyboards.suggested_prices(item, kind, quantity, typed)
    return [b.text for row in markup.inline_keyboard for b in row], markup


def _button(markup, data):
    return next(
        (b.text for row in markup.inline_keyboard for b in row if b.callback_data == data),
        None,
    )


def test_both_price_lists_are_on_screen_with_their_prices():
    """The wholesale price went missing once by being absent from the item search
    payload, so every product looked like one nobody sells wholesale."""
    labels, _ = _price_screen()

    assert any("Մանրածախ" in label and "3,500" in label for label in labels)
    assert any("Մեծածախ" in label and "3,000" in label for label in labels)


def test_retail_is_ticked_to_begin_with():
    """Nearly every sale is retail, so the common case stays one tap on
    «Շարունակել» — the same single tap it has always been."""
    labels, _ = _price_screen()

    assert texts.BTN_RETAIL_ON in "".join(labels), "մանրածախ starts ticked"
    assert texts.BTN_WHOLESALE_OFF in "".join(labels), "and մեծածախ does not"


def test_ticking_wholesale_moves_the_tick_and_the_price():
    labels, markup = _price_screen(kind="wholesale", quantity=2)

    assert texts.BTN_WHOLESALE_ON in "".join(labels)
    assert texts.BTN_RETAIL_OFF in "".join(labels)
    from app import keyboards
    assert "6,000" in _button(markup, keyboards.CB_PRICE_OK), "2 × the trade price"


def test_the_continue_button_shows_what_the_customer_pays():
    """The line total, not the unit price. It is the number a cashier checks
    against the money in their hand before committing."""
    from app import keyboards

    _, markup = _price_screen(quantity=3)

    assert "10,500" in _button(markup, keyboards.CB_PRICE_OK)


def test_a_typed_price_replaces_the_number_not_the_tick():
    """The whole point of the change. A box sold wholesale after haggling is a
    wholesale sale, and filing it as neither list is what made «Մեծածախ» read low."""
    from app import keyboards

    labels, markup = _price_screen(kind="wholesale", quantity=2, typed=Decimal("2800.00"))

    assert texts.BTN_WHOLESALE_ON in "".join(labels), "still wholesale"
    assert "5,600" in _button(markup, keyboards.CB_PRICE_OK), "at the typed price"


def test_a_product_with_no_trade_price_falls_back_to_the_shelf_price():
    """With no separate trade price, the shelf price *is* the price, and the two
    ticks differ only in which list the line is filed under. The screen keeps the
    same shape either way — «Շարունակել» always goes, «Այլ գին» is always optional.
    It used to demand a typed number here, which made one tick behave unlike the
    other on exactly the products least likely to have their prices filled in."""
    from app import keyboards

    labels, markup = _price_screen(item=NO_TRADE_PRICE, kind="wholesale", quantity=2)

    assert any("Մեծածախ" in label and "3,500" in label for label in labels)
    assert "7,000" in _button(markup, keyboards.CB_PRICE_OK), "2 × the shelf price"
    assert _button(markup, f"{keyboards.CB_KIND}:other") == texts.BTN_OTHER_PRICE


class _Quiet:
    """Nothing on this step reaches Telegram in a test."""

    def __enter__(self):
        async def noop(*args, **kwargs):
            return None

        self._patches = [
            mock.patch.object(CallbackQuery, "answer", noop),
            mock.patch.object(CallbackQuery, "edit_message_reply_markup", noop),
            mock.patch.object(Message, "reply_text", noop),
        ]
        for patch in self._patches:
            patch.start()
        return self

    def __exit__(self, *exc):
        for patch in self._patches:
            patch.stop()


async def test_ticking_a_box_commits_nothing():
    """It redraws and stays put, exactly like the delivery box. A tap that sent the
    sale would leave a cashier who mistapped with a receipt to cancel."""
    context = _Context(sell_item=ITEM, sell_qty=2)

    with _Quiet():
        state = await sell.choose_suggested_price(_tap("k:wholesale"), context)

    assert context.user_data["sell_kind"] == "wholesale"
    assert "sell_price" not in context.user_data, "nothing is charged yet"
    assert state == sell.ASK_PRICE, "and the cashier is still on this step"


async def test_continue_charges_the_ticked_list():
    context = _Context(sell_item=ITEM, sell_qty=2, sell_kind="wholesale")

    with _Quiet():
        await sell.confirm_price(_tap("pk"), context)

    assert context.user_data["sell_price"] == Decimal("3000.00")
    assert context.user_data["sell_kind"] == "wholesale"


async def test_continue_after_haggling_keeps_the_ticked_list():
    """The regression this whole change exists to prevent."""
    context = _Context(sell_item=ITEM, sell_qty=1, sell_kind="wholesale")

    with _Quiet():
        await sell.type_price(_typed("2800"), context)
        await sell.confirm_price(_tap("pk"), context)

    assert context.user_data["sell_price"] == Decimal("2800.00")
    assert context.user_data["sell_kind"] == "wholesale", "not 'custom'"


async def test_changing_the_list_drops_a_number_typed_for_the_other_one():
    """«Մեծածախ» after typing 2,800 means the trade price, not 2,800 relabelled."""
    context = _Context(sell_item=ITEM, sell_qty=1, sell_kind="retail")

    with _Quiet():
        await sell.type_price(_typed("2800"), context)
        await sell.choose_suggested_price(_tap("k:wholesale"), context)
        await sell.confirm_price(_tap("pk"), context)

    assert context.user_data["sell_price"] == Decimal("3000.00")


# -- committing ---------------------------------------------------------------

async def test_a_recorded_sale_is_never_reported_as_a_failure():
    """Everything after the server accepts the sale is presentation. If any of
    it raises, the cashier must still be told the sale went through."""
    replies = []

    async def fake_sell(**kwargs):
        return {"ok": True}          # a shape the confirmation cannot render

    async def fake_reply(self, text, *args, **kwargs):
        replies.append(text)

    async def noop(*args, **kwargs):
        return None

    context = _Context(sell_item={"id": 3, "name": "HQD Cuvie"}, sell_qty=2,
                       sell_price=Decimal("3500.00"), sell_kind="retail")

    with (
        mock.patch.object(sell.api, "sell", fake_sell),
        mock.patch.object(CallbackQuery, "answer", noop),
        mock.patch.object(CallbackQuery, "edit_message_reply_markup", noop),
        mock.patch.object(Message, "reply_text", fake_reply),
    ):
        state = await sell.choose_method(_tap("p:cash"), context)

    assert state == ConversationHandler.END
    assert replies == [texts.SALE_RECORDED_PLAINLY]
    assert context.user_data == {}, "the basket is cleared either way"


async def test_a_refusal_from_the_server_is_reported_verbatim():
    """The other direction: when the sale genuinely did not happen, say so in
    the server's own words rather than inventing a sentence."""
    replies = []

    async def fake_sell(**kwargs):
        raise ApiError("insufficient_stock", "«HQD Cuvie» — պահեստում կա 1 հատ։")

    async def fake_reply(self, text, *args, **kwargs):
        replies.append(text)

    async def noop(*args, **kwargs):
        return None

    context = _Context(sell_item={"id": 3, "name": "HQD Cuvie"}, sell_qty=9,
                       sell_price=Decimal("3500.00"))

    with (
        mock.patch.object(sell.api, "sell", fake_sell),
        mock.patch.object(CallbackQuery, "answer", noop),
        mock.patch.object(CallbackQuery, "edit_message_reply_markup", noop),
        mock.patch.object(Message, "reply_text", fake_reply),
    ):
        state = await sell.choose_method(_tap("p:cash"), context)

    assert state == ConversationHandler.END
    assert replies == ["«HQD Cuvie» — պահեստում կա 1 հատ։"]


async def test_the_price_and_its_kind_reach_the_server():
    """Picking «Մեծածախ» has to arrive as a wholesale line, or the reports
    cannot tell a wholesale run from a discount."""
    sent = {}

    async def fake_sell(**kwargs):
        sent.update(kwargs)
        return _server_answer()

    async def noop(*args, **kwargs):
        return None

    async def fake_reply(self, *args, **kwargs):
        return None

    context = _Context(sell_item={"id": 3, "name": "HQD Cuvie"}, sell_qty=2,
                       sell_price=Decimal("3000.00"), sell_kind="wholesale")

    with (
        mock.patch.object(sell.api, "sell", fake_sell),
        mock.patch.object(CallbackQuery, "answer", noop),
        mock.patch.object(CallbackQuery, "edit_message_reply_markup", noop),
        mock.patch.object(Message, "reply_text", fake_reply),
    ):
        await sell.choose_method(_tap("p:card"), context)

    assert sent["price_kind"] == "wholesale"
    assert sent["unit_price"] == "3000.00"
    assert sent["payment_method"] == "card"
    assert sent["quantity"] == 2


# -- delivery -----------------------------------------------------------------

async def test_ticking_delivery_sends_nothing():
    """The whole point of it being a tickbox. It only redraws the keyboard, so
    the cashier sees the box filled in and then decides how it was paid."""
    sent = []

    async def fake_sell(**kwargs):
        sent.append(kwargs)
        return _server_answer()

    async def noop(*args, **kwargs):
        return None

    context = _Context(sell_item={"id": 3, "name": "HQD Cuvie"}, sell_qty=1,
                       sell_price=Decimal("3500.00"))

    with (
        mock.patch.object(sell.api, "sell", fake_sell),
        mock.patch.object(CallbackQuery, "answer", noop),
        mock.patch.object(CallbackQuery, "edit_message_reply_markup", noop),
    ):
        state = await sell.toggle_delivery(_tap("dl"), context)

    assert sent == [], "no sale was recorded by ticking the box"
    assert state == sell.ASK_METHOD, "and the cashier is still choosing how it was paid"
    assert context.user_data["sell_delivery"] is True


async def test_ticking_it_twice_unticks_it():
    """A mistap has to be undoable, which a button that sent the sale would not
    have allowed."""
    async def noop(*args, **kwargs):
        return None

    context = _Context(sell_item={"id": 3, "name": "HQD Cuvie"}, sell_qty=1,
                       sell_price=Decimal("3500.00"))

    with (
        mock.patch.object(CallbackQuery, "answer", noop),
        mock.patch.object(CallbackQuery, "edit_message_reply_markup", noop),
    ):
        await sell.toggle_delivery(_tap("dl"), context)
        await sell.toggle_delivery(_tap("dl"), context)

    assert context.user_data["sell_delivery"] is False


async def test_the_ticked_box_reaches_the_server_when_cash_is_tapped():
    sent = {}

    async def fake_sell(**kwargs):
        sent.update(kwargs)
        return _server_answer()

    async def noop(*args, **kwargs):
        return None

    async def fake_reply(self, *args, **kwargs):
        return None

    context = _Context(sell_item={"id": 3, "name": "HQD Cuvie"}, sell_qty=1,
                       sell_price=Decimal("3500.00"))

    with (
        mock.patch.object(sell.api, "sell", fake_sell),
        mock.patch.object(CallbackQuery, "answer", noop),
        mock.patch.object(CallbackQuery, "edit_message_reply_markup", noop),
        mock.patch.object(Message, "reply_text", fake_reply),
    ):
        await sell.toggle_delivery(_tap("dl"), context)
        await sell.choose_method(_tap("p:cash"), context)

    assert sent["is_delivery"] is True
    assert sent["payment_method"] == "cash", "ticking it did not replace the method"
    assert sent["unit_price"] == "3500.00", "and it did not change the money"


async def test_an_ordinary_sale_is_not_a_delivery():
    sent = {}

    async def fake_sell(**kwargs):
        sent.update(kwargs)
        return _server_answer()

    async def noop(*args, **kwargs):
        return None

    async def fake_reply(self, *args, **kwargs):
        return None

    context = _Context(sell_item={"id": 3, "name": "HQD Cuvie"}, sell_qty=1,
                       sell_price=Decimal("3500.00"))

    with (
        mock.patch.object(sell.api, "sell", fake_sell),
        mock.patch.object(CallbackQuery, "answer", noop),
        mock.patch.object(CallbackQuery, "edit_message_reply_markup", noop),
        mock.patch.object(Message, "reply_text", fake_reply),
    ):
        await sell.choose_method(_tap("p:card"), context)

    assert sent["is_delivery"] is False


def test_there_is_one_delivery_tickbox_and_two_ways_to_pay():
    """Not four buttons saying two things. Cash at the door and card in advance
    are both deliveries, so the box is separate from the method."""
    from app import keyboards

    rows = keyboards.payment_methods().inline_keyboard
    data = [b.callback_data for row in rows for b in row]

    assert data.count("dl") == 1
    assert [d for d in data if d.startswith("p:")] == ["p:cash", "p:card"]


def test_the_tickbox_shows_whether_it_is_ticked():
    """It is the only feedback there is — the message text does not change."""
    from app import keyboards

    def label(is_delivery):
        return keyboards.payment_methods(is_delivery).inline_keyboard[0][0].text

    assert label(False) == texts.BTN_DELIVERY_OFF
    assert label(True) == texts.BTN_DELIVERY_ON
    assert label(False) != label(True)


async def test_one_sale_keeps_one_key_across_retries():
    """The idempotency key is minted once per sale, so a retry after a timeout
    resolves to the receipt already written instead of selling twice."""
    keys = []

    async def fake_sell(**kwargs):
        keys.append(kwargs["key"])
        raise ApiError("internal", "…")

    async def noop(*args, **kwargs):
        return None

    async def fake_reply(self, *args, **kwargs):
        return None

    context = _Context(sell_item={"id": 3, "name": "HQD Cuvie"}, sell_qty=1,
                       sell_price=Decimal("3500.00"))

    with (
        mock.patch.object(sell.api, "sell", fake_sell),
        mock.patch.object(CallbackQuery, "answer", noop),
        mock.patch.object(CallbackQuery, "edit_message_reply_markup", noop),
        mock.patch.object(Message, "reply_text", fake_reply),
    ):
        await sell.choose_method(_tap("p:cash"), context)
        # The failure left the key behind so the next attempt reuses it.
        context.user_data.update(
            sell_item={"id": 3, "name": "HQD Cuvie"}, sell_qty=1,
            sell_price=Decimal("3500.00"), sell_key=keys[0],
        )
        await sell.choose_method(_tap("p:cash"), context)

    assert keys[0] == keys[1]


# -- whose money is on the confirmation --------------------------------------

def test_the_confirmation_shows_the_counter_and_not_the_drawer():
    """It used to show the drawer and the shop's card income, so a cashier who had
    taken one card payment of their own read «Քարտ՝ 16,000» — 3,000 of which was
    somebody else's delivery, paid in advance."""
    result = _server_answer()
    result["sold_totals"] = {"cash": "7000.00", "card": "0.00"}

    body = sell._confirmation(result, "cash")

    assert "7,000" in body
    assert "27,000" not in body, "the drawer is not this worker's sales"
    assert "4,000" not in body, "and neither is the shop's card income"


def test_a_service_that_sends_no_counter_figures_shows_none_at_all():
    """There used to be a fallback here to the drawer, for a server older than the
    bot. What it actually did was bring the wrong figure back under the right label —
    «Ձեր վաճառքը՝ քարտ 4,000» over a card sale the worker never made.

    The sale is in the books either way, so the honest answer when the figures cannot
    be trusted is to print none of them.
    """
    result = _server_answer()
    result.pop("sold_totals", None)

    body = sell._confirmation(result, "cash")

    assert body == texts.SALE_RECORDED_PLAINLY
    assert "27,000" not in body, "no falling back to the drawer"
    assert "4,000" not in body, "nor to the shop's card income"
