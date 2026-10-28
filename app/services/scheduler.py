"""Background tasks: verification expiry, draws and packet refunds."""
from __future__ import annotations

import asyncio
import logging
import random
from datetime import timedelta
from html import escape

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from sqlalchemy import func, select, update

from ..db import session_scope
from ..models import (
    Auction,
    Group,
    Lottery,
    LotteryEntry,
    LotteryWinner,
    Member,
    RedPacket,
    RequiredChannel,
    Verification,
    utcnow,
)
from . import repo

log = logging.getLogger(__name__)


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
    try:
        await bot.send_message(chat_id, text[:4000])
    except TelegramAPIError:
        pass


async def _mark_left(chat_id: int, user_id: int) -> None:
    async with session_scope() as session:
        member = await repo.get_member(session, chat_id, user_id)
        if member:
            member.is_member = False
            member.is_verified = False
            member.is_muted = False
            member.muted_until = None


async def _kick_timed_out(
    bot: Bot,
    chat_id: int,
    user_id: int,
    title: str,
) -> bool:
    """Remove a member whose verification expired and verify the outcome.

    ``ban_chat_member`` used to be called without checking the result, so a
    failed ban still produced "已被移出" and the user stayed in the group.
    Returns True when the member is no longer present.
    """
    error = ""
    removed = False
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        status = _member_status(member)
        if status not in _PRESENT_STATUSES:
            # Already left, or an earlier ban already removed them.
            await _mark_left(chat_id, user_id)
            return True
        if status in {"administrator", "creator"}:
            error = "该用户是群管理员，无法自动移出"
        else:
            try:
                await bot.ban_chat_member(chat_id, user_id, revoke_messages=False)
                removed = True
            except TelegramAPIError as exc:
                error = str(exc)
                # The request may have reached Telegram even though the
                # response failed; check before claiming the removal failed.
                try:
                    current = await bot.get_chat_member(chat_id, user_id)
                    removed = _member_status(current) not in _PRESENT_STATUSES
                except TelegramAPIError:
                    removed = False
            if removed:
                try:
                    await bot.unban_chat_member(chat_id, user_id, only_if_banned=True)
                    error = ""
                except TelegramAPIError as exc:
                    error = f"解除封禁失败：{exc}"
                    # One immediate retry: leaving the user banned is worse
                    # than a plain kick.
                    try:
                        await bot.unban_chat_member(chat_id, user_id, only_if_banned=True)
                        error = ""
                    except TelegramAPIError as exc2:
                        error = f"解除封禁失败：{exc2}"
    except TelegramAPIError as exc:
        error = str(exc)

    if removed:
        await _notice(
            bot,
            chat_id,
            f"⏱ {_mention(user_id)} 未在规定时间内完成验证，已被移出 <b>{escape(title)}</b>。",
        )
        if error:
            log.warning("verify timeout: %s removed from %s but %s", user_id, chat_id, error)
            await _notice(
                bot,
                chat_id,
                "⚠️ 解除封禁失败，请管理员在面板成员页手动解封。",
            )
            await _record_kick(
                chat_id, user_id, "verify_timeout_kick_unban_failed", error
            )
        else:
            await _record_kick(chat_id, user_id, "verify_timeout_kick", "验证超时自动移出")
        await _mark_left(chat_id, user_id)
    else:
        log.warning("verify timeout: cannot remove %s from %s: %s", user_id, chat_id, error)
        hint = ""
        lowered = error.lower()
        if "not enough rights" in lowered or "chat_admin_required" in lowered:
            hint = "（机器人缺少“封禁用户”权限）"
        await _notice(
            bot,
            chat_id,
            f"⚠️ {_mention(user_id)} 验证超时，但自动移出失败{hint}，请管理员手动处理。",
        )
        await _record_kick(chat_id, user_id, "verify_timeout_kick_failed", error)
    return removed


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
    expired: list[tuple[int, int, str, int | None, bool]] = []
    async with session_scope() as session:
        now = utcnow()
        pending = (
            await session.scalars(
                select(Verification).where(Verification.status == "pending")
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
                    bool(group.verify_enabled or channels),
                )
            )

    for chat_id, user_id, title, message_id, should_kick in expired:
        if should_kick:
            await _kick_timed_out(bot, chat_id, user_id, title)
        await _delete_verification_message(bot, chat_id, message_id)


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


