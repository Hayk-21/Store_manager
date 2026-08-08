"""The correction screen: the whole shelf, a − and a + against each product.

The behaviour worth pinning down is that nothing is written until «Հաստատել». Until
then the numbers on screen are a draft, so a mistap costs another tap rather than a
wrong stock count — and untapping has to work.
"""

from __future__ import annotations

from unittest import mock

import pytest
from telegram import CallbackQuery, Chat, Message, Update, User
from telegram.ext import ConversationHandler

from app import keyboards, texts
from app.api import ApiError, ApiUnavailable
from app.handlers import restock


def _server_answer() -> dict:
    """Exactly what the web service sends back for ``POST /items/adjust``."""
    return {
        "ok": True,
        "duplicate": False,
        "adjusted": [
            {"id": 1, "item_id": 3, "name": "HQD Cuvie", "delta": 4, "count_after": 14},
        ],
    }


class _Context:
    def __init__(self, **data) -> None:
        self.user_data = dict(data)


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


def _typed(text: str) -> Update:
    user = User(id=1, first_name="Հայկ", is_bot=False)
    return Update(
        update_id=1,
        message=Message(
            message_id=1, date=None, chat=Chat(id=1, type="private"),
            from_user=user, text=text,
        ),
    )


def _loaded(**overrides) -> _Context:
    """The screen, with three products on it and nothing changed yet."""
    return _Context(**{
        "rs_items": [
            {"id": 3, "name": "HQD Cuvie", "count": 10},
            {"id": 4, "name": "Waka 1800", "count": 2},
            {"id": 5, "name": "Geek Bar", "count": 0},
        ],
        "rs_page": 0,
    } | overrides)


# -- loading the shelf -------------------------------------------------------

async def test_the_list_is_asked_for_within_the_endpoints_ceiling():
    """It was asked for 200, and the endpoint caps `limit` at 100 — so the screen
    answered 422 and the cashier was told nothing useful. A number the server will
    refuse is not a generous default."""
    asked = {}

    async def fake_search(telegram_id, query, limit=8):
        asked.update({"limit": limit})
        return {"ok": True, "store_name": "Խանութ 1", "items": []}

    async def fake_reply(self, *args, **kwargs):
        return None

    with (
        mock.patch.object(restock.api, "search_items", fake_search),
        mock.patch.object(Message, "reply_text", fake_reply),
    ):
        await restock.begin(_typed("x"), _Context())

    assert asked["limit"] <= 100


async def test_an_empty_shelf_still_offers_the_other_door():
    """They pressed the button because a delivery arrived; it may be the shop's
    first product."""
    async def fake_search(telegram_id, query, limit=8):
        return {"ok": True, "store_name": "Խանութ 1", "items": []}

    markups = []

    async def fake_reply(self, text, *args, **kwargs):
        markups.append(kwargs.get("reply_markup"))

    context = _Context()
    with (
        mock.patch.object(restock.api, "search_items", fake_search),
        mock.patch.object(Message, "reply_text", fake_reply),
    ):
        state = await restock.begin(_typed("x"), context)

    assert state == restock.PICK
    data = [b.callback_data for row in markups[-1].inline_keyboard for b in row]
    assert keyboards.CB_NEW_ITEM in data


# -- the draft ---------------------------------------------------------------

async def test_a_plus_changes_nothing_but_the_draft():
    sent = []

    async def fake_adjust(**kwargs):
        sent.append(kwargs)
        return _server_answer()

    async def noop(*args, **kwargs):
        return None

    context = _loaded()
    with (
        mock.patch.object(restock.api, "adjust_stock", fake_adjust),
        mock.patch.object(CallbackQuery, "answer", noop),
        mock.patch.object(CallbackQuery, "edit_message_text", noop),
    ):
        state = await restock.nudge(_tap("n:3:1"), context)

    assert sent == [], "nothing was written"
    assert state == restock.PICK
    assert context.user_data["rs_deltas"] == {"3": 1}


