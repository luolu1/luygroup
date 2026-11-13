"""Per-deadline countdown claims, committed before Telegram dispatch."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from html import escape
from time import monotonic
from typing import Literal
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.exceptions import (
    ClientDecodeError,
    TelegramAPIError,
    TelegramBadRequest,
    TelegramConflictError,
    TelegramForbiddenError,
    TelegramMigrateToChat,
    TelegramNotFound,
    TelegramRetryAfter,
    TelegramUnauthorizedError,
)
from sqlalchemy import literal, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from ..bot import helpers, keyboards
from ..db import session_scope
from ..models import Auction, AuctionReminder, Member

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ReminderCandidate:
    auction_id: int
    ends_at: datetime
    window: Literal[2, 3]
    hammer_interval: int


def _current_window(ends_at: datetime, now: datetime, hammer_interval: int) -> Literal[2, 3]:
    hammer = max(1, hammer_interval)
    return 2 if ends_at > now + timedelta(minutes=hammer) else 3


async def scan_reminders(now: datetime) -> list[ReminderCandidate]:
    async with session_scope() as session:
        # Scan with a reasonable max window (assume max hammer_interval is 60)
        rows = (
            await session.execute(
                select(Auction.id, Auction.ends_at, Auction.hammer_interval).where(
                    Auction.status == "open",
                    Auction.ends_at > now,
                    Auction.ends_at <= now + timedelta(minutes=180),
                )
            )
        ).all()
    # Filter to only those within their dynamic window (2x hammer_interval)
    candidates = []
    for auction_id, ends_at, hammer_interval in rows:
        hammer = max(1, hammer_interval or 1)
        if ends_at <= now + timedelta(minutes=hammer * 2):
            candidates.append(
                ReminderCandidate(
                    auction_id,
                    ends_at,
                    _current_window(ends_at, now, hammer),
                    hammer,
                )
            )
    return candidates


async def claim_reminder(
    session: AsyncSession, candidate: ReminderCandidate, now: datetime
) -> int | None:
    """Revalidate the scan atomically; only confirmed rejections may be reclaimed."""
    hammer = max(1, candidate.hammer_interval)
    lower = now + timedelta(minutes=(3 - candidate.window) * hammer)
    upper = lower + timedelta(minutes=hammer)
    eligible = select(
        Auction.id, Auction.ends_at, literal(candidate.window), literal("pending"), literal(now)
    ).where(
        Auction.id == candidate.auction_id,
        Auction.status == "open",
        Auction.ends_at == candidate.ends_at,
        Auction.ends_at > lower,
        Auction.ends_at <= upper,
    )
    statement = (
        sqlite_insert(AuctionReminder)
        .from_select(["auction_id", "ends_at", "window", "status", "claimed_at"], eligible)
        .on_conflict_do_update(
            index_elements=["auction_id", "ends_at", "window"],
            set_={"status": "pending", "claimed_at": now, "last_error": ""},
            where=AuctionReminder.status == "failed",
        )
        .returning(AuctionReminder.id)
    )
    return (await session.execute(statement)).scalar_one_or_none()


def _reminder_text(auction: Auction, candidate: ReminderCandidate, bidder: str) -> str:
    sequence = ("一", "二")[candidate.window - 2]
    unit = "CNY" if auction.auction_type == "currency" else "积分"
    deadline = candidate.ends_at.replace(tzinfo=UTC).astimezone(ZoneInfo("Asia/Shanghai"))
    extend = max(3, auction.extend_minutes or 0)
    hammer = max(1, candidate.hammer_interval)
    window = hammer * 3
    rule = f"仅最后 {window} 分钟内有效出价延时，截止时间至少为出价后 {extend} 分钟"
    code = auction.code or f"#{auction.id}"
    return "\n".join([
        f"🔨 <b>拍品当前出价第{sequence}次：</b>",
        "──────────────",
        f"📦 <b>拍品</b>　{escape(auction.item)}",
        f"🔖 <b>编号</b>　{escape(code)}",
        f"📄 <b>拍品描述</b>　{escape(auction.description or '')}",
        f"👤 <b>出价用户</b>　{escape(bidder)}",
        f"💰 <b>当前出价</b>　{auction.current_price} {unit}",
        f"➕ <b>最低加价</b>　{max(1, auction.step)} {unit}",
        f"⏰ <b>截止时间</b>　{deadline:%Y-%m-%d %H:%M:%S}（北京时间）",
        f"📈 <b>延时规则</b>　{rule}",
        "──────────────",
        f"点击下方按钮按最新最低价出价或查询，或发送 /bid {escape(code)} 金额。",
    ])


async def _mark_failure(reminder_id: int, status: Literal["failed", "unknown"], error: str) -> None:
    async with session_scope() as session:
        await session.execute(
            update(AuctionReminder)
            .where(AuctionReminder.id == reminder_id, AuctionReminder.status == "pending")
            .values(status=status, last_error=error[:500])
        )


async def send_auction_reminders(bot: Bot, now: datetime) -> None:
    started = monotonic()
    for scanned in await scan_reminders(now):
        claim_now = now + timedelta(seconds=monotonic() - started)
        if scanned.ends_at <= claim_now:
            continue
        candidate = replace(scanned, window=_current_window(scanned.ends_at, claim_now, scanned.hammer_interval))
        async with session_scope() as session:
            reminder_id = await claim_reminder(session, candidate, claim_now)
            if reminder_id is None:
                continue
            auction = await session.get(Auction, candidate.auction_id)
            assert auction is not None
            bidder = "暂无出价"
            if auction.current_bidder is not None:
                member = await session.scalar(
                    select(Member).where(
                        Member.group_id == auction.group_id,
                        Member.user_id == auction.current_bidder,
                    )
                )
                bidder = str(auction.current_bidder)
                if member is not None:
                    bidder = f"@{member.username}" if member.username else member.first_name or bidder
            text = _reminder_text(auction, candidate, bidder)
            unit = "CNY" if auction.auction_type == "currency" else "积分"
            keyboard = keyboards.auction_kb(auction.id, auction.code or "", unit=unit)

        log.info(
            "Auction reminder claimed id=%s auction=%s deadline=%s window=%s",
            reminder_id, candidate.auction_id, candidate.ends_at, candidate.window,
        )
        try:
            sent = await bot.send_message(auction.group_id, text, reply_markup=keyboard)
        except asyncio.CancelledError:
            log.warning("Auction reminder dispatch interrupted; ownership retained id=%s", reminder_id)
            raise
        except (
            TelegramBadRequest, TelegramForbiddenError, TelegramNotFound,
            TelegramUnauthorizedError, TelegramConflictError, TelegramRetryAfter, TelegramMigrateToChat,
        ) as exc:
            log.warning("Auction reminder rejected id=%s: %s", reminder_id, exc)
            await _mark_failure(reminder_id, "failed", str(exc))
            continue
        except (TelegramAPIError, ClientDecodeError, TimeoutError) as exc:
            log.error("Auction reminder dispatch unknown; suppressing resend id=%s: %s", reminder_id, exc)
            await _mark_failure(reminder_id, "unknown", str(exc))
            continue

        cleanup = asyncio.create_task(helpers.delete_message_safe(
            bot, auction.group_id, sent.message_id, delay=helpers.NOTICE_TTL_SECONDS,
        ))
        helpers.track_task(cleanup)

        try:
            async with session_scope() as session:
                await session.execute(
                    update(AuctionReminder)
                    .where(AuctionReminder.id == reminder_id, AuctionReminder.status == "pending")
                    .values(
                        status="sent", message_id=sent.message_id,
                        sent_at=now + timedelta(seconds=monotonic() - started),
                    )
                )
        except asyncio.CancelledError:
            log.warning("Auction reminder receipt interrupted; ownership retained id=%s", reminder_id)
            raise
        except SQLAlchemyError:
            log.exception("Auction reminder receipt failed; ownership retained id=%s", reminder_id)
            raise
