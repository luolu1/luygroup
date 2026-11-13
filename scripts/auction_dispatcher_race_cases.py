"""Financial failure scenarios using real SQL and real dispatcher bid paths."""

from __future__ import annotations

import asyncio
from datetime import datetime
from unittest.mock import patch

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.handlers import games
from app.bot.handlers import games_auction
from app.models import Member
from app.services import repo
from auction_dispatcher_fixture import Harness
from auction_dispatcher_transport import bid_cards


async def test_mixed_race_publishes_once_when_snapshots_match(harness: Harness) -> None:
    scene = await harness.seed("MIXRACE")
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
            harness.say(scene.group_id, scene.second, "/bid MIXRACE 100"),
            harness.click(scene.first, scene.original, f"bid:{scene.auction.id}:100"),
        )
    state = await harness.state(scene)
    assert len(state.bids) == 1 and state.price == 100
    assert sorted(dict(state.balances).values()) == [400, 500]
    assert len(state.transactions) == 1 and state.transactions[0][1] == -100
    assert len(bid_cards(tuple(harness.session.calls[start:]))) == 1


async def debit_failure(harness: Harness, *, callback: bool) -> None:
    scene = await harness.seed(f"ROLL{harness.serial + 1}")
    await harness.click(scene.first, scene.original, f"bid:{scene.auction.id}:100")
    before = await harness.state(scene)
    original = repo.adjust_points

    async def depleted_balance(
        session: AsyncSession, group_id: int, user_id: int, amount: int, reason: str,
        *, allow_negative: bool = False, username: str = "", first_name: str = "",
    ) -> tuple[Member, int] | None:
        if group_id == scene.group_id and user_id == scene.second.id and amount < 0:
            await session.execute(update(Member).where(
                Member.group_id == group_id, Member.user_id == user_id
            ).values(points=0))
        return await original(
            session, group_id, user_id, amount, reason,
            allow_negative=allow_negative, username=username, first_name=first_name,
        )

    with patch.object(repo, "adjust_points", new=depleted_balance):
        if callback:
            requests = await harness.click(scene.second, scene.original, f"bid:{scene.auction.id}:100")
        else:
            requests = await harness.say(scene.group_id, scene.second, f"/bid {scene.auction.code} 110")
    after = await harness.state(scene)
    assert after == before, (before, after)
    assert not bid_cards(requests)


async def test_command_rolls_back_when_debit_fails_after_cas_and_refund(harness: Harness) -> None:
    await debit_failure(harness, callback=False)


async def test_callback_rolls_back_when_debit_fails_after_cas_and_refund(harness: Harness) -> None:
    await debit_failure(harness, callback=True)
