"""Points, shop, red packets, lotteries and auctions."""
from __future__ import annotations

import asyncio
import random
from datetime import datetime, timedelta
from html import escape

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message
from sqlalchemy import func, or_, select

from ...db import session_scope
from ...entitlements import group_feature_allowed
from ...models import (
    Auction,
    AuctionBid,
    Lottery,
    LotteryEntry,
    Member,
    PointsTransaction,
    RedPacket,
    RedPacketClaim,
    ShopItem,
    ShopOrder,
    UserNotificationPreference,
    utcnow,
)
from ...services import repo
from .. import helpers, keyboards

router = Router(name="games")

# Keep strong references to fire-and-forget promotion pushes.  The event loop
# only holds weak references to tasks, so an unreferenced task can be collected
# before its network sends finish.
_promotion_tasks: set[asyncio.Task] = set()


def _tail(message: Message) -> str:
    text = message.text or ""
    parts = text.split(maxsplit=1)
    return parts[1].strip() if len(parts) > 1 else ""


def _parse_auction_args(raw: str) -> tuple[str, int, int, int]:
    """Parse ``title | start_price [step] [minutes]``."""
    if "|" not in raw:
        return "", 0, 0, 60
    title, rest = raw.split("|", 1)
    title = title.strip()
    chunks = rest.strip().split()
    if not title or not chunks or not chunks[0].isdigit():
        return "", 0, 0, 60
    start_price = int(chunks[0])
    step = int(chunks[1]) if len(chunks) > 1 and chunks[1].isdigit() else 0
    minutes = 60
    if len(chunks) > 2 and chunks[2].isdigit():
        minutes = max(1, min(int(chunks[2]), 60 * 24 * 30))
    return title, start_price, max(0, step), minutes


def _parse_game_args(raw: str) -> tuple[str, str, int, int]:
    """Parse ``title | prize [fee] [minutes]``."""
    if "|" not in raw:
        return "", "", 0, 60
    title, rest = raw.split("|", 1)
    title = title.strip()
    chunks = rest.strip().split()
    minutes = 60
    fee = 0
    if chunks and chunks[-1].lstrip("-").isdigit():
        minutes = max(1, min(int(chunks.pop()), 60 * 24 * 30))
    if chunks and chunks[-1].lstrip("-").isdigit():
        fee = max(0, int(chunks.pop()))
    return title, " ".join(chunks).strip(), fee, minutes


async def _group_for_message(session, message: Message):
    return await repo.ensure_group(
        session,
        message.chat.id,
        title=message.chat.title or "",
        username=message.chat.username or "",
        chat_type=message.chat.type,
    )


def _need_group(message: Message) -> bool:
    return message.chat.type in {"group", "supergroup"}


async def _feature_denied(session, group, feature: str) -> bool:
    """Return True when a bound group's plan lacks a feature."""
    return not await group_feature_allowed(session, group.id, feature)


async def _reply_feature_denied(message: Message, feature: str) -> None:
    label = "积分玩法" if feature == "points" else "抽奖与竞拍"
    await message.reply(f"⛔ 本群所属订阅未开通{label}，请联系群主升级订阅。")


async def _send_promotion(
    bot: Bot,
    *,
    group_id: int,
    group_title: str,
    kind: str,
    title: str,
    detail: str,
    exclude_user_id: int | None = None,
) -> None:
    """Push a new lottery/auction to members who enabled private notifications."""
    enabled = (
        UserNotificationPreference.lottery_enabled
        if kind == "lottery"
        else UserNotificationPreference.auction_enabled
    )
    async with session_scope() as session:
        result = await session.scalars(
            select(UserNotificationPreference.telegram_id)
            .join(Member, Member.user_id == UserNotificationPreference.telegram_id)
            .where(
                Member.group_id == int(group_id),
                Member.is_member.is_(True),
                enabled.is_(True),
            )
            .limit(300)
        )
        recipients = [int(item) for item in result.all()]
    if exclude_user_id is not None:
        recipients = [item for item in recipients if item != int(exclude_user_id)]
    if not recipients:
        return
    heading = "🎁 抽奖" if kind == "lottery" else "🔨 竞拍"
    title_line = f" · {escape(title)}" if title else ""
    text = (
        f"<b>{heading}{title_line} · {escape(group_title or '群组')}</b>\n\n"
        f"{detail}\n\n"
        "请回到群组参与。不想收到此类推送可私聊机器人发送 "
        f"/{'cjtz' if kind == 'lottery' else 'jptz'} off。"
    )
    semaphore = asyncio.Semaphore(8)

    async def _deliver(user_id: int) -> None:
        async with semaphore:
            try:
                await bot.send_message(
                    user_id, text, disable_web_page_preview=True
                )
            except TelegramAPIError:
                pass

    await asyncio.gather(*(_deliver(user_id) for user_id in recipients))


