"""Points, shop, red packets, lotteries and auctions."""
from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from html import escape
from zoneinfo import ZoneInfo

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from sqlalchemy import func, or_, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ...db import session_scope
from ...entitlements import group_feature_allowed
from ...models import (
    Auction,
    AuctionBid,
    DailyCheckin,
    Lottery,
    LotteryEntry,
    LotteryWinner,
    Member,
    PointsTransaction,
    RedPacket,
    RedPacketClaim,
    ShopItem,
    ShopOrder,
    UserNotificationPreference,
    Group,
    utcnow,
)
from ...services import lottery_rules, repo
from .. import helpers, keyboards

router = Router(name="games")
_LOTTERY_DRAFT_TTL = timedelta(minutes=5)
_LOTTERY_INPUT_TIMEZONE = ZoneInfo("Asia/Shanghai")
_lottery_expiry_tasks: set[asyncio.Task[None]] = set()


@dataclass(frozen=True, slots=True)
class LotteryDraft:
    user_id: int
    group_id: int
    created_at: datetime
    step: str
    title: str
    prize: str
    participation_type: str = "none"
    participation_value: str = ""
    winners_count: int = 1
    draw_type: str = "time"
    draw_value: int = 30
    draw_at: datetime | None = None
    claim_contact: str = ""
    edit_lottery_id: int | None = None
    edit_field: str | None = None


_lottery_drafts: dict[int, LotteryDraft] = {}
_lottery_creating: set[int] = set()
_lottery_history_page_size = 5

# Keep strong references to fire-and-forget promotion pushes.  The event loop
# only holds weak references to tasks, so an unreferenced task can be collected
# before its network sends finish.
_promotion_tasks: set[asyncio.Task] = set()


def _tail(message: Message) -> str:
    text = message.text or ""
    parts = text.split(maxsplit=1)
    return parts[1].strip() if len(parts) > 1 else ""


def _lottery_user_mention(user_id: int) -> str:
    return f'<a href="tg://user?id={user_id}">用户 {user_id}</a>'


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


def _bounded_int(raw: str, low: int, high: int) -> int | None:
    text = (raw or "").strip()
    if not text.isdigit():
        return None
    value = int(text)
    return value if low <= value <= high else None


def _parse_lottery_draw_time(raw: str, now: datetime) -> datetime | None:
    try:
        local_time = datetime.strptime(raw.strip(), "%Y-%m-%d %H:%M").replace(
            tzinfo=_LOTTERY_INPUT_TIMEZONE
        )
    except ValueError:
        return None
    draw_at = local_time.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
    return draw_at if draw_at > now else None


def _lottery_draft_expired(draft: LotteryDraft, now: datetime) -> bool:
    return now - draft.created_at >= _LOTTERY_DRAFT_TTL


_LOTTERY_CONDITION_LABELS = {
    "none": "无门槛",
    "keyword": "消息精确匹配「{value}」",
    "points": "积分达到 {value}",
    "message_count": "累计发言达到 {value}",
}


def _lottery_condition_text(kind: str, value: str) -> str:
    template = _LOTTERY_CONDITION_LABELS.get(kind, _LOTTERY_CONDITION_LABELS["none"])
    return template.format(value=escape(str(value)))


def _lottery_draw_text(draw_type: str, value: int) -> str:
    if draw_type == "participant":
        return f"参与人数达到 {value} 人"
    return f"{value} 分钟后开奖"


def _lottery_draw_summary(draft: LotteryDraft) -> str:
    if draft.draw_type == "participant":
        return f"参与人数达到 {draft.draw_value} 人"
    if draft.draw_at is None:
        return "未设置"
    local_draw_at = draft.draw_at.replace(tzinfo=ZoneInfo("UTC")).astimezone(
        _LOTTERY_INPUT_TIMEZONE
    )
    return local_draw_at.strftime("%Y-%m-%d %H:%M（北京时间）")


async def _expire_lottery_draft(user_id: int, created_at: datetime) -> None:
    remaining = (created_at + _LOTTERY_DRAFT_TTL - utcnow()).total_seconds()
    await asyncio.sleep(max(0.0, remaining))
    draft = _lottery_drafts.get(user_id)
    if draft is not None and draft.created_at == created_at:
        _lottery_drafts.pop(user_id, None)


def _schedule_lottery_draft_expiry(draft: LotteryDraft) -> None:
    task = asyncio.create_task(_expire_lottery_draft(draft.user_id, draft.created_at))
    _lottery_expiry_tasks.add(task)
    task.add_done_callback(_lottery_expiry_tasks.discard)


