"""Auction setup, bidding and management handlers."""

from __future__ import annotations

import re
from aiogram import Bot, F
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from dataclasses import replace
from datetime import timedelta
from html import escape
from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from .games_shared import AuctionDraft, _LOTTERY_DRAFT_TTL, _active_auction_for_group, _auction_bid_card, _auction_bid_hint, _auction_body_text, _auction_code, _auction_creating, _auction_drafts, _auction_label, _auction_minimum, _auction_query_page_size, _beijing_time_text, _bounded_int, _claim_auction_slot, _feature_denied, _group_for_message, _is_group_admin, _lottery_drafts, _lottery_history_page_size, _need_group, _open_auction_for_bid, _refresh_auction_deadline, _reply_feature_denied, _schedule_promotion, _update_auction_message, log, router
from .. import helpers, keyboards
from ...db import session_scope
from ...models import Auction, AuctionBid, Group, Member, utcnow
from ...services import repo
from ...services.auction_logic import entry_fee as _auction_entry_fee, price_label as _auction_price_label


# Auction
# ---------------------------------------------------------------------------
def _auction_draft_expired(draft: AuctionDraft) -> bool:
    return utcnow() - draft.created_at >= _LOTTERY_DRAFT_TTL


def _auction_step_prompt(draft: AuctionDraft) -> tuple[str, InlineKeyboardMarkup | None]:
    prompts = {
        "item": "第 1/8 步 · 拍品\n请发送拍品名称，例如：稀有头像框",
        "description": "第 2/8 步 · 拍品描述\n请发送拍品描述，例如：稀有动画头像框，限量发行。不填写可点击「跳过描述」。",
        "auction_type": "第 3/8 步 · 竞拍类型\n请选择竞拍类型：积分竞拍（消耗积分）或货币竞拍（10积分门槛，价格以CNY计）。",
        "start_price": "第 4/8 步 · 起拍价\n请发送起拍价，例如：100",
        "step": "第 5/8 步 · 加价幅度\n请发送每次最低加价，例如：10",
        "minutes": "第 6/8 步 · 持续时间\n请发送竞拍持续分钟数，例如：30",
        "extend": "第 7/8 步 · 延时规则\n请发送出价后至少保留的分钟数，例如：3；小于 3 的设置按 3 分钟执行，更长的设置予以保留，截止时间不会缩短。",
        "hammer_interval": "第 8/8 步 · 落锤间隔\n请发送落锤提醒的时间间隔（分钟），例如：1（每分钟提醒）或 2（每两分钟提醒）。提醒将在截止前 间隔×3、间隔×2、间隔×1 分钟时发送，且只有在这些时段内的出价才会延时截止时间。",
    }
    target = {
        "item": "item",
        "description": "item",
        "auction_type": "description",
        "start_price": "auction_type",
        "step": "start_price",
        "minutes": "step",
        "extend": "minutes",
        "hammer_interval": "extend",
    }
    if draft.step == "auction_type":
        return prompts[draft.step], keyboards.auction_type_kb()
    return prompts[draft.step], keyboards.auction_back_kb(
        target[draft.step], allow_description_skip=draft.step == "description"
    )


def _auction_summary(draft: AuctionDraft) -> str:
    hammer = max(1, draft.hammer_interval)
    window = hammer * 3
    extend = f"仅最后 {window} 分钟内有效出价后至少保留 {max(3, draft.extend_minutes)} 分钟，截止时间不会缩短"
    type_label = "积分竞拍" if draft.auction_type == "points" else "货币竞拍"
    return (
        "请确认竞拍配置：\n"
        f"拍品：{escape(draft.item)}\n"
        f"拍品描述：{escape(draft.description) or '（无）'}\n"
        f"竞拍类型：{type_label}\n"
        f"起拍价：{draft.start_price}\n"
        f"加价幅度：{draft.step_amount}\n"
        f"持续时间：{draft.minutes} 分钟\n"
        f"延时规则：{extend}\n"
        f"落锤间隔：每 {hammer} 分钟提醒"
    )


async def _send_auction_step(bot: Bot, draft: AuctionDraft) -> None:
    text, markup = _auction_step_prompt(draft)
    sent = await bot.send_message(draft.user_id, text, reply_markup=markup)
    if _auction_drafts.get(draft.user_id) is draft:
        _auction_drafts[draft.user_id] = replace(draft, prompt_message_id=sent.message_id)


