"""Background tasks: verification expiry, draws and packet refunds."""
from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from html import escape

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from sqlalchemy import and_, func, or_, select, update

from ..db import session_scope
from ..bot import helpers
from ..models import (
    Auction,
    Group,
    Lottery,
    PendingAction,
    LotteryEntry,
    LotteryWinner,
    Member,
    RedPacket,
    RequiredChannel,
    Verification,
    utcnow,
)
from . import auction_reminders, repo
from .auction_logic import entry_fee as _auction_entry_fee, price_label as _auction_price_label

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _SettlementNotice:
    group_id: int
    text: str




async def _enqueue_action(
    session,
    action_type: str,
    group_id: int,
    user_id: int | None = None,
    target_id: int | None = None,
    payload: dict | None = None,
) -> None:
    """Queue an action for retry."""
    import json
    session.add(
        PendingAction(
            action_type=action_type,
            group_id=group_id,
            user_id=user_id,
            target_id=target_id,
            payload=json.dumps(payload or {}, ensure_ascii=False),
            attempts=0,
            created_at=utcnow(),
        )
    )

async def _deliver_settlement_notices(bot: Bot, notices: Sequence[_SettlementNotice]) -> None:
    for notice in notices:
        try:
            sent = await bot.send_message(notice.group_id, notice.text)
        except TelegramAPIError as exc:
            log.warning(
                "settlement notice failed group=%s error=%s", notice.group_id, type(exc).__name__
            )
            async with session_scope() as session:
                await _enqueue_action(
                    session,
                    "settlement_notice",
                    notice.group_id,
                    payload={"text": notice.text},
                )
            continue
        await helpers.pin_message_safe(bot, notice.group_id, sent.message_id)


async def _member_mention(session, group_id: int, user_id: int) -> str:
    member = await session.scalar(
        select(Member).where(Member.group_id == group_id, Member.user_id == user_id)
    )
    if member and getattr(member, "username", ""):
        return f"@{member.username}"
    if member and getattr(member, "first_name", ""):
        return member.first_name[:64]
    return str(user_id)


async def _member_mentions(session, group_id: int, user_ids: Sequence[int]) -> dict[int, str]:
    members = (
        await session.scalars(
            select(Member).where(Member.group_id == group_id, Member.user_id.in_(set(user_ids)))
        )
    ).all()
    found = {int(member.user_id): member for member in members}
    mentions: dict[int, str] = {}
    for user_id in user_ids:
        member = found.get(int(user_id))
        if member and member.username:
            mentions[user_id] = f"@{member.username}"
        elif member and member.first_name:
            mentions[user_id] = member.first_name[:64]
        else:
            mentions[user_id] = str(user_id)
    return mentions


def _mention(user_id: int, name: str = "") -> str:
    label = name or f"用户 {user_id}"
    return f'<a href="tg://user?id={user_id}">{label}</a>'


_PRESENT_STATUSES = {"member", "restricted", "administrator", "creator"}


def _member_status(member) -> str:
    return getattr(member.status, "value", str(member.status))


async def _record_kick(chat_id: int, user_id: int, action: str, detail: str) -> None:
    async with session_scope() as session:
        await repo.add_log(session, chat_id, None, action, user_id, detail[:500])


async def _notice(bot: Bot, chat_id: int, text: str) -> None:
    await helpers.send_temp_notice(bot, chat_id, text[:4000])


async def _mark_left(chat_id: int, user_id: int, *, banned: bool) -> None:
    async with session_scope() as session:
        member = await repo.get_member(session, chat_id, user_id)
        if member:
            member.is_member = False
            member.is_verified = False
            member.verification_restricted = False
            member.is_banned = banned