async def _lottery_due(session, lottery: Lottery, now) -> bool:
    """Return whether a lottery reached its configured draw condition."""
    if (lottery.draw_type or "time").lower() == "participant":
        target = int(lottery.draw_participant_count or 0)
        if target <= 0:
            return False
        entries = int(
            await session.scalar(
                select(func.count(LotteryEntry.id)).where(
                    LotteryEntry.lottery_id == lottery.id
                )
            )
            or 0
        )
        return entries >= target
    return lottery.ends_at is not None and lottery.ends_at <= now


async def _claim_auction(
    session, auction_id: int, current_bidder: int | None, current_price: int
) -> bool:
    """Atomically finish (or cancel) a due auction.

    The bidder/price predicates are part of the CAS so a bid that arrives
    between the scan and the claim makes the claim fail; the next tick then
    settles the auction with the newer price instead of announcing a stale one.
    """
    stmt = update(Auction).where(Auction.id == auction_id, Auction.status == "open")
    if current_bidder is None:
        stmt = stmt.where(Auction.current_bidder.is_(None)).values(status="cancelled")
    else:
        stmt = stmt.where(
            Auction.current_bidder.is_not(None),
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
    messages: list[tuple[int, str]] = []
    async with session_scope() as session:
        now = utcnow()
        lotteries = (
            await session.scalars(
                select(Lottery).where(Lottery.status == "open")
            )
        ).all()
        for lottery in lotteries:
            if not await _lottery_due(session, lottery, now):
                continue
            if not await _claim_lottery(session, lottery.id):
                continue
            entries = (
                await session.scalars(
                    select(LotteryEntry).where(LotteryEntry.lottery_id == lottery.id)
                )
            ).all()
            if entries:
                count = max(1, int(lottery.winners_count or 1))
                winners = random.sample(entries, min(count, len(entries)))
                session.add_all(
                    [
                        LotteryWinner(lottery_id=lottery.id, user_id=winner.user_id)
                        for winner in winners
                    ]
                )
                winner_text = "、".join(_mention(w.user_id) for w in winners)
                contact = (lottery.claim_contact or "").strip()
                claim_line = (
                    f"领奖联系：{escape(contact)}"
                    if contact
                    else "请中奖者联系管理员领奖。"
                )
                text = (
                    f"🎉 <b>抽奖开奖：{lottery.title}</b>\n"
                    f"奖品：{escape(lottery.prize)} × {count}\n"
                    f"参与人数：{len(entries)}\n"
                    f"中奖者：{winner_text}\n"
                    f"{claim_line}"
                )
            else:
                text = f"🎲 抽奖「{lottery.title}」已到期，但无人参与，本轮流奖。"
            await repo.add_log(
                session, lottery.group_id, None, "lottery_draw", None, lottery.title
            )
            messages.append((lottery.group_id, text))
    for chat_id, text in messages:
        try:
            await bot.send_message(chat_id, text)
        except TelegramAPIError:
            continue


async def finish_auctions(bot: Bot) -> None:
    messages: list[tuple[int, str]] = []
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
            if auction.current_bidder is not None:
                text = (
                    f"🔨 <b>竞拍结束：{auction.item}</b>\n"
                    f"成交价：{auction.current_price} 积分\n"
                    f"得主：{_mention(auction.current_bidder)}\n"
                    "积分已在出价时扣除，请联系管理员领取拍品。"
                )
            else:
                text = f"🔨 竞拍「{auction.item}」已到期，无人出价，已流拍。"
            await repo.add_log(
                session, auction.group_id, None, "auction_finish", auction.current_bidder, auction.item
            )
            messages.append((auction.group_id, text))
    for chat_id, text in messages:
        try:
            await bot.send_message(chat_id, text)
        except TelegramAPIError:
            continue


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


async def tick(bot: Bot) -> None:
    await expire_verifications(bot)
    await draw_lotteries(bot)
    await finish_auctions(bot)
    await refund_red_packets(bot)
    await cleanup_members()


async def run_scheduler(bot: Bot, interval: int = 20) -> None:
    log.info("Scheduler started (interval=%ss)", interval)
    while True:
        try:
            await tick(bot)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Scheduler tick failed")
        await asyncio.sleep(interval)