def _lottery_step_prompt(
    draft: LotteryDraft,
) -> tuple[str, InlineKeyboardMarkup | None]:
    if draft.step == "field_value":
        prompts = {
            "title": ("请发送新的抽奖标题。", "title"),
            "prize": ("请发送新的奖品内容。", "prize"),
            "contact": ("请发送新的领奖联系人。", "contact"),
            "quantity": ("请发送新的中奖人数（1-100）。", "quantity"),
        }
        prompt, _field = prompts[draft.edit_field or "title"]
        return prompt, keyboards.lottery_back_kb("contact")
    if draft.step == "title":
        return (
            "第 1/6 步 · 标题\n请发送抽奖标题，例如：周末抽奖",
            keyboards.lottery_back_kb("title"),
        )
    if draft.step == "prize":
        return (
            "第 2/6 步 · 奖品\n请发送奖品内容，例如：一个月会员",
            keyboards.lottery_back_kb("title"),
        )
    if draft.step == "condition":
        return (
            "第 3/6 步 · 参与条件\n请选择成员参与抽奖需要满足的条件。",
            keyboards.lottery_condition_kb(),
        )
    if draft.step == "condition_value":
        if draft.participation_type == "keyword":
            return (
                "请发送精确参与关键词。消息内容必须与关键词完全相同才会自动报名，例如：抽奖",
                keyboards.lottery_back_kb("condition"),
            )
        if draft.participation_type == "points":
            return ("请发送所需最低积分，例如：100", keyboards.lottery_back_kb("condition"))
        return ("请发送所需最低累计发言数，例如：50", keyboards.lottery_back_kb("condition"))
    if draft.step == "quantity":
        previous_step = "condition" if draft.participation_type == "none" else "condition_value"
        return (
            "第 4/6 步 · 奖品数量\n请发送中奖人数（1-100），例如：1",
            keyboards.lottery_back_kb(previous_step),
        )
    if draft.step == "draw":
        return (
            "第 5/6 步 · 开奖条件\n请选择开奖方式。",
            keyboards.lottery_draw_condition_kb(),
        )
    if draft.step == "draw_value":
        if draft.draw_type == "participant":
            return (
                "请发送开奖所需参与人数，例如：20",
                keyboards.lottery_back_kb("draw"),
            )
        return (
            "请发送开奖日期和时间，格式：YYYY-MM-DD HH:MM\n"
            "按北京时间（UTC+8）解释，例如：2026-11-01 20:30",
            keyboards.lottery_back_kb("draw"),
        )
    return (
        "第 6/6 步 · 领奖方式\n请发送领奖联系人，例如：@admin",
        keyboards.lottery_back_kb("draw_value"),
    )


async def _send_lottery_step(bot: Bot, draft: LotteryDraft) -> None:
    text, markup = _lottery_step_prompt(draft)
    await bot.send_message(draft.user_id, text, reply_markup=markup)


def _lottery_summary(draft: LotteryDraft) -> str:
    return (
        "请确认抽奖配置：\n"
        f"标题：{escape(draft.title)}\n"
        f"奖品：{escape(draft.prize)} × {draft.winners_count}\n"
        f"参与条件：{_lottery_condition_text(draft.participation_type, draft.participation_value)}\n"
        f"开奖条件：{_lottery_draw_summary(draft)}\n"
        f"领奖联系：{escape(draft.claim_contact)}"
    )


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