async def _kick_timed_out(
    bot: Bot,
    chat_id: int,
    user_id: int,
    title: str,
) -> bool:
    try:
        current = await bot.get_chat_member(chat_id, user_id)
        status = _member_status(current)
        if status in {"administrator", "creator"}:
            await _record_kick(chat_id, user_id, "verify_timeout_kick_skipped", status)
            return False
        if status not in _PRESENT_STATUSES or (
            status == "restricted" and not getattr(current, "is_member", True)
        ):
            await _mark_left(chat_id, user_id, banned=status == "kicked")
            await _record_kick(chat_id, user_id, "verify_timeout_kick_skipped", status)
            return False
        unban_error: TelegramAPIError | None = None
        async with session_scope() as session:
            member = await repo.get_member(session, chat_id, user_id)
            if member is not None and member.is_banned:
                await repo.add_log(
                    session, chat_id, None, "verify_timeout_kick_skipped", user_id,
                    "已有封禁记录，保留管理员封禁",
                )
                return False
            await bot.ban_chat_member(chat_id, user_id, revoke_messages=False)
            if member is not None:
                member.is_member = False
                member.is_verified = False
                member.verification_restricted = False
                member.is_banned = True
            try:
                await bot.unban_chat_member(chat_id, user_id, only_if_banned=True)
            except TelegramAPIError as exc:
                unban_error = exc
            else:
                if member is not None:
                    member.is_banned = False
    except TelegramAPIError as exc:
        log.warning("verification timeout kick failed group=%s user=%s: %s", chat_id, user_id, exc)
        await _record_kick(chat_id, user_id, "verify_timeout_kick_failed", str(exc))
        async with session_scope() as session:
            await _enqueue_action(
                session,
                "kick_timeout",
                chat_id,
                user_id=user_id,
                payload={"title": title},
            )
        await _notice(bot, chat_id, f"⚠️ {user_id!s} 验证超时，处理失败，已加入重试队列。")
        return False
    if unban_error is not None:
        log.warning("verification timeout unban failed group=%s user=%s: %s", chat_id, user_id, unban_error)
        await _record_kick(chat_id, user_id, "verify_timeout_unban_failed", str(unban_error))
        await _notice(bot, chat_id, f"⚠️ {user_id!s} 验证超时，已封禁但解封失败，暂时无法重新入群，请管理员手动解封。")
        return False
    await _record_kick(chat_id, user_id, "verify_timeout_kick", "验证超时移出，可重新入群验证")
    await _notice(bot, chat_id, f"⏱ {user_id!s} 验证超时，已移出群组，可重新入群验证。")
    return True


async def _delete_verification_message(
    bot: Bot, chat_id: int, message_id: int | None
) -> None:
    if not message_id:
        return
    try:
        await bot.delete_message(chat_id, message_id)
    except TelegramAPIError as exc:
        log.debug(
            "verification prompt delete failed chat=%s message=%s: %s",
            chat_id,
            message_id,
            exc,
        )


async def expire_verifications(bot: Bot) -> None:
    expired: list[tuple[int, int, str, int | None, int, bool]] = []
    async with session_scope() as session:
        now = utcnow()
        active_group_ids = select(Group.id).where(Group.is_active.is_(True))
        pending = (
            await session.scalars(
                select(Verification).where(
                    Verification.status == "pending",
                    or_(
                        Verification.created_at <= now - timedelta(seconds=60),
                        Verification.group_id.not_in(active_group_ids),
                    ),
                )
            )
        ).all()
        group_ids = {int(verification.group_id) for verification in pending}
        groups = {
            int(group.id): group
            for group in (
                await session.scalars(select(Group).where(Group.id.in_(group_ids)))
            ).all()
        }
        channel_counts = {
            int(group_id): int(count)
            for group_id, count in (
                await session.execute(
                    select(RequiredChannel.group_id, func.count(RequiredChannel.id))
                    .where(RequiredChannel.group_id.in_(group_ids))
                    .group_by(RequiredChannel.group_id)
                )
            ).all()
        }
        user_ids = {int(verification.user_id) for verification in pending}
        members = {
            (int(member.group_id), int(member.user_id)): member
            for member in (
                await session.scalars(
                    select(Member).where(
                        Member.group_id.in_(group_ids),
                        Member.user_id.in_(user_ids),
                    )
                )
            ).all()
        }
        for verification in pending:
            group = groups.get(int(verification.group_id))
            if group is None or not group.is_active:
                verification.status = "expired"
                expired.append(
                    (
                        verification.group_id,
                        verification.user_id,
                        str(verification.group_id),
                        verification.message_id,
                        verification.message_chat_id or verification.group_id,
                        False,
                    )
                )
                continue
            timeout = max(60, int(group.verify_timeout or 300))
            if verification.created_at + timedelta(seconds=timeout) > now:
                continue
            channels = channel_counts.get(int(group.id), 0)
            verification.status = "timeout"
            member = members.get((int(group.id), int(verification.user_id)))
            if member is not None:
                member.is_verified = False
            await repo.add_log(
                session,
                group.id,
                None,
                "verify_timeout",
                verification.user_id,
                "验证超时",
            )
            expired.append(
                (
                    group.id,
                    verification.user_id,
                    group.title or str(group.id),
                    verification.message_id,
                    verification.message_chat_id or group.id,
                    bool(group.verify_enabled or channels or group.require_username),
                )
            )

    for chat_id, user_id, title, message_id, message_chat_id, should_kick in expired:
        if should_kick:
            await _kick_timed_out(bot, chat_id, user_id, title)
        await _delete_verification_message(bot, message_chat_id, message_id)