def _schedule_promotion(
    bot: Bot,
    *,
    group_id: int,
    group_title: str,
    kind: str,
    title: str,
    detail: str,
    exclude_user_id: int | None = None,
) -> None:
    task = asyncio.create_task(
        _send_promotion(
            bot,
            group_id=group_id,
            group_title=group_title,
            kind=kind,
            title=title,
            detail=detail,
            exclude_user_id=exclude_user_id,
        )
    )
    _promotion_tasks.add(task)
    task.add_done_callback(_promotion_tasks.discard)


# ---------------------------------------------------------------------------
# Points
# ---------------------------------------------------------------------------
@router.message(Command("points", "score", "balance", "jf"))
async def cmd_points(message: Message) -> None:
    if not _need_group(message) or not message.from_user:
        await message.reply("请在群组中使用。")
        return
    async with session_scope() as session:
        group = await _group_for_message(session, message)
        if await _feature_denied(session, group, "points"):
            await _reply_feature_denied(message, "points")
            return
        member = await repo.ensure_member(
            session,
            group.id,
            message.from_user.id,
            username=message.from_user.username or "",
            first_name=message.from_user.first_name or "",
        )
        units = group.points_name or "积分"
        if not group.points_enabled:
            await message.reply(f"本群暂未开启{units}系统。")
            return
        balance = int(member.points or 0)
        above = int(
            await session.scalar(
                select(func.count(Member.id)).where(
                    Member.group_id == group.id, Member.points > balance
                )
            )
            or 0
        )
        total = int(
            await session.scalar(
                select(func.count(Member.id)).where(Member.group_id == group.id)
            )
            or 0
        )
    await message.reply(
        f"⭐ <b>{escape(message.from_user.first_name or '你')}</b>\n"
        f"{units}余额：<b>{balance}</b>\n"
        f"排名：第 {above + 1} / {total} 名\n\n"
        "发送 /checkin 每日签到，/shop 进入积分商城。"
    )


@router.message(Command("checkin", "sign"))
async def cmd_checkin(message: Message) -> None:
    if not _need_group(message) or not message.from_user:
        await message.reply("请在群组中使用。")
        return
    async with session_scope() as session:
        group = await _group_for_message(session, message)
        if await _feature_denied(session, group, "points"):
            await _reply_feature_denied(message, "points")
            return
        if not group.points_enabled:
            await message.reply("本群暂未开启积分系统。")
            return
        now = utcnow()
        start = datetime(now.year, now.month, now.day)
        exists = await session.scalar(
            select(PointsTransaction.id).where(
                PointsTransaction.group_id == group.id,
                PointsTransaction.user_id == message.from_user.id,
                PointsTransaction.reason == "每日签到",
                PointsTransaction.created_at >= start,
            )
        )
        if exists:
            await message.reply("今天已经签到过了，明天再来吧～")
            return
        bonus = max(1, int(group.daily_bonus or 1))
        result = await repo.adjust_points(
            session,
            group.id,
            message.from_user.id,
            bonus,
            "每日签到",
            username=message.from_user.username or "",
            first_name=message.from_user.first_name or "",
        )
        if result is None:
            await message.reply("签到失败，请稍后再试。")
            return
        _member, balance = result
    await message.reply(
        f"✅ 签到成功，获得 <b>{bonus}</b> {escape(group.points_name or '积分')}！\n"
        f"当前余额：<b>{balance}</b>"
    )


