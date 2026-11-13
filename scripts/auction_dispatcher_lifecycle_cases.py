from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import patch

from aiogram.methods import PinChatMessage, SendMessage
from sqlalchemy import select

from app.bot import helpers
from app.bot.handlers import games_auction, games_shared
from app.db import dispose_db, session_scope
from app.models import Auction, AuctionReminder
from app.services import auction_reminders, scheduler
from auction_dispatcher_bid_cases import visible_card
from auction_dispatcher_fixture import Harness
from auction_dispatcher_setup_cases import complete_setup
from auction_dispatcher_transport import callbacks


async def test_start_identity_survives_when_command_and_button_refresh_pin(harness: Harness) -> None:
    start = len(harness.session.calls)
    scene, auction, post = await complete_setup(harness, auction_type="points", skip=False)
    original = visible_card(harness, post)
    pins = [request for request in harness.session.calls[start:] if isinstance(request, PinChatMessage)]
    assert [(pin.chat_id, pin.message_id) for pin in pins] == [(scene.group_id, original.message_id)]
    title = post.text.splitlines()[0]

    await harness.say(scene.group_id, scene.second, f"/bid {auction.code} 150")
    refreshed = harness.session.messages[(scene.group_id, original.message_id)]
    assert refreshed.text is not None
    assert refreshed.text.splitlines()[0] == title, refreshed.text
    assert "150 积分" in refreshed.text and f"@{scene.second.username}" in refreshed.text
    assert f"bid:{auction.id}:160" in callbacks(refreshed.reply_markup)

    await harness.click(scene.first, original, f"bid:{auction.id}:100")
    refreshed = harness.session.messages[(scene.group_id, original.message_id)]
    assert refreshed.text is not None and refreshed.text.splitlines()[0] == title
    assert "160 积分" in refreshed.text and f"@{scene.first.username}" in refreshed.text
    assert {f"bid:{auction.id}:170", f"aucquery:{auction.id}"} <= callbacks(refreshed.reply_markup)
    assert [request for request in harness.session.calls[start:] if isinstance(request, PinChatMessage)] == pins


async def test_receipt_is_transient_when_command_deletion_fails(harness: Harness) -> None:
    scene = await harness.seed("TTLFAIL")
    harness.session.fail_next.add("deleteMessage")

    with patch.object(helpers, "NOTICE_TTL_SECONDS", 0.01):
        requests = await harness.say(scene.group_id, scene.first, "/bid TTLFAIL 150")
    sent = [visible_card(harness, request) for request in requests if isinstance(request, SendMessage)]
    assert len(sent) == 2
    for task in tuple(helpers._background_tasks):
        await task

    remaining = [message.text for message in sent if (scene.group_id, message.message_id) in harness.session.messages]
    assert remaining == [], f"Permanent bid output after cleanup: {remaining}"
    assert (scene.group_id, scene.original.message_id) in harness.session.messages
    assert (await harness.state(scene)).bids == ((scene.first.id, 150),)


