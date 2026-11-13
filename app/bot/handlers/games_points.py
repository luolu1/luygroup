"""Points, check-in and shop handlers."""

from __future__ import annotations

from aiogram import F
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message
from datetime import datetime
from html import escape
from sqlalchemy import func, or_, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from .games_shared import _feature_denied, _group_for_message, _need_group, _reply_feature_denied, router
from .. import helpers, keyboards
from ...db import session_scope
from ...models import DailyCheckin, Member, PointsTransaction, ShopItem, ShopOrder, utcnow
from ...services import repo


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
            await session.scalar(select(func.count(Member.id)).where(Member.group_id == group.id))
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
    return f"✅ 购买成功：<b>{escape(item.name)}</b>，消耗 {item.cost} 积分。\n" + (
        f"🎁 {escape(item.reward_text)}" if item.reward_text else "请联系管理员领取奖励。"
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

