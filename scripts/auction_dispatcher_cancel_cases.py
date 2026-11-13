from __future__ import annotations

from typing import Never
from unittest.mock import patch

import anyio
from aiogram.types import Chat
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.handlers import games
from app.bot.handlers import games_auction
from app.db import session_scope
from app.models import Auction, Member, PointsTransaction
from app.services import repo
from auction_dispatcher_fixture import Harness, Scene


class RefundMutationError(RuntimeError):
    def __init__(self, auction_id: int) -> None:
        self.auction_id = auction_id
        super().__init__(f"Synthetic refund mutation failure for auction {auction_id}")


async def creator_scene(harness: Harness, *, currency: bool = True) -> Scene:
    scene = await harness.seed(
        f"CANCEL{harness.serial + 1}", auction_type="currency" if currency else "points"
    )
    async with session_scope() as session:
        await session.execute(update(Auction).where(Auction.id == scene.auction.id).values(
            creator_id=scene.first.id, start_price=0, current_price=0,
        ))
    return scene


async def funded_scene(harness: Harness, price: int = 125000, *, currency: bool = True) -> Scene:
    scene = await creator_scene(harness, currency=currency)
    await harness.say(scene.group_id, scene.second, f"/bid {scene.auction.code} {max(1, price)}")
    if price == 0:
        async with session_scope() as session:
            await session.execute(update(Auction).where(Auction.id == scene.auction.id).values(current_price=0))
    state = await harness.state(scene)
    assert state.price == price and state.bidder == scene.second.id, state
    assert dict(state.balances) == {scene.first.id: 500, scene.second.id: 490 if currency else 500 - price}, state
    return scene


async def cancel(harness: Harness, scene: Scene, *, callback: bool) -> None:
    if callback:
        prompt = scene.original.model_copy(update={"chat": Chat(id=scene.first.id, type="private")})
        await harness.click(scene.first, prompt, f"aucmanage:cancel:{scene.auction.id}")
    else:
        await harness.say(scene.first.id, scene.first, f"/jpcancel {scene.auction.code}")


async def assert_refund(harness: Harness, scene: Scene, amount: int) -> None:
    state = await harness.state(scene)
    assert dict(state.balances) == {scene.first.id: 500, scene.second.id: 500}, state.balances
    assert state.transactions == ((scene.second.id, -amount), (scene.second.id, amount)), state.transactions
    async with session_scope() as session:
        assert await session.scalar(select(Auction.status).where(Auction.id == scene.auction.id)) == "cancelled"


async def test_cancel_command_refunds_fee_when_currency_price_is_large(harness: Harness) -> None:
    scene = await funded_scene(harness)
    await cancel(harness, scene, callback=False)
    await assert_refund(harness, scene, 10)


async def test_cancel_callback_refunds_fee_when_currency_price_is_large(harness: Harness) -> None:
    scene = await funded_scene(harness)
    await cancel(harness, scene, callback=True)
    await assert_refund(harness, scene, 10)


async def test_cancel_command_refunds_fee_when_currency_price_is_below_fee(harness: Harness) -> None:
    scene = await funded_scene(harness, 5)
    await cancel(harness, scene, callback=False)
    await assert_refund(harness, scene, 10)


async def test_cancel_callback_refunds_fee_when_currency_price_is_below_fee(harness: Harness) -> None:
    scene = await funded_scene(harness, 5)
    await cancel(harness, scene, callback=True)
    await assert_refund(harness, scene, 10)


async def test_cancel_command_refunds_fee_when_currency_price_is_zero(harness: Harness) -> None:
    scene = await funded_scene(harness, 0)
    await cancel(harness, scene, callback=False)
    await assert_refund(harness, scene, 10)


async def test_cancel_callback_refunds_fee_when_currency_price_is_zero(harness: Harness) -> None:
    scene = await funded_scene(harness, 0)
    await cancel(harness, scene, callback=True)
    await assert_refund(harness, scene, 10)


async def test_cancel_command_refunds_bid_when_auction_uses_points(harness: Harness) -> None:
    scene = await funded_scene(harness, 150, currency=False)
    await cancel(harness, scene, callback=False)
    await assert_refund(harness, scene, 150)