@router.message(Command("auction", "bidstart", "jp"))
async def cmd_auction(message: Message, bot: Bot) -> None:
    if not _need_group(message) or not message.from_user:
        await message.reply("请在群组中使用。")
        return
    if not await _is_group_admin(bot, message.chat.id, message.from_user.id):
        await message.reply("只有本群管理员可以发起竞拍。")
        return
    async with session_scope() as session:
        group = await _group_for_message(session, message)
        if await _feature_denied(session, group, "games"):
            await _reply_feature_denied(message, "games")
            return
        if not group.points_enabled:
            await message.reply("本群未开启积分系统，无法竞拍。")
            return
        group_title = group.title or str(group.id)
    draft = AuctionDraft(
        user_id=message.from_user.id,
        group_id=message.chat.id,
        created_at=utcnow(),
        step="item",
    )
    _auction_drafts[message.from_user.id] = draft
    try:
        await bot.send_message(
            message.from_user.id,
            f"本次竞拍将在群组「{escape(group_title)}」创建。\n"
            "开始配置，发送 /cancel 可随时取消。\n"
            "第 1/7 步 · 拍品\n请发送拍品名称，例如：稀有头像框",
        )
    except TelegramAPIError:
        _auction_drafts.pop(message.from_user.id, None)
        await message.reply("无法私聊你，请先打开与机器人的私聊并发送 /start。")
        return
    await message.reply("竞拍配置已发送到你的私聊，请在那里完成创建。")


async def auction_private_config(message: Message, bot: Bot) -> None:
    if not message.from_user:
        return
    draft = _auction_drafts[message.from_user.id]
    if _auction_draft_expired(draft):
        _auction_drafts.pop(message.from_user.id, None)
        await message.reply("竞拍配置已超过 5 分钟并自动撤销，请回群重新发送 /jp。")
        return
    raw = (message.text or "").strip()
    if draft.step == "item":
        if not raw:
            await message.reply("请发送拍品名称。")
            return
        draft = replace(draft, item=raw[:255], step="description")
    elif draft.step == "description":
        draft = replace(draft, description=raw[:500], step="auction_type")
    elif draft.step == "auction_type":
        await message.reply("请使用下方按钮选择竞拍类型。")
        return
    elif draft.step == "start_price":
        value = _bounded_int(raw, 0, 2_000_000_000)
        if value is None:
            await message.reply("请发送 0 或更大的整数作为起拍价。")
            return
        draft = replace(draft, start_price=value, step="step")
    elif draft.step == "step":
        value = _bounded_int(raw, 0, 2_000_000_000)
        if value is None:
            await message.reply("请发送 0 或更大的整数作为加价幅度。")
            return
        draft = replace(draft, step_amount=value, step="minutes")
    elif draft.step == "minutes":
        value = _bounded_int(raw, 1, 60 * 24 * 30)
        if value is None:
            await message.reply("请发送 1-43200 之间的持续分钟数。")
            return
        draft = replace(draft, minutes=value, step="extend")
    elif draft.step == "extend":
        value = _bounded_int(raw, 0, 60 * 24 * 30)
        if value is None:
            await message.reply("请发送 0 或更大的延时分钟数。")
            return
        draft = replace(draft, extend_minutes=value, step="hammer_interval")
    elif draft.step == "hammer_interval":
        value = _bounded_int(raw, 1, 60)
        if value is None:
            await message.reply("请发送 1-60 之间的落锤间隔分钟数。")
            return
        draft = replace(draft, hammer_interval=value, step="confirm")
    else:
        return
    _auction_drafts[message.from_user.id] = draft
    if draft.step == "confirm":
        await message.reply(_auction_summary(draft), reply_markup=keyboards.auction_setup_kb())
    else:
        await _send_auction_step(bot, draft)


@router.message(Command("cancel"))
async def lottery_cancel(message: Message) -> None:
    if message.chat.type == "private" and message.from_user:
        _lottery_drafts.pop(message.from_user.id, None)
        _auction_drafts.pop(message.from_user.id, None)
        await message.reply("已取消当前创建流程。")


