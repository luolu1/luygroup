"""Real SQLite claim, restart, concurrency and additive-schema reminder regressions."""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta
from unittest.mock import patch

from sqlalchemy import update

from auction_reminder_fixtures import (
    GROUP_ID,
    NOW,
    ROOT,
    WorkerReceipt,
    auction_at,
    captured_bot,
    financial_rows,
    isolated_db,
    persist_auction,
    reminder_rows,
)

from app.db import dispose_db, get_engine, init_db, session_scope
from app.models import (
    AccountLedgerEntry,
    AdminLog,
    Auction,
    AuctionBid,
    AuctionReminder,
    ConsoleUser,
    Member,
    PaymentOrder,
    PointsTransaction,
)
from app.services import auction_reminders, scheduler
from app.services.auction_reminders import ReminderCandidate, claim_reminder, scan_reminders


async def run_restart_worker(now: datetime) -> WorkerReceipt:
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-B", str(ROOT / "scripts/smoke_auction_reminders.py"),
        "--restart-worker", now.isoformat(),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await process.communicate()
        assert process.returncode == 0, stderr.decode()
        return WorkerReceipt.model_validate_json(stdout)
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


async def claim_rejected_when_status_or_window_changed() -> None:
    for status, claim_time in (("finished", NOW), ("cancelled", NOW), ("open", NOW + timedelta(seconds=60)), ("open", NOW + timedelta(seconds=180))):
        async with isolated_db():
            await persist_auction(auction_at(NOW + timedelta(seconds=120)))
            candidate = (await scan_reminders(NOW))[0]
            async with session_scope() as session:
                await session.execute(update(Auction).where(Auction.id == 1).values(status=status))

            async with session_scope() as session:
                claimed = await claim_reminder(session, candidate, claim_time)

            assert claimed is None and await reminder_rows() == [], (status, claim_time)
    print("PASS claim CAS revalidates status and current window, including expiry")


async def pending_claim_when_process_restarts() -> None:
    async with isolated_db():
        await persist_auction(auction_at(NOW + timedelta(seconds=120)))
        candidate = (await scan_reminders(NOW))[0]
        async with session_scope() as session:
            reminder_id = await claim_reminder(session, candidate, NOW)
        assert reminder_id is not None
        await dispose_db()

        receipt = await run_restart_worker(NOW + timedelta(seconds=1))

        assert receipt == WorkerReceipt(sends=0, pins=0, claims=1), receipt
        rows = await reminder_rows()
        assert len(rows) == 1
        assert (rows[0].id, rows[0].status, rows[0].message_id) == (reminder_id, "pending", None)
        print("PASS committed/crashed claim survives new process: pending=1 duplicate sends=0")


async def sent_claim_when_process_restarts() -> None:
    async with isolated_db(), captured_bot() as (bot, transport):
        await persist_auction(auction_at(NOW + timedelta(seconds=120)))
        before = await financial_rows()
        await scheduler._send_auction_reminders(bot, NOW)
        await dispose_db()

        duplicate = await run_restart_worker(NOW + timedelta(seconds=1))
        next_window = await run_restart_worker(NOW + timedelta(seconds=60))
        await scheduler._send_auction_reminders(bot, NOW + timedelta(seconds=120))

        assert duplicate == WorkerReceipt(sends=0, pins=0, claims=1), duplicate
        assert next_window == WorkerReceipt(sends=1, pins=0, claims=2), next_window
        rows = await reminder_rows()
        assert [(row.window, row.status) for row in rows] == [(2, "sent"), (3, "sent")]
        assert len(transport.sends) == 1 and transport.pins == []
        assert await financial_rows() == before
        print("PASS real restart: old window sends=0, next window sends=1, durable windows=[2,3]")