async def _is_group_admin(bot: Bot, group_id: int, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(group_id, user_id)
    except TelegramAPIError:
        return False
    status = getattr(member.status, "value", str(member.status))
    return status in {"administrator", "creator"}


async def _reply_feature_denied(message: Message, feature: str) -> None:
    label = "积分玩法" if feature == "points" else "抽奖与竞拍"
    await message.reply(f"⛔ 本群所属订阅未开通{label}，请联系群主升级订阅。")


async def _claim_auction_slot(
    session,
    auction_id: int,
    observed_price: int,
    observed_bidder: int | None,
    new_bidder: int,
    new_price: int,
    now,
) -> bool:
    """CAS the auction price/bidder so only one of two racing bids can win."""
    stmt = update(Auction).where(
        Auction.id == auction_id,
        Auction.status == "open",
        Auction.current_price == observed_price,
        or_(Auction.ends_at.is_(None), Auction.ends_at > now),
    )
    if observed_bidder is None:
        stmt = stmt.where(Auction.current_bidder.is_(None))
    else:
        stmt = stmt.where(Auction.current_bidder == observed_bidder)
    stmt = (
        stmt.values(current_price=new_price, current_bidder=new_bidder)
        .returning(Auction.id)
        .execution_options(synchronize_session=False)
    )
    row = (await session.execute(stmt)).first()
    return row is not None


async def _claim_packet_slot(
    session,
    packet_id: int,
    remaining_count: int,
    remaining_amount: int,
    amount: int,
    now,
) -> bool:
    """CAS the packet remainder so concurrent claims cannot overdraw it."""
    new_count = remaining_count - 1
    new_amount = remaining_amount - amount
    stmt = (
        update(RedPacket)
        .where(
            RedPacket.id == packet_id,
            RedPacket.status == "open",
            RedPacket.remaining_count == remaining_count,
            RedPacket.remaining_amount == remaining_amount,
            or_(RedPacket.expires_at.is_(None), RedPacket.expires_at > now),
        )
        .values(
            remaining_amount=new_amount,
            remaining_count=new_count,
            status="finished" if (new_count <= 0 or new_amount <= 0) else "open",
        )
        .returning(RedPacket.id)
        .execution_options(synchronize_session=False)
    )
    row = (await session.execute(stmt)).first()
    return row is not None


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
        claimed = (
            await session.execute(
                sqlite_insert(DailyCheckin)
                .values(group_id=group.id, user_id=message.from_user.id, day=now.date())
                .on_conflict_do_nothing(index_elements=["group_id", "user_id", "day"])
                .returning(DailyCheckin.id)
            )
        ).first()
        if claimed is None:
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
            await session.rollback()
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
    if item.stock > 0:
        stock_row = (
            await session.execute(
                update(ShopItem)
                .where(
                    ShopItem.id == item.id,
                    ShopItem.enabled.is_(True),
                    ShopItem.cost == int(item.cost),
                    func.coalesce(ShopItem.stock, 0) >= 1,
                )
                .values(stock=func.coalesce(ShopItem.stock, 0) - 1)
                .returning(ShopItem.stock)
                .execution_options(synchronize_session=False)
            )
        ).first()
        if stock_row is None:
            await session.rollback()
            return "商品库存不足。"
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
        await session.rollback()
        return "积分不足，无法购买。"
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
        now = utcnow()
        if packet.status != "open" or (packet.expires_at and packet.expires_at <= now):
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
            await callback.answer(
                "手慢了，红包已被抢完或已过期。", show_alert=True
            )
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
# Lottery
# ---------------------------------------------------------------------------
@router.message(Command("lottery", "draw", "cj"))
async def cmd_lottery(message: Message, bot: Bot) -> None:
    if not _need_group(message):
        await message.reply("请在群组中使用。")
        return
    if message.from_user is None:
        if message.sender_chat is not None and message.sender_chat.id == message.chat.id:
            await message.reply("匿名管理员身份无法接收私聊，请切换为个人管理员账号后发送 /cj。")
        else:
            await message.reply("无法识别发起抽奖的管理员账号，请使用个人管理员账号。")
        return
    if not await _is_group_admin(bot, message.chat.id, message.from_user.id):
        await message.reply("只有本群管理员可以发起抽奖。")
        return
    async with session_scope() as session:
        group = await _group_for_message(session, message)
        if await _feature_denied(session, group, "games"):
            await _reply_feature_denied(message, "games")
            return
        group_title = group.title or str(group.id)
    draft = LotteryDraft(
        user_id=message.from_user.id,
        group_id=message.chat.id,
        created_at=utcnow(),
        step="title",
        title="",
        prize="",
    )
    _lottery_drafts[message.from_user.id] = draft
    _schedule_lottery_draft_expiry(draft)
    try:
        await bot.send_message(
            message.from_user.id,
            f"本次抽奖将在群组「{escape(group_title)}」创建。\n"
            "开始配置，发送 /cancel 可随时取消。\n"
            "第 1/6 步 · 标题\n请发送抽奖标题，例如：周末抽奖",
        )
    except TelegramAPIError:
        _lottery_drafts.pop(message.from_user.id, None)
        await message.reply("无法私聊你，请先打开与机器人的私聊并发送 /start。")
        return
    await message.reply("抽奖配置已发送到你的私聊，请在那里完成创建。")


@router.message(F.chat.type == "private", F.text, ~F.text.startswith("/"))
async def lottery_private_config(message: Message, bot: Bot) -> None:
    if not message.from_user:
        return
    draft = _lottery_drafts.get(message.from_user.id)
    if draft is None:
        return
    if _lottery_draft_expired(draft, utcnow()):
        _lottery_drafts.pop(message.from_user.id, None)
        await message.reply("抽奖配置已超过 5 分钟并自动撤销，请回群重新发送 /cj。")
        return
    raw = (message.text or "").strip()
    if draft.step == "field_value" and draft.edit_lottery_id is not None:
        if draft.edit_field == "title":
            draft = replace(draft, title=raw[:255])
        elif draft.edit_field == "prize":
            draft = replace(draft, prize=raw[:500])
        elif draft.edit_field == "contact":
            draft = replace(draft, claim_contact=raw[:255])
        elif draft.edit_field == "quantity":
            quantity = _bounded_int(raw, 1, 100)
            if quantity is None:
                await message.reply("请发送 1-100 之间的中奖人数。")
                return
            draft = replace(draft, winners_count=quantity)
        else:
            await message.reply("请使用完整编辑流程修改该字段。")
            return
        draft = replace(draft, step="confirm")
        _lottery_drafts[message.from_user.id] = draft
        await message.reply(_lottery_summary(draft), reply_markup=keyboards.lottery_setup_kb())
        return
    if draft.step == "title":
        if not raw:
            await message.reply("标题不能为空，请重新发送抽奖标题，例如：周末抽奖")
            return
        draft = replace(draft, title=raw[:255], step="prize")
        _lottery_drafts[message.from_user.id] = draft
        await message.reply("第 2/6 步 · 奖品\n请发送奖品内容，例如：一个月会员", reply_markup=keyboards.lottery_back_kb("title"))
        return
    if draft.step == "prize":
        if not raw:
            await message.reply("奖品不能为空，请重新发送奖品内容，例如：一个月会员")
            return
        draft = replace(draft, prize=raw[:500], step="condition")
    elif draft.step == "condition_value":
        if draft.participation_type == "keyword":
            if not raw:
                await message.reply("请发送参与关键词。")
                return
            draft = replace(draft, participation_value=raw[:255], step="quantity")
        else:
            value = _bounded_int(raw, 1, 10000000)
            if value is None:
                await message.reply("请发送一个正整数。")
                return
            draft = replace(draft, participation_value=str(value), step="quantity")
    elif draft.step == "quantity":
        quantity = _bounded_int(raw, 1, 100)
        if quantity is None:
            await message.reply("请发送 1-100 之间的中奖人数。")
            return
        draft = replace(draft, winners_count=quantity, step="draw")
    elif draft.step == "draw_value":
        if draft.draw_type == "participant":
            value = _bounded_int(raw, 1, 100000)
            if value is None:
                await message.reply("请发送开奖所需参与人数。")
                return
            draft = replace(draft, draw_value=value, step="contact")
        else:
            draw_at = _parse_lottery_draw_time(raw, utcnow())
            if draw_at is None:
                await message.reply(
                    "日期时间格式无效或已过去。请按 YYYY-MM-DD HH:MM 输入未来时间（北京时间）。"
                )
                return
            draft = replace(draft, draw_at=draw_at, step="contact")
    elif draft.step == "contact":
        if not raw:
            await message.reply("请发送领奖联系人。")
            return
        draft = replace(draft, claim_contact=raw[:255], step="confirm")
    else:
        return
    _lottery_drafts[message.from_user.id] = draft
    if draft.step == "confirm":
        await message.reply(
            _lottery_summary(draft), reply_markup=keyboards.lottery_setup_kb()
        )
        return
    await _send_lottery_step(bot, draft)


@router.message(Command("cancel"))
async def lottery_cancel(message: Message) -> None:
    if message.chat.type == "private" and message.from_user:
        _lottery_drafts.pop(message.from_user.id, None)
        await message.reply("已取消抽奖创建。")


@router.message(Command("cjhistory"))
async def lottery_history(message: Message) -> None:
    if message.chat.type != "private" or message.from_user is None:
        await message.reply("请私聊机器人使用 /cjhistory。")
        return
    page = 1
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) == 2 and parts[1].strip().isdigit():
        page = max(1, int(parts[1].strip()))
    await _send_lottery_history_page(message, int(message.from_user.id), page)