async def test_tapping_plus_three_times_counts_to_three():
    async def noop(*args, **kwargs):
        return None

    context = _loaded()
    with (
        mock.patch.object(CallbackQuery, "answer", noop),
        mock.patch.object(CallbackQuery, "edit_message_text", noop),
    ):
        for _ in range(3):
            await restock.nudge(_tap("n:3:1"), context)

    assert context.user_data["rs_deltas"] == {"3": 3}


async def test_a_minus_undoes_a_plus_and_leaves_no_trace():
    """Back to zero is not "a change of zero" — it is no change, and the confirm
    button should disappear again."""
    async def noop(*args, **kwargs):
        return None

    context = _loaded()
    with (
        mock.patch.object(CallbackQuery, "answer", noop),
        mock.patch.object(CallbackQuery, "edit_message_text", noop),
    ):
        await restock.nudge(_tap("n:3:1"), context)
        await restock.nudge(_tap("n:3:-1"), context)

    assert context.user_data["rs_deltas"] == {}


async def test_stock_cannot_be_pushed_below_zero():
    """The shelf cannot hold less than nothing. Said out loud rather than ignored,
    so a cashier does not think the button is broken."""
    answers = []

    async def fake_answer(self, text=None, **kwargs):
        answers.append(text)

    async def noop(*args, **kwargs):
        return None

    context = _loaded()
    with (
        mock.patch.object(CallbackQuery, "answer", fake_answer),
        mock.patch.object(CallbackQuery, "edit_message_text", noop),
    ):
        state = await restock.nudge(_tap("n:5:-1"), context)

    assert state == restock.PICK
    assert context.user_data.get("rs_deltas", {}) == {}
    assert texts.RESTOCK_AT_ZERO in answers


async def test_going_down_to_zero_is_allowed():
    """Emptying a shelf is a real correction; going past empty is not."""
    async def noop(*args, **kwargs):
        return None

    context = _loaded()
    with (
        mock.patch.object(CallbackQuery, "answer", noop),
        mock.patch.object(CallbackQuery, "edit_message_text", noop),
    ):
        await restock.nudge(_tap("n:4:-1"), context)
        await restock.nudge(_tap("n:4:-1"), context)

    assert context.user_data["rs_deltas"] == {"4": -2}


async def test_a_stale_arrow_does_not_report_an_error():
    """Telegram refuses an edit that changes nothing and answers with an error, so
    turning past the last page from an old keyboard used to tell the worker
    "unexpected error" for tapping an arrow. Nothing changed, so nothing is said."""
    from telegram.error import BadRequest

    async def noop(*args, **kwargs):
        return None

    async def refuse(*args, **kwargs):
        raise BadRequest("Message is not modified")

    context = _loaded(rs_page=0)
    with (
        mock.patch.object(CallbackQuery, "answer", noop),
        mock.patch.object(CallbackQuery, "edit_message_text", refuse),
    ):
        state = await restock.turn_page(_tap(f"{keyboards.CB_PAGE}:-1"), context)

    assert state == restock.PICK


async def test_a_real_edit_failure_is_still_raised():
    """Only "not modified" is swallowed. Anything else is a genuine fault and the
    error handler should see it rather than have it quietly dropped."""
    from telegram.error import BadRequest

    async def noop(*args, **kwargs):
        return None

    async def refuse(*args, **kwargs):
        raise BadRequest("MESSAGE_ID_INVALID")

    with (
        mock.patch.object(CallbackQuery, "answer", noop),
        mock.patch.object(CallbackQuery, "edit_message_text", refuse),
        pytest.raises(BadRequest),
    ):
        await restock.nudge(_tap("n:3:1"), _loaded())


