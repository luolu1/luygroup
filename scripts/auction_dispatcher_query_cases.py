"""Group-scoped read-only query scenarios against real dispatcher routing."""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta
from html import escape

from aiogram.methods import AnswerCallbackQuery, DeleteMessage, SendMessage
from aiogram.types import InlineKeyboardMarkup
from sqlalchemy import select
from app.bot import helpers
from app.bot.main import BOT_COMMANDS, GROUP_COMMANDS, PRIVATE_COMMANDS
from app.db import session_scope
from app.models import Auction, AuctionBid, Member, utcnow
from scripts.auction_dispatcher_bid_cases import visible_card
from scripts.auction_dispatcher_fixture import Harness
from scripts.auction_dispatcher_transport import AuctionRequest, bid_cards, callbacks


def texts(requests: tuple[AuctionRequest, ...]) -> str:
    return "\n".join(request.text for request in requests if isinstance(request, SendMessage))


def query_controls(requests: tuple[AuctionRequest, ...]) -> set[str]:
    return {
        data for request in requests
        if isinstance(request, SendMessage) and isinstance(request.reply_markup, InlineKeyboardMarkup)
        for data in callbacks(request.reply_markup)
    }


async def test_query_is_discoverable_when_group_menu_is_opened(harness: Harness) -> None:
    scene = await harness.seed("QMENU")
    requests = await harness.say(scene.group_id, scene.first, "/ads")
    commands = set(re.findall(r"^/([a-z]+)\b", texts(requests), re.MULTILINE))
    assert {"bidinfo", "bid"} <= commands, commands
    assert {"bidinfo", "bid"} <= {command.command for command in GROUP_COMMANDS}
    assert BOT_COMMANDS is PRIVATE_COMMANDS
    assert {command.command for command in PRIVATE_COMMANDS} == {"start", "console", "sub", "unbanme", "unmuteme", "cjtz", "cjhistory", "cjcancel", "jptz", "jphistory", "jpcancel"}


async def test_discovery_lists_all_active_when_group_has_multiple_auctions(harness: Harness) -> None:
    scene = await harness.seed("QACTIVE1")
    second = await harness.another(scene.group_id, "QACTIVE2")
    foreign = await harness.seed("QSECRET")
    closed = await harness.another(scene.group_id, "QCLOSED")
    deleted = await harness.another(scene.group_id, "QDELETED")
    expired = await harness.another(scene.group_id, "QEXPIRED")
    async with session_scope() as session:
        for auction_id, status in ((closed.id, "finished"), (deleted.id, "deleted")):
            auction = await session.get(Auction, auction_id)
            assert auction is not None
            auction.status = status
        auction = await session.get(Auction, expired.id)
        assert auction is not None
        auction.ends_at = utcnow() - timedelta(minutes=1)
    before = await harness.state(scene)
    requests = await harness.say(scene.group_id, scene.first, "/bidinfo")
    text = texts(requests)
    assert scene.auction.code is not None
    assert second.code is not None
    assert foreign.auction.code is not None
    assert closed.code is not None
    assert deleted.code is not None
    assert expired.code is not None
    assert scene.auction.code in text and second.code in text, text
    excluded_codes = (
        foreign.auction.code,
        closed.code,
        deleted.code,
        expired.code,
    )
    assert all(code is not None and code not in text for code in excluded_codes), text
    assert query_controls(requests) == {f"aucquery:{scene.auction.id}", f"aucquery:{second.id}"}
    assert await harness.state(scene) == before


async def test_numeric_code_is_selected_when_query_argument_is_digits(harness: Harness) -> None:
    scene = await harness.seed("33333333")
    other = await harness.another(scene.group_id, "QNOTTHIS")
    requests = await harness.say(scene.group_id, scene.first, "/bidinfo 33333333")
    text = texts(requests)
    assert scene.auction.item in text and other.item not in text, text


async def test_numeric_id_selects_when_query_is_unambiguous(harness: Harness) -> None:
    scene = await harness.seed("QINTID")
    other = await harness.another(scene.group_id, "QINTNEXT")
    requests = await harness.say(scene.group_id, scene.first, f"/bidinfo {scene.auction.id}")
    text = texts(requests)
    assert scene.auction.item in text and other.item not in text, text


async def test_numeric_query_lists_choices_when_code_and_id_collide(harness: Harness) -> None:
    scene = await harness.seed("QCOLLIDE")
    other = await harness.another(scene.group_id, str(scene.auction.id))
    requests = await harness.say(scene.group_id, scene.first, f"/bidinfo {scene.auction.id}")
    assert query_controls(requests) == {f"aucquery:{scene.auction.id}", f"aucquery:{other.id}"}, texts(requests)


