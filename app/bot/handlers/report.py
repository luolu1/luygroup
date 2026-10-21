"""Member reports and cross-group report delivery."""
from __future__ import annotations

from html import escape

from aiogram import Bot, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.types import Message
from sqlalchemy import select

from ...config import is_global_admin
from ...db import session_scope
from ...entitlements import group_feature_allowed
from ...models import Report, ReportLink
from ...services import repo
from .. import helpers

router = Router(name="report")


def _message_link(message: Message) -> str:
    username = message.chat.username
    if username:
        return f"https://t.me/{username}/{message.message_id}"
    return ""


@router.message(Command("report", "jubao"))
async def cmd_report(message: Message, bot: Bot) -> None:
    if message.chat.type not in {"group", "supergroup"} or not message.from_user:
        await message.reply("请在群组中回复违规消息后使用 /report。")
        return
    reply = message.reply_to_message
    if reply is None:
        await message.reply(
            "请回复要举报的消息后发送 /report [原因]。\n"
            "例如：回复广告消息并发送 /report 发广告"
        )
        return
    if reply.from_user is None:
        await message.reply("无法举报匿名消息或频道消息。")
        return
    target = reply.from_user
    if target.id == message.from_user.id:
        await message.reply("不能举报自己。")
        return

    reason = " ".join(helpers.parse_args(message.text)).strip()[:500] or "未填写原因"
    excerpt = (reply.text or reply.caption or "")[:1500]
    link = _message_link(reply)

    async with session_scope() as session:
        group = await repo.ensure_group(
            session,
            message.chat.id,
            title=message.chat.title or "",
            username=message.chat.username or "",
            chat_type=message.chat.type,
        )
        if not group.report_enabled:
            await message.reply("本群暂未开启举报通报功能。")
            return
        if not await group_feature_allowed(session, group.id, "reports"):
            await message.reply("举报通报属于订阅版功能，请联系群主升级。")
            return
        if target.id == (await bot.me()).id:
            await message.reply("不能举报机器人。")
            return
        if is_global_admin(target.id) or await repo.get_admin(session, group.id, target.id):
            await message.reply("不能举报管理员。")
            return
        if await repo.is_whitelisted(session, group.id, target.id):
            await message.reply("该用户在免检名单中。")
            return

        report = Report(
            group_id=group.id,
            reporter_id=message.from_user.id,
            target_user_id=target.id,
            reason=reason,
            excerpt=excerpt,
            message_id=reply.message_id,
        )
        session.add(report)
        await session.flush()
        report_id = report.id

        destinations: set[int] = set()
        if group.report_chat_id:
            destinations.add(int(group.report_chat_id))
        links = (
            await session.scalars(
                select(ReportLink).where(ReportLink.group_id == group.id)
            )
        ).all()
        for item in links:
            subscriber = await repo.get_group(session, item.subscriber_id)
            if subscriber and subscriber.report_receive:
                destinations.add(int(item.subscriber_id))
        destinations.discard(group.id)

        detail = (
            f"📮 <b>群组举报 #{report_id}</b>\n"
            f"来源群：{escape(group.title or str(group.id))}\n"
            f"举报人：{message.from_user.mention_html()}\n"
            f"被举报人：{target.mention_html()}\n"
            f"原因：{escape(reason)}\n"
        )
        if excerpt:
            detail += f"内容：{escape(excerpt[:800])}\n"
        if link:
            detail += f"消息链接：{link}\n"
        detail += "\n请在来源群中处理，管理员可使用 /ban 或 /warn。"

        await repo.add_log(
            session,
            group.id,
            None,
            "report",
            target.id,
            f"#{report_id} {reason}",
        )

    for dest in destinations:
        try:
            await bot.send_message(dest, detail[:4000], disable_web_page_preview=True)
        except TelegramAPIError:
            continue
    try:
        await bot.send_message(group.id, f"✅ 已收到举报 #{report_id}，管理员会尽快处理。")
    except TelegramAPIError:
        pass


@router.message(Command("reports"))
async def cmd_reports(message: Message, bot: Bot) -> None:
    if message.chat.type not in {"group", "supergroup"}:
        await message.reply("请在群组中使用。")
        return
    async with session_scope() as session:
        group = await repo.ensure_group(
            session,
            message.chat.id,
            title=message.chat.title or "",
            username=message.chat.username or "",
            chat_type=message.chat.type,
        )
        actor = message.from_user.id if message.from_user else 0
        if not await helpers.has_permission(bot, session, group.id, actor, "info"):
            await message.reply("⛔ 查看举报记录需要管理员权限。")
            return
        reports = (
            await session.scalars(
                select(Report)
                .where(Report.group_id == group.id)
                .order_by(Report.id.desc())
                .limit(10)
            )
        ).all()
    if not reports:
        await message.reply("暂无举报记录。")
        return
    lines = ["📮 <b>最近举报</b>"]
    for item in reports:
        lines.append(
            f"· #{item.id} · {repo.dt(item.created_at)} · 被举报 <code>{item.target_user_id}</code>\n"
            f"  {escape(item.reason or '未填写原因')}"
        )
    await message.reply("\n".join(lines))