# ---------------------------------------------------------------------------
# Shop
# ---------------------------------------------------------------------------
@router.message(Command("shop", "mall"))
async def cmd_shop(message: Message) -> None:
    if not _need_group(message):
        await message.reply("请在群组中使用。")
        return
    async with session_scope() as session:
        group = await _group_for_message(session, message)
        if await _feature_denied(session, group, "points"):
            await _reply_feature_denied(message, "points")
            return
        if not group.points_enabled:
            await message.reply("本群暂未开启积分商城。")
            return
        items = (
            await session.scalars(
                select(ShopItem)
                .where(
                    ShopItem.enabled.is_(True),
                    or_(ShopItem.group_id == group.id, ShopItem.group_id.is_(None)),
                )
                .order_by(ShopItem.cost.asc(), ShopItem.id.asc())
                .limit(20)
            )
        ).all()
    if not items:
        await message.reply("商城暂时没有上架商品，管理员可在 Web 面板添加。")
        return
    lines = [f"🛒 <b>{escape(group.title or '本群')} 积分商城</b>"]
    for item in items:
        stock = "不限" if item.stock < 0 else f"剩 {item.stock}"
        lines.append(
            f"· <code>{item.id}</code> <b>{escape(item.name)}</b> · {item.cost} 积分 · {stock}\n"
            f"  {escape(item.description or '')}"
        )
    lines.append("\n点击下方按钮或发送 /buy 编号 购买。")
    await message.reply(
        "\n".join(lines),
        reply_markup=keyboards.shop_kb([(i.id, i.name, i.cost) for i in items]),
    )


async def _purchase(
    session,
    group,
    user,
    item_id: int,
) -> str:
    if await _feature_denied(session, group, "points"):
        return "本群所属订阅未开通积分玩法，请联系群主升级订阅。"
    item = await session.get(ShopItem, item_id)
    if item is None or not item.enabled:
        return "商品不存在或已下架。"
    if item.group_id is not None and item.group_id != group.id:
        return "该商品不属于本群。"
    if item.stock == 0:
        return "商品库存不足。"
    if not group.points_enabled:
        return "本群未开启积分商城。"
    result = await repo.adjust_points(
        session,
        group.id,
        user.id,
        -int(item.cost),
        f"商城购买：{item.name}",
        username=user.username or "",
        first_name=user.first_name or "",
    )
    if result is None:
        return "积分不足，无法购买。"
    if item.stock > 0:
        item.stock -= 1
    session.add(
        ShopOrder(
            group_id=group.id,
            user_id=user.id,
            item_id=item.id,
            cost=item.cost,
            status="completed",
            detail=(item.reward_text or "")[:4000],
        )
    )
    return (
        f"✅ 购买成功：<b>{escape(item.name)}</b>，消耗 {item.cost} 积分。\n"
        + (f"🎁 {escape(item.reward_text)}" if item.reward_text else "请联系管理员领取奖励。")
    )


@router.message(Command("buy"))
async def cmd_buy(message: Message) -> None:
    if not _need_group(message) or not message.from_user:
        await message.reply("请在群组中使用。")
        return
    args = helpers.parse_args(message.text)
    if not args or not args[0].isdigit():
        await message.reply("用法：/buy 商品编号")
        return
    async with session_scope() as session:
        group = await _group_for_message(session, message)
        text = await _purchase(session, group, message.from_user, int(args[0]))
    await message.reply(text)


@router.callback_query(F.data.startswith("buy:"))
async def on_buy(callback: CallbackQuery) -> None:
    if callback.message is None:
        await callback.answer("消息已失效。", show_alert=True)
        return
    try:
        item_id = int((callback.data or "").split(":", 1)[1])
    except (IndexError, ValueError):
        await callback.answer("商品编号无效。", show_alert=True)
        return
    if callback.message.chat.type not in {"group", "supergroup"}:
        await callback.answer("请在群组中购买。", show_alert=True)
        return
    async with session_scope() as session:
        group = await repo.ensure_group(
            session,
            callback.message.chat.id,
            title=callback.message.chat.title or "",
            username=callback.message.chat.username or "",
            chat_type=callback.message.chat.type,
        )
        text = await _purchase(session, group, callback.from_user, item_id)
    await callback.answer(text[:200], show_alert=True)