async def test_unknown_numeric_query_rejects_when_another_auction_is_active(harness: Harness) -> None:
    scene = await harness.seed("QUNKNOWN")
    before = await harness.state(scene)
    requests = await harness.say(scene.group_id, scene.first, "/bidinfo 99999999")
    assert scene.auction.item not in texts(requests), texts(requests)
    assert await harness.state(scene) == before


async def test_full_count_and_newest_five_are_accurate_when_history_exceeds_ten(harness: Harness) -> None:
    scene = await harness.seed("QHISTORY")
    base = datetime(2026, 6, 1, 4, 0)
    bidders = [
        scene.first.id, scene.second.id, scene.first.id, scene.second.id,
        scene.first.id, scene.second.id, scene.first.id, scene.second.id,
        scene.first.id, scene.second.id, scene.first.id, scene.first.id, scene.first.id,
    ]
    async with session_scope() as session:
        for user_id, first_name, username in (
            (scene.first.id, "Alice <One> & Co", scene.first.username),
            (scene.second.id, "", "bob<&>"),
        ):
            member = await session.scalar(select(Member).where(
                Member.group_id == scene.group_id, Member.user_id == user_id,
            ))
            assert member is not None
            member.first_name = first_name
            member.username = username
        session.add_all([
            AuctionBid(auction_id=scene.auction.id, user_id=user_id, amount=100 + index * 10, created_at=base + timedelta(minutes=index))
            for index, user_id in enumerate(bidders)
        ])
        auction = await session.get(Auction, scene.auction.id)
        assert auction is not None
        auction.current_price = 220
        auction.current_bidder = scene.first.id
    before = await harness.state(scene)
    requests = await harness.say(scene.group_id, scene.first, "/bidinfo QHISTORY")
    text = texts(requests)
    assert re.search(r"出价次数</b>\s+13\b", text), text
    rows = [line for line in text.splitlines() if line.startswith("· ") and "出价" in line]
    amounts = [int(match.group(1)) for line in rows if (match := re.search(r"出价 (\d+)", line))]
    assert amounts == [220, 210, 200, 190, 180], rows
    assert re.search(r"当前连续领先</b>\s+3\s+次", text), text
    assert escape("Alice <One> & Co") in rows[0] and escape("Alice <One> & Co") in rows[1], rows
    assert escape("bob<&>") in rows[3], rows
    assert f"当前出价用户</b>　{escape('Alice <One> & Co')}" in text, text
    assert "tg://user" not in text and "@" not in text, text
    assert "2026-06-01 12:12（北京时间）" in rows[0], rows[0]
    assert "2026-06-01 12:08（北京时间）" in rows[-1], rows[-1]
    assert await harness.state(scene) == before


async def test_query_results_are_temporary_for_command_and_callback(harness: Harness) -> None:
    scene = await harness.seed("QTTL")
    original_ttl = helpers.NOTICE_TTL_SECONDS
    helpers.NOTICE_TTL_SECONDS = 0.01
    try:
        command_call_start = len(harness.session.calls)
        command_requests = await harness.say(scene.group_id, scene.first, "/bidinfo QTTL")
        command_result = next(
            request for request in command_requests
            if isinstance(request, SendMessage) and "竞拍出价情况" in (request.text or "")
        )
        command_message = next(
            message for request, message in harness.session.sent_messages if request is command_result
        )
        await asyncio.sleep(0.02)
        assert any(
            isinstance(request, DeleteMessage)
            and request.chat_id == scene.group_id
            and request.message_id == command_message.message_id
            for request in tuple(harness.session.calls[command_call_start:])
        )

        callback_call_start = len(harness.session.calls)
        callback_requests = await harness.click(
            scene.second, scene.original, f"aucquery:{scene.auction.id}"
        )
        callback_result = next(
            request for request in callback_requests
            if isinstance(request, SendMessage) and "竞拍出价情况" in (request.text or "")
        )
        callback_message = next(
            message for request, message in harness.session.sent_messages if request is callback_result
        )
        await asyncio.sleep(0.02)
        later_calls = tuple(harness.session.calls)
        assert any(
            isinstance(request, DeleteMessage)
            and request.chat_id == scene.group_id
            and request.message_id == callback_message.message_id
            for request in later_calls[callback_call_start:]
        )
        assert (scene.group_id, scene.original.message_id) in harness.session.messages
    finally:
        helpers.NOTICE_TTL_SECONDS = original_ttl