async def test_a_stale_confirm_answers_the_tap_exactly_once():
    """Answering a callback twice is itself an error. The confirm button is only
    drawn once something has changed, so reaching it with an empty draft means the
    keyboard is old."""
    answers = []

    async def count_answer(self, *args, **kwargs):
        answers.append(1)

    async def fake_adjust(**kwargs):
        raise AssertionError("nothing should be sent for an empty draft")

    context = _loaded()
    with (
        mock.patch.object(restock.api, "adjust_stock", fake_adjust),
        mock.patch.object(CallbackQuery, "answer", count_answer),
    ):
        state = await restock.submit(_tap(keyboards.CB_APPLY), context)

    assert state == restock.PICK
    assert len(answers) == 1


# -- the keyboard ------------------------------------------------------------

def test_the_confirm_button_appears_only_once_something_changed():
    """An always-present confirm on an unchanged list invites a tap that does
    nothing and teaches the cashier the button is unreliable."""
    items = [{"id": 3, "name": "HQD Cuvie", "count": 10}]

    unchanged = keyboards.restock(items, {}, page=0, pages=1, has_changes=False)
    changed = keyboards.restock(items, {"3": 2}, page=0, pages=1, has_changes=True)

    def data(markup):
        return [b.callback_data for row in markup.inline_keyboard for b in row]

    assert keyboards.CB_APPLY not in data(unchanged)
    assert keyboards.CB_APPLY in data(changed)


def test_each_row_has_a_minus_and_a_plus():
    markup = keyboards.restock(
        [{"id": 3, "name": "HQD Cuvie", "count": 10}],
        {}, page=0, pages=1, has_changes=False,
    )

    row = markup.inline_keyboard[0]
    assert [b.callback_data for b in row] == ["n:3:-1", keyboards.CB_NOOP, "n:3:1"]


def test_the_label_shows_where_the_count_is_heading():
    markup = keyboards.restock(
        [{"id": 3, "name": "HQD Cuvie", "count": 10}],
        {"3": 4}, page=0, pages=1, has_changes=True,
    )

    label = markup.inline_keyboard[0][1].text
    assert "14" in label
    assert "+4" in label


def test_a_long_list_is_paged_rather_than_truncated():
    items = [{"id": i, "name": f"Ապրանք {i}", "count": 1} for i in range(1, 20)]

    markup = keyboards.restock(items[:6], {}, page=0, pages=4, has_changes=False)

    data = [b.callback_data for row in markup.inline_keyboard for b in row]
    assert f"{keyboards.CB_PAGE}:1" in data
    assert f"{keyboards.CB_PAGE}:-1" not in data, "no back button on the first page"


def test_the_last_page_has_no_forward_button():
    markup = keyboards.restock(
        [{"id": 1, "name": "Ապրանք", "count": 1}], {}, page=3, pages=4, has_changes=False
    )

    data = [b.callback_data for row in markup.inline_keyboard for b in row]
    assert f"{keyboards.CB_PAGE}:-1" in data
    assert f"{keyboards.CB_PAGE}:1" not in data


def test_a_brand_new_product_is_always_one_tap_away():
    """The cashier came here because a delivery arrived; it may be something the
    shop has never stocked."""
    for markup in (
        keyboards.restock([], {}, page=0, pages=1, has_changes=False),
        keyboards.restock_empty(),
    ):
        data = [b.callback_data for row in markup.inline_keyboard for b in row]
        assert keyboards.CB_NEW_ITEM in data


# -- committing --------------------------------------------------------------

async def test_confirming_sends_every_pending_change_at_once():
    """One request, because the cashier counted the shelf once. A request per
    product would let half of it land, and the half that failed is invisible."""
    sent = {}

    async def fake_adjust(**kwargs):
        sent.update(kwargs)
        return _server_answer()

    async def noop(*args, **kwargs):
        return None

    async def fake_reply(self, *args, **kwargs):
        return None

    context = _loaded(rs_deltas={"3": 4, "4": -1})
    with (
        mock.patch.object(restock.api, "adjust_stock", fake_adjust),
        mock.patch.object(CallbackQuery, "answer", noop),
        mock.patch.object(CallbackQuery, "edit_message_reply_markup", noop),
        mock.patch.object(Message, "reply_text", fake_reply),
    ):
        state = await restock.submit(_tap(keyboards.CB_APPLY), context)

    assert state == ConversationHandler.END
    assert sorted(sent["lines"], key=lambda line: line["item_id"]) == [
        {"item_id": 3, "delta": 4},
        {"item_id": 4, "delta": -1},
    ]
    assert context.user_data == {}