# ---------------------------------------------------------------------------
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
            await repo.adjust_points(session, group.id, message.from_user.id, total, "红包发送失败退款")
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
        if packet.status != "open" or (packet.expires_at and packet.expires_at <= utcnow()):
            packet.status = "expired" if packet.status == "open" else packet.status
            await callback.answer("红包已过期或已领完。", show_alert=True)
            return
        group = await repo.ensure_group(session, group_id)
        if await _feature_denied(session, group, "points"):
            await callback.answer(
                "本群所属订阅未开通积分玩法。", show_alert=True
            )
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

        remaining_count = max(1, int(packet.remaining_count or 0))
        remaining_amount = max(1, int(packet.remaining_amount or 0))
        if remaining_count == 1:
            amount = remaining_amount
        else:
            safe_max = max(1, remaining_amount - (remaining_count - 1))
            average = max(1, remaining_amount // remaining_count)
            amount = min(safe_max, random.randint(1, max(1, average * 2)))
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
        packet.remaining_amount = remaining_amount - amount
        packet.remaining_count = remaining_count - 1
        if packet.remaining_count <= 0 or packet.remaining_amount <= 0:
            packet.status = "finished"
        status_text = "🎉 最后一个红包！" if packet.status == "finished" else "继续抢"
        await callback.answer(f"抢到 {amount} 积分！{status_text}", show_alert=True)
        try:
            if callback.message:
                await callback.message.edit_reply_markup(reply_markup=None if packet.status == "finished" else keyboards.redpacket_kb(packet.id))
        except TelegramAPIError:
            pass


# ---------------------------------------------------------------------------
# Lottery
# ---------------------------------------------------------------------------
@router.message(Command("lottery", "draw", "cj"))
async def cmd_lottery(message: Message, bot: Bot) -> None:
    if not _need_group(message) or not message.from_user:
        await message.reply("请在群组中使用。")
        return
    title, prize, fee, minutes = _parse_game_args(_tail(message))
    if not title or not prize:
        await message.reply(
            "用法：/cj 标题 | 奖品 [参与费] [分钟]\n"
            "例如：/cj 周末抽奖 | 一个月会员 10 30"
        )
        return
    async with session_scope() as session:
        group = await _group_for_message(session, message)
        if await _feature_denied(session, group, "games"):
            await _reply_feature_denied(message, "games")
            return
        if not group.points_enabled and fee > 0:
            await message.reply("本群未开启积分系统，无法设置参与费。")
            return
        lottery = Lottery(
            group_id=group.id,
            creator_id=message.from_user.id,
            title=title[:255],
            prize=prize[:500],
            entry_fee=fee,
            winners_count=1,
            status="open",
            ends_at=utcnow() + timedelta(minutes=minutes),
        )
        session.add(lottery)
        await session.flush()
        ends_text = lottery.ends_at.strftime("%Y-%m-%d %H:%M")
        text = (
            f"🎁 <b>{escape(title)}</b>\n"
            f"奖品：{escape(prize)}\n"
            f"参与费：{fee} {escape(group.points_name or '积分')}\n"
            f"开奖时间：{ends_text}（UTC）\n"
            "点击下方按钮参与抽奖。"
        )
        try:
            sent = await bot.send_message(group.id, text, reply_markup=keyboards.lottery_kb(lottery.id))
            lottery.message_id = sent.message_id
        except TelegramAPIError as exc:
            await message.reply(f"发送抽奖失败：{exc}")
            return
        detail = (
            f"奖品：{escape(prize)}\n"
            f"参与费：{fee} {escape(group.points_name or '积分')}\n"
            f"开奖时间：{ends_text}（UTC）"
        )
    await message.reply("抽奖已发布！")
    _schedule_promotion(
        bot,
        group_id=group.id,
        group_title=group.title or "",
        kind="lottery",
        title=title,
        detail=detail,
        exclude_user_id=message.from_user.id,
    )


@router.callback_query(F.data.startswith("lot:"))
async def on_lottery_join(callback: CallbackQuery) -> None:
    if callback.message is None:
        await callback.answer("消息已失效。", show_alert=True)
        return
    try:
        lottery_id = int((callback.data or "").split(":", 1)[1])
    except (IndexError, ValueError):
        await callback.answer("抽奖编号无效。", show_alert=True)
        return
    async with session_scope() as session:
        lottery = await session.get(Lottery, lottery_id)
        if lottery is None or lottery.group_id != callback.message.chat.id:
            await callback.answer("抽奖不存在。", show_alert=True)
            return
        if lottery.status != "open" or (lottery.ends_at and lottery.ends_at <= utcnow()):
            await callback.answer("抽奖已经结束。", show_alert=True)
            return
        exists = await session.scalar(
            select(LotteryEntry.id).where(
                LotteryEntry.lottery_id == lottery.id,
                LotteryEntry.user_id == callback.from_user.id,
            )
        )
        if exists:
            await callback.answer("你已经参与过了。", show_alert=True)
            return
        group = await repo.ensure_group(session, lottery.group_id)
        if await _feature_denied(session, group, "games"):
            await callback.answer(
                "本群所属订阅未开通抽奖与竞拍。", show_alert=True
            )
            return
        if lottery.entry_fee > 0:
            result = await repo.adjust_points(
                session,
                group.id,
                callback.from_user.id,
                -lottery.entry_fee,
                f"抽奖参与费：{lottery.title}",
                username=callback.from_user.username or "",
                first_name=callback.from_user.first_name or "",
            )
            if result is None:
                await callback.answer("积分不足，无法参与。", show_alert=True)
                return
        session.add(LotteryEntry(lottery_id=lottery.id, user_id=callback.from_user.id))
        await session.flush()
        total = int(
            await session.scalar(
                select(func.count(LotteryEntry.id)).where(LotteryEntry.lottery_id == lottery.id)
            )
            or 0
        )
        await callback.answer(f"参与成功！当前共 {total} 人")


# ---------------------------------------------------------------------------
# Auction
# ---------------------------------------------------------------------------
@router.message(Command("auction", "bidstart", "jp"))
async def cmd_auction(message: Message, bot: Bot) -> None:
    if not _need_group(message) or not message.from_user:
        await message.reply("请在群组中使用。")
        return
    title, start_price, step, minutes = _parse_auction_args(_tail(message))
    if not title:
        await message.reply(
            "用法：/jp 物品 | 起拍价 [加价幅度] [分钟]\n"
            "例如：/jp 稀有头像框 | 100 10 30"
        )
        return
    if start_price < 0 or step < 0:
        await message.reply("起拍价和加价幅度不能为负数。")
        return
    async with session_scope() as session:
        group = await _group_for_message(session, message)
        if await _feature_denied(session, group, "games"):
            await _reply_feature_denied(message, "games")
            return
        if not group.points_enabled:
            await message.reply("本群未开启积分系统，无法竞拍。")
            return
        auction = Auction(
            group_id=group.id,
            creator_id=message.from_user.id,
            item=title[:255],
            start_price=start_price,
            step=max(0, step),
            current_price=start_price,
            current_bidder=None,
            status="open",
            ends_at=utcnow() + timedelta(minutes=minutes),
        )
        session.add(auction)
        await session.flush()
        ends_text = auction.ends_at.strftime("%Y-%m-%d %H:%M")
        text = (
            f"🔨 <b>竞拍：{escape(title)}</b>\n"
            f"起拍价：{start_price} {escape(group.points_name or '积分')}\n"
            f"加价幅度：{max(0, step)}\n"
            f"截止时间：{ends_text}（UTC）\n"
            "发送 /bid 金额 出价，或点击下方按钮查看提示。"
        )
        try:
            sent = await bot.send_message(group.id, text, reply_markup=keyboards.auction_kb(auction.id))
            auction.message_id = sent.message_id
        except TelegramAPIError as exc:
            await message.reply(f"发送竞拍失败：{exc}")
            return
        detail = (
            f"起拍价：{start_price} {escape(group.points_name or '积分')}\n"
            f"加价幅度：{max(0, step)}\n"
            f"截止时间：{ends_text}（UTC）"
        )
    await message.reply("竞拍已开始！")
    _schedule_promotion(
        bot,
        group_id=group.id,
        group_title=group.title or "",
        kind="auction",
        title=title,
        detail=detail,
        exclude_user_id=message.from_user.id,
    )


@router.callback_query(F.data.startswith("bid:"))
async def on_bid_button(callback: CallbackQuery) -> None:
    if callback.message is None:
        await callback.answer("消息已失效。", show_alert=True)
        return
    try:
        auction_id = int((callback.data or "").split(":", 1)[1])
    except (IndexError, ValueError):
        await callback.answer("竞拍编号无效。", show_alert=True)
        return
    async with session_scope() as session:
        auction = await session.get(Auction, auction_id)
        if auction is None or auction.group_id != callback.message.chat.id:
            await callback.answer("竞拍不存在。", show_alert=True)
            return
        if auction.status != "open" or (auction.ends_at and auction.ends_at <= utcnow()):
            await callback.answer("竞拍已经结束。", show_alert=True)
            return
        group = await repo.ensure_group(session, auction.group_id)
        if await _feature_denied(session, group, "games"):
            await callback.answer(
                "本群所属订阅未开通抽奖与竞拍。", show_alert=True
            )
            return
        minimum = auction.start_price if auction.current_bidder is None else auction.current_price + max(1, auction.step)
        await callback.answer(f"发送 /bid 金额 出价，最低 {minimum} 积分。", show_alert=True)


@router.message(Command("bid"))
async def cmd_bid(message: Message, bot: Bot) -> None:
    if not _need_group(message) or not message.from_user:
        await message.reply("请在群组中使用。")
        return
    args = helpers.parse_args(message.text)
    if not args or not args[0].isdigit():
        await message.reply("用法：/bid 金额（可回复竞拍消息）")
        return
    amount = int(args[0])
    async with session_scope() as session:
        group = await _group_for_message(session, message)
        if await _feature_denied(session, group, "games"):
            await _reply_feature_denied(message, "games")
            return
        if not group.points_enabled:
            await message.reply("本群未开启积分系统。")
            return
        query = select(Auction).where(
            Auction.group_id == group.id, Auction.status == "open"
        )
        if message.reply_to_message:
            query = query.where(Auction.message_id == message.reply_to_message.message_id)
        else:
            query = query.order_by(Auction.id.desc())
        auction = await session.scalar(query.limit(1))
        if auction is None:
            await message.reply("当前没有进行中的竞拍。")
            return
        if auction.ends_at and auction.ends_at <= utcnow():
            await message.reply("竞拍已经结束，等待结算。")
            return
        minimum = auction.start_price if auction.current_bidder is None else auction.current_price + max(1, auction.step)
        if amount < minimum:
            await message.reply(f"出价太低，最低需要 {minimum} 积分。")
            return
        member = await repo.ensure_member(
            session,
            group.id,
            message.from_user.id,
            username=message.from_user.username or "",
            first_name=message.from_user.first_name or "",
        )
        if int(member.points or 0) < amount:
            await message.reply(f"积分不足，当前余额 {int(member.points or 0)}。")
            return
        if auction.current_bidder is not None:
            await repo.adjust_points(
                session,
                group.id,
                auction.current_bidder,
                auction.current_price,
                f"竞拍被超过退款：{auction.item}",
            )
        result = await repo.adjust_points(
            session,
            group.id,
            message.from_user.id,
            -amount,
            f"竞拍出价：{auction.item}",
        )
        if result is None:
            await message.reply("积分不足，出价失败。")
            return
        auction.current_price = amount
        auction.current_bidder = message.from_user.id
        session.add(
            AuctionBid(
                auction_id=auction.id,
                user_id=message.from_user.id,
                amount=amount,
            )
        )
        _member, balance = result
    await message.reply(
        f"✅ 出价成功：<b>{amount}</b> 积分\n"
        f"当前余额：{balance}\n"
        "被其他用户超过时，积分会自动退回。"
    )