async def test_callback_reuses_query_when_clicked_in_exact_group(harness: Harness) -> None:
    scene = await harness.seed("QCALL")
    command = await harness.say(scene.group_id, scene.first, "/bidquery QCALL")
    before = await harness.state(scene)
    requests = await harness.click(scene.second, scene.original, f"aucquery:{scene.auction.id}")
    assert texts(requests) == texts(command) and scene.auction.item in texts(requests), requests
    assert await harness.state(scene) == before
    assert not bid_cards(requests)


async def test_query_callback_rejects_when_id_belongs_to_other_group(harness: Harness) -> None:
    scene = await harness.seed("QOWN")
    other = await harness.seed("QFOREIGN")
    before = await harness.state(other)
    requests = await harness.click(scene.first, scene.original, f"aucquery:{other.auction.id}")
    assert not texts(requests)
    assert any(isinstance(request, AnswerCallbackQuery) and request.show_alert for request in requests), requests
    assert await harness.state(other) == before


async def test_query_command_rejects_when_code_belongs_to_other_group(harness: Harness) -> None:
    scene = await harness.seed("QOWNCMD")
    other = await harness.seed("QFORECMD")
    before = await harness.state(other)
    requests = await harness.say(scene.group_id, scene.first, "/bidinfo QFORECMD")
    assert other.auction.code not in texts(requests) and other.auction.item not in texts(requests)
    assert await harness.state(other) == before


async def test_closed_status_is_shown_when_code_is_explicit(harness: Harness) -> None:
    scene = await harness.seed("QFINISH")
    async with session_scope() as session:
        auction = await session.get(Auction, scene.auction.id)
        assert auction is not None
        auction.status = "finished"
    requests = await harness.say(scene.group_id, scene.first, "/bidinfo QFINISH")
    text = texts(requests)
    assert scene.auction.item in text and "已成交" in text, text
    assert "最低可出价" not in text
    assert not query_controls(requests)


async def test_reply_query_selects_when_legacy_card_has_no_query_control(harness: Harness) -> None:
    scene = await harness.seed("QREPLY")
    other = await harness.another(scene.group_id, "QREPNEXT")
    requests = await harness.say(scene.group_id, scene.first, "/bidinfo", reply=scene.original)
    text = texts(requests)
    assert scene.auction.item in text and other.item not in text, text


async def test_reply_query_selects_when_latest_bid_card_is_not_original(harness: Harness) -> None:
    scene = await harness.seed("QNEWREPL")
    requests = await harness.say(scene.group_id, scene.first, "/bid QNEWREPL 150")
    card = visible_card(harness, bid_cards(requests)[0])
    assert card.message_id != scene.original.message_id, (card.message_id, scene.original.message_id)
    other = await harness.another(scene.group_id, "QNEWNEXT")
    before = await harness.state(scene)
    query = await harness.say(scene.group_id, scene.second, "/bidinfo", reply=card)
    text = texts(query)
    assert scene.auction.item in text and other.item not in text, text
    assert await harness.state(scene) == before


async def test_reply_bid_selects_when_latest_card_emits_auction_id(harness: Harness) -> None:
    scene = await harness.seed("BNEWREPL")
    requests = await harness.say(scene.group_id, scene.first, "/bid BNEWREPL 150")
    card = visible_card(harness, bid_cards(requests)[0])
    await harness.another(scene.group_id, "BNEWNEXT")
    await harness.say(scene.group_id, scene.second, "/bid 160", reply=card)
    state = await harness.state(scene)
    assert state.bids == ((scene.first.id, 150), (scene.second.id, 160))
    assert dict(state.balances) == {scene.first.id: 500, scene.second.id: 340}


async def test_reply_query_uses_emitted_id_when_markup_is_removed(harness: Harness) -> None:
    scene = await harness.seed("QTEXTID")
    requests = await harness.say(scene.group_id, scene.first, "/bid QTEXTID 150")
    card = visible_card(harness, bid_cards(requests)[0])
    assert card.text is not None
    plain_card = card.model_copy(update={"reply_markup": None, "text": re.sub(r"<[^>]+>", "", card.text)})
    other = await harness.another(scene.group_id, "QTEXTNEXT")
    before = await harness.state(scene)
    query = await harness.say(scene.group_id, scene.second, "/bidinfo", reply=plain_card)
    text = texts(query)
    assert scene.auction.item in text and other.item not in text, text
    assert await harness.state(scene) == before