async def single_dispatch_when_two_workers_scan_same_window() -> None:
    async with isolated_db(), captured_bot() as (bot_a, transport_a), captured_bot() as (bot_b, transport_b):
        await persist_auction(auction_at(NOW + timedelta(seconds=119)))
        before = await financial_rows()
        real_scan = auction_reminders.scan_reminders
        snapshots: list[list[ReminderCandidate]] = []
        ready = asyncio.Event()

        async def synchronized_scan(now: datetime) -> list[ReminderCandidate]:
            candidates = await real_scan(now)
            snapshots.append(candidates)
            if len(snapshots) == 2:
                ready.set()
            await ready.wait()
            return candidates

        with patch.object(auction_reminders, "scan_reminders", synchronized_scan):
            await asyncio.gather(
                scheduler._send_auction_reminders(bot_a, NOW),
                scheduler._send_auction_reminders(bot_b, NOW),
            )

        assert snapshots == [[ReminderCandidate(1, NOW + timedelta(seconds=119), 2, 1)]] * 2
        assert len(transport_a.sends) + len(transport_b.sends) == 1, (
            f"claims={len(await reminder_rows())} sends={len(transport_a.sends) + len(transport_b.sends)} "
            f"pins={len(transport_a.pins) + len(transport_b.pins)}"
        )
        assert transport_a.pins + transport_b.pins == []
        rows = await reminder_rows()
        assert len(rows) == 1 and (rows[0].window, rows[0].status) == (2, "sent")
        assert rows[0].message_id == (transport_a.delivered + transport_b.delivered)[0].message_id
        assert await financial_rows() == before
        print("PASS two synchronized scans: durable winners=1 sends=1 pins=0")


async def rows_retained_when_schema_initialized_twice() -> None:
    async with isolated_db(), captured_bot() as (bot, transport):
        async with get_engine().begin() as connection:
            await connection.run_sync(AuctionReminder.__table__.drop)
        auction = auction_at(NOW + timedelta(seconds=120))
        auction.current_bidder = 819001
        auction.current_price = 130
        await persist_auction(auction)
        async with session_scope() as session:
            await session.execute(update(Member).where(Member.id == 1).values(points=870))
            session.add_all([
                AuctionBid(auction_id=1, user_id=819001, amount=130, created_at=NOW),
                PointsTransaction(group_id=GROUP_ID, user_id=819001, amount=-130,
                                  balance_after=870, reason="Existing frozen bid", created_at=NOW),
                ConsoleUser(id=1, token_hash="offline-schema-user", balance_cents=731, created_at=NOW),
                AccountLedgerEntry(user_id=1, kind="adjustment", amount_cents=731,
                                   balance_after_cents=731, description="Existing balance", created_at=NOW),
                PaymentOrder(order_no="offline-existing", user_id=1, amount_cents=314,
                             charge_cents=314, status="pending", created_at=NOW, updated_at=NOW),
                AdminLog(group_id=GROUP_ID, action="same_action", detail="same", created_at=NOW),
                AdminLog(group_id=GROUP_ID, action="same_action", detail="same", created_at=NOW),
            ])
        before = await financial_rows()

        await init_db()
        await scheduler._send_auction_reminders(bot, NOW)
        original = await reminder_rows()
        await init_db()
        await scheduler._send_auction_reminders(bot, NOW)

        assert await financial_rows() == before
        rows = await reminder_rows()
        assert len(rows) == 1 and len(transport.sends) == 1 and transport.pins == []
        assert (rows[0].id, rows[0].status, rows[0].message_id) == (
            original[0].id, "sent", original[0].message_id,
        )
        print("PASS additive schema initialized twice: auction/bids/points/cash/payment/log rows and claim retained")


async def run_claim_tests() -> None:
    await settlement_rejected_when_scan_is_stale()
    await legacy_claims_when_upgrade_keeps_physical_windows()
    await claim_rejected_when_status_or_window_changed()
    await pending_claim_when_process_restarts()
    await sent_claim_when_process_restarts()
    await single_dispatch_when_two_workers_scan_same_window()
    await rows_retained_when_schema_initialized_twice()


async def settlement_rejected_when_scan_is_stale() -> None:
    for bidder, deadline in ((819001, NOW + timedelta(seconds=180)), (819002, NOW)):
        async with isolated_db():
            auction = auction_at(deadline)
            auction.current_bidder = bidder
            await persist_auction(auction)
            before = await financial_rows()

            with patch.object(scheduler, "utcnow", return_value=NOW):
                async with session_scope() as session:
                    claimed = await scheduler._claim_auction(session, 1, 819001, 100)

            assert not claimed, (bidder, deadline)
            assert await financial_rows() == before


async def legacy_claims_when_upgrade_keeps_physical_windows() -> None:
    for window, remaining, expected in ((1, 120, 1), (2, 120, 0), (3, 60, 0)):
        async with isolated_db(), captured_bot() as (bot, transport):
            deadline = NOW + timedelta(seconds=remaining)
            await persist_auction(auction_at(deadline))
            async with session_scope() as session:
                session.add(AuctionReminder(
                    auction_id=1, ends_at=deadline, window=window,
                    status="sent", claimed_at=NOW - timedelta(seconds=60),
                ))

            await scheduler._send_auction_reminders(bot, NOW)

            assert len(transport.sends) == expected, window
            assert len(await reminder_rows()) == 1 + expected
