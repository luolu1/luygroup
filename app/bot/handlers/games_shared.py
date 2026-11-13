"""Points, shop, red packets, lotteries and auctions."""

from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass
from datetime import datetime, timedelta
from html import escape
from zoneinfo import ZoneInfo

from aiogram import Bot, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.types import InlineKeyboardMarkup, Message
from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ...db import session_scope
from ...entitlements import group_feature_allowed
from ...models import (
    Auction,
    Member,
    RedPacket,
    UserNotificationPreference,
    utcnow,
)
from ...services import repo
from ...services.auction_logic import price_label as _auction_price_label
from .. import keyboards

log = logging.getLogger("app.bot.handlers.games")
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


@dataclass(frozen=True, slots=True)
class AuctionDraft:
    user_id: int
    group_id: int
    created_at: datetime
    step: str
    item: str = ""
    description: str = ""
    auction_type: str = "points"
    start_price: int = 0
    step_amount: int = 0
    minutes: int = 60
    extend_minutes: int = 1
    hammer_interval: int = 1
    prompt_message_id: int | None = None


_lottery_drafts: dict[int, LotteryDraft] = {}
_auction_drafts: dict[int, AuctionDraft] = {}
_lottery_creating: set[int] = set()
_auction_creating: set[int] = set()
_lottery_history_page_size = 5
_auction_query_page_size = 10
_auction_code_alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


async def _auction_code(session) -> str:
    for _ in range(12):
        code = "".join(random.choices(_auction_code_alphabet, k=8))
        if not await session.scalar(select(Auction.id).where(Auction.code == code)):
            return code
    return "".join(random.choices(_auction_code_alphabet, k=8))


def _auction_label(auction: Auction) -> str:
    return auction.code or f"#{auction.id}"


def _auction_bid_hint(auction: Auction) -> str:
    label = _auction_label(auction)
    if getattr(auction, "auction_type", "points") == "currency":
        return f"发送 /bid {label} 金额（CNY）"
    return f"发送 /bid {label} 金额"


def _auction_bidder_text(auction: Auction) -> str:
    if auction.current_bidder is None:
        return ""
    return f"{auction.current_bidder}"


def _auction_body_text(
    auction: Auction,
    title: str,
    bidder_text: str = "",
    *,
    creator_text: str = "",
    finish_text: str = "",
) -> str:
    price_label = _auction_price_label(auction)
    desc = escape(auction.description or "")
    desc_line = f"📄 <b>拍品描述</b>　{desc}" if desc else "📄 <b>拍品描述</b>　"
    lines = [
        f"🔨 <b>{title}</b>",
        "──────────────",
        f"📦 <b>拍品</b>　{escape(auction.item)}",
        f"🔖 <b>编号</b>　{escape(_auction_label(auction))}",
        f"🆔 <b>竞拍 ID</b>　<code>{auction.id}</code>",
        desc_line,
    ]
    if creator_text:
        lines.append(f"👤 <b>竞拍发起者</b>　{creator_text}")
    if bidder_text:
        lines.append(f"👤 <b>出价用户</b>　{bidder_text}")
    if finish_text:
        lines.append(f"🏆 <b>竞拍获得者</b>　{finish_text}")
    if finish_text or auction.status != "open":
        lines.append(f"💰 <b>成交价</b>　{auction.current_price} {price_label}")
    elif bidder_text:
        lines.append(f"💰 <b>当前出价</b>　{auction.current_price} {price_label}")
    else:
        lines.append(f"💰 <b>起拍价</b>　{auction.start_price} {price_label}")
    lines.append(f"➕ <b>最低加价</b>　{max(1, auction.step)} {price_label}")
    lines.append(f"⏰ <b>截止时间</b>　{_beijing_time_text(auction.ends_at)}")
    extend = max(3, auction.extend_minutes or 0)
    hammer = max(1, auction.hammer_interval or 1)
    window = hammer * 3
    lines.append(f"📈 <b>延时规则</b>　仅最后 {window} 分钟内有效出价延时，截止时间至少为出价后 {extend} 分钟")
    lines.extend(["──────────────", f"点击下方按钮出价，或{escape(_auction_bid_hint(auction))}。"])
    return "\n".join(lines)


def _auction_minimum(auction: Auction) -> int:
    return (
        auction.start_price
        if auction.current_bidder is None
        else auction.current_price + max(1, auction.step)
    )


