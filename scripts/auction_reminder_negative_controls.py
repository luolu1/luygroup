from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Final
from unittest.mock import patch

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

import auction_reminder_claim_tests as claim_tests
from auction_reminder_extension_tests import rearmed_when_old_deadline_already_has_sent_claim
from auction_reminder_fixtures import WorkerReceipt
from smoke_auction_reminders import repeated_ticks_when_deadline_unchanged

from app.db import session_scope
from app.models import AuctionReminder
from app.services import auction_reminders
from app.services.auction_reminders import ReminderCandidate

type Scenario = Callable[[], Awaitable[None]]
type Claim = Callable[[AsyncSession, ReminderCandidate, datetime], Awaitable[int | None]]
REAL_CLAIM: Final = auction_reminders.claim_reminder
REAL_RESTART: Final = claim_tests.run_restart_worker


async def reclaim_successful_send(
    session: AsyncSession, candidate: ReminderCandidate, now: datetime
) -> int | None:
    await session.execute(update(AuctionReminder).where(
        AuctionReminder.auction_id == candidate.auction_id,
        AuctionReminder.ends_at == candidate.ends_at,
        AuctionReminder.window == candidate.window,
        AuctionReminder.status == "sent",
    ).values(status="failed"))
    return await REAL_CLAIM(session, candidate, now)


async def suppress_new_deadline_with_lifetime_key(
    session: AsyncSession, candidate: ReminderCandidate, now: datetime
) -> int | None:
    existing = await session.scalar(select(AuctionReminder.id).where(
        AuctionReminder.auction_id == candidate.auction_id,
        AuctionReminder.window == candidate.window,
    ))
    if existing is not None:
        return None
    return await REAL_CLAIM(session, candidate, now)


async def dispatch_after_losing_claim(
    session: AsyncSession, candidate: ReminderCandidate, now: datetime
) -> int | None:
    claimed = await REAL_CLAIM(session, candidate, now)
    if claimed is not None:
        return claimed
    return await session.scalar(select(AuctionReminder.id).where(
        AuctionReminder.auction_id == candidate.auction_id,
        AuctionReminder.ends_at == candidate.ends_at,
        AuctionReminder.window == candidate.window,
    ))


async def forget_ownership_before_restart(now: datetime) -> WorkerReceipt:
    async with session_scope() as session:
        await session.execute(delete(AuctionReminder))
    return await REAL_RESTART(now)


async def expect_red(scenario: Scenario, label: str) -> None:
    try:
        await scenario()
    except AssertionError as exc:
        print(f"RED negative control {label}: {exc}")
        return
    raise AssertionError(f"Negative control escaped detection: {label}")


async def prove_regressions() -> None:
    controls: tuple[tuple[str, Scenario, Claim], ...] = (
        ("repeated successful send reclaimed", repeated_ticks_when_deadline_unchanged, reclaim_successful_send),
        ("old lifetime deadline key", rearmed_when_old_deadline_already_has_sent_claim, suppress_new_deadline_with_lifetime_key),
        ("loser dispatches without ownership", claim_tests.single_dispatch_when_two_workers_scan_same_window, dispatch_after_losing_claim),
    )
    for label, scenario, mutant in controls:
        with patch.object(auction_reminders, "claim_reminder", mutant):
            await expect_red(scenario, label)
        await scenario()
        print(f"GREEN real implementation {label}")

    with patch.object(claim_tests, "run_restart_worker", forget_ownership_before_restart):
        await expect_red(claim_tests.sent_claim_when_process_restarts, "restart loses ownership")
    await claim_tests.sent_claim_when_process_restarts()
    print("GREEN real implementation restart retains ownership")