async def _send_lottery_history_page(message: Message, user_id: int, page: int) -> None:
    async with session_scope() as session:
        rows = (
            await session.execute(
                select(Lottery, Group.title)
                .outerjoin(Group, Group.id == Lottery.group_id)
                .where(
                    Lottery.creator_id == int(user_id), Lottery.hidden_at.is_(None)
                )
                .order_by(Lottery.id.desc())
                .offset((page - 1) * _lottery_history_page_size)
                .limit(_lottery_history_page_size + 1)
            )
        ).all()
        if not rows:
            await message.answer("没有更多抽奖记录。")
            return
        has_next = len(rows) > _lottery_history_page_size
        cards: list[tuple[int, str, str, int]] = []
        for lottery, _group_title in rows[:_lottery_history_page_size]:
            entries = int(
                await session.scalar(
                    select(func.count(LotteryEntry.id)).where(
                        LotteryEntry.lottery_id == lottery.id
                    )
                )
                or 0
            )
            cards.append((int(lottery.id), lottery.title or "", lottery.status, entries))
        await message.answer(
            f"🎁 <b>我创建的抽奖</b> · 第 {page} 页\n"
            "点击对应抽奖按钮查看详情、取消或编辑。",
            reply_markup=keyboards.lottery_history_list_kb(cards),
        )
        if page > 1 or has_next:
            await message.answer(
                "翻页",
                reply_markup=keyboards.lottery_history_pagination_kb(
                    page, page > 1, has_next
                ),
            )


