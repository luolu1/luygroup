"""Lottery setup, entry and management handlers."""

from __future__ import annotations

from aiogram import Bot, F
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message
from dataclasses import replace
from html import escape
from sqlalchemy import func, or_, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession
from .games_shared import LotteryDraft, _LOTTERY_CONDITION_LABELS, _auction_drafts, _bounded_int, _feature_denied, _group_for_message, _is_group_admin, _lottery_condition_text, _lottery_creating, _lottery_draft_expired, _lottery_drafts, _lottery_draw_summary, _lottery_history_page_size, _lottery_summary, _need_group, _parse_lottery_draw_time, _reply_feature_denied, _schedule_lottery_draft_expiry, _schedule_promotion, _send_lottery_step, router
from .. import helpers, keyboards
from ...db import session_scope
from ...models import Group, Lottery, LotteryEntry, LotteryWinner, utcnow
from ...services import lottery_rules, repo


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
async def game_private_config(message: Message, bot: Bot) -> None:
    from .games_auction import auction_private_config
    if not message.from_user:
        return
    if message.from_user.id in _auction_drafts:
        await auction_private_config(message, bot)
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
        await message.reply(
            "第 2/6 步 · 奖品\n请发送奖品内容，例如：一个月会员",
            reply_markup=keyboards.lottery_back_kb("title"),
        )
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
        await message.reply(_lottery_summary(draft), reply_markup=keyboards.lottery_setup_kb())
        return
    await _send_lottery_step(bot, draft)


lottery_private_config = game_private_config


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
                .where(Lottery.creator_id == int(user_id), Lottery.hidden_at.is_(None))
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
                    select(func.count(LotteryEntry.id)).where(LotteryEntry.lottery_id == lottery.id)
                )
                or 0
            )
            cards.append((int(lottery.id), lottery.title or "", lottery.status, entries))
        await message.answer(
            f"🎁 <b>我创建的抽奖</b> · 第 {page} 页\n点击对应抽奖按钮查看详情、取消或编辑。",
            reply_markup=keyboards.lottery_history_list_kb(cards),
        )
        if page > 1 or has_next:
            await message.answer(
                "翻页",
                reply_markup=keyboards.lottery_history_pagination_kb(page, page > 1, has_next),
            )


async def _creator_lottery(session: AsyncSession, lottery_id: int, user_id: int) -> Lottery | None:
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
            lottery = await _creator_lottery(session, int(parts[2]), callback.from_user.id)
            if lottery is None or lottery.hidden_at is not None:
                await callback.answer("找不到这个抽奖。", show_alert=True)
                return
            group = await session.get(Group, lottery.group_id)
            entries = int(
                await session.scalar(
                    select(func.count(LotteryEntry.id)).where(LotteryEntry.lottery_id == lottery.id)
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
                reply_markup=keyboards.lottery_management_kb(lottery.id, lottery.status == "open"),
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
                "\n中奖者：" + "、".join(str(user_id) for user_id in winners)
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
            await _send_lottery_history_page(callback.message, callback.from_user.id, 1)
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
            await _send_lottery_history_page(callback.message, callback.from_user.id, 1)
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


async def _cancel_lottery(session: AsyncSession, lottery: Lottery | None, user_id: int) -> str:
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
            await session.scalars(select(LotteryEntry).where(LotteryEntry.lottery_id == lottery.id))
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
            await callback.answer("本群未开启积分系统，无法使用积分参与条件。", show_alert=True)
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
            draw_participant_count=(draft.draw_value if draft.draw_type == "participant" else None),
            claim_contact=draft.claim_contact,
            status="open",
            ends_at=(draft.draw_at if draft.draw_type == "time" else None),
        )
        session.add(lottery)
        await session.flush()
        lines = [
            f"🎁 <b>{escape(draft.title)}</b>",
            "──────────────",
            f"🏆 <b>奖品</b>　{escape(draft.prize)} × {draft.winners_count}",
            f"👥 <b>参与条件</b>　{_lottery_condition_text(draft.participation_type, draft.participation_value)}",
            f"⏰ <b>开奖时间</b>　{_lottery_draw_summary(draft)}",
        ]
        if draft.claim_contact:
            lines.append(f"📩 <b>领奖联系</b>　{escape(draft.claim_contact)}")
        participation_instruction = (
            f"在群内发送「{escape(draft.participation_value)}」即可自动报名（不区分大小写，首尾空格不影响）。"
            if draft.participation_type == "keyword"
            else "点击下方「参与抽奖」按钮报名。"
        )
        lines.extend(["──────────────", participation_instruction])
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
    await helpers.pin_message_safe(bot, group_id_value, sent.message_id)
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
        eligible, reason = await lottery_rules.eligible(session, lottery, callback.from_user.id)
        if not eligible:
            await callback.answer(reason, show_alert=True)
            return
        group = await repo.ensure_group(session, lottery.group_id)
        if await _feature_denied(session, group, "games"):
            await callback.answer("本群所属订阅未开通抽奖与竞拍。", show_alert=True)
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