@router.callback_query(F.data.startswith("aucsetup:"))
async def auction_setup_callback(callback: CallbackQuery, bot: Bot) -> None:
    if (
        callback.message is None
        or callback.message.chat.type != "private"
        or callback.message.chat.id != callback.from_user.id
    ):
        await callback.answer("请在私聊中操作。", show_alert=True)
        return
    draft = _auction_drafts.get(callback.from_user.id)
    if draft is None or _auction_draft_expired(draft):
        _auction_drafts.pop(callback.from_user.id, None)
        await callback.answer("竞拍配置不存在或已失效。", show_alert=True)
        return
    parts = (callback.data or "").split(":")
    action = parts[1] if len(parts) > 1 else ""
    if action == "skip":
        if (
            parts != ["aucsetup", "skip", "description"]
            or draft.step != "description"
            or callback.message.message_id != draft.prompt_message_id
        ):
            await callback.answer("当前步骤不能跳过描述，请使用最新的配置消息。", show_alert=True)
            return
        draft = replace(draft, description="", step="auction_type", prompt_message_id=None)
        _auction_drafts[callback.from_user.id] = draft
        await _send_auction_step(bot, draft)
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except TelegramAPIError as exc:
            log.warning("Failed to clear auction setup control user=%s: %s", draft.user_id, exc)
        try:
            await callback.answer("已跳过拍品描述。")
        except TelegramAPIError as exc:
            log.warning("Failed to acknowledge auction setup skip user=%s: %s", draft.user_id, exc)
        return
    if action == "cancel":
        _auction_drafts.pop(callback.from_user.id, None)
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.answer("已取消竞拍创建。")
        return
    if action == "back":
        target = parts[2] if len(parts) > 2 else "item"
        if target not in {"item", "description", "auction_type", "start_price", "step", "minutes"}:
            await callback.answer("无法返回到该步骤。", show_alert=True)
            return
        draft = replace(draft, step=target)
        _auction_drafts[callback.from_user.id] = draft
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.answer("已返回修改。")
        await _send_auction_step(bot, draft)
        return
    if action == "type":
        if draft.step != "auction_type" or callback.message.message_id != draft.prompt_message_id:
            await callback.answer("当前步骤不能选择竞拍类型，请使用最新的配置消息。", show_alert=True)
            return
        auction_type = parts[2] if len(parts) > 2 else ""
        if auction_type not in {"points", "currency"}:
            await callback.answer("竞拍类型无效。", show_alert=True)
            return
        draft = replace(draft, auction_type=auction_type, step="start_price")
        _auction_drafts[callback.from_user.id] = draft
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.answer("已选择竞拍类型。")
        await _send_auction_step(bot, draft)
        return
    if action != "create" or draft.step != "confirm":
        await callback.answer("请先完成竞拍配置。", show_alert=True)
        return
    user_id = callback.from_user.id
    if user_id in _auction_creating:
        await callback.answer("竞拍正在发布，请稍候。")
        return
    _auction_creating.add(user_id)
    try:
        await _auction_confirm_create(callback, bot, draft)
    finally:
        _auction_creating.discard(user_id)


async def _auction_confirm_create(callback: CallbackQuery, bot: Bot, draft: AuctionDraft) -> None:
    assert callback.message is not None
    user_id = callback.from_user.id
    if not await _is_group_admin(bot, draft.group_id, user_id):
        _auction_drafts.pop(user_id, None)
        await callback.answer("你已不是该群管理员，无法创建竞拍。", show_alert=True)
        return
    async with session_scope() as session:
        group = await session.get(Group, draft.group_id)
        if group is None or not group.is_active or not group.points_enabled:
            _auction_drafts.pop(user_id, None)
            await callback.answer("目标群组不存在、已停用或未开启积分系统。", show_alert=True)
            return
        if await _feature_denied(session, group, "games"):
            _auction_drafts.pop(user_id, None)
            await callback.answer("本群所属订阅未开通抽奖与竞拍功能。", show_alert=True)
            return
        auction = Auction(
            code=await _auction_code(session),
            group_id=group.id,
            creator_id=user_id,
            item=draft.item[:255],
            description=draft.description[:500],
            auction_type=draft.auction_type,
            entry_fee=10 if draft.auction_type == "currency" else 0,
            start_price=draft.start_price,
            step=draft.step_amount,
            extend_minutes=draft.extend_minutes,
            hammer_interval=draft.hammer_interval,
            current_price=draft.start_price,
            status="open",
            ends_at=utcnow() + timedelta(minutes=draft.minutes),
        )
        session.add(auction)
        await session.flush()
        text = _auction_body_text(auction, title="竞拍开始")
        try:
            sent = await bot.send_message(
                group.id,
                text,
                reply_markup=keyboards.auction_kb(
                    auction.id, _auction_label(auction), auction.start_price,
                    unit=_auction_price_label(auction),
                ),
            )
        except TelegramAPIError:
            auction.status = "cancelled"
            _auction_drafts.pop(user_id, None)
            await callback.answer("发布竞拍失败，请检查机器人在群内的权限。", show_alert=True)
            return
        auction.message_id = sent.message_id
        detail = (
            f"起拍价：{draft.start_price}\n"
            f"加价幅度：{draft.step_amount}\n"
            f"截止时间：{_beijing_time_text(auction.ends_at)}"
        )
        group_title = group.title or ""
    _auction_drafts.pop(user_id, None)
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.answer("竞拍已发布。")
    await helpers.pin_message_safe(bot, draft.group_id, sent.message_id)
    _schedule_promotion(
        bot,
        group_id=draft.group_id,
        group_title=group_title,
        kind="auction",
        title=draft.item,
        detail=detail,
        exclude_user_id=user_id,
    )