async def _creator_lottery(
    session: AsyncSession, lottery_id: int, user_id: int
) -> Lottery | None:
    lottery = await session.get(Lottery, int(lottery_id))
    return lottery if lottery is not None and lottery.creator_id == int(user_id) else None


@router.callback_query(F.data.startswith("lotmanage:"))
async def lottery_management_callback(callback: CallbackQuery, bot: Bot) -> None:
    if callback.message is None or callback.message.chat.type != "private":
        await callback.answer("请在私聊中操作。", show_alert=True)
        return
    parts = (callback.data or "").split(":")
    if len(parts) >= 3 and parts[1] == "page" and parts[2].isdigit():
        await _send_lottery_history_page(
            callback.message, callback.from_user.id, max(1, int(parts[2]))
        )
        await callback.answer()
        return
    if len(parts) >= 3 and parts[1] == "select" and parts[2].isdigit():
        async with session_scope() as session:
            lottery = await _creator_lottery(
                session, int(parts[2]), callback.from_user.id
            )
            if lottery is None or lottery.hidden_at is not None:
                await callback.answer("找不到这个抽奖。", show_alert=True)
                return
            group = await session.get(Group, lottery.group_id)
            entries = int(
                await session.scalar(
                    select(func.count(LotteryEntry.id)).where(
                        LotteryEntry.lottery_id == lottery.id
                    )
                )
                or 0
            )
            status_label = {
                "open": "进行中",
                "drawn": "已开奖",
                "cancelled": "已取消",
            }.get(lottery.status, lottery.status)
            await callback.message.edit_text(
                f"#{lottery.id} · {escape(lottery.title)}\n"
                f"群组：{escape(group.title if group else str(lottery.group_id))}\n"
                f"状态：{status_label}\n报名人数：{entries}",
                reply_markup=keyboards.lottery_management_kb(
                    lottery.id, lottery.status == "open"
                ),
            )
            await callback.answer()
        return
    if len(parts) < 3 or not parts[2].isdigit():
        await callback.answer("抽奖编号无效。", show_alert=True)
        return
    action, lottery_id = parts[1], int(parts[2])
    async with session_scope() as session:
        lottery = await _creator_lottery(session, lottery_id, callback.from_user.id)
        if lottery is None:
            await callback.answer("找不到你创建的这个抽奖。", show_alert=True)
            return
        if action == "view":
            entries = int(
                await session.scalar(
                    select(func.count(LotteryEntry.id)).where(LotteryEntry.lottery_id == lottery.id)
                )
                or 0
            )
            winners = [
                int(user_id)
                for user_id in (
                    await session.scalars(
                        select(LotteryWinner.user_id).where(LotteryWinner.lottery_id == lottery.id)
                    )
                ).all()
            ]
            winner_text = (
                "\n中奖者：" + "、".join(_lottery_user_mention(user_id) for user_id in winners)
                if winners
                else ""
            )
            await callback.message.answer(
                f"#{lottery.id} · {escape(lottery.title)}\n"
                f"奖品：{escape(lottery.prize)} × {lottery.winners_count}\n"
                f"状态：{lottery.status}\n报名人数：{entries}{winner_text}"
            )
            await callback.answer()
            return
        if action == "delete":
            if lottery.status == "open":
                await callback.answer("进行中的抽奖不能删除，请先取消。", show_alert=True)
                return
            await session.execute(
                update(Lottery)
                .where(
                    Lottery.id == lottery.id,
                    Lottery.creator_id == callback.from_user.id,
                    Lottery.status != "open",
                )
                .values(hidden_at=utcnow())
            )
            await _send_lottery_history_page(
                callback.message, callback.from_user.id, 1
            )
            await callback.answer("抽奖记录已删除。")
            return
        if action == "page":
            await _send_lottery_history_page(
                callback.message, callback.from_user.id, max(1, int(parts[2]))
            )
            await callback.answer()
            return
        if action == "refresh":
            await callback.answer("请重新发送 /cjhistory 查看抽奖列表。")
            return
        if lottery.status != "open":
            await callback.answer("只有未开奖的抽奖可以操作。", show_alert=True)
            return
        if action == "cancel":
            result = await _cancel_lottery(session, lottery, callback.from_user.id)
            await _send_lottery_history_page(
                callback.message, callback.from_user.id, 1
            )
            await callback.answer(result, show_alert=result != "抽奖已取消。")
            return
        if action == "edit":
            await callback.message.answer(
                f"选择要编辑的字段：#{lottery.id}",
                reply_markup=keyboards.lottery_edit_fields_kb(lottery.id),
            )
            await callback.answer()
            return
        if action == "field" and len(parts) == 4:
            field = parts[3]
            if field not in {"title", "prize", "contact", "quantity"}:
                await callback.answer("该字段暂不支持单独编辑。", show_alert=True)
                return
            current_values = {
                "title": lottery.title or "",
                "prize": lottery.prize or "",
                "contact": lottery.claim_contact or "",
                "quantity": str(lottery.winners_count or 1),
            }
            draft = LotteryDraft(
                user_id=callback.from_user.id,
                group_id=lottery.group_id,
                created_at=utcnow(),
                step="field_value",
                title=lottery.title or "",
                prize=lottery.prize or "",
                participation_type=lottery.participation_type or "none",
                participation_value=lottery.participation_value or "",
                winners_count=int(lottery.winners_count or 1),
                draw_type=lottery.draw_type or "time",
                draw_value=int(lottery.draw_participant_count or 1),
                claim_contact=lottery.claim_contact or "",
                edit_lottery_id=lottery.id,
                edit_field=field,
            )
            _lottery_drafts[callback.from_user.id] = draft
            await _send_lottery_step(bot, draft)
            await callback.message.answer(f"当前值：{escape(current_values[field])}")
            await callback.answer()
            return
        if action == "full_edit":
            draft = LotteryDraft(
                user_id=callback.from_user.id,
                group_id=lottery.group_id,
                created_at=utcnow(),
                step="title",
                title=lottery.title or "",
                prize=lottery.prize or "",
                participation_type=lottery.participation_type or "none",
                participation_value=lottery.participation_value or "",
                winners_count=int(lottery.winners_count or 1),
                draw_type=lottery.draw_type or "time",
                draw_value=int(lottery.draw_participant_count or 1),
                claim_contact=lottery.claim_contact or "",
                edit_lottery_id=lottery.id,
            )
            _lottery_drafts[callback.from_user.id] = draft
            await callback.message.answer(
                "开始完整编辑抽奖，五分钟内完成。请重新逐步填写；发送 /cancel 可取消。"
            )
            await callback.answer()
            return
    await callback.answer("未知操作。", show_alert=True)


