"""Offline settlement delivery regression: .venv/bin/python scripts/smoke_settlement_delivery.py."""
from __future__ import annotations

import atexit
import json
import os
import sys
from collections.abc import Awaitable, Callable
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Final, Literal, assert_never, final, override
from unittest.mock import patch

RUNTIME: Final = ExitStack()
_ = atexit.register(RUNTIME.close)
ROOT: Final = Path(__file__).resolve().parents[1]
TMP: Final = Path(RUNTIME.enter_context(TemporaryDirectory(
    prefix="settlement-delivery-", dir="/tmp/opencode",
)))
CONFIG: Final = TMP / "config.json"
TOKEN: Final = "123456:offline-settlement"
_ = CONFIG.write_text(json.dumps({
    "bot": {"token": TOKEN, "admin_ids": [], "proxy": ""},
    "web": {"secret_key": "offline-only", "base_url": "https://example.test"},
    "database": {"url": f"sqlite+aiosqlite:///{TMP / 'settlement.db'}"},
    "ai": {"providers": [], "learning_dir": str(TMP / "learning")},
    "payments": {"enabled": False},
}), encoding="utf-8")
RUNTIME.enter_context(patch.dict(os.environ, {"GROUPGUARD_CONFIG": str(CONFIG)}))
_ = RUNTIME.enter_context(patch("socket.socket.connect", side_effect=AssertionError("offline network guard")))
sys.path.insert(0, str(ROOT))

import anyio
from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.methods import PinChatMessage, SendMessage, TelegramMethod
from aiogram.methods.base import TelegramType
from aiogram.types import Chat, Message
from sqlalchemy import select, text, update
from sqlalchemy.exc import OperationalError

from app.config import get_config
from app.db import dispose_db, get_engine, session_scope
from app.models import AdminLog, Auction, Base, Group, Lottery, LotteryEntry, LotteryWinner, Member, PointsTransaction, RedPacket
from app.services import scheduler

_ = RUNTIME.callback(get_config.cache_clear)
NOW: Final = datetime(2026, 11, 10, 12, 0)
GROUP: Final = -100819701
type Kind = Literal["lottery", "auction"]


@final
class OfflineSession(AiohttpSession):
    """Capture real aiogram methods; never open a network connection."""

    def __init__(self) -> None:
        super().__init__()
        self.reject_at = 0
        self.send_count = 0
        self.delivered: list[int] = []
        self.pins: list[int] = []
        self.before_send: Callable[[], Awaitable[None]] | None = None
        self.failure: BaseException | None = None

    @override
    async def make_request(
        self, bot: Bot, method: TelegramMethod[TelegramType], timeout: int | None = None,
    ) -> TelegramType:
        requested = method
        assert isinstance(method, SendMessage | PinChatMessage), type(method).__name__
        match method:
            case SendMessage():
                self.send_count += 1
                if self.before_send is not None:
                    await self.before_send()
                if self.send_count == self.reject_at:
                    raise TelegramBadRequest(method=method, message="synthetic rejection")
                if self.failure is not None:
                    raise self.failure
                self.delivered.append(self.send_count)
                message = Message(message_id=self.send_count, date=NOW.replace(tzinfo=UTC),
                                  chat=Chat(id=int(method.chat_id), type="supergroup"), text=method.text)
                body = message.model_dump_json()
            case PinChatMessage():
                assert method.disable_notification is True
                self.pins.append(method.message_id)
                body = "true"
            case unreachable:
                assert_never(unreachable)
        response = self.check_response(bot, requested, 200, '{"ok":true,"result":' + body + "}")
        assert response.result is not None
        return response.result


async def seed(kind: Kind) -> None:
    async with get_engine().begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)
    async with session_scope() as session:
        session.add(Group(id=GROUP, title="Offline settlements"))
        for item_id in (1, 2):
            session.add(Member(group_id=GROUP, user_id=10 + item_id, points=90))
            match kind:
                case "lottery":
                    session.add(Lottery(id=item_id, group_id=GROUP, creator_id=10,
                                        title=f"Draw {item_id}", prize="Prize", winners_count=1,
                                        ends_at=NOW - timedelta(seconds=1)))
                    session.add(LotteryEntry(lottery_id=item_id, user_id=10 + item_id))
                case "auction":
                    session.add(Auction(id=item_id, group_id=GROUP, creator_id=10,
                                        item=f"Item {item_id}", auction_type="currency", entry_fee=99,
                                        current_bidder=10 + item_id, current_price=120,
                                        ends_at=NOW - timedelta(seconds=1)))
                case unreachable:
                    assert_never(unreachable)
        session.add(Member(group_id=GROUP, user_id=13, points=7, is_muted=True,
                           muted_until=NOW - timedelta(seconds=1)))
        session.add(RedPacket(id=1, group_id=GROUP, sender_id=13, total=30, count=2,
                              remaining_amount=30, remaining_count=2, expires_at=NOW - timedelta(seconds=1)))