async def test_cancel_callback_refunds_bid_when_auction_uses_points(harness: Harness) -> None:
    scene = await funded_scene(harness, 150, currency=False)
    await cancel(harness, scene, callback=True)
    await assert_refund(harness, scene, 150)


async def cancel_without_bidder(harness: Harness, *, callback: bool) -> None:
    scene = await creator_scene(harness)
    before = await harness.state(scene)
    await cancel(harness, scene, callback=callback)
    assert await harness.state(scene) == before
    async with session_scope() as session:
        assert await session.scalar(select(Auction.status).where(Auction.id == scene.auction.id)) == "cancelled"


async def test_cancel_command_has_no_refund_when_bidder_is_absent(harness: Harness) -> None:
    await cancel_without_bidder(harness, callback=False)


async def test_cancel_callback_has_no_refund_when_bidder_is_absent(harness: Harness) -> None:
    await cancel_without_bidder(harness, callback=True)


async def unauthorized_cancel(harness: Harness, *, callback: bool) -> None:
    scene = await funded_scene(harness)
    before = await harness.state(scene)
    if callback:
        prompt = scene.original.model_copy(update={"chat": Chat(id=scene.second.id, type="private")})
        await harness.click(scene.second, prompt, f"aucmanage:cancel:{scene.auction.id}")
    else:
        await harness.say(scene.second.id, scene.second, f"/jpcancel {scene.auction.code}")
    assert await harness.state(scene) == before
    async with session_scope() as session:
        assert await session.scalar(select(Auction.status).where(Auction.id == scene.auction.id)) == "open"


async def test_cancel_command_keeps_funds_when_actor_is_not_creator(harness: Harness) -> None:
    await unauthorized_cancel(harness, callback=False)


async def test_cancel_callback_keeps_funds_when_actor_is_not_creator(harness: Harness) -> None:
    await unauthorized_cancel(harness, callback=True)


async def repeated_cancel(harness: Harness, *, callback: bool) -> None:
    scene = await funded_scene(harness)
    await cancel(harness, scene, callback=callback)
    await cancel(harness, scene, callback=callback)
    await cancel(harness, scene, callback=not callback)
    await assert_refund(harness, scene, 10)


async def test_cancel_command_refunds_once_when_command_and_button_are_replayed(harness: Harness) -> None:
    await repeated_cancel(harness, callback=False)


async def test_cancel_callback_refunds_once_when_button_and_command_are_replayed(harness: Harness) -> None:
    await repeated_cancel(harness, callback=True)


async def test_cancel_refunds_current_bidder_only_when_creator_was_outbid(harness: Harness) -> None:
    scene = await creator_scene(harness)
    await harness.say(scene.group_id, scene.first, f"/bid {scene.auction.code} 100")
    await harness.say(scene.group_id, scene.second, f"/bid {scene.auction.code} 125000")
    before = await harness.state(scene)
    assert before.transactions == ((scene.first.id, -10), (scene.first.id, 10), (scene.second.id, -10)), before

    await cancel(harness, scene, callback=False)

    state = await harness.state(scene)
    assert dict(state.balances) == {scene.first.id: 500, scene.second.id: 500}, state.balances
    assert state.transactions == (*before.transactions, (scene.second.id, 10)), state.transactions
    async with session_scope() as session:
        assert await session.scalar(select(Auction.status).where(Auction.id == scene.auction.id)) == "cancelled"


async def test_cancel_refunds_once_when_command_and_callback_compete(harness: Harness) -> None:
    scene = await funded_scene(harness)
    original = games._cancel_auction
    ready = anyio.Event()
    arrivals = 0

    async def gated_cancel(session: AsyncSession, auction: Auction | None, user_id: int) -> str:
        nonlocal arrivals
        assert auction is not None and auction.status == "open"
        arrivals += 1
        if arrivals == 2:
            ready.set()
        with anyio.fail_after(5):
            await ready.wait()
        return await original(session, auction, user_id)

    prompt = scene.original.model_copy(update={"chat": Chat(id=scene.first.id, type="private")})
    with patch.object(games_auction, "_cancel_auction", new=gated_cancel):
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(harness.say, scene.first.id, scene.first, f"/jpcancel {scene.auction.code}")
            tasks.start_soon(harness.click, scene.first, prompt, f"aucmanage:cancel:{scene.auction.id}")
    assert arrivals == 2
    await assert_refund(harness, scene, 10)