async def _claim_lottery(session, lottery_id: int) -> bool:
    """Atomically take ownership of a due lottery.

    Only one transaction can flip ``open`` to ``drawn`` and get a RETURNING
    row; the loser sees no row and must skip all side effects.
    """
    stmt = (
        update(Lottery)
        .where(Lottery.id == lottery_id, Lottery.status == "open")
        .values(status="drawn")
        .returning(Lottery.id)
        .execution_options(synchronize_session=False)
    )
    row = (await session.execute(stmt)).first()
    return row is not None


async def _claim_auction(
    session, auction_id: int, current_bidder: int | None, current_price: int
) -> bool:
    """Atomically finish (or cancel) a due auction.

    The bidder/price predicates are part of the CAS so a bid that arrives
    between the scan and the claim makes the claim fail; the next tick then
    settles the auction with the newer price instead of announcing a stale one.
    """
    stmt = update(Auction).where(
        Auction.id == auction_id, Auction.status == "open", Auction.ends_at <= utcnow(),
    )
    if current_bidder is None:
        stmt = stmt.where(Auction.current_bidder.is_(None)).values(status="cancelled")
    else:
        stmt = stmt.where(
            Auction.current_bidder == current_bidder,
            Auction.current_price == current_price,
        ).values(status="finished")
    stmt = stmt.returning(Auction.id).execution_options(synchronize_session=False)
    row = (await session.execute(stmt)).first()
    return row is not None


async def _claim_red_packet(session, packet_id: int) -> int | None:
    """Atomically expire a due red packet and return its refundable remainder.

    ``None`` means another worker already settled it; callers must not refund
    or announce anything in that case.
    """
    stmt = (
        update(RedPacket)
        .where(RedPacket.id == packet_id, RedPacket.status == "open")
        .values(status="expired")
        .returning(RedPacket.remaining_amount)
        .execution_options(synchronize_session=False)
    )
    row = (await session.execute(stmt)).first()
    if row is None:
        return None
    return int(row[0] or 0)


async def draw_lotteries(bot: Bot) -> None:
    notices: list[_SettlementNotice] = []
    async with session_scope() as session:
        now = utcnow()
        is_participant = func.lower(func.coalesce(Lottery.draw_type, "time")) == "participant"
        lotteries = (
            await session.scalars(
                select(Lottery).where(
                    Lottery.status == "open",
                    or_(
                        is_participant,
                        and_(Lottery.ends_at.is_not(None), Lottery.ends_at <= now),
                    ),
                )
            )
        ).all()
        participant_lotteries = {
            lottery.id: lottery
            for lottery in lotteries
            if (lottery.draw_type or "time").lower() == "participant"
            and int(lottery.draw_participant_count or 0) > 0
        }
        participant_counts: dict[int, int] = {}
        if participant_lotteries:
            participant_counts = {
                int(lottery_id): int(count)
                for lottery_id, count in (
                    await session.execute(
                        select(LotteryEntry.lottery_id, func.count(LotteryEntry.id))
                        .where(LotteryEntry.lottery_id.in_(participant_lotteries))
                        .group_by(LotteryEntry.lottery_id)
                    )
                ).all()
            }
        for lottery in lotteries:
            if (lottery.draw_type or "time").lower() == "participant":
                target = int(lottery.draw_participant_count or 0)
                due = target > 0 and participant_counts.get(lottery.id, 0) >= target
            else:
                due = lottery.ends_at is not None and lottery.ends_at <= now
            if not due:
                continue
            if not await _claim_lottery(session, lottery.id):
                continue
            entries = (
                await session.scalars(
                    select(LotteryEntry.user_id).where(LotteryEntry.lottery_id == lottery.id)
                )
            ).all()
            if entries:
                count = max(1, int(lottery.winners_count or 1))
                winners = random.sample(list(entries), min(count, len(entries)))
                session.add_all(
                    [
                        LotteryWinner(lottery_id=lottery.id, user_id=winner)
                        for winner in winners
                    ]
                )
                mentions = await _member_mentions(session, lottery.group_id, winners)
                winner_text = "、".join(mentions[winner] for winner in winners)
                contact = (lottery.claim_contact or "").strip()
                claim_line = (
                    f"领奖联系：{escape(contact)}"
                    if contact
                    else "请中奖者联系管理员领奖。"
                )
                text = (
                    f"🎉 <b>抽奖开奖：{escape(lottery.title)}</b>\n"
                    f"奖品：{escape(lottery.prize)} × {count}\n"
                    f"参与人数：{len(entries)}\n"
                    f"中奖者：{winner_text}\n"
                    f"{claim_line}"
                )
            else:
                text = f"🎲 抽奖「{escape(lottery.title)}」已到期，但无人参与，本轮流奖。"
            await repo.add_log(
                session, lottery.group_id, None, "lottery_draw", None, lottery.title
            )
            notices.append(_SettlementNotice(lottery.group_id, text))
    await _deliver_settlement_notices(bot, notices)


