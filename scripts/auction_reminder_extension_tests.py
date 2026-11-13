from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import patch

from aiogram.types import Message

from auction_reminder_fixtures import (
    NOW,
    auction_at,
    captured_bot,
    financial_rows,
    isolated_db,
    persist_auction,
    reminder_rows,
)

from app.bot.handlers.games import _refresh_auction_deadline
from app.db import session_scope
from app.models import Auction
from app.services import auction_reminders, scheduler
from app.services.auction_reminders import ReminderCandidate, claim_reminder, scan_reminders


async def deadline_semantics_when_extension_applied() -> None:
    for ends_at, minutes, bid_time, expected in (
        (NOW + timedelta(seconds=180), 0, NOW + timedelta(seconds=150), NOW + timedelta(seconds=330)),
        (NOW + timedelta(seconds=180), 1, NOW, NOW + timedelta(seconds=180)),
        (NOW + timedelta(seconds=180), 1, NOW + timedelta(seconds=150), NOW + timedelta(seconds=330)),
        (None, 1, NOW, None),
        (None, 0, NOW, None),
        (NOW + timedelta(minutes=10), 1, NOW, NOW + timedelta(minutes=10)),
        (NOW + timedelta(seconds=181), 5, NOW, NOW + timedelta(seconds=181)),
        (NOW + timedelta(seconds=180), 5, NOW, NOW + timedelta(minutes=5)),
        (NOW + timedelta(seconds=59), 5, NOW, NOW + timedelta(minutes=5)),
    ):
        async with isolated_db():
            auction = auction_at(ends_at)
            auction.extend_minutes = minutes
            await persist_auction(auction)

            async with session_scope() as session:
                current = await session.get(Auction, 1)
                assert current is not None
                await _refresh_auction_deadline(current, bid_time)

            async with session_scope() as session:
                current = await session.get(Auction, 1)
                assert current is not None and current.ends_at == expected
    print("PASS extension policy: max(old deadline,bid time+max(3,extend) minutes)")


async def postponed_when_extended_before_scan() -> None:
    async with isolated_db(), captured_bot() as (bot, transport):
        old_deadline = NOW + timedelta(seconds=59)
        new_deadline = NOW + timedelta(seconds=300)
        auction = auction_at(old_deadline)
        auction.extend_minutes = 5
        await persist_auction(auction)
        async with session_scope() as session:
            current = await session.get(Auction, 1)
            assert current is not None
            await _refresh_auction_deadline(current, NOW)
        before = await financial_rows()

        await scheduler._send_auction_reminders(bot, NOW)
        await scheduler._send_auction_reminders(bot, old_deadline)
        for remaining in (179, 119, 59):
            await scheduler._send_auction_reminders(bot, new_deadline - timedelta(seconds=remaining))

        rows = await reminder_rows()
        assert [(row.ends_at, row.window, row.status) for row in rows] == [
            (new_deadline, 2, "sent"), (new_deadline, 3, "sent"),
        ]
        assert len(transport.sends) == 2 and transport.pins == []
        assert await financial_rows() == before
        print("PASS extension before scan: old deadline claims=0; latest windows=[2,3] sends=2")


async def scan_rejected_when_deadline_extended_inside_same_window() -> None:
    async with isolated_db(), captured_bot() as (bot, transport):
        old_deadline = NOW + timedelta(seconds=119)
        new_deadline = NOW + timedelta(seconds=180)
        auction = auction_at(old_deadline)
        auction.extend_minutes = 3
        await persist_auction(auction)
        real_scan = auction_reminders.scan_reminders

        async def extend_after_scan(now: datetime) -> list[ReminderCandidate]:
            candidates = await real_scan(now)
            assert candidates == [ReminderCandidate(1, old_deadline, 2, 1)]
            async with session_scope() as session:
                current = await session.get(Auction, 1)
                assert current is not None
                await _refresh_auction_deadline(current, now)
            return candidates

        with patch.object(auction_reminders, "scan_reminders", extend_after_scan):
            await scheduler._send_auction_reminders(bot, NOW)

        assert transport.methods == [] and await reminder_rows() == []
        await scheduler._send_auction_reminders(bot, NOW + timedelta(seconds=60))
        rows = await reminder_rows()
        assert len(rows) == len(transport.sends) == 1
        assert (rows[0].ends_at, rows[0].window, rows[0].status) == (new_deadline, 2, "sent")
        assert "2026-11-09 09:05:03" in transport.sends[0].text
        print("PASS extension after scan: stale same-window deadline sends=0; new deadline sends=1")