async def _send_auction_history_page(message: Message, user_id: int, page: int) -> None:
    async with session_scope() as session:
        rows = (
            await session.execute(
                select(Auction, Group.title)
                .outerjoin(Group, Group.id == Auction.group_id)
                .where(Auction.creator_id == int(user_id))
                .order_by(Auction.id.desc())
                .offset((page - 1) * _lottery_history_page_size)
                .limit(_lottery_history_page_size + 1)
            )
        ).all()
        if not rows:
            await message.answer("没有更多竞拍记录。")
            return
        has_next = len(rows) > _lottery_history_page_size
        cards = []
        for auction, _group_title in rows[:_lottery_history_page_size]:
            cards.append(
                (int(auction.id), _auction_label(auction), auction.item or "", auction.status)
            )
        await message.answer(
            f"🔨 <b>我创建的竞拍</b> · 第 {page} 页\n点击对应竞拍查看详情或取消。",
            reply_markup=keyboards.auction_history_list_kb(cards),
        )
        if page > 1 or has_next:
            await message.answer(
                "翻页",
                reply_markup=keyboards.lottery_history_pagination_kb(page, page > 1, has_next),
            )


@router.message(Command("jphistory"))
async def auction_history(message: Message) -> None:
    if message.chat.type != "private" or message.from_user is None:
        await message.reply("请私聊机器人使用 /jphistory。")
        return
    page = 1
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) == 2 and parts[1].strip().isdigit():
        page = max(1, int(parts[1].strip()))
    await _send_auction_history_page(message, int(message.from_user.id), page)


@router.message(Command("jpcancel"))
async def auction_cancel_created(message: Message) -> None:
    if message.chat.type != "private" or message.from_user is None:
        await message.reply("请私聊机器人使用 /jpcancel 竞拍编号或内部ID。")
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) != 2 or not parts[1].strip():
        await message.reply("用法：/jpcancel 竞拍编号或内部ID")
        return
    async with session_scope() as session:
        auction = await _creator_auction(session, parts[1].strip(), message.from_user.id)
        result = await _cancel_auction(session, auction, message.from_user.id)
    await message.reply(result)


async def _creator_auction(session: AsyncSession, key: str, user_id: int) -> Auction | None:
    stmt = select(Auction).where(Auction.creator_id == int(user_id))
    if key.isdigit():
        auction = await session.scalar(stmt.where(Auction.id == int(key)))
        if auction is not None:
            return auction
    return await session.scalar(stmt.where(Auction.code == key.upper()))