@router.message(Command("cjcancel"))
async def lottery_cancel_created(message: Message) -> None:
    if message.chat.type != "private" or message.from_user is None:
        await message.reply("请私聊机器人使用 /cjcancel 编号。")
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) != 2 or not parts[1].strip().isdigit():
        await message.reply("用法：/cjcancel 抽奖编号")
        return
    lottery_id = int(parts[1].strip())
    async with session_scope() as session:
        lottery = await session.get(Lottery, lottery_id)
        result = await _cancel_lottery(session, lottery, message.from_user.id)
    await message.reply(result)


async def _cancel_lottery(
    session: AsyncSession, lottery: Lottery | None, user_id: int
) -> str:
    if lottery is None or lottery.creator_id != int(user_id):
        return "找不到你创建的这个抽奖。"
    if lottery.status != "open":
        return "只有尚未开奖的抽奖可以取消。"
    now = utcnow()
    changed = (
        await session.execute(
            update(Lottery)
            .where(
                Lottery.id == lottery.id,
                Lottery.creator_id == int(user_id),
                Lottery.status == "open",
                or_(Lottery.ends_at.is_(None), Lottery.ends_at > now),
                or_(
                    Lottery.draw_type != "participant",
                    select(func.count(LotteryEntry.id))
                    .where(LotteryEntry.lottery_id == Lottery.id)
                    .scalar_subquery()
                    < Lottery.draw_participant_count,
                ),
            )
            .values(status="cancelled")
            .returning(Lottery.id)
        )
    ).first()
    if changed is None:
        return "抽奖已到开奖条件或已被处理，不能取消。"
    if lottery.entry_fee > 0:
        entries = (
            await session.scalars(
                select(LotteryEntry).where(LotteryEntry.lottery_id == lottery.id)
            )
        ).all()
        for entry in entries:
            await repo.adjust_points(
                session,
                lottery.group_id,
                entry.user_id,
                lottery.entry_fee,
                f"抽奖取消退款：{lottery.title}",
            )
    return "抽奖已取消，已退还参与费。" if lottery.entry_fee > 0 else "抽奖已取消。"