async def rearmed_when_old_deadline_already_has_sent_claim() -> None:
    async with isolated_db(), captured_bot() as (bot, transport):
        old_deadline = NOW + timedelta(seconds=120)
        new_deadline = NOW + timedelta(seconds=181)
        auction = auction_at(old_deadline)
        auction.extend_minutes = 3
        await persist_auction(auction)
        await scheduler._send_auction_reminders(bot, NOW)
        async with session_scope() as session:
            current = await session.get(Auction, 1)
            assert current is not None
            await _refresh_auction_deadline(current, NOW + timedelta(seconds=1))
        before = await financial_rows()

        for elapsed in (1, 60, 61, 120, 121, 180, 181):
            await scheduler._send_auction_reminders(bot, NOW + timedelta(seconds=elapsed))

        rows = await reminder_rows()
        assert [(row.ends_at, row.window, row.status) for row in rows] == [
            (old_deadline, 2, "sent"),
            (new_deadline, 2, "sent"), (new_deadline, 3, "sent"),
        ], f"deadlines/windows={[(row.ends_at.isoformat(), row.window) for row in rows]} sends={len(transport.sends)}"
        assert len(transport.sends) == 3 and transport.pins == []
        assert await financial_rows() == before
        print("PASS latest-deadline rearm: old sent window retained; new windows=[2,3]; obsolete windows skipped")


async def obsolete_window_skipped_when_claim_clock_advances() -> None:
    async with isolated_db(), captured_bot() as (bot, transport):
        await persist_auction(auction_at(NOW + timedelta(seconds=120)))
        candidate = (await scan_reminders(NOW))[0]

        async with session_scope() as session:
            claim = await claim_reminder(session, candidate, NOW + timedelta(seconds=61))
        await scheduler._send_auction_reminders(bot, NOW + timedelta(seconds=61))
        await scheduler._send_auction_reminders(bot, NOW + timedelta(seconds=121))

        assert claim is None
        assert [(row.window, row.status) for row in await reminder_rows()] == [(3, "sent")]
        assert len(transport.sends) == 1 and transport.pins == []
        print("PASS delayed scan never backfills first reminder; current window=3")


async def current_window_when_scan_takes_a_minute() -> None:
    async with isolated_db(), captured_bot() as (bot, transport):
        await persist_auction(auction_at(NOW + timedelta(seconds=119)))
        expiring = auction_at(NOW + timedelta(seconds=59))
        expiring.id = 2
        expiring.code = "A00000002"
        await persist_auction(expiring)
        real_scan = auction_reminders.scan_reminders
        elapsed = 0.0

        async def delayed_scan(now: datetime) -> list[ReminderCandidate]:
            nonlocal elapsed
            candidates = await real_scan(now)
            elapsed = 60.0
            return candidates

        with patch.object(auction_reminders, "monotonic", lambda: elapsed), patch.object(auction_reminders, "scan_reminders", delayed_scan):
            await scheduler._send_auction_reminders(bot, NOW)

        actual = [(row.auction_id, row.window, row.claimed_at.isoformat()) for row in await reminder_rows()]
        print(f"CLOCK elapsed=60s claims={actual} sends={len(transport.sends)}")
        assert actual == [(1, 3, (NOW + timedelta(seconds=60)).isoformat())], actual
        assert len(transport.sends) == 1 and transport.pins == []
    print("PASS slow scan selects the current window and skips expired deadlines")


async def current_window_when_an_earlier_send_takes_a_minute() -> None:
    async with isolated_db(), captured_bot() as (bot, transport):
        first = auction_at(NOW + timedelta(seconds=119))
        second = auction_at(NOW + timedelta(seconds=119))
        second.id = 2
        second.code = "A00000002"
        await persist_auction(first)
        await persist_auction(second)
        elapsed = 0.0

        async def advance_clock(_message: Message) -> None:
            nonlocal elapsed
            elapsed = 60.0

        transport.after_send = advance_clock

        with patch.object(auction_reminders, "monotonic", lambda: elapsed):
            await scheduler._send_auction_reminders(bot, NOW)

        rows = await reminder_rows()
        actual = [(row.window, row.claimed_at.isoformat()) for row in rows]
        assert actual == [(2, NOW.isoformat()), (3, (NOW + timedelta(seconds=60)).isoformat())], actual
        assert {row.auction_id for row in rows} == {1, 2}
        assert len(transport.sends) == 2 and transport.pins == []
        print("PASS earlier slow send: later claim uses window 3, never obsolete window 2")


async def run_extension_tests() -> None:
    await deadline_semantics_when_extension_applied()
    await postponed_when_extended_before_scan()
    await scan_rejected_when_deadline_extended_inside_same_window()
    await rearmed_when_old_deadline_already_has_sent_claim()
    await obsolete_window_skipped_when_claim_clock_advances()
    await current_window_when_scan_takes_a_minute()
    await current_window_when_an_earlier_send_takes_a_minute()
