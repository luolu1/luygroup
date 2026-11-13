"""Red packet handlers."""

from __future__ import annotations

import random
from aiogram import Bot, F
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message
from datetime import timedelta
from html import escape
from sqlalchemy import select
from .games_shared import _claim_packet_slot, _feature_denied, _group_for_message, _need_group, _reply_feature_denied, router
from .. import helpers, keyboards
from ...db import session_scope
from ...models import RedPacket, RedPacketClaim, utcnow
from ...services import repo


# Red packets
# ---------------------------------------------------------------------------
@router.message(Command("redpacket", "rp", "hongbao"))
async def cmd_redpacket(message: Message, bot: Bot) -> None:
    if not _need_group(message) or not message.from_user:
        await message.reply("请在群组中使用。")
        return
    args = helpers.parse_args(message.text)
    if len(args) < 2 or not args[0].isdigit() or not args[1].isdigit():
        await message.reply("用法：/redpacket 总积分 个数 [备注]")
        return
    total, count = int(args[0]), int(args[1])
    note = " ".join(args[2:])[:200]
    if total <= 0 or count <= 0 or count > 100 or total < count:
        await message.reply("参数不合法：总积分需大于 0，个数 1-100，且每个红包至少 1 积分。")
        return
    async with session_scope() as session:
        group = await _group_for_message(session, message)
        if await _feature_denied(session, group, "points"):
            await _reply_feature_denied(message, "points")
            return
        if not group.points_enabled:
            await message.reply("本群暂未开启积分系统。")
            return
        result = await repo.adjust_points(
            session,
            group.id,
            message.from_user.id,
            -total,
            "发积分红包",
            username=message.from_user.username or "",
            first_name=message.from_user.first_name or "",
        )
        if result is None:
            await message.reply("积分不足，无法发红包。")
            return
        _member, balance = result
        packet = RedPacket(
            group_id=group.id,
            sender_id=message.from_user.id,
            total=total,
            count=count,
            remaining_amount=total,
            remaining_count=count,
            note=note,
            status="open",
            expires_at=utcnow() + timedelta(hours=24),
        )
        session.add(packet)
        await session.flush()
        text = (
            f"🧧 <b>{message.from_user.mention_html()}</b> 发了一个积分红包！\n"
            f"总额 <b>{total}</b> {escape(group.points_name or '积分')}，"
            f"共 <b>{count}</b> 个，先到先得。\n"
            + (f"💬 {escape(note)}\n" if note else "")
            + f"你的余额：{balance}"
        )
        try:
            sent = await bot.send_message(
                group.id, text, reply_markup=keyboards.redpacket_kb(packet.id)
            )
            packet.message_id = sent.message_id
        except TelegramAPIError as exc:
            await session.rollback()
            await message.reply(f"发送红包失败：{exc}")
            return
    await message.reply("红包已发出！")


@router.callback_query(F.data.startswith("rp:"))
async def on_claim_packet(callback: CallbackQuery) -> None:
    if callback.message is None:
        await callback.answer("消息已失效。", show_alert=True)
        return
    try:
        packet_id = int((callback.data or "").split(":", 1)[1])
    except (IndexError, ValueError):
        await callback.answer("红包编号无效。", show_alert=True)
        return
    group_id = callback.message.chat.id
    async with session_scope() as session:
        packet = await session.get(RedPacket, packet_id)
        if packet is None or packet.group_id != group_id:
            await callback.answer("红包不存在。", show_alert=True)
            return
        if packet.sender_id == callback.from_user.id:
            await callback.answer("不能领取自己发的红包。", show_alert=True)
            return
        now = utcnow()
        if packet.status != "open" or (packet.expires_at and packet.expires_at <= now):
            await callback.answer("红包已过期或已领完。", show_alert=True)
            return
        group = await repo.ensure_group(session, group_id)
        if await _feature_denied(session, group, "points"):
            await callback.answer("本群所属订阅未开通积分玩法。", show_alert=True)
            return
        if not group.points_enabled:
            await callback.answer("本群未开启积分系统。", show_alert=True)
            return
        claim = await session.scalar(
            select(RedPacketClaim).where(
                RedPacketClaim.red_packet_id == packet.id,
                RedPacketClaim.user_id == callback.from_user.id,
            )
        )
        if claim:
            await callback.answer(f"你已经领过 {claim.amount} 积分了。", show_alert=True)
            return

        remaining_count = int(packet.remaining_count or 0)
        remaining_amount = int(packet.remaining_amount or 0)
        if remaining_count <= 0 or remaining_amount <= 0:
            await callback.answer("红包已领完。", show_alert=True)
            return
        if remaining_count == 1:
            amount = remaining_amount
        else:
            safe_max = max(1, remaining_amount - (remaining_count - 1))
            average = max(1, remaining_amount // remaining_count)
            amount = min(safe_max, random.randint(1, max(1, average * 2)))
        if not await _claim_packet_slot(
            session, packet.id, remaining_count, remaining_amount, amount, now
        ):
            await session.rollback()
            await callback.answer("手慢了，红包已被抢完或已过期。", show_alert=True)
            return
        await repo.adjust_points(
            session,
            group_id,
            callback.from_user.id,
            amount,
            f"领取红包 #{packet.id}",
            username=callback.from_user.username or "",
            first_name=callback.from_user.first_name or "",
        )
        session.add(
            RedPacketClaim(
                red_packet_id=packet.id,
                user_id=callback.from_user.id,
                amount=amount,
            )
        )
        finished = remaining_count - 1 <= 0 or remaining_amount - amount <= 0
    status_text = "🎉 最后一个红包！" if finished else "继续抢"
    await callback.answer(f"抢到 {amount} 积分！{status_text}", show_alert=True)
    try:
        if callback.message:
            await callback.message.edit_reply_markup(
                reply_markup=None if finished else keyboards.redpacket_kb(packet_id)
            )
    except TelegramAPIError:
        pass


# ---------------------------------------------------------------------------

