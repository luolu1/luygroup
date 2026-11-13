"""Offline reminder fixtures with an isolated SQLite database and Telegram transport."""
from __future__ import annotations

import json
import asyncio
import logging
import os
import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Final, assert_never
from unittest.mock import patch

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.methods import DeleteMessage, PinChatMessage, SendMessage, TelegramMethod
from aiogram.methods.base import TelegramType
from aiogram.types import Chat, Message
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select

ROOT: Final = Path(__file__).resolve().parents[1]
NOW: Final = datetime(2026, 11, 9, 1, 2, 3)
GROUP_ID: Final = -100819001
TOKEN: Final = "123456:offline-reminders"
sys.path.insert(0, str(ROOT))

from app.config import AppConfig, get_config
from app.bot import helpers
from app.db import dispose_db, init_db, session_scope
from app.models import (
    AccountLedgerEntry,
    AdminLog,
    Auction,
    AuctionBid,
    AuctionReminder,
    ConsoleUser,
    Group,
    Member,
    PaymentOrder,
    PointsTransaction,
)
from app.services import auction_reminders


class WorkerReceipt(BaseModel):
    model_config = ConfigDict(frozen=True)

    sends: int
    pins: int
    claims: int


class CaptureSession(AiohttpSession):
    """Record real aiogram methods instead of opening an HTTP connection."""

    def __init__(self) -> None:
        super().__init__()
        self.methods: list[SendMessage | PinChatMessage | DeleteMessage] = []
        self.delivered: list[Message] = []
        self.before_request: Callable[[SendMessage | PinChatMessage | DeleteMessage], Awaitable[None]] | None = None
        self.after_send: Callable[[Message], Awaitable[None]] | None = None

    @property
    def sends(self) -> list[SendMessage]:
        return [method for method in self.methods if isinstance(method, SendMessage)]

    @property
    def pins(self) -> list[PinChatMessage]:
        return [method for method in self.methods if isinstance(method, PinChatMessage)]

    @property
    def deletes(self) -> list[DeleteMessage]:
        return [method for method in self.methods if isinstance(method, DeleteMessage)]

    async def make_request(
        self, bot: Bot, method: TelegramMethod[TelegramType], timeout: int | None = None
    ) -> TelegramType:
        requested_method = method
        assert isinstance(method, SendMessage | PinChatMessage | DeleteMessage), type(method).__name__
        self.methods.append(method)
        if self.before_request is not None:
            await self.before_request(method)
        match method:
            case SendMessage():
                message = Message(
                    message_id=9000 + len(self.sends),
                    date=NOW.replace(tzinfo=UTC),
                    chat=Chat(id=int(method.chat_id), type="supergroup"),
                    text=method.text,
                )
                self.delivered.append(message)
                if self.after_send is not None:
                    await self.after_send(message)
                content = '{"ok":true,"result":' + message.model_dump_json() + "}"
            case PinChatMessage() | DeleteMessage():
                content = json.dumps({"ok": True, "result": True})
            case unexpected:
                assert_never(unexpected)
        response = self.check_response(bot, requested_method, 200, content)
        assert response.result is not None
        return response.result


@asynccontextmanager
async def captured_bot() -> AsyncIterator[tuple[Bot, CaptureSession]]:
    with patch.object(auction_reminders, "monotonic", return_value=0.0):
        async with CaptureSession() as transport:
            previous_tasks = set(helpers._background_tasks)
            try:
                yield Bot(
                    TOKEN, session=transport, default=DefaultBotProperties(parse_mode="HTML")
                ), transport
            finally:
                tasks = tuple(helpers._background_tasks - previous_tasks)
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)


@asynccontextmanager
async def isolated_db() -> AsyncIterator[Path]:
    """Create only synthetic config/data, restoring environment and closing DB in finally."""
    previous_config = os.environ.get("GROUPGUARD_CONFIG")
    with TemporaryDirectory(prefix="auction-reminders-", dir="/tmp/opencode") as directory:
        root = Path(directory)
        config = AppConfig.model_validate_json(
            (ROOT / "config.example.json").read_text(encoding="utf-8")
        )
        config.bot.token = TOKEN
        config.bot.admin_ids = []
        config.ai.providers = []
        config.ai.learning_dir = str(root / "learning")
        config.database.url = f"sqlite+aiosqlite:///{root / 'reminders.db'}"
        config.config_path = root / "config.json"
        config.config_path.write_text(config.model_dump_json(), encoding="utf-8")
        os.environ["GROUPGUARD_CONFIG"] = str(config.config_path)
        get_config.cache_clear()
        try:
            await init_db()
            async with session_scope() as session:
                session.add(Group(
                    id=GROUP_ID, title="Offline reminders", points_name="积分",
                    created_at=NOW, updated_at=NOW,
                ))
                session.add(Member(
                    group_id=GROUP_ID, user_id=819001, points=1000,
                    joined_at=NOW, last_seen=NOW,
                ))
            yield root
        finally:
            await dispose_db()
            get_config.cache_clear()
            if previous_config is None:
                os.environ.pop("GROUPGUARD_CONFIG", None)
            else:
                os.environ["GROUPGUARD_CONFIG"] = previous_config


def auction_at(ends_at: datetime | None) -> Auction:
    return Auction(
        id=1,
        code="A00000001",
        group_id=GROUP_ID,
        creator_id=819001,
        item="Offline item <one>",
        description="Description & details",
        auction_type="points",
        start_price=100,
        current_price=100,
        step=10,
        extend_minutes=1,
        current_bidder=None,
        status="open",
        ends_at=ends_at,
        created_at=NOW,
    )


async def persist_auction(auction: Auction) -> None:
    async with session_scope() as session:
        session.add(auction)


async def reminder_rows() -> list[AuctionReminder]:
    async with session_scope() as session:
        return list((await session.scalars(select(AuctionReminder).order_by(AuctionReminder.id))).all())


async def financial_rows() -> tuple[str, ...]:
    rows: list[str] = []
    async with session_scope() as session:
        for model in (
            Auction, AuctionBid, Member, PointsTransaction,
            ConsoleUser, AccountLedgerEntry, PaymentOrder, AdminLog,
        ):
            result = await session.execute(select(model.__table__).order_by(model.id))
            rows.extend(f"{model.__tablename__}:{tuple(row)!r}" for row in result.all())
    return tuple(rows)


class CaptureLogs(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@asynccontextmanager
async def captured_logs() -> AsyncIterator[CaptureLogs]:
    handler = CaptureLogs()
    logger = logging.getLogger("app")
    logger.addHandler(handler)
    try:
        yield handler
    finally:
        logger.removeHandler(handler)
        handler.close()