async def _send_auction_reminders(bot: Bot, now: datetime) -> None:
    await auction_reminders.send_auction_reminders(bot, now)


async def finish_auctions(bot: Bot) -> None:
    notices: list[_SettlementNotice] = []
    async with session_scope() as session:
        now = utcnow()
        auctions = (
            await session.scalars(
                select(Auction).where(
                    Auction.status == "open",
                    Auction.ends_at.is_not(None),
                    Auction.ends_at <= now,
                )
            )
        ).all()
        for auction in auctions:
            if not await _claim_auction(
                session, auction.id, auction.current_bidder, auction.current_price
            ):
                continue
            is_currency = getattr(auction, "auction_type", "points") == "currency"
            price_label = _auction_price_label(auction)
            desc = escape(auction.description or "")
            desc_line = f"📄 <b>拍品描述</b>　{desc}" if desc else "📄 <b>拍品描述</b>　"
            if auction.current_bidder is not None:
                mentions = await _member_mentions(
                    session, auction.group_id, [auction.current_bidder, auction.creator_id]
                )
                winner_text = mentions[auction.current_bidder]
                creator_text = mentions[auction.creator_id]
                lines = [
                    "🔨 <b>竞拍交割完成：</b>",
                    "第三次落槌，竞拍结束。",
                    "──────────────",
                    f"📦 <b>拍品</b>　{escape(auction.item)}",
                    f"🔖 <b>编号</b>　{escape(auction.code or f'#{auction.id}')}",
                    desc_line,
                    f"👤 <b>竞拍发起者</b>　{creator_text}",
                    f"🏆 <b>竞拍获得者</b>　{winner_text}",
                    f"💰 <b>成交价</b>　{auction.current_price} {price_label}",
                    f"🗓️ <b>交割时间</b>　{now.strftime('%Y/%m/%d %H:%M:%S')}",
                ]
                if is_currency:
                    entry_fee = _auction_entry_fee(auction)
                    await repo.adjust_points(
                        session, auction.group_id, auction.current_bidder, entry_fee,
                        f"货币竞拍入场费返还：{auction.item}",
                    )
            else:
                lines = [
                    "🔨 <b>竞拍交割完成：</b>",
                    "第三次落槌，竞拍结束。",
                    "──────────────",
                    f"📦 <b>拍品</b>　{escape(auction.item)}",
                    f"🔖 <b>编号</b>　{escape(auction.code or f'#{auction.id}')}",
                    desc_line,
                    f"🗓️ <b>交割时间</b>　{now.strftime('%Y/%m/%d %H:%M:%S')}",
                    "无人出价，已流拍。",
                ]
            await repo.add_log(
                session, auction.group_id, None, "auction_finish", auction.current_bidder, auction.item
            )
            notices.append(_SettlementNotice(auction.group_id, "\n".join(lines)))
    await _deliver_settlement_notices(bot, notices)