async def test_a_refusal_keeps_the_draft_so_one_line_can_be_fixed():
    """Throwing away four right answers because the fifth was wrong is not help."""
    async def fake_adjust(**kwargs):
        raise ApiError("validation_error", "«Waka 1800» — պահեստում կա 2 հատ։")

    async def noop(*args, **kwargs):
        return None

    async def fake_reply(self, *args, **kwargs):
        return None

    context = _loaded(rs_deltas={"3": 4, "4": -9})
    with (
        mock.patch.object(restock.api, "adjust_stock", fake_adjust),
        mock.patch.object(CallbackQuery, "answer", noop),
        mock.patch.object(Message, "reply_text", fake_reply),
    ):
        state = await restock.submit(_tap(keyboards.CB_APPLY), context)

    assert state == restock.PICK
    assert context.user_data["rs_deltas"] == {"3": 4, "4": -9}
    assert "rs_key" not in context.user_data, "a fresh key for the corrected batch"


async def test_a_network_failure_ends_the_flow():
    async def fake_adjust(**kwargs):
        raise ApiUnavailable()

    async def noop(*args, **kwargs):
        return None

    async def fake_reply(self, *args, **kwargs):
        return None

    context = _loaded(rs_deltas={"3": 4})
    with (
        mock.patch.object(restock.api, "adjust_stock", fake_adjust),
        mock.patch.object(CallbackQuery, "answer", noop),
        mock.patch.object(Message, "reply_text", fake_reply),
    ):
        state = await restock.submit(_tap(keyboards.CB_APPLY), context)

    assert state == ConversationHandler.END
    assert context.user_data == {}


async def test_an_applied_correction_is_never_reported_as_a_failure():
    replies = []

    async def fake_adjust(**kwargs):
        return {"ok": True}          # a shape the confirmation cannot render

    async def noop(*args, **kwargs):
        return None

    async def fake_reply(self, text, *args, **kwargs):
        replies.append(text)

    context = _loaded(rs_deltas={"3": 4})
    with (
        mock.patch.object(restock.api, "adjust_stock", fake_adjust),
        mock.patch.object(CallbackQuery, "answer", noop),
        mock.patch.object(CallbackQuery, "edit_message_reply_markup", noop),
        mock.patch.object(Message, "reply_text", fake_reply),
    ):
        state = await restock.submit(_tap(keyboards.CB_APPLY), context)

    assert state == ConversationHandler.END
    assert replies == [texts.RESTOCK_DONE_PLAINLY]
    assert "սխալ" not in replies[0].lower()


async def test_a_retry_reuses_the_key():
    keys = []

    async def fake_adjust(**kwargs):
        keys.append(kwargs["key"])
        raise ApiError("internal", "…")

    async def noop(*args, **kwargs):
        return None

    async def fake_reply(self, *args, **kwargs):
        return None

    context = _loaded(rs_deltas={"3": 4})
    with (
        mock.patch.object(restock.api, "adjust_stock", fake_adjust),
        mock.patch.object(CallbackQuery, "answer", noop),
        mock.patch.object(Message, "reply_text", fake_reply),
    ):
        await restock.submit(_tap(keyboards.CB_APPLY), context)
        context.user_data.update(_loaded(rs_deltas={"3": 4}, rs_key=keys[0]).user_data)
        await restock.submit(_tap(keyboards.CB_APPLY), context)

    assert keys[0] == keys[1]