async def rollback_cancel(harness: Harness, *, callback: bool) -> None:
    scene = await funded_scene(harness)
    before = await harness.state(scene)
    original = repo.adjust_points
    mutated = False

    async def refund_then_fail(session: AsyncSession, group_id: int, user_id: int, amount: int, reason: str) -> Never:
        nonlocal mutated
        result = await original(session, group_id, user_id, amount, reason)
        assert result is not None
        await session.flush()
        assert await session.scalar(select(Auction.status).where(Auction.id == scene.auction.id)) == "cancelled"
        assert await session.scalar(select(Member.points).where(Member.group_id == group_id, Member.user_id == user_id)) == 490 + amount
        assert await session.scalar(select(PointsTransaction.amount).where(PointsTransaction.group_id == group_id).order_by(PointsTransaction.id.desc()).limit(1)) == amount
        mutated = True
        raise RefundMutationError(int(scene.auction.id))

    with patch.object(repo, "adjust_points", new=refund_then_fail):
        try:
            await cancel(harness, scene, callback=callback)
        except RefundMutationError as error:
            assert error.auction_id == scene.auction.id
        else:
            raise AssertionError("Cancellation did not propagate the injected refund failure")
    assert mutated
    assert await harness.state(scene) == before
    async with session_scope() as session:
        assert await session.scalar(select(Auction.status).where(Auction.id == scene.auction.id)) == "open"


async def test_cancel_command_rolls_back_when_refund_raises_after_sql_mutation(harness: Harness) -> None:
    await rollback_cancel(harness, callback=False)


async def test_cancel_callback_rolls_back_when_refund_raises_after_sql_mutation(harness: Harness) -> None:
    await rollback_cancel(harness, callback=True)


async def cancel_after_outbid(harness: Harness, *, currency: bool, callback: bool) -> None:
    scene = await creator_scene(harness, currency=currency)
    await harness.say(scene.group_id, scene.first, f"/bid {scene.auction.code} 100")
    original = games_auction._cancel_auction
    arrivals = 0
    first_cost, second_cost = (10, 10) if currency else (100, 150)

    async def outbid_before_cancel(session: AsyncSession, auction: Auction | None, user_id: int) -> str:
        nonlocal arrivals
        assert auction is not None and auction.current_bidder == scene.first.id
        arrivals += 1
        await harness.say(scene.group_id, scene.second, f"/bid {scene.auction.code} 150")
        committed = await harness.state(scene)
        assert committed.bidder == scene.second.id and committed.price == 150, committed
        assert committed.transactions == (
            (scene.first.id, -first_cost), (scene.first.id, first_cost),
            (scene.second.id, -second_cost),
        ), committed.transactions
        assert auction.current_bidder == scene.first.id and auction.current_price == 100
        return await original(session, auction, user_id)

    with patch.object(games_auction, "_cancel_auction", new=outbid_before_cancel):
        await cancel(harness, scene, callback=callback)

    assert arrivals == 1
    state = await harness.state(scene)
    assert dict(state.balances) == {scene.first.id: 500, scene.second.id: 500}, state.balances
    assert state.transactions == (
        (scene.first.id, -first_cost), (scene.first.id, first_cost),
        (scene.second.id, -second_cost), (scene.second.id, second_cost),
    ), state.transactions
    async with session_scope() as session:
        assert await session.scalar(select(Auction.status).where(Auction.id == scene.auction.id)) == "cancelled"


async def test_cancel_command_refunds_latest_bid_when_points_bid_commits_after_read(harness: Harness) -> None:
    await cancel_after_outbid(harness, currency=False, callback=False)


async def test_cancel_callback_refunds_latest_bid_when_points_bid_commits_after_read(harness: Harness) -> None:
    await cancel_after_outbid(harness, currency=False, callback=True)


async def test_cancel_command_refunds_latest_fee_when_currency_bid_commits_after_read(harness: Harness) -> None:
    await cancel_after_outbid(harness, currency=True, callback=False)


async def test_cancel_callback_refunds_latest_fee_when_currency_bid_commits_after_read(harness: Harness) -> None:
    await cancel_after_outbid(harness, currency=True, callback=True)