async def assert_durable(kind: Kind, refunded: bool) -> None:
    async with session_scope() as session:
        ledger = [(13, 30, 37)] if refunded else []
        balances = [(11, 90), (12, 90), (13, 37 if refunded else 7)]
        match kind:
            case "lottery":
                rows = list((await session.execute(select(Lottery.id, Lottery.status).order_by(Lottery.id))).all())
                assert rows == [(1, "drawn"), (2, "drawn")], rows
                winners = list((await session.execute(select(LotteryWinner.lottery_id, LotteryWinner.user_id).order_by(LotteryWinner.lottery_id))).all())
                assert winners == [(1, 11), (2, 12)], winners
                action = "lottery_draw"
            case "auction":
                rows = list((await session.execute(select(Auction.id, Auction.status, Auction.current_bidder, Auction.current_price).order_by(Auction.id))).all())
                assert rows == [(1, "finished", 11, 120), (2, "finished", 12, 120)], rows
                ledger.extend([(11, 10, 100), (12, 10, 100)])
                balances = [(11, 100), (12, 100), (13, 37 if refunded else 7)]
                action = "auction_finish"
            case unreachable:
                assert_never(unreachable)
        actual = list((await session.execute(select(PointsTransaction.user_id, PointsTransaction.amount, PointsTransaction.balance_after).order_by(PointsTransaction.user_id))).all())
        assert actual == sorted(ledger), actual
        actual_balances = list((await session.execute(select(Member.user_id, Member.points).order_by(Member.user_id))).all())
        assert actual_balances == balances, actual_balances
        actions = list((await session.scalars(select(AdminLog.action).order_by(AdminLog.action))).all())
        assert actions == sorted([action, action] + (["redpacket_expire"] if refunded else [])), actions
        assert await session.scalar(select(RedPacket.status)) == ("expired" if refunded else "open")
        assert await session.scalar(select(Member.is_muted).where(Member.user_id == 13)) is (not refunded)


async def rejection_preserves_settlements(kind: Kind, reject_at: int) -> None:
    await seed(kind)
    async with OfflineSession() as transport:
        transport.reject_at = reject_at
        bot = Bot(TOKEN, session=transport)
        escaped = None
        try:
            await scheduler.tick(bot)
        except TelegramAPIError as exc:
            escaped = type(exc).__name__
        print(f"OBSERVE {kind}/reject-{reject_at}: accepted={transport.delivered} pins={transport.pins} escaped={escaped}")
        await assert_durable(kind, refunded=True)
        assert escaped is None, escaped
        assert transport.delivered == [number for number in (1, 2, 3) if number != reject_at]
        assert transport.pins == [number for number in (1, 2) if number != reject_at]
        await scheduler.tick(bot)
        await assert_durable(kind, refunded=True)
        assert transport.send_count == 3, transport.send_count


async def writer_during_dispatch(kind: Kind) -> None:
    await seed(kind)
    writes: list[int] = []

    async def write() -> None:
        async with session_scope() as session:
            _ = await session.execute(text("PRAGMA busy_timeout=0"))
            _ = await session.execute(update(Group).where(Group.id == GROUP).values(title="Dispatch writer", updated_at=NOW))
            for item_id in (1, 2):
                match kind:
                    case "lottery":
                        assert not await scheduler._claim_lottery(session, item_id)
                    case "auction":
                        assert not await scheduler._claim_auction(session, item_id, 10 + item_id, 120)
                    case unreachable:
                        assert_never(unreachable)
        writes.append(1)
        await assert_durable(kind, refunded=False)

    async with OfflineSession() as transport:
        transport.before_send = write
        bot = Bot(TOKEN, session=transport)
        match kind:
            case "lottery":
                await scheduler.draw_lotteries(bot)
            case "auction":
                await scheduler.finish_auctions(bot)
            case unreachable:
                assert_never(unreachable)
        assert len(writes) == 2
        assert transport.delivered == transport.pins == [1, 2]


async def unexpected_failure_propagates(kind: Kind, failure: BaseException) -> None:
    await seed(kind)
    async with OfflineSession() as transport:
        transport.failure = failure
        try:
            await scheduler.tick(Bot(TOKEN, session=transport))
        except type(failure) as exc:
            assert exc is failure
        else:
            raise AssertionError("delivery failure was swallowed")
        await assert_durable(kind, refunded=False)
        assert transport.send_count == 1 and transport.pins == []


async def main() -> None:
    failures: list[str] = []
    try:
        with patch.object(scheduler, "utcnow", return_value=NOW):
            for kind in ("lottery", "auction"):
                for name, case in (
                    ("reject-second", partial(rejection_preserves_settlements, kind, 2)),
                    ("reject-first", partial(rejection_preserves_settlements, kind, 1)),
                    ("writer", partial(writer_during_dispatch, kind)),
                    ("unexpected", partial(unexpected_failure_propagates, kind, RuntimeError())),
                    ("cancel", partial(unexpected_failure_propagates, kind, anyio.get_cancelled_exc_class()())),
                ):
                    try:
                        await case()
                    except (AssertionError, TelegramAPIError, OperationalError) as exc:
                        failures.append(f"{kind}/{name}")
                        print(f"FAIL {kind}/{name}: {type(exc).__name__}: {exc}")
                    else:
                        print(f"PASS {kind}/{name}")
    finally:
        await dispose_db()
    assert not failures, failures


if __name__ == "__main__":
    with RUNTIME:
        anyio.run(main)