@router.callback_query(F.data.startswith("lotsetup:"))
async def lottery_setup_callback(callback: CallbackQuery, bot: Bot) -> None:
    action = (callback.data or "").split(":")[1:2]
    if action == ["create"]:
        user_id = callback.from_user.id
        if user_id in _lottery_creating:
            await callback.answer("抽奖正在发布，请稍候。")
            return
        _lottery_creating.add(user_id)
        try:
            await _lottery_setup_callback_impl(callback, bot)
        finally:
            _lottery_creating.discard(user_id)
        return
    await _lottery_setup_callback_impl(callback, bot)


async def _lottery_setup_callback_impl(callback: CallbackQuery, bot: Bot) -> None:
    user_id = callback.from_user.id
    draft = _lottery_drafts.get(user_id)
    if callback.message is None or callback.message.chat.id != user_id or draft is None:
        await callback.answer("抽奖配置不存在或已失效。", show_alert=True)
        return
    if _lottery_draft_expired(draft, utcnow()):
        _lottery_drafts.pop(user_id, None)
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.answer(
            "抽奖配置已超过 5 分钟并自动撤销，请回群重新发送 /cj。",
            show_alert=True,
        )
        return
    parts = (callback.data or "").split(":")
    action = parts[1] if len(parts) > 1 else ""
    choice = parts[2] if len(parts) > 2 else ""
    if action == "back":
        valid_steps = {
            "title",
            "prize",
            "condition",
            "condition_value",
            "quantity",
            "draw",
            "draw_value",
            "contact",
        }
        if choice not in valid_steps:
            await callback.answer("无法返回到该步骤。", show_alert=True)
            return
        draft = replace(draft, step=choice)
        _lottery_drafts[user_id] = draft
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.answer("已返回修改。")
        await _send_lottery_step(bot, draft)
        return
    if action == "cancel":
        _lottery_drafts.pop(user_id, None)
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.answer("已取消抽奖创建。")
        return
    if action == "condition":
        if draft.step != "condition" or choice not in _LOTTERY_CONDITION_LABELS:
            await callback.answer("当前步骤不能选择参与条件。", show_alert=True)
            return
        draft = replace(
            draft,
            participation_type=choice,
            step="quantity" if choice == "none" else "condition_value",
        )
        _lottery_drafts[user_id] = draft
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.answer("已记录参与条件。")
        await _send_lottery_step(bot, draft)
        return
    if action == "draw":
        if draft.step != "draw" or choice not in {"time", "participant"}:
            await callback.answer("当前步骤不能选择开奖条件。", show_alert=True)
            return
        draft = replace(draft, draw_type=choice, step="draw_value")
        _lottery_drafts[user_id] = draft
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.answer("已记录开奖条件。")
        await _send_lottery_step(bot, draft)
        return
    if action != "create":
        await callback.answer("未知操作。", show_alert=True)
        return
    if draft.step != "confirm":
        await callback.answer("请先完成前面的配置步骤。", show_alert=True)
        return
    if not await _is_group_admin(bot, draft.group_id, user_id):
        _lottery_drafts.pop(user_id, None)
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.answer("你已不是该群管理员，无法创建抽奖。", show_alert=True)
        return
    async with session_scope() as session:
        group = await session.get(Group, draft.group_id)
        if group is None or not group.is_active:
            _lottery_drafts.pop(user_id, None)
            await callback.answer("目标群组不存在或已停用。", show_alert=True)
            return
        if await _feature_denied(session, group, "games"):
            _lottery_drafts.pop(user_id, None)
            await callback.answer("本群所属订阅未开通抽奖功能。", show_alert=True)
            return
        if draft.participation_type == "points" and not group.points_enabled:
            _lottery_drafts.pop(user_id, None)
            await callback.answer(
                "本群未开启积分系统，无法使用积分参与条件。", show_alert=True
            )
            return
        group_id_value = int(group.id)
        group_title_value = group.title or ""
        if draft.edit_lottery_id is not None:
            existing = await session.get(Lottery, draft.edit_lottery_id)
            if existing is None or existing.creator_id != user_id or existing.status != "open":
                _lottery_drafts.pop(user_id, None)
                await callback.answer("抽奖已不存在或不能编辑。", show_alert=True)
                return
            if draft.edit_field is None:
                existing.title = draft.title[:255]
                existing.prize = draft.prize[:500]
                existing.participation_type = draft.participation_type
                existing.participation_value = draft.participation_value
                existing.winners_count = draft.winners_count
                existing.draw_type = draft.draw_type
                existing.draw_participant_count = (
                    draft.draw_value if draft.draw_type == "participant" else None
                )
                existing.ends_at = draft.draw_at if draft.draw_type == "time" else None
                existing.claim_contact = draft.claim_contact
            elif draft.edit_field == "title":
                existing.title = draft.title[:255]
            elif draft.edit_field == "prize":
                existing.prize = draft.prize[:500]
            elif draft.edit_field == "contact":
                existing.claim_contact = draft.claim_contact
            elif draft.edit_field == "quantity":
                existing.winners_count = draft.winners_count
            _lottery_drafts.pop(user_id, None)
            await callback.message.edit_reply_markup(reply_markup=None)
            await callback.answer("抽奖已更新。")
            return
        lottery = Lottery(
            group_id=group_id_value,
            creator_id=user_id,
            title=draft.title[:255],
            prize=draft.prize[:500],
            entry_fee=0,
            winners_count=draft.winners_count,
            participation_type=draft.participation_type,
            participation_value=draft.participation_value,
            draw_type=draft.draw_type,
            draw_participant_count=(
                draft.draw_value if draft.draw_type == "participant" else None
            ),
            claim_contact=draft.claim_contact,
            status="open",
            ends_at=(
                draft.draw_at
                if draft.draw_type == "time"
                else None
            ),
        )
        session.add(lottery)
        await session.flush()
        lines = [
            f"🎁 <b>{escape(draft.title)}</b>",
            f"奖品：{escape(draft.prize)} × {draft.winners_count}",
            f"参与条件：{_lottery_condition_text(draft.participation_type, draft.participation_value)}",
            f"开奖条件：{_lottery_draw_summary(draft)}",
        ]
        if draft.claim_contact:
            lines.append(f"领奖联系：{escape(draft.claim_contact)}")
        lines.append("点击下方按钮参与抽奖。")
        try:
            sent = await bot.send_message(
                group_id_value,
                "\n".join(lines),
                reply_markup=keyboards.lottery_kb(lottery.id),
            )
        except TelegramAPIError:
            lottery.status = "cancelled"
            _lottery_drafts.pop(user_id, None)
            await callback.message.edit_reply_markup(reply_markup=None)
            await callback.answer("发布抽奖失败，请检查机器人在群内的权限。", show_alert=True)
            return
        lottery.message_id = sent.message_id
        detail = "\n".join(lines[1:-1])
    _lottery_drafts.pop(user_id, None)
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.answer("抽奖已发布。")
    _schedule_promotion(
        bot,
        group_id=group_id_value,
        group_title=group_title_value,
        kind="lottery",
        title=draft.title,
        detail=detail,
        exclude_user_id=user_id,
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
        eligible, reason = await lottery_rules.eligible(
            session, lottery, callback.from_user.id
        )
        if not eligible:
            await callback.answer(reason, show_alert=True)
            return
        group = await repo.ensure_group(session, lottery.group_id)
        if await _feature_denied(session, group, "games"):
            await callback.answer(
                "本群所属订阅未开通抽奖与竞拍。", show_alert=True
            )
            return
        claimed = (
            await session.execute(
                sqlite_insert(LotteryEntry)
                .values(lottery_id=lottery.id, user_id=callback.from_user.id)
                .on_conflict_do_nothing(index_elements=["lottery_id", "user_id"])
                .returning(LotteryEntry.id)
            )
        ).first()
        if claimed is None:
            await session.rollback()
            await callback.answer("你已经参与过了。", show_alert=True)
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
                await session.rollback()
                await callback.answer("积分不足，无法参与。", show_alert=True)
                return
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
        now = utcnow()
        if auction.ends_at and auction.ends_at <= now:
            await message.reply("竞拍已经结束，等待结算。")
            return
        observed_price = int(auction.current_price or 0)
        observed_bidder = auction.current_bidder
        minimum = auction.start_price if observed_bidder is None else observed_price + max(1, auction.step)
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
        if not await _claim_auction_slot(
            session,
            auction.id,
            observed_price,
            observed_bidder,
            message.from_user.id,
            amount,
            now,
        ):
            await session.rollback()
            await message.reply(
                "出价未生效：已有其他用户抢先出价，请重新出价。"
            )
            return
        if observed_bidder is not None:
            await repo.adjust_points(
                session,
                group.id,
                observed_bidder,
                observed_price,
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
            await session.rollback()
            await message.reply("积分不足，出价失败。")
            return
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
