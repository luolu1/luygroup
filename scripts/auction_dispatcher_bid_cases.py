"""Real-dispatcher bid scenarios with independent financial and Telegram evidence."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from html import escape
from unittest.mock import patch

from aiogram.methods import DeleteMessage, EditMessageText, SendMessage
from aiogram.types import InlineKeyboardMarkup, Message
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import helpers
from app.bot.handlers import games
from app.bot.handlers import games_auction
from app.db import session_scope
from app.models import Auction, Member
from auction_dispatcher_fixture import Harness, Scene, Snapshot
from auction_dispatcher_transport import AuctionRequest, bid_cards, callbacks


def assert_card(scene: Scene, card: SendMessage, state: Snapshot) -> None:
    unit = "CNY" if scene.auction.auction_type == "currency" else "积分"
    username = {scene.first.id: scene.first.username, scene.second.id: scene.second.username}[state.bidder]
    assert card.chat_id == scene.group_id
    for field in (
        f"<b>拍品</b>　{escape(scene.auction.item)}",
        f"<b>拍品描述</b>　{escape(scene.auction.description)}",
        f"<b>编号</b>　{scene.auction.code}",
        f"<b>当前出价</b>　{state.price} {unit}",
        f"@{username}</a>", f"tg://user?id={state.bidder}",
        f"<b>最低加价</b>　10 {unit}",
        (state.ends_at + timedelta(hours=8)).strftime("%Y-%m-%d %H:%M（北京时间）"),
        "截止时间至少为出价后 3 分钟",
    ):
        assert field in card.text, (field, card.text)
    assert isinstance(card.reply_markup, InlineKeyboardMarkup)
    minimum = state.price + 10
    assert {f"bid:{scene.auction.id}:{minimum}", f"aucquery:{scene.auction.id}"} <= callbacks(card.reply_markup)
    button = next(button for row in card.reply_markup.inline_keyboard for button in row if button.callback_data == f"bid:{scene.auction.id}:{minimum}")
    assert f"{minimum} {unit}" in button.text, button.text


def visible_card(harness: Harness, card: SendMessage) -> Message:
    return next(
        message for request, message in harness.session.sent_messages if request is card
    )


async def test_command_announces_after_commit_when_bid_is_accepted(harness: Harness) -> None:
    scene = await harness.seed("CMDNEW")

    async def verify_commit(request: AuctionRequest) -> None:
        if isinstance(request, SendMessage) and "<b>拍品最新出价</b>" in request.text:
            committed = await harness.state(scene)
            assert committed.bids == ((scene.first.id, 150),)
            assert dict(committed.balances)[scene.first.id] == 350

    harness.session.on_request = verify_commit
    requests = await harness.say(scene.group_id, scene.first, "/bid CMDNEW 150")
    state = await harness.state(scene)
    assert state.price == 150 and state.bidder == scene.first.id
    assert state.bids == ((scene.first.id, 150),)
    assert dict(state.balances) == {scene.first.id: 350, scene.second.id: 500}
    cards = bid_cards(requests)
    assert len(cards) == 1, f"accepted command emitted {len(cards)} persistent cards"
    assert_card(scene, cards[0], state)
    assert requests.index(cards[0]) < next(index for index, request in enumerate(requests) if isinstance(request, DeleteMessage))


async def test_callback_announces_when_bid_is_accepted(harness: Harness) -> None:
    scene = await harness.seed("CBNEW")
    requests = await harness.click(scene.first, scene.original, f"bid:{scene.auction.id}:100")
    state = await harness.state(scene)
    assert state.bids == ((scene.first.id, 100),)
    assert dict(state.balances)[scene.first.id] == 400
    cards = bid_cards(requests)
    assert len(cards) == 1, f"accepted callback emitted {len(cards)} persistent cards"
    assert_card(scene, cards[0], state)


async def test_edit_fields_are_valid_when_original_card_is_refreshed(harness: Harness) -> None:
    scene = await harness.seed("SDKEDIT")
    requests = await harness.say(scene.group_id, scene.first, "/bid 150")
    edit = next(request for request in requests if isinstance(request, EditMessageText))
    assert (edit.chat_id, edit.message_id, edit.business_connection_id) == (scene.group_id, scene.original.message_id, None)
    assert isinstance(edit.text, str) and "150 积分" in edit.text and f"@{scene.first.username}" in edit.text
    assert isinstance(edit.reply_markup, InlineKeyboardMarkup)
    assert f"bid:{scene.auction.id}:160" in callbacks(edit.reply_markup)
    assert f"aucquery:{scene.auction.id}" in callbacks(edit.reply_markup)


async def test_old_button_uses_current_minimum_when_price_has_advanced(harness: Harness) -> None:
    scene = await harness.seed("OLDBTN")
    async with session_scope() as session:
        auction = await session.get(Auction, scene.auction.id)
        assert auction is not None
        auction.current_price = 170
        auction.current_bidder = scene.first.id
        await session.execute(update(Member).where(
            Member.group_id == scene.group_id, Member.user_id == scene.first.id
        ).values(points=330))
    requests = await harness.click(scene.second, scene.original, f"bid:{scene.auction.id}:100")
    state = await harness.state(scene)
    assert state.bids == ((scene.second.id, 180),), state
    assert dict(state.balances) == {scene.first.id: 500, scene.second.id: 320}
    assert state.transactions == ((scene.first.id, 170), (scene.second.id, -180))
    cards = bid_cards(requests)
    assert len(cards) == 1
    assert_card(scene, cards[0], state)


async def test_successive_autobids_refresh_when_old_card_is_reused(harness: Harness) -> None:
    scene = await harness.seed("AUTOBIDS")
    original_end = scene.auction.ends_at
    for user, amount in ((scene.first, 100), (scene.second, 110), (scene.first, 120)):
        requests = await harness.click(user, scene.original, f"bid:{scene.auction.id}:100")
        state = await harness.state(scene)
        assert state.price == amount and state.bidder == user.id
        cards = bid_cards(requests)
        assert len(cards) == 1
        assert_card(scene, cards[0], state)
    assert state.bids == ((scene.first.id, 100), (scene.second.id, 110), (scene.first.id, 120))
    assert dict(state.balances) == {scene.first.id: 380, scene.second.id: 500}
    assert state.transactions == ((scene.first.id, -100), (scene.first.id, 100), (scene.second.id, -110), (scene.second.id, 110), (scene.first.id, -120))
    assert state.ends_at > original_end


async def test_currency_buttons_and_fee_stay_correct_when_reused(harness: Harness) -> None:
    scene = await harness.seed("CNYBIDS", auction_type="currency")
    for user, amount in ((scene.first, 100), (scene.second, 110)):
        requests = await harness.click(user, scene.original, f"bid:{scene.auction.id}:100")
        state = await harness.state(scene)
        assert state.price == amount and state.bidder == user.id
        cards = bid_cards(requests)
        assert len(cards) == 1
        assert_card(scene, cards[0], state)
    assert dict(state.balances) == {scene.first.id: 500, scene.second.id: 490}
    assert state.transactions == ((scene.first.id, -10), (scene.first.id, 10), (scene.second.id, -10))


async def test_numeric_command_keeps_amount_when_no_selector_is_given(harness: Harness) -> None:
    scene = await harness.seed("AMOUNT")
    await harness.say(scene.group_id, scene.first, "/bid 150")
    assert (await harness.state(scene)).bids == ((scene.first.id, 150),)


async def test_numeric_code_selects_group_auction_when_amount_follows(harness: Harness) -> None:
    scene = await harness.seed("22222222")
    other = await harness.another(scene.group_id, "OTHERCOD")
    await harness.say(scene.group_id, scene.first, "/bid 22222222 150")
    assert (await harness.state(scene)).bids == ((scene.first.id, 150),)
    async with session_scope() as session:
        untouched = await session.get(Auction, other.id)
        assert untouched is not None and untouched.current_bidder is None


async def test_rejection_has_no_card_when_amount_is_too_low(harness: Harness) -> None:
    scene = await harness.seed("LOWBID")
    before = await harness.state(scene)
    requests = await harness.say(scene.group_id, scene.first, "/bid LOWBID 99")
    assert await harness.state(scene) == before
    assert not bid_cards(requests)


async def test_rejection_has_no_card_when_callback_is_from_other_group(harness: Harness) -> None:
    scene = await harness.seed("GROUPBID")
    other = await harness.seed("FOREIGNB")
    before = await harness.state(scene)
    requests = await harness.click(scene.first, other.original, f"bid:{scene.auction.id}:100")
    assert await harness.state(scene) == before
    assert not bid_cards(requests)


async def test_cas_loser_has_no_card_when_two_commands_race(harness: Harness) -> None:
    scene = await harness.seed("RACECARD")
    async with session_scope() as session:
        auction = await session.get(Auction, scene.auction.id)
        assert auction is not None
        auction.message_id = None
    original = games._claim_auction_slot
    ready = asyncio.Event()
    arrivals = 0

    async def gated_claim(session: AsyncSession, auction_id: int, observed_price: int, observed_bidder: int | None, new_bidder: int, new_price: int, now: datetime) -> bool:
        nonlocal arrivals
        arrivals += 1
        if arrivals == 2:
            ready.set()
        async with asyncio.timeout(5):
            await ready.wait()
        return await original(session, auction_id, observed_price, observed_bidder, new_bidder, new_price, now)

    start = len(harness.session.calls)
    with patch.object(games_auction, "_claim_auction_slot", new=gated_claim):
        await asyncio.gather(
            harness.say(scene.group_id, scene.first, "/bid RACECARD 150"),
            harness.say(scene.group_id, scene.second, "/bid RACECARD 150"),
        )
    requests = tuple(harness.session.calls[start:])
    state = await harness.state(scene)
    assert len(state.bids) == 1 and state.price == 150
    assert sorted(dict(state.balances).values()) == [350, 500]
    assert len(state.transactions) == 1 and state.transactions[0][1] == -150
    assert len(bid_cards(requests)) == 1, requests


async def test_latest_card_deleted_when_receipt_cleanup_finishes(harness: Harness) -> None:
    scene = await harness.seed("PERSIST")
    with patch.object(helpers, "NOTICE_TTL_SECONDS", 0.01):
        requests = await harness.say(scene.group_id, scene.first, "/bid PERSIST 150")
    cards = bid_cards(requests)
    assert len(cards) == 1
    message = visible_card(harness, cards[0])
    tasks = tuple(helpers._background_tasks)
    assert tasks, "command receipt must still exercise its real delayed cleanup"
    await asyncio.gather(*tasks)
    assert (scene.group_id, message.message_id) not in harness.session.messages
    assert any(isinstance(request, DeleteMessage) and request.message_id == message.message_id for request in harness.session.calls)