async def _auction_bid_card(
    session: AsyncSession, auction: Auction, *, title: str = "拍品最新出价"
) -> tuple[str, InlineKeyboardMarkup]:
    bidder_text = (
        await _user_mention(session, auction.group_id, auction.current_bidder)
        if auction.current_bidder is not None else ""
    )
    return (
        _auction_body_text(auction, title=title, bidder_text=bidder_text),
        keyboards.auction_kb(
            auction.id, _auction_label(auction), _auction_minimum(auction),
            unit=_auction_price_label(auction),
        ),
    )


async def _update_auction_message(bot: Bot, auction_id: int) -> None:
    async with session_scope() as session:
        auction = await session.get(Auction, auction_id)
        if auction is None or not auction.message_id:
            return
        text, keyboard = await _auction_bid_card(session, auction, title="竞拍开始")
        group_id, message_id = int(auction.group_id), int(auction.message_id)
        markup = keyboard if auction.status == "open" else None
    try:
        await bot.edit_message_text(
            text=text, chat_id=group_id, message_id=message_id, reply_markup=markup
        )
    except TelegramAPIError as exc:
        log.warning(
            "Failed to update auction message auction=%s chat=%s message=%s: %s",
            auction_id, group_id, message_id, exc,
        )


async def _open_auction_for_bid(session, group_id: int, code: str | None = None) -> Auction | None:
    stmt = select(Auction).where(Auction.group_id == int(group_id), Auction.status == "open")
    stmt = stmt.where(Auction.code == code) if code else stmt.order_by(Auction.id.desc()).limit(2)
    auctions = (await session.scalars(stmt)).all()
    if code:
        return auctions[0] if auctions else None
    return auctions[0] if len(auctions) == 1 else None


async def _refresh_auction_deadline(auction: Auction, now) -> None:
    if auction.ends_at is None:
        return
    hammer = max(1, auction.hammer_interval or 1)
    window_seconds = hammer * 3 * 60
    if auction.ends_at > now + timedelta(seconds=window_seconds):
        return
    extend = max(3, auction.extend_minutes or 0)
    target = now + timedelta(minutes=extend)
    if target > auction.ends_at:
        auction.ends_at = target


async def _active_auction_for_group(session, group_id: int) -> Auction | None:
    return await session.scalar(
        select(Auction)
        .where(Auction.group_id == int(group_id), Auction.status == "open")
        .order_by(Auction.id.desc())
        .limit(1)
    )


# Keep strong references to fire-and-forget promotion pushes.  The event loop
# only holds weak references to tasks, so an unreferenced task can be collected
# before its network sends finish.
_promotion_tasks: set[asyncio.Task] = set()


async def _user_mention(session, group_id: int, user_id: int) -> str:
    member = await session.scalar(
        select(Member).where(Member.group_id == int(group_id), Member.user_id == int(user_id))
    )
    if member is not None and member.username:
        handle = member.username.lstrip("@")
        if handle:
            return f'<a href="tg://user?id={int(user_id)}">@{escape(handle)}</a>'
    if member is not None and member.first_name:
        return f'<a href="tg://user?id={int(user_id)}">{escape(member.first_name)}</a>'
    return f'<a href="tg://user?id={int(user_id)}">用户 {int(user_id)}</a>'


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


def _lottery_draw_summary(draft: LotteryDraft) -> str:
    if draft.draw_type == "participant":
        return f"参与人数达到 {draft.draw_value} 人"
    if draft.draw_at is None:
        return "未设置"
    local_draw_at = draft.draw_at.replace(tzinfo=ZoneInfo("UTC")).astimezone(
        _LOTTERY_INPUT_TIMEZONE
    )
    return local_draw_at.strftime("%Y-%m-%d %H:%M（北京时间）")


def _beijing_time_text(dt: datetime | None) -> str:
    if dt is None:
        return "—"
    return dt.replace(tzinfo=ZoneInfo("UTC")).astimezone(_LOTTERY_INPUT_TIMEZONE).strftime(
        "%Y-%m-%d %H:%M（北京时间）"
    )


def _lottery_time_example(now: datetime) -> str:
    return (
        now.replace(tzinfo=ZoneInfo("UTC")).astimezone(_LOTTERY_INPUT_TIMEZONE)
        + timedelta(hours=1)
    ).strftime("%Y-%m-%d %H:%M")


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
        example = _lottery_time_example(utcnow())
        return (
            "请发送开奖日期和时间，格式：YYYY-MM-DD HH:MM\n"
            f"按北京时间（UTC+8），例如：{example}",
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
                await bot.send_message(user_id, text, disable_web_page_preview=True)
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