async def countdown_after_bid(harness: Harness, *, button: bool) -> None:
    start = len(harness.session.calls)
    scene = await harness.seed("RESETBTN" if button else "RESETCMD")
    now = datetime(2026, 1, 1)
    old_deadline = now + timedelta(seconds=120)
    bid_time = now + timedelta(seconds=30)
    expected_deadline = bid_time + timedelta(minutes=3)
    async with session_scope() as session:
        auction = await session.get(Auction, scene.auction.id)
        assert auction is not None
        auction.ends_at = old_deadline
        auction.extend_minutes = 1
    send_start = len(harness.session.sent_messages)

    with patch.object(auction_reminders, "monotonic", return_value=0.0), patch.object(helpers, "NOTICE_TTL_SECONDS", 0.01):
        await scheduler._send_auction_reminders(harness.bot, now)
        reminder = harness.session.sent_messages[-1][1]
        assert f"bid:{scene.auction.id}" in callbacks(reminder.reply_markup)
        with patch.object(games_auction, "utcnow", return_value=bid_time), patch.object(games_shared, "utcnow", return_value=bid_time):
            if button:
                await harness.click(scene.first, reminder, f"bid:{scene.auction.id}")
            else:
                await harness.say(scene.group_id, scene.first, "/bid 150", reply=reminder)
        state = await harness.state(scene)
        assert state.ends_at == expected_deadline, state
        assert state.bids == ((scene.first.id, 100 if button else 150),)
        await dispose_db()
        assert (await harness.state(scene)).ends_at == expected_deadline
        with patch.object(games_auction, "utcnow", return_value=bid_time), patch.object(games_shared, "utcnow", return_value=bid_time):
            await harness.say(scene.group_id, scene.second, f"/bidinfo {scene.auction.code}")
        for elapsed in (30, 60, 90, 120, 150, 180, 210):
            await scheduler._send_auction_reminders(harness.bot, now + timedelta(seconds=elapsed))
    for task in tuple(helpers._background_tasks):
        await task

    async with session_scope() as session:
        rows = (await session.scalars(select(AuctionReminder).where(
            AuctionReminder.auction_id == scene.auction.id,
        ).order_by(AuctionReminder.id))).all()
        assert [(row.ends_at, row.window, row.status) for row in rows] == [
            (old_deadline, 2, "sent"),
            (expected_deadline, 2, "sent"), (expected_deadline, 3, "sent"),
        ]
    sent = harness.session.sent_messages[send_start:]
    reminders = [message for _, message in sent if message.message_id in {row.message_id for row in rows}]
    assert len(reminders) == 3
    for message, sequence in zip(reminders, ("一", "一", "二"), strict=True):
        assert message.text is not None and f"第{sequence}次" in message.text
    assert all((message.chat.id, message.message_id) not in harness.session.messages for _, message in sent)
    assert (scene.group_id, scene.original.message_id) in harness.session.messages
    assert not any(isinstance(request, PinChatMessage) for request in harness.session.calls[start:])
    assert await harness.state(scene) == state
    print(f"COUNTDOWN {'button' if button else 'command'}: persisted {expected_deadline}; old=[2], new=[2,3]; transient outputs={len(sent)}")


async def test_countdown_restarts_when_command_bids_after_first_reminder(harness: Harness) -> None:
    await countdown_after_bid(harness, button=False)


async def test_countdown_restarts_when_reminder_button_bids(harness: Harness) -> None:
    await countdown_after_bid(harness, button=True)


async def test_bid_rearms_after_legacy_first_when_179_seconds_remain(harness: Harness) -> None:
    scene = await harness.seed("LEGACY179")
    now = datetime(2026, 1, 1)
    old_deadline = now + timedelta(seconds=179)
    async with session_scope() as session:
        auction = await session.get(Auction, scene.auction.id)
        assert auction is not None
        auction.ends_at = old_deadline
        auction.extend_minutes = 1
        session.add(AuctionReminder(
            auction_id=auction.id, ends_at=old_deadline, window=1,
            status="sent", claimed_at=now - timedelta(seconds=1),
        ))

    with patch.object(games_auction, "utcnow", return_value=now), patch.object(games_shared, "utcnow", return_value=now), patch.object(auction_reminders, "monotonic", return_value=0.0):
        await harness.say(scene.group_id, scene.first, "/bid LEGACY179 150")
        state = await harness.state(scene)
        assert state.ends_at == now + timedelta(minutes=3)
        assert state.bids == ((scene.first.id, 150),)
        for remaining in (120, 60):
            await scheduler._send_auction_reminders(harness.bot, state.ends_at - timedelta(seconds=remaining))

    async with session_scope() as session:
        rows = (await session.scalars(select(AuctionReminder).where(
            AuctionReminder.auction_id == scene.auction.id,
        ).order_by(AuctionReminder.id))).all()
    assert [(row.ends_at, row.window) for row in rows] == [
        (old_deadline, 1), (state.ends_at, 2), (state.ends_at, 3),
    ]