async def refund_red_packets(bot: Bot) -> None:
    messages: list[tuple[int, str]] = []
    async with session_scope() as session:
        now = utcnow()
        packets = (
            await session.scalars(
                select(RedPacket).where(
                    RedPacket.status == "open",
                    RedPacket.expires_at.is_not(None),
                    RedPacket.expires_at <= now,
                )
            )
        ).all()
        for packet in packets:
            remaining = await _claim_red_packet(session, packet.id)
            if remaining is None:
                continue
            if remaining > 0:
                await repo.adjust_points(
                    session,
                    packet.group_id,
                    packet.sender_id,
                    remaining,
                    f"红包过期退回 #{packet.id}",
                )
            await repo.add_log(
                session,
                packet.group_id,
                None,
                "redpacket_expire",
                packet.sender_id,
                f"退回 {remaining}",
            )
            group = await session.get(Group, packet.group_id)
            unit = (group.points_name if group else "积分") or "积分"
            messages.append(
                (
                    packet.group_id,
                    f"⌛ 红包 #{packet.id} 已过期，未领取的 {remaining} {unit}已退回发送者。",
                )
            )
    for chat_id, text in messages:
        try:
            await bot.send_message(chat_id, text)
        except TelegramAPIError:
            continue


async def cleanup_members() -> None:
    async with session_scope() as session:
        await repo.expire_warnings(session)
        await session.execute(
            update(Member)
            .where(
                Member.is_muted.is_(True),
                Member.muted_until.is_not(None),
                Member.muted_until <= utcnow(),
            )
            .values(is_muted=False, muted_until=None)
            .execution_options(synchronize_session=False)
        )




async def retry_pending_actions(bot: Bot) -> None:
    """Retry pending actions with exponential backoff."""
    import json
    async with session_scope() as session:
        now = utcnow()
        # Retry actions with <5 attempts and created/last attempted >5min ago
        retry_threshold = now - timedelta(minutes=5)
        pending = (
            await session.scalars(
                select(PendingAction)
                .where(
                    PendingAction.attempts < 5,
                    or_(
                        and_(
                            PendingAction.last_attempt_at.is_(None),
                            PendingAction.created_at <= retry_threshold,
                        ),
                        PendingAction.last_attempt_at <= retry_threshold,
                    ),
                )
                .limit(20)
            )
        ).all()

        for action in pending:
            action.attempts += 1
            action.last_attempt_at = now
            success = False
            error_msg = ""

            try:
                payload = json.loads(action.payload) if action.payload else {}

                if action.action_type == "kick_timeout":
                    title = payload.get("title", "")
                    success = await _kick_timed_out(bot, action.group_id, action.user_id, title)

                elif action.action_type == "settlement_notice":
                    text = payload.get("text", "")
                    if text:
                        sent = await bot.send_message(action.group_id, text)
                        await helpers.pin_message_safe(bot, action.group_id, sent.message_id)
                        success = True

            except TelegramAPIError as exc:
                error_msg = f"{type(exc).__name__}: {exc}"
                log.warning(
                    "retry failed type=%s group=%s attempts=%s: %s",
                    action.action_type,
                    action.group_id,
                    action.attempts,
                    error_msg,
                )
            except Exception as exc:
                error_msg = f"unexpected: {exc}"
                log.exception("retry unexpected error type=%s group=%s", action.action_type, action.group_id)

            if success:
                await session.delete(action)
            else:
                action.last_error = error_msg[:500]

        # Clean up old/exhausted actions
        old_threshold = now - timedelta(days=7)
        failed_threshold = now - timedelta(hours=24)
        for old_action in (
            await session.scalars(
                select(PendingAction).where(
                    or_(
                        PendingAction.created_at <= old_threshold,
                        and_(
                            PendingAction.attempts >= 5,
                            PendingAction.last_attempt_at <= failed_threshold,
                        ),
                    )
                )
            )
        ).all():
            await session.delete(old_action)

def _stages(bot: Bot) -> tuple[tuple[str, Callable[[], Awaitable[None]]], ...]:
    return (
        ("verifications", lambda: expire_verifications(bot)),
        ("lotteries", lambda: draw_lotteries(bot)),
        ("auction_reminders", lambda: _send_auction_reminders(bot, utcnow())),
        ("auctions", lambda: finish_auctions(bot)),
        ("red_packets", lambda: refund_red_packets(bot)),
        ("retry_actions", lambda: retry_pending_actions(bot)),
        ("members", cleanup_members),
    )


async def tick(bot: Bot) -> None:
    for _name, stage in _stages(bot):
        await stage()


async def run_scheduler(bot: Bot, interval: int = 20) -> None:
    log.info("Scheduler started (interval=%ss)", interval)
    while True:
        # Each stage has its own failure boundary so one failing job cannot
        # starve the jobs scheduled after it.
        for name, stage in _stages(bot):
            try:
                await stage()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Scheduler stage failed: %s", name)
        await asyncio.sleep(interval)