async def _cancel_auction(session: AsyncSession, auction: Auction | None, user_id: int) -> str:
    if auction is None:
        return "找不到你创建的这个竞拍。"
    if auction.status != "open":
        return "只有尚未结束的竞拍可以取消。"
    cancelled = (
        await session.execute(
            update(Auction)
            .where(
                Auction.id == auction.id,
                Auction.creator_id == int(user_id),
                Auction.status == "open",
            )
            .values(status="cancelled")
            .returning(Auction)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if cancelled is None:
        return "竞拍已被处理，不能取消。"
    if cancelled.current_bidder is not None:
        await repo.adjust_points(
            session,
            cancelled.group_id,
            cancelled.current_bidder,
            (
                _auction_entry_fee(cancelled)
                if cancelled.auction_type == "currency"
                else int(cancelled.current_price or 0)
            ),
            f"竞拍取消退款：{cancelled.item}",
        )
    return f"竞拍 {_auction_label(cancelled)} 已取消。"


@router.callback_query(F.data.startswith("aucmanage:"))
async def auction_management_callback(callback: CallbackQuery, bot: Bot) -> None:
    if callback.message is None or callback.message.chat.type != "private":
        await callback.answer("请在私聊中操作。", show_alert=True)
        return
    parts = (callback.data or "").split(":")
    if len(parts) >= 3 and parts[1] == "select" and parts[2].isdigit():
        async with session_scope() as session:
            auction = await session.get(Auction, int(parts[2]))
            if auction is None or auction.creator_id != callback.from_user.id:
                await callback.answer("找不到这个竞拍。", show_alert=True)
                return
            group = await session.get(Group, auction.group_id)
            bids = int(
                await session.scalar(
                    select(func.count(AuctionBid.id)).where(AuctionBid.auction_id == auction.id)
                )
                or 0
            )
            status_label = {"open": "进行中", "finished": "已成交", "cancelled": "已取消"}.get(
                auction.status, auction.status
            )
            await callback.message.edit_text(
                f"{_auction_label(auction)} · {escape(auction.item)}\n"
                f"群组：{escape(group.title if group else str(auction.group_id))}\n"
                f"状态：{status_label}\n"
                f"当前价：{auction.current_price} 积分\n"
                f"领先者：{auction.current_bidder or '—'}\n"
                f"出价记录：{bids} 条\n"
                f"截止时间：{_beijing_time_text(auction.ends_at)}",
                reply_markup=keyboards.auction_management_kb(auction.id, auction.status == "open"),
            )
            await callback.answer()
        return
    if len(parts) >= 3 and parts[1] == "cancel" and parts[2].isdigit():
        async with session_scope() as session:
            auction = await session.get(Auction, int(parts[2]))
            result = await _cancel_auction(session, auction, callback.from_user.id)
        await callback.answer(result, show_alert=True)
        if auction is not None:
            await _send_auction_history_page(callback.message, callback.from_user.id, 1)
        return
    if len(parts) >= 3 and parts[1] == "delete" and parts[2].isdigit():
        async with session_scope() as session:
            changed = (
                await session.execute(
                    update(Auction)
                    .where(
                        Auction.id == int(parts[2]),
                        Auction.creator_id == int(callback.from_user.id),
                        Auction.status != "open",
                    )
                    .values(status="deleted")
                    .returning(Auction.id)
                )
            ).first()
        await callback.answer(
            "记录已删除。" if changed else "只有已结束的竞拍记录可以删除。",
            show_alert=True,
        )
        if changed:
            await _send_auction_history_page(callback.message, callback.from_user.id, 1)
        return
    await callback.answer("未知操作。", show_alert=True)


@router.callback_query(F.data.startswith("bid:"))
async def on_bid_button(callback: CallbackQuery, bot: Bot) -> None:
    if callback.message is None or callback.from_user is None:
        await callback.answer("消息已失效。", show_alert=True)
        return
    parts = (callback.data or "").split(":")
    auction_id = _bounded_int(parts[1], 1, 2**63 - 1) if len(parts) in {2, 3} else None
    if auction_id is None or (len(parts) == 3 and not parts[2].isdigit()):
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
            await callback.answer("本群所属订阅未开通抽奖与竞拍。", show_alert=True)
            return
        if not group.points_enabled:
            await callback.answer("本群未开启积分系统。", show_alert=True)
            return
        amount = _auction_minimum(auction)
        is_currency = getattr(auction, "auction_type", "points") == "currency"
        entry_fee = _auction_entry_fee(auction)
        cost = entry_fee if is_currency else amount
        member = await repo.ensure_member(
            session,
            group.id,
            callback.from_user.id,
            username=callback.from_user.username or "",
            first_name=callback.from_user.first_name or "",
        )
        if int(member.points or 0) < cost:
            needed = cost if is_currency else amount
            await callback.answer(
                f"积分不足，当前余额 {int(member.points or 0)}，需要 {needed} 积分。", show_alert=True
            )
            return
        now = utcnow()
        observed_price = int(auction.current_price or 0)
        observed_bidder = auction.current_bidder
        if not await _claim_auction_slot(
            session, auction.id, observed_price, observed_bidder, callback.from_user.id, amount, now
        ):
            await session.rollback()
            await callback.answer("已有其他用户抢先出价，请重试。", show_alert=True)
            return
        await _refresh_auction_deadline(auction, now)
        if observed_bidder is not None:
            refund_amount = entry_fee if is_currency else observed_price
            await repo.adjust_points(
                session, group.id, observed_bidder, refund_amount, f"竞拍被超过退款：{auction.item}"
            )
        result = await repo.adjust_points(
            session, group.id, callback.from_user.id, -cost, f"竞拍出价：{auction.item}"
        )
        if result is None:
            await session.rollback()
            await callback.answer("积分不足，出价失败。", show_alert=True)
            return
        session.add(AuctionBid(auction_id=auction.id, user_id=callback.from_user.id, amount=amount))
        _member, balance = result
        await session.flush()
        await session.refresh(auction)
        bid_text, bid_keyboard = await _auction_bid_card(session, auction)
        group_id = int(auction.group_id)
        result_text = (
            f"✅ 出价成功：{amount} {_auction_price_label(auction)}"
            + (f"，入场费 {cost} 积分" if is_currency else "")
            + f"，余额 {balance}，截止 {_beijing_time_text(auction.ends_at)}"
        )
    await helpers.send_temp_notice(bot, group_id, bid_text, reply_markup=bid_keyboard)
    try:
        await callback.answer(result_text, show_alert=True)
    except TelegramAPIError as exc:
        log.warning("Failed to acknowledge auction bid auction=%s: %s", auction_id, exc)
    await _update_auction_message(bot, auction_id)


async def _auction_bid_info_text(session: AsyncSession, auction: Auction) -> str:
    bid_count = int(
        await session.scalar(
            select(func.count(AuctionBid.id)).where(AuctionBid.auction_id == auction.id)
        ) or 0
    )
    current_streak = 0
    if auction.current_bidder is not None:
        streak_stmt = (
            select(func.count(AuctionBid.id))
            .where(
                AuctionBid.auction_id == auction.id,
                AuctionBid.id >= select(func.coalesce(func.max(AuctionBid.id), 0))
                .where(
                    AuctionBid.auction_id == auction.id,
                    AuctionBid.user_id != auction.current_bidder,
                ).scalar_subquery(),
                AuctionBid.user_id == auction.current_bidder,
            )
        )
        current_streak = int(await session.scalar(streak_stmt) or 0)
    bid_rows = (await session.scalars(
        select(AuctionBid).where(AuctionBid.auction_id == auction.id)
        .order_by(AuctionBid.created_at.desc(), AuctionBid.id.desc()).limit(5)
    )).all()
    active = auction.status == "open" and (auction.ends_at is None or auction.ends_at > utcnow())
    status = {"open": "进行中", "finished": "已成交", "cancelled": "已取消"}.get(
        auction.status, auction.status
    )
    if auction.status == "open" and not active:
        status = "已结束，等待结算"
    price_label = _auction_price_label(auction)
    desc = escape(auction.description or "")
    desc_line = f"📄 <b>拍品描述</b>　{desc}" if desc else "📄 <b>拍品描述</b>　"
    lines = [
        f"🔖 <b>竞拍出价情况</b>　{escape(_auction_label(auction))}",
        "──────────────",
        f"📦 <b>拍品</b>　{escape(auction.item)}",
        f"🆔 <b>竞拍 ID</b>　<code>{auction.id}</code>",
        desc_line,
        f"📅 <b>状态</b>　{escape(status)}",
    ]
    if auction.current_bidder is not None:
        bidder = await _auction_bidder_name(session, auction.group_id, auction.current_bidder)
        lines.append(f"👤 <b>当前出价用户</b>　{bidder}")
    lines.append(f"💰 <b>当前出价</b>　{auction.current_price} {price_label}")
    lines.append(f"➕ <b>最低加价</b>　{max(1, auction.step)} {price_label}")
    if active:
        lines.append(f"📌 <b>最低可出价</b>　{_auction_minimum(auction)} {price_label}")
    lines.append(f"⏰ <b>截止时间</b>　{_beijing_time_text(auction.ends_at)}")
    extend = max(3, auction.extend_minutes or 0)
    hammer = max(1, auction.hammer_interval or 1)
    window = hammer * 3
    lines.append(f"📈 <b>延时规则</b>　仅最后 {window} 分钟内有效出价延时，截止时间至少为出价后 {extend} 分钟")
    lines.append(f"📊 <b>出价次数</b>　{bid_count}")
    lines.append(f"🔁 <b>当前连续领先</b>　{current_streak} 次")
    if bid_rows:
        lines.append("──────────────")
        lines.append("<b>最近出价记录：</b>")
        for row in bid_rows:
            bidder = await _auction_bidder_name(session, auction.group_id, row.user_id)
            lines.append(
                f"· {bidder} 出价 {row.amount} {price_label} · {_beijing_time_text(row.created_at)}"
            )
    return "\n".join(lines)


async def _auction_bidder_name(session: AsyncSession, group_id: int, user_id: int) -> str:
    member = await session.scalar(select(Member).where(
        Member.group_id == group_id, Member.user_id == user_id,
    ))
    name = (member.first_name or member.username.lstrip("@")) if member is not None else ""
    return escape(name or f"用户 {user_id}")


async def _auction_query_matches(
    session: AsyncSession, group_id: int, selector: str | None
) -> list[Auction]:
    stmt = select(Auction).where(Auction.group_id == int(group_id), Auction.status != "deleted")
    if selector is None:
        stmt = stmt.where(
            Auction.status == "open", or_(Auction.ends_at.is_(None), Auction.ends_at > utcnow())
        )
    else:
        key = selector.upper()
        internal_id = _bounded_int(key.removeprefix("#"), 1, 2**63 - 1)
        if key.startswith("#"):
            if internal_id is None:
                return []
            stmt = stmt.where(Auction.id == internal_id)
        else:
            match_code = Auction.code == key
            stmt = stmt.where(
                or_(match_code, Auction.id == internal_id) if internal_id is not None else match_code
            )
    return list((await session.scalars(stmt.order_by(Auction.id.desc()))).all())


async def _auction_from_reply(
    session: AsyncSession, message: Message, bot_id: int
) -> Auction | None:
    reply = message.reply_to_message
    if reply is None or reply.chat.id != message.chat.id:
        return None
    auction = await session.scalar(
        select(Auction).where(
            Auction.group_id == message.chat.id,
            Auction.status != "deleted",
            Auction.message_id == reply.message_id,
        )
    )
    if auction is not None:
        return auction
    if reply.from_user is None or reply.from_user.id != bot_id:
        return None
    ids = {
        parts[1]
        for row in (reply.reply_markup.inline_keyboard if reply.reply_markup else [])
        for button in row
        if button.callback_data
        and len(parts := button.callback_data.split(":")) >= 2
        and parts[0] in {"aucquery", "bid"}
        and parts[1].isdigit()
    }
    if not ids:
        ids = set(re.findall(r"竞拍 ID(?:</b>)?\s*(?:<code>)?(\d+)", reply.text or ""))
    if len(ids) != 1:
        return None
    matches = await _auction_query_matches(session, message.chat.id, f"#{next(iter(ids))}")
    return matches[0] if matches else None


@router.message(Command("bidinfo", "bidquery"))
async def cmd_bid_info(message: Message, bot: Bot) -> None:
    if not _need_group(message) or not message.from_user:
        await message.reply("请在群组中使用。")
        return
    args = helpers.parse_args(message.text)
    async with session_scope() as session:
        if not args and message.reply_to_message:
            auction = await _auction_from_reply(session, message, bot.id)
            auctions = [auction] if auction is not None else []
        else:
            auctions = await _auction_query_matches(
                session, message.chat.id, args[0] if args else None
            )
        text = await _auction_bid_info_text(session, auctions[0]) if len(auctions) == 1 else ""
    if not auctions:
        await message.reply("找不到该竞拍或本群当前没有进行中的竞拍。")
        return
    if len(auctions) > 1:
        for offset in range(0, len(auctions), _auction_query_page_size):
            page = auctions[offset:offset + _auction_query_page_size]
            lines = ["🔎 <b>本群竞拍列表</b> · 点击编号查看出价情况"]
            lines.extend(
                f"· <code>{escape(_auction_label(auction))}</code> · {escape(auction.item)}"
                f" · {auction.current_price} {_auction_price_label(auction)}"
                for auction in page
            )
            await helpers.send_temp_notice(
                bot,
                message.chat.id,
                "\n".join(lines),
                reply_markup=keyboards.auction_query_list_kb([
                    (int(auction.id), _auction_label(auction), auction.item) for auction in page
                ]),
            )
        return
    await helpers.send_temp_notice(bot, message.chat.id, text)


@router.callback_query(F.data.startswith("aucquery:"))
async def on_auction_query(callback: CallbackQuery, bot: Bot) -> None:
    if callback.message is None or callback.message.chat.type not in {"group", "supergroup"}:
        await callback.answer("请在竞拍所在的群组查询。", show_alert=True)
        return
    parts = (callback.data or "").split(":")
    auction_id = _bounded_int(parts[1], 1, 2**63 - 1) if len(parts) == 2 else None
    if auction_id is None:
        await callback.answer("竞拍编号无效。", show_alert=True)
        return
    group_id = callback.message.chat.id
    async with session_scope() as session:
        matches = await _auction_query_matches(session, group_id, f"#{auction_id}")
        if not matches:
            await callback.answer("本群找不到该竞拍。", show_alert=True)
            return
        text = await _auction_bid_info_text(session, matches[0])
    await helpers.send_temp_notice(bot, group_id, text)
    try:
        await callback.answer()
    except TelegramAPIError as exc:
        log.warning("Failed to acknowledge auction query auction=%s: %s", auction_id, exc)


@router.message(Command("bid"))
async def cmd_bid(message: Message, bot: Bot) -> None:
    if not _need_group(message) or not message.from_user:
        await message.reply("请在群组中使用。")
        return
    args = helpers.parse_args(message.text)
    code = None
    amount_arg = args[0] if args else ""
    if args and (not args[0].isdigit() or len(args) == 2):
        code = args[0].upper()
        amount_arg = args[1] if len(args) > 1 else ""
    if not amount_arg.isdigit():
        await message.reply("用法：/bid 金额，或 /bid 竞拍编号 金额（可回复竞拍消息）")
        return
    amount = int(amount_arg)
    async with session_scope() as session:
        group = await _group_for_message(session, message)
        if await _feature_denied(session, group, "games"):
            await _reply_feature_denied(message, "games")
            return
        if not group.points_enabled:
            await message.reply("本群未开启积分系统。")
            return
        if code:
            auction = await _open_auction_for_bid(session, group.id, code)
            if auction is None:
                await message.reply("找不到该编号的进行中竞拍。")
                return
        elif message.reply_to_message:
            auction = await _auction_from_reply(session, message, bot.id)
            if auction is None or auction.status != "open":
                await message.reply("这条消息没有对应的进行中竞拍。")
                return
        else:
            auction = await _open_auction_for_bid(session, group.id)
            if auction is None:
                active = await _active_auction_for_group(session, group.id)
                if active is None:
                    await message.reply("当前没有进行中的竞拍。")
                else:
                    await message.reply(
                        f"当前有多个竞拍，请发送 /bid {escape(_auction_label(active))} 金额 指定。"
                    )
                return
        now = utcnow()
        if auction.ends_at and auction.ends_at <= now:
            await message.reply("竞拍已经结束，等待结算。")
            return
        observed_price = int(auction.current_price or 0)
        observed_bidder = auction.current_bidder
        minimum = _auction_minimum(auction)
        if amount < minimum:
            await message.reply(
                f"出价太低，{_auction_bid_hint(auction)}，最低需要 {minimum} {_auction_price_label(auction)}。"
            )
            return
        is_currency = getattr(auction, "auction_type", "points") == "currency"
        entry_fee = _auction_entry_fee(auction)
        cost = entry_fee if is_currency else amount
        member = await repo.ensure_member(
            session,
            group.id,
            message.from_user.id,
            username=message.from_user.username or "",
            first_name=message.from_user.first_name or "",
        )
        if int(member.points or 0) < cost:
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
            await message.reply("出价未生效：已有其他用户抢先出价，请重新出价。")
            return
        await _refresh_auction_deadline(auction, now)
        if observed_bidder is not None:
            refund_amount = entry_fee if is_currency else observed_price
            await repo.adjust_points(
                session,
                group.id,
                observed_bidder,
                refund_amount,
                f"竞拍被超过退款：{auction.item}",
            )
        result = await repo.adjust_points(
            session,
            group.id,
            message.from_user.id,
            -cost,
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
        await session.flush()
        await session.refresh(auction)
        bid_text, bid_keyboard = await _auction_bid_card(session, auction)
        deadline = _beijing_time_text(auction.ends_at)
        reply_text = (
            f"✅ 出价成功：{_auction_label(auction)} · <b>{amount}</b> {_auction_price_label(auction)}\n"
            + (f"入场费：{cost} 积分（竞拍结束返还）\n" if is_currency else "")
            + f"当前余额：{balance}\n"
            f"新的截止时间：{deadline}\n"
            "被其他用户超过时，积分会自动退回。"
        )
        chat_id = message.chat.id
        auction_id = auction.id
        group_id_val = auction.group_id
    await helpers.send_temp_notice(bot, group_id_val, bid_text, reply_markup=bid_keyboard)
    await helpers.delete_message_safe(bot, chat_id, message.message_id)
    notice_id = await helpers.send_temp_notice(bot, chat_id, reply_text, delay=8)
    if notice_id is None:
        log.warning("Failed to send auction bid receipt auction=%s chat=%s", auction_id, chat_id)
    await _update_auction_message(bot, auction_id)
