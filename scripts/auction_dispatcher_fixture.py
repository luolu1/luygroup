"""Synthetic SQLite scenes and real dispatcher updates for auction regressions."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import assert_never

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.types import (
    CallbackQuery, Chat, InlineKeyboardButton, InlineKeyboardMarkup, Message,
    MessageEntity, Update, User,
)
from sqlalchemy import select

from app.bot import helpers
from app.bot.handlers import games
from app.db import session_scope
from app.models import Auction, AuctionBid, Group, Member, PointsTransaction, utcnow
from auction_dispatcher_transport import AuctionRequest, OfflineAuctionSession

TOKEN = "123456789:AUCTION_DISPATCHER_SYNTHETIC_TOKEN"


@dataclass(frozen=True, slots=True)
class Scene:
    auction: Auction
    first: User
    second: User
    original: Message

    @property
    def group_id(self) -> int:
        return int(self.auction.group_id)


@dataclass(frozen=True, slots=True)
class Snapshot:
    price: int
    bidder: int | None
    ends_at: datetime
    bids: tuple[tuple[int, int], ...]
    balances: tuple[tuple[int, int], ...]
    transactions: tuple[tuple[int, int], ...]


class Harness:
    """Feed SDK updates into one Dispatcher; each scene owns a unique synthetic group."""

    def __init__(self, dispatcher: Dispatcher) -> None:
        self.dispatcher = dispatcher
        self.session = OfflineAuctionSession()
        self.bot = Bot(TOKEN, session=self.session, default=DefaultBotProperties(parse_mode="HTML"))
        self.group_chats: dict[int, Chat] = {}
        self.serial = 0
        self.update_id = 0

    async def seed(self, code: str, *, auction_type: str = "points") -> Scene:
        self.serial += 1
        group_id = -100730000 - self.serial
        first = User(id=730000 + self.serial * 10 + 1, is_bot=False, first_name="Alice", username=f"alice{self.serial}")
        second = User(id=first.id + 1, is_bot=False, first_name="Bob", username=f"bob{self.serial}")
        async with session_scope() as session:
            session.add(Group(
                id=group_id, title=f"Synthetic {code}", points_enabled=True,
                points_per_message=0, ai_assistant_enabled=False, anti_ad_enabled=False,
                anti_spam_enabled=False, flood_limit=0,
            ))
            session.add_all([
                Member(group_id=group_id, user_id=user.id, username=user.username, first_name=user.first_name, points=500)
                for user in (first, second)
            ])
        auction = await self.another(group_id, code, auction_type=auction_type)
        chat = Chat(id=group_id, type="supergroup", title=f"Synthetic {code}")
        self.group_chats[group_id] = chat
        original = Message(
            message_id=int(auction.message_id), date=datetime.now(UTC),
            chat=chat,
            from_user=User(id=self.bot.id, is_bot=True, first_name="Offline auction bot"),
            text=f"Legacy auction {code}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="Legacy bid", callback_data=f"bid:{auction.id}:100")
            ]]),
        )
        self.session.messages[(group_id, original.message_id)] = original
        return Scene(auction, first, second, original)

    async def another(self, group_id: int, code: str, *, auction_type: str = "points") -> Auction:
        async with session_scope() as session:
            auction = Auction(
                group_id=group_id, code=code, creator_id=900001, item=f"Item {code}",
                description=f"Description {code}", auction_type=auction_type,
                entry_fee=10 if auction_type == "currency" else 0,
                start_price=100, current_price=100, step=10, extend_minutes=3,
                status="open", ends_at=utcnow() + timedelta(seconds=20),
            )
            session.add(auction)
            await session.flush()
            auction.message_id = 500 + int(auction.id)
            return auction

    async def feed(self, event: Message | CallbackQuery) -> tuple[AuctionRequest, ...]:
        self.update_id += 1
        match event:
            case Message():
                update = Update(update_id=self.update_id, message=event)
            case CallbackQuery():
                update = Update(update_id=self.update_id, callback_query=event)
            case unreachable:
                assert_never(unreachable)
        start = len(self.session.calls)
        await self.dispatcher.feed_update(self.bot, update)
        return tuple(self.session.calls[start:])

    async def say(
        self, chat_id: int, user: User, text: str, *, reply: Message | None = None
    ) -> tuple[AuctionRequest, ...]:
        message = Message(
            message_id=10000 + self.update_id, date=datetime.now(UTC),
            chat=Chat(id=chat_id, type="private") if chat_id > 0 else self.group_chats[chat_id],
            from_user=user, text=text, reply_to_message=reply,
            entities=[MessageEntity(type="bot_command", offset=0, length=len(text.split()[0]))] if text.startswith("/") else None,
        )
        self.session.messages[(chat_id, message.message_id)] = message
        return await self.feed(message)

    async def click(self, user: User, message: Message, data: str) -> tuple[AuctionRequest, ...]:
        return await self.feed(CallbackQuery(
            id=f"auction-callback-{self.update_id}", from_user=user,
            chat_instance="synthetic-auction-chat", message=message, data=data,
        ))

    async def state(self, scene: Scene) -> Snapshot:
        async with session_scope() as session:
            auction = await session.get(Auction, int(scene.auction.id))
            assert auction is not None and auction.ends_at is not None
            bids = (await session.execute(
                select(AuctionBid.user_id, AuctionBid.amount)
                .where(AuctionBid.auction_id == auction.id).order_by(AuctionBid.id)
            )).all()
            balances = (await session.execute(
                select(Member.user_id, Member.points).where(Member.group_id == scene.group_id).order_by(Member.user_id)
            )).all()
            transactions = (await session.execute(
                select(PointsTransaction.user_id, PointsTransaction.amount)
                .where(PointsTransaction.group_id == scene.group_id).order_by(PointsTransaction.id)
            )).all()
            return Snapshot(
                int(auction.current_price), auction.current_bidder, auction.ends_at,
                tuple((int(user), int(amount)) for user, amount in bids),
                tuple((int(user), int(points)) for user, points in balances),
                tuple((int(user), int(amount)) for user, amount in transactions),
            )

    def private_prompt(self, user: User) -> Message:
        return max(
            (message for (chat_id, _), message in self.session.messages.items() if chat_id == user.id and message.from_user and message.from_user.is_bot),
            key=lambda message: message.message_id,
        )

    async def settle(self) -> None:
        self.session.fail_next.clear()
        self.session.on_request = None
        await asyncio.gather(*tuple(games._promotion_tasks))
        tasks = tuple(helpers._background_tasks | games._lottery_expiry_tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        games._auction_drafts.clear()
        games._lottery_drafts.clear()

    async def close(self) -> None:
        await self.settle()
        await self.dispatcher.storage.close()
        await self.bot.session.close()
