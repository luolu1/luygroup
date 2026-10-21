"""Administrative commands."""
from __future__ import annotations

import json
import re
from html import escape

from aiogram import Bot, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.types import Message
from sqlalchemy import delete, select

from ...config import is_global_admin
from ...db import session_scope
from ...entitlements import group_feature_allowed
from ...models import (
    Admin,
    AdminLog,
    GlobalBlacklist,
    Keyword,
    RequiredChannel,
    ReportLink,
    CrossLink,
    Warning,
    Whitelist,
)
from ...services import repo
from .. import helpers

router = Router(name="admin")

ACTIONS = {"delete", "warn", "mute", "kick", "ban"}
WARN_ACTIONS = {"mute", "kick", "ban"}


async def _reply_error(message: Message, text: str) -> None:
    await message.reply(text)


_DURATION_RE = re.compile(
    r"^(\d+)\s*(s|m|h|d|w|秒|分钟|分|小时|时|天|日|周|星期)?$",
    re.IGNORECASE,
)


def _duration_minutes(raw: str) -> int | None:
    """Parse ``10``, ``10m``, ``1h``, ``2d`` or Chinese duration tokens."""
    match = _DURATION_RE.match((raw or "").strip())
    if not match:
        return None
    amount = int(match.group(1))
    unit = (match.group(2) or "m").lower()
    if unit in {"s", "秒"}:
        minutes = max(1, (amount + 59) // 60)
    elif unit in {"h", "小时", "时"}:
        minutes = amount * 60
    elif unit in {"d", "天", "日"}:
        minutes = amount * 1440
    elif unit in {"w", "周", "星期"}:
        minutes = amount * 10080
    else:
        minutes = amount
    return max(1, min(minutes, 100000))


def _admin_help() -> str:
    return (
        "用法：\n"
        "<code>/ban [原因]</code> 回复目标消息\n"
        "<code>/jy 用户ID 10m [原因]</code> 手动禁言（也支持 1h / 2d）\n"
        "<code>/mute [分钟] [原因]</code>\n"
        "<code>/warn [原因]</code>\n"
        "<code>/wladd 回复用户 [备注]</code>\n"
        "<code>/addkeyword 关键词 [delete|warn|mute|kick|ban]</code>\n"
        "<code>/addreply 关键词 =&gt; 回复内容</code>"
    )


# ---------------------------------------------------------------------------
# Moderation actions
# ---------------------------------------------------------------------------
@router.message(Command("ban"))
async def cmd_ban(message: Message, bot: Bot) -> None:
    async with session_scope() as session:
        ctx = await helpers.admin_context(bot, session, message, "ban")
        if not ctx:
            return
        group, actor = ctx
        args = helpers.parse_args(message.text)
        target = await helpers.resolve_target(bot, message, args)
        if not target:
            await _reply_error(message, "请回复要封禁的用户，或发送 /ban 用户ID 原因。")
            return
        allowed, why = await helpers.can_moderate_target(bot, session, group.id, target.user_id)
        if not allowed:
            await _reply_error(message, why)
            return
        reason = " ".join(args[1:]) if not message.reply_to_message else " ".join(args)
        try:
            result = await helpers.ban_user(
                bot, session, group, target, reason=reason, admin_id=actor
            )
            if group.crossban_enabled:
                linked = await helpers.apply_crossban(
                    bot, session, group, target, reason=reason, admin_id=actor
                )
                if linked:
                    result += f"\n🔗 已联动封禁：{escape('、'.join(linked))}"
        except TelegramAPIError as exc:
            await _reply_error(message, f"封禁失败：{exc}")
            return
        await helpers.send_group_log(
            bot, group, f"🔨 {target.label} 被 {actor} 封禁\n原因：{reason or '-'}"
        )
        await message.reply(result)


@router.message(Command("unban"))
async def cmd_unban(message: Message, bot: Bot) -> None:
    async with session_scope() as session:
        ctx = await helpers.admin_context(bot, session, message, "ban")
        if not ctx:
            return
        group, actor = ctx
        target = await helpers.resolve_target(bot, message, helpers.parse_args(message.text))
        if not target:
            await _reply_error(message, _admin_help())
            return
        try:
            result = await helpers.unban_user(bot, session, group, target, admin_id=actor)
        except TelegramAPIError as exc:
            await _reply_error(message, f"解封失败：{exc}")
            return
        await helpers.send_group_log(bot, group, f"🔓 {target.label} 被 {actor} 解封")
        await message.reply(result)


@router.message(Command("iunban"))
async def cmd_iunban(message: Message, bot: Bot) -> None:
    """Unban and lift restrictions across the current tenant's groups."""
    async with session_scope() as session:
        ctx = await helpers.admin_context(bot, session, message, "ban")
        if not ctx:
            return
        group, actor = ctx
        target = await helpers.resolve_target(bot, message, helpers.parse_args(message.text))
        if not target:
            await _reply_error(message, "请回复要全局解封的用户，或 /iunban 用户ID。")
            return
        allowed, why = await helpers.can_moderate_target(bot, session, group.id, target.user_id)
        if not allowed:
            await _reply_error(message, why)
            return

        groups = await helpers.tenant_groups(session, group, actor_id=actor)
        unlocked: list[str] = []
        failed: list[str] = []
        for target_group in groups[:200]:
            member = await repo.get_member(session, target_group.id, target.user_id)
            restricted = bool(
                member
                and (
                    member.is_banned
                    or member.is_muted
                    or member.mute_on_rejoin
                )
            )
            if not restricted:
                continue
            title = target_group.title or str(target_group.id)
            try:
                await bot.unban_chat_member(
                    int(target_group.id), int(target.user_id), only_if_banned=True
                )
            except TelegramAPIError:
                failed.append(title)
                continue
            if member is not None:
                member.is_banned = False
                member.is_muted = False
                member.muted_until = None
                member.mute_on_rejoin = False
            try:
                await helpers.lift_restrictions(
                    bot, int(target_group.id), int(target.user_id)
                )
            except TelegramAPIError:
                pass
            await repo.add_log(
                session,
                target_group.id,
                actor,
                "global_unban",
                target.user_id,
                "全局解封并解除限制",
            )
            unlocked.append(title)

        blacklisted = await session.scalar(
            select(GlobalBlacklist.id)
            .where(GlobalBlacklist.user_id == int(target.user_id))
            .limit(1)
        )

    if not unlocked and not failed:
        await message.reply("没有找到该用户在可管理群组中的封禁或禁言记录。")
        return
    lines = [f"🔓 已处理 {len(unlocked)} 个群组："]
    lines.extend(f"· {escape(name)}" for name in unlocked[:10])
    if len(unlocked) > 10:
        lines.append(f"…… 其余 {len(unlocked) - 10} 个群组")
    if failed:
        lines.append("\n⚠️ 以下群组失败（机器人权限不足）：")
        lines.extend(f"· {escape(name)}" for name in failed[:10])
    if blacklisted is not None:
        lines.append(
            "\n⚠️ 该用户仍在全局黑名单中，黑名单策略可能会再次限制其入群。"
        )
    await message.reply("\n".join(lines))


@router.message(Command("kick"))
async def cmd_kick(message: Message, bot: Bot) -> None:
    async with session_scope() as session:
        ctx = await helpers.admin_context(bot, session, message, "ban")
        if not ctx:
            return
        group, actor = ctx
        args = helpers.parse_args(message.text)
        target = await helpers.resolve_target(bot, message, args)
        if not target:
            await _reply_error(message, "请回复要移出的用户。")
            return
        allowed, why = await helpers.can_moderate_target(bot, session, group.id, target.user_id)
        if not allowed:
            await _reply_error(message, why)
            return
        reason = " ".join(args)
        try:
            result = await helpers.kick_user(
                bot, session, group, target, reason=reason, admin_id=actor
            )
        except TelegramAPIError as exc:
            await _reply_error(message, f"移出失败：{exc}")
            return
        await helpers.send_group_log(bot, group, f"👢 {target.label} 被 {actor} 移出\n原因：{reason or '-'}")
        await message.reply(result)


@router.message(Command("mute", "jy"))
async def cmd_mute(message: Message, bot: Bot) -> None:
    async with session_scope() as session:
        ctx = await helpers.admin_context(bot, session, message, "mute")
        if not ctx:
            return
        group, actor = ctx
        args = helpers.parse_args(message.text)
        target = await helpers.resolve_target(bot, message, args)
        if not target:
            await _reply_error(
                message,
                "请回复要禁言的用户，或 /jy 用户ID 10m [原因]。",
            )
            return
        allowed, why = await helpers.can_moderate_target(bot, session, group.id, target.user_id)
        if not allowed:
            await _reply_error(message, why)
            return
        minutes = group.mute_minutes or 60
        if message.reply_to_message:
            reason_parts = args
            if args:
                parsed = _duration_minutes(args[0])
                if parsed is not None:
                    minutes = parsed
                    reason_parts = args[1:]
        else:
            reason_parts = args[1:]
            if len(args) > 1:
                parsed = _duration_minutes(args[1])
                if parsed is not None:
                    minutes = parsed
                    reason_parts = args[2:]
        reason = " ".join(reason_parts)
        try:
            result = await helpers.mute_user(
                bot, session, group, target, minutes, reason=reason, admin_id=actor
            )
        except TelegramAPIError as exc:
            await _reply_error(message, f"禁言失败：{exc}")
            return
        await helpers.send_group_log(
            bot, group, f"🔇 {target.label} 被 {actor} 禁言 {minutes} 分钟\n原因：{reason or '-'}"
        )
        await message.reply(result)


@router.message(Command("unmute", "jcjy"))
async def cmd_unmute(message: Message, bot: Bot) -> None:
    async with session_scope() as session:
        ctx = await helpers.admin_context(bot, session, message, "mute")
        if not ctx:
            return
        group, actor = ctx
        target = await helpers.resolve_target(bot, message, helpers.parse_args(message.text))
        if not target:
            await _reply_error(message, "请回复要解除禁言的用户。")
            return
        try:
            result = await helpers.unmute_user(bot, session, group, target, admin_id=actor)
        except TelegramAPIError as exc:
            await _reply_error(message, f"解除禁言失败：{exc}")
            return
        await helpers.send_group_log(bot, group, f"🔊 {target.label} 被 {actor} 解除禁言")
        await message.reply(result)


@router.message(Command("warn"))
async def cmd_warn(message: Message, bot: Bot) -> None:
    async with session_scope() as session:
        ctx = await helpers.admin_context(bot, session, message, "warn")
        if not ctx:
            return
        group, actor = ctx
        args = helpers.parse_args(message.text)
        target = await helpers.resolve_target(bot, message, args)
        if not target:
            await _reply_error(message, "请回复要警告的用户。")
            return
        reason = " ".join(args) if message.reply_to_message else " ".join(args[1:])
        count, result = await helpers.warn_user(
            bot, session, group, target, reason or "未填写原因", admin_id=actor
        )
        await helpers.send_group_log(
            bot, group, f"⚠️ {target.label} 被 {actor} 警告（{count}）\n原因：{reason or '-'}"
        )
        await message.reply(result)


@router.message(Command("unwarn"))
async def cmd_unwarn(message: Message, bot: Bot) -> None:
    async with session_scope() as session:
        ctx = await helpers.admin_context(bot, session, message, "warn")
        if not ctx:
            return
        group, actor = ctx
        target = await helpers.resolve_target(bot, message, helpers.parse_args(message.text))
        if not target:
            await _reply_error(message, "请回复要撤销警告的用户。")
            return
        warning = await session.scalar(
            select(Warning)
            .where(
                Warning.group_id == group.id,
                Warning.user_id == target.user_id,
                Warning.active.is_(True),
            )
            .order_by(Warning.created_at.desc())
            .limit(1)
        )
        if not warning:
            await message.reply("该用户没有有效警告。")
            return
        warning.active = False
        await repo.add_log(session, group.id, actor, "unwarn", target.user_id, "撤销 1 条警告")
        await message.reply(f"已撤销 {target.mention} 的 1 条警告。")


@router.message(Command("resetwarns"))
async def cmd_resetwarns(message: Message, bot: Bot) -> None:
    async with session_scope() as session:
        ctx = await helpers.admin_context(bot, session, message, "warn")
        if not ctx:
            return
        group, actor = ctx
        target = await helpers.resolve_target(bot, message, helpers.parse_args(message.text))
        if not target:
            await _reply_error(message, "请回复要清空警告的用户。")
            return
        await session.execute(
            delete(Warning).where(Warning.group_id == group.id, Warning.user_id == target.user_id)
        )
        await repo.add_log(session, group.id, actor, "resetwarns", target.user_id, "")
        await message.reply(f"已清空 {target.mention} 的全部警告记录。")


@router.message(Command("del", "delete"))
async def cmd_delete(message: Message, bot: Bot) -> None:
    async with session_scope() as session:
        ctx = await helpers.admin_context(bot, session, message, "delete")
        if not ctx:
            return
        group, actor = ctx
        reply = message.reply_to_message
        if not reply:
            await _reply_error(message, "请回复要删除的消息。")
            return
        deleted = await helpers.delete_message_safe(bot, group.id, reply.message_id)
        if deleted:
            await repo.add_log(session, group.id, actor, "delete", reply.from_user.id if reply.from_user else None, "删除消息")
            await repo.bump_stat(session, group.id, "deletes")
            try:
                await message.delete()
            except TelegramAPIError:
                pass
        else:
            await message.reply("删除失败：请检查机器人是否有删除消息权限。")


# ---------------------------------------------------------------------------
# Whitelist / keywords
# ---------------------------------------------------------------------------
@router.message(Command("whitelist", "wl", "wladd"))
async def cmd_whitelist(message: Message, bot: Bot) -> None:
    async with session_scope() as session:
        ctx = await helpers.admin_context(bot, session, message, "warn")
        if not ctx:
            return
        group, actor = ctx
        args = helpers.parse_args(message.text)
        target = await helpers.resolve_target(bot, message, args)
        if not target:
            await _reply_error(message, "请回复要加入白名单的用户。")
            return
        existing = await session.scalar(
            select(Whitelist).where(
                Whitelist.group_id == group.id, Whitelist.user_id == target.user_id
            )
        )
        if existing:
            await message.reply("该用户已在白名单中。")
            return
        note = " ".join(args[1:]) if not message.reply_to_message else " ".join(args)
        session.add(
            Whitelist(group_id=group.id, user_id=target.user_id, note=note[:255], added_by=actor)
        )
        await repo.ensure_member(
            session, group.id, target.user_id, username=target.username, first_name=target.first_name
        )
        await repo.add_log(session, group.id, actor, "whitelist", target.user_id, note)
        await message.reply(f"✅ 已将 {target.mention} 加入免检白名单。")


@router.message(Command("unwhitelist", "unwl", "wlrm"))
async def cmd_unwhitelist(message: Message, bot: Bot) -> None:
    async with session_scope() as session:
        ctx = await helpers.admin_context(bot, session, message, "warn")
        if not ctx:
            return
        group, actor = ctx
        target = await helpers.resolve_target(bot, message, helpers.parse_args(message.text))
        if not target:
            await _reply_error(message, "请回复要移出白名单的用户。")
            return
        entry = await session.scalar(
            select(Whitelist).where(
                Whitelist.group_id == group.id, Whitelist.user_id == target.user_id
            )
        )
        if not entry:
            await message.reply("该用户不在白名单中。")
            return
        await session.delete(entry)
        await repo.add_log(session, group.id, actor, "unwhitelist", target.user_id, "")
        await message.reply(f"已移出白名单：{target.mention}")


@router.message(Command("addkeyword", "addkw"))
async def cmd_addkeyword(message: Message, bot: Bot) -> None:
    async with session_scope() as session:
        ctx = await helpers.admin_context(bot, session, message, "settings")
        if not ctx:
            return
        group, actor = ctx
        args = helpers.parse_args(message.text)
        if not args:
            await _reply_error(
                message,
                "用法：/addkeyword 关键词 [delete|warn|mute|kick|ban]\n"
                "自动回复：/addreply 关键词 => 回复内容\n正则请以 re: 开头。",
            )
            return
        action = "delete"
        if len(args) >= 2 and args[-1] in ACTIONS:
            action = args[-1]
            args = args[:-1]
        pattern = " ".join(args).strip()
        is_regex = pattern.startswith("re:")
        if is_regex:
            pattern = pattern[3:].strip()
            try:
                re.compile(pattern)
            except re.error as exc:
                await _reply_error(message, f"正则表达式无效：{exc}")
                return
        if not pattern:
            await _reply_error(message, "关键词不能为空。")
            return
        session.add(
            Keyword(
                group_id=group.id,
                pattern=pattern[:255],
                is_regex=is_regex,
                action=action,
                created_by=actor,
            )
        )
        await repo.add_log(session, group.id, actor, "addkeyword", None, f"{pattern} -> {action}")
        await message.reply(f"✅ 已添加关键词：<code>{escape(pattern)}</code>（{action}）")


@router.message(Command("addreply", "addkwreply"))
async def cmd_addreply(message: Message, bot: Bot) -> None:
    """Add a keyword that sends a canned reply instead of punishing."""
    async with session_scope() as session:
        ctx = await helpers.admin_context(bot, session, message, "settings")
        if not ctx:
            return
        group, actor = ctx
        raw = (message.text or "").split(maxsplit=1)
        body = raw[1].strip() if len(raw) > 1 else ""
        if "=>" not in body:
            await _reply_error(message, "用法：/addreply 关键词 => 回复内容\n正则请以 re: 开头。")
            return
        pattern, reply_text = (part.strip() for part in body.split("=>", 1))
        is_regex = pattern.startswith("re:")
        if is_regex:
            pattern = pattern[3:].strip()
            try:
                re.compile(pattern)
            except re.error as exc:
                await _reply_error(message, f"正则表达式无效：{exc}")
                return
        if not pattern or not reply_text:
            await _reply_error(message, "关键词和回复内容都不能为空。")
            return
        session.add(
            Keyword(
                group_id=group.id,
                pattern=pattern[:255],
                is_regex=is_regex,
                action="reply",
                reply_text=reply_text[:4000],
                created_by=actor,
            )
        )
        await repo.add_log(
            session, group.id, actor, "addkeyword", None, f"{pattern} -> reply"
        )
        await message.reply(
            f"✅ 已添加自动回复关键词：<code>{escape(pattern)}</code>"
        )


@router.message(Command("keywords", "kw"))
async def cmd_keywords(message: Message, bot: Bot) -> None:
    async with session_scope() as session:
        ctx = await helpers.admin_context(bot, session, message, "settings")
        if not ctx:
            return
        group, _actor = ctx
        result = await session.scalars(
            select(Keyword).where(Keyword.group_id == group.id).order_by(Keyword.id.desc()).limit(60)
        )
        keywords = list(result.all())
    if not keywords:
        await message.reply("本群暂无自定义关键词。使用 /addkeyword 添加。")
        return
    lines = ["🔑 <b>本群关键词</b>"]
    for kw in keywords:
        flag = "正则" if kw.is_regex else "文本"
        lines.append(
            f"<code>{kw.id}</code> · {escape(kw.pattern)} · {flag} · {kw.action}"
            + (f" · 回复：{escape(kw.reply_text[:60])}" if kw.action == "reply" else "")
            + ("" if kw.enabled else " · 已停用")
        )
    lines.append("\n删除：/delkeyword 编号")
    await message.reply("\n".join(lines))


@router.message(Command("delkeyword", "delkw"))
async def cmd_delkeyword(message: Message, bot: Bot) -> None:
    async with session_scope() as session:
        ctx = await helpers.admin_context(bot, session, message, "settings")
        if not ctx:
            return
        group, actor = ctx
        args = helpers.parse_args(message.text)
        if not args:
            await _reply_error(message, "用法：/delkeyword 编号")
            return
        query = select(Keyword).where(Keyword.group_id == group.id)
        if args[0].isdigit():
            query = query.where(Keyword.id == int(args[0]))
        else:
            query = query.where(Keyword.pattern == " ".join(args))
        keyword = await session.scalar(query)
        if not keyword:
            await message.reply("未找到该关键词。")
            return
        await session.delete(keyword)
        await repo.add_log(session, group.id, actor, "delkeyword", None, keyword.pattern)
        await message.reply(f"已删除关键词：<code>{escape(keyword.pattern)}</code>")


# ---------------------------------------------------------------------------
# Group settings
# ---------------------------------------------------------------------------
@router.message(Command("settings"))
async def cmd_settings(message: Message, bot: Bot) -> None:
    async with session_scope() as session:
        ctx = await helpers.admin_context(bot, session, message, "settings")
        if not ctx:
            return
        group, _actor = ctx
    text = (
        f"⚙️ <b>{escape(group.title or '本群')} 设置</b>\n"
        f"反广告：{'开' if group.anti_ad_enabled else '关'} · 反刷屏：{'开' if group.anti_spam_enabled else '关'}\n"
        f"AI 广告：{'开' if group.ai_ad_enabled else '关'} · AI 助理：{'开' if group.ai_assistant_enabled else '关'} · AI 闲聊：{'开' if group.ai_chat_enabled else '关'}\n"
        f"警告阈值：{group.warn_limit} → {group.warn_action} · 默认禁言：{group.mute_minutes} 分钟\n"
        f"刷屏限制：{group.flood_limit} 条 / {group.flood_window} 秒 → {group.flood_action}\n"
        f"进群验证：{'开' if group.verify_enabled else '关'} · 必填用户名：{'是' if group.require_username else '否'} · 邀请核销：{'开' if group.invite_redemption else '否'}\n"
        f"跨群封禁：{'开' if group.crossban_enabled else '关'}（{group.crossban_mode}）\n"
        f"通报：{'开' if group.report_enabled else '关'} · 接收通报：{'开' if group.report_receive else '关'}\n"
        f"积分：{'开' if group.points_enabled else '关'} · 每条 +{group.points_per_message} · 冷却 {group.points_cooldown}s\n"
        f"AI 日额度：{group.ai_daily_quota} 请求 / {group.sample_daily_quota} 样本\n\n"
        "完整设置请使用 /console 打开 Web 控制台。"
    )
    await message.reply(text)


@router.message(Command("setrules"))
async def cmd_setrules(message: Message, bot: Bot) -> None:
    async with session_scope() as session:
        ctx = await helpers.admin_context(bot, session, message, "settings")
        if not ctx:
            return
        group, actor = ctx
        args = helpers.parse_args(message.text)
        text = " ".join(args).strip()
        if not text and message.reply_to_message:
            text = message.reply_to_message.text or ""
        if not text:
            await _reply_error(message, "用法：/setrules 群规内容（也可回复一条消息）")
            return
        group.rules_text = text[:4000]
        await repo.add_log(session, group.id, actor, "setrules", None, text[:200])
    await message.reply("✅ 群规已更新。发送 /rules 查看。")


@router.message(Command("setwarn"))
async def cmd_setwarn(message: Message, bot: Bot) -> None:
    async with session_scope() as session:
        ctx = await helpers.admin_context(bot, session, message, "settings")
        if not ctx:
            return
        group, actor = ctx
        args = helpers.parse_args(message.text)
        if len(args) < 2 or not args[0].isdigit() or args[1] not in WARN_ACTIONS:
            await _reply_error(message, "用法：/setwarn 次数 mute|kick|ban")
            return
        group.warn_limit = max(0, int(args[0]))
        group.warn_action = args[1]
        await repo.add_log(session, group.id, actor, "setwarn", None, " ".join(args))
    await message.reply(f"✅ 警告阈值已设为 {group.warn_limit} 次，触发动作：{group.warn_action}")


@router.message(Command("setflood"))
async def cmd_setflood(message: Message, bot: Bot) -> None:
    async with session_scope() as session:
        ctx = await helpers.admin_context(bot, session, message, "settings")
        if not ctx:
            return
        group, actor = ctx
        args = helpers.parse_args(message.text)
        if len(args) < 3 or not args[0].isdigit() or not args[1].isdigit() or args[2] not in WARN_ACTIONS:
            await _reply_error(message, "用法：/setflood 条数 秒数 mute|kick|ban")
            return
        group.flood_limit = max(1, int(args[0]))
        group.flood_window = max(1, int(args[1]))
        group.flood_action = args[2]
        await repo.add_log(session, group.id, actor, "setflood", None, " ".join(args))
    await message.reply(
        f"✅ 刷屏限制：{group.flood_window} 秒内超过 {group.flood_limit} 条 → {group.flood_action}"
    )


@router.message(Command("setmute"))
async def cmd_setmute(message: Message, bot: Bot) -> None:
    async with session_scope() as session:
        ctx = await helpers.admin_context(bot, session, message, "settings")
        if not ctx:
            return
        group, actor = ctx
        args = helpers.parse_args(message.text)
        if not args or not args[0].isdigit():
            await _reply_error(message, "用法：/setmute 分钟")
            return
        group.mute_minutes = max(1, min(int(args[0]), 100000))
        await repo.add_log(session, group.id, actor, "setmute", None, str(group.mute_minutes))
    await message.reply(f"✅ 默认禁言时长：{group.mute_minutes} 分钟")


@router.message(Command("setwelcome"))
async def cmd_setwelcome(message: Message, bot: Bot) -> None:
    async with session_scope() as session:
        ctx = await helpers.admin_context(bot, session, message, "settings")
        if not ctx:
            return
        group, actor = ctx
        args = helpers.parse_args(message.text)
        text = " ".join(args).strip()
        if not text and message.reply_to_message:
            text = message.reply_to_message.text or ""
        if not text:
            await _reply_error(
                message, "用法：/setwelcome 欢迎语\n支持占位符 {name} {group} {id}"
            )
            return
        group.welcome_text = text[:4000]
        await repo.add_log(session, group.id, actor, "setwelcome", None, text[:200])
    await message.reply("✅ 欢迎语已更新。发送 /setverify on 开启进群验证后，验证通过会自动欢迎。")


@router.message(Command("setverify"))
async def cmd_setverify(message: Message, bot: Bot) -> None:
    async with session_scope() as session:
        ctx = await helpers.admin_context(bot, session, message, "settings")
        if not ctx:
            return
        group, actor = ctx
        args = helpers.parse_args(message.text)
        if not args or args[0].lower() not in {"on", "off"}:
            await _reply_error(message, "用法：/setverify on|off")
            return
        group.verify_enabled = args[0].lower() == "on"
        await repo.add_log(session, group.id, actor, "setverify", None, args[0])
    await message.reply(f"✅ 进群验证已{'开启' if group.verify_enabled else '关闭'}。")


@router.message(Command("setai"))
async def cmd_setai(message: Message, bot: Bot) -> None:
    async with session_scope() as session:
        ctx = await helpers.admin_context(bot, session, message, "settings")
        if not ctx:
            return
        group, actor = ctx
        args = helpers.parse_args(message.text)
        if len(args) < 2 or args[1].lower() not in {"on", "off"}:
            await _reply_error(message, "用法：/setai ad|assistant|chat on|off")
            return
        kind, value = args[0].lower(), args[1].lower() == "on"
        mapping = {"ad": "ai_ad_enabled", "assistant": "ai_assistant_enabled", "chat": "ai_chat_enabled"}
        if kind not in mapping:
            await _reply_error(message, "类型必须是 ad、assistant 或 chat。")
            return
        setattr(group, mapping[kind], value)
        await repo.add_log(session, group.id, actor, "setai", None, f"{kind}={value}")
    await message.reply(f"✅ AI {kind} 已{'开启' if value else '关闭'}。")


@router.message(Command("setquota"))
async def cmd_setquota(message: Message, bot: Bot) -> None:
    async with session_scope() as session:
        ctx = await helpers.admin_context(bot, session, message, "settings")
        if not ctx:
            return
        group, actor = ctx
        args = helpers.parse_args(message.text)
        if len(args) < 2 or not args[0].isdigit() or not args[1].isdigit():
            await _reply_error(message, "用法：/setquota AI请求数 样本拦截数")
            return
        group.ai_daily_quota = max(0, int(args[0]))
        group.sample_daily_quota = max(0, int(args[1]))
        await repo.add_log(session, group.id, actor, "setquota", None, " ".join(args))
    await message.reply(
        f"✅ 每日额度：AI 请求 {group.ai_daily_quota} · 样本 {group.sample_daily_quota}"
    )


@router.message(Command("setcrossban"))
async def cmd_setcrossban(message: Message, bot: Bot) -> None:
    async with session_scope() as session:
        ctx = await helpers.admin_context(bot, session, message, "settings")
        if not ctx:
            return
        group, actor = ctx
        args = helpers.parse_args(message.text)
        if not args:
            await _reply_error(message, "用法：/setcrossban on|off|own|shared")
            return
        value = args[0].lower()
        if value == "off":
            group.crossban_enabled = False
        elif value == "on":
            group.crossban_enabled = True
        elif value in {"own", "shared"}:
            group.crossban_enabled = True
            group.crossban_mode = value
        else:
            await _reply_error(message, "参数必须是 on、off、own 或 shared。")
            return
        await repo.add_log(session, group.id, actor, "setcrossban", None, value)
    mode = group.crossban_mode
    await message.reply(
        f"✅ 跨群封禁已{'开启' if group.crossban_enabled else '关闭'}"
        + (f"（{'所有已开启跨群封禁的群组' if mode == 'own' else '共享联动群'}）" if group.crossban_enabled else "")
    )


@router.message(Command("setreport"))
async def cmd_setreport(message: Message, bot: Bot) -> None:
    async with session_scope() as session:
        ctx = await helpers.admin_context(bot, session, message, "settings")
        if not ctx:
            return
        group, actor = ctx
        args = helpers.parse_args(message.text)
        if not args or args[0].lower() not in {"on", "off"}:
            await _reply_error(message, "用法：/setreport on|off")
            return
        group.report_enabled = args[0].lower() == "on"
        await repo.add_log(session, group.id, actor, "setreport", None, args[0])
    await message.reply(f"✅ 通报功能已{'开启' if group.report_enabled else '关闭'}。")


@router.message(Command("logs"))
async def cmd_logs(message: Message, bot: Bot) -> None:
    async with session_scope() as session:
        ctx = await helpers.admin_context(bot, session, message, "settings")
        if not ctx:
            return
        group, _actor = ctx
        args = helpers.parse_args(message.text)
        limit = int(args[0]) if args and args[0].isdigit() else 15
        limit = max(1, min(limit, 50))
        logs = (
            await session.scalars(
                select(AdminLog)
                .where(AdminLog.group_id == group.id)
                .order_by(AdminLog.id.desc())
                .limit(limit)
            )
        ).all()
    if not logs:
        await message.reply("暂无管理日志。")
        return
    lines = ["📋 <b>最近管理日志</b>"]
    for log in logs:
        lines.append(
            f"· {repo.dt(log.created_at)} · {log.action} · 管理员 <code>{log.admin_id or '-'}</code>"
            + (f" → <code>{log.target_id}</code>" if log.target_id else "")
            + (f" · {escape(log.detail[:80])}" if log.detail else "")
        )
    lines.append("\n更多日志请访问 Web 面板。")
    await message.reply("\n".join(lines))


# ---------------------------------------------------------------------------
# Required channels
# ---------------------------------------------------------------------------
@router.message(Command("channels"))
async def cmd_channels(message: Message, bot: Bot) -> None:
    async with session_scope() as session:
        ctx = await helpers.admin_context(bot, session, message, "settings")
        if not ctx:
            return
        group, _actor = ctx
        channels = (
            await session.scalars(
                select(RequiredChannel).where(RequiredChannel.group_id == group.id)
            )
        ).all()
    if not channels:
        await message.reply("本群暂未设置必关频道。使用 /addchannel @频道 添加。")
        return
    lines = ["📢 <b>必关频道</b>"]
    for ch in channels:
        lines.append(f"· {escape(ch.title or str(ch.channel_id))} · {ch.invite_link or ('@' + ch.username if ch.username else '')}")
    lines.append("\n删除：/delchannel @频道")
    await message.reply("\n".join(lines))


@router.message(Command("addchannel"))
async def cmd_addchannel(message: Message, bot: Bot) -> None:
    async with session_scope() as session:
        ctx = await helpers.admin_context(bot, session, message, "settings")
        if not ctx:
            return
        group, actor = ctx
        args = helpers.parse_args(message.text)
        if not args:
            await _reply_error(message, "用法：/addchannel @频道 [邀请链接]")
            return
        raw = args[0]
        link = args[1] if len(args) > 1 and args[1].startswith(("http://", "https://")) else ""
        try:
            chat = await bot.get_chat(int(raw) if raw.lstrip("-").isdigit() else raw)
        except TelegramAPIError as exc:
            await _reply_error(message, f"无法获取频道：{exc}")
            return
        if chat.type != "channel":
            await _reply_error(message, "目标不是频道。")
            return
        if not link:
            if chat.username:
                link = f"https://t.me/{chat.username}"
            else:
                try:
                    link = await bot.export_chat_invite_link(chat.id)
                except TelegramAPIError:
                    link = ""
        existing = await session.scalar(
            select(RequiredChannel).where(
                RequiredChannel.group_id == group.id, RequiredChannel.channel_id == chat.id
            )
        )
        if existing:
            existing.title = chat.title or ""
            existing.username = chat.username or ""
            existing.invite_link = link
        else:
            session.add(
                RequiredChannel(
                    group_id=group.id,
                    channel_id=chat.id,
                    title=chat.title or "",
                    username=chat.username or "",
                    invite_link=link,
                )
            )
        await repo.add_log(session, group.id, actor, "addchannel", chat.id, chat.title or "")
        await message.reply(f"✅ 已添加必关频道：{escape(chat.title or str(chat.id))}")


@router.message(Command("delchannel"))
async def cmd_delchannel(message: Message, bot: Bot) -> None:
    async with session_scope() as session:
        ctx = await helpers.admin_context(bot, session, message, "settings")
        if not ctx:
            return
        group, actor = ctx
        args = helpers.parse_args(message.text)
        if not args:
            await _reply_error(message, "用法：/delchannel @频道 或 频道ID")
            return
        raw = args[0]
        channel = None
        if raw.lstrip("-").isdigit():
            channel = await session.scalar(
                select(RequiredChannel).where(
                    RequiredChannel.group_id == group.id,
                    RequiredChannel.channel_id == int(raw),
                )
            )
        else:
            username = raw.lstrip("@")
            channel = await session.scalar(
                select(RequiredChannel).where(
                    RequiredChannel.group_id == group.id,
                    RequiredChannel.username == username,
                )
            )
        if not channel:
            await message.reply("未找到该必关频道。")
            return
        await session.delete(channel)
        await repo.add_log(session, group.id, actor, "delchannel", channel.channel_id, "")
        await message.reply("✅ 已删除必关频道。")


# ---------------------------------------------------------------------------
# Assistant admins
# ---------------------------------------------------------------------------
@router.message(Command("assistant", "assistants"))
async def cmd_assistant(message: Message, bot: Bot) -> None:
    async with session_scope() as session:
        ctx = await helpers.admin_context(bot, session, message, "settings")
        if not ctx:
            return
        group, actor = ctx
        if not await group_feature_allowed(session, group.id, "admins"):
            await _reply_error(message, "协助管理员属于订阅版功能，当前群组未解锁。")
            return
        args = helpers.parse_args(message.text)
        if not args:
            await _reply_error(
                message,
                "用法：/assistant add|del|list [用户]\n权限：ban,mute,warn,delete,info,settings",
            )
            return
        sub = args[0].lower()
        if sub == "list":
            admins = (
                await session.scalars(select(Admin).where(Admin.group_id == group.id))
            ).all()
            if not admins:
                await message.reply("本群暂未配置协助管理员。")
                return
            lines = ["👮 <b>协助管理员</b>"]
            for admin in admins:
                lines.append(
                    f"· <code>{admin.user_id}</code> · {admin.role} · {','.join(sorted(admin.permission_set))}"
                )
            await message.reply("\n".join(lines))
            return

        target = await helpers.resolve_target(bot, message, args[1:])
        if not target:
            await _reply_error(message, "请回复目标用户，或 /assistant add 用户ID。")
            return
        existing = await repo.get_admin(session, group.id, target.user_id)
        if sub == "add":
            allowed = {"ban", "mute", "warn", "delete", "info", "settings"}
            perms = ["ban", "mute", "warn", "delete", "info"]
            if len(args) > 2:
                extra = set(args[2].split(",")) & allowed
                if extra:
                    perms = sorted(extra)
            perms_json = json.dumps(perms)
            if existing:
                existing.permissions = perms_json
                existing.role = "assistant"
            else:
                session.add(
                    Admin(
                        group_id=group.id,
                        user_id=target.user_id,
                        role="assistant",
                        permissions=perms_json,
                        added_by=actor,
                    )
                )
            await repo.add_log(session, group.id, actor, "assistant_add", target.user_id, perms_json)
            await message.reply(
                f"✅ 已添加协助管理员：{target.mention}\n权限：{','.join(perms)}"
            )
        elif sub == "del":
            if not existing:
                await message.reply("该用户不是协助管理员。")
                return
            await session.delete(existing)
            await repo.add_log(session, group.id, actor, "assistant_del", target.user_id, "")
            await message.reply(f"已移除协助管理员：{target.mention}")
        else:
            await _reply_error(message, "子命令必须是 add、del 或 list。")


# ---------------------------------------------------------------------------
# Cross links / report links
# ---------------------------------------------------------------------------
@router.message(Command("crosslink"))
async def cmd_crosslink(message: Message, bot: Bot) -> None:
    async with session_scope() as session:
        ctx = await helpers.admin_context(bot, session, message, "settings")
        if not ctx:
            return
        group, actor = ctx
        if not await group_feature_allowed(session, group.id, "linkage"):
            await _reply_error(message, "跨群封禁联动属于订阅版功能，当前群组未解锁。")
            return
        args = helpers.parse_args(message.text)
        if not args:
            await _reply_error(message, "用法：/crosslink add|del|list [群ID]")
            return
        sub = args[0].lower()
        if sub == "list":
            links = (
                await session.scalars(
                    select(CrossLink).where(
                        (CrossLink.group_id == group.id) | (CrossLink.linked_group_id == group.id)
                    )
                )
            ).all()
            if not links:
                await message.reply("暂无共享联动群。")
                return
            lines = ["🔗 <b>共享联动群</b>"]
            for link in links:
                other = link.linked_group_id if link.group_id == group.id else link.group_id
                target_group = await repo.get_group(session, other)
                lines.append(f"· <code>{other}</code> {escape(target_group.title if target_group else '')}")
            await message.reply("\n".join(lines))
            return
        if len(args) < 2 or not args[1].lstrip("-").isdigit():
            await _reply_error(message, "请提供群 ID。")
            return
        other_id = int(args[1])
        if other_id == group.id:
            await message.reply("不能联动自己。")
            return
        try:
            chat = await bot.get_chat(other_id)
            if chat.type not in {"group", "supergroup"}:
                await _reply_error(message, "目标必须是 Telegram 群组，不能是私聊或频道。")
                return
            await repo.ensure_group(
                session, other_id, title=chat.title or "", username=chat.username or "", chat_type=chat.type
            )
        except TelegramAPIError as exc:
            await _reply_error(message, f"无法获取群组：{exc}")
            return
        if sub == "add":
            exists = await session.scalar(
                select(CrossLink).where(
                    ((CrossLink.group_id == group.id) & (CrossLink.linked_group_id == other_id))
                    | ((CrossLink.group_id == other_id) & (CrossLink.linked_group_id == group.id))
                )
            )
            if not exists:
                session.add(CrossLink(group_id=group.id, linked_group_id=other_id, created_by=actor))
            await repo.add_log(session, group.id, actor, "crosslink_add", other_id, "")
            await message.reply(f"✅ 已添加共享联动群 <code>{other_id}</code>。")
        elif sub == "del":
            links = (
                await session.scalars(
                    select(CrossLink).where(
                        ((CrossLink.group_id == group.id) & (CrossLink.linked_group_id == other_id))
                        | ((CrossLink.group_id == other_id) & (CrossLink.linked_group_id == group.id))
                    )
                )
            ).all()
            for link in links:
                await session.delete(link)
            await repo.add_log(session, group.id, actor, "crosslink_del", other_id, "")
            await message.reply("✅ 已删除联动。")
        else:
            await _reply_error(message, "子命令必须是 add、del 或 list。")


@router.message(Command("reportlink"))
async def cmd_reportlink(message: Message, bot: Bot) -> None:
    async with session_scope() as session:
        ctx = await helpers.admin_context(bot, session, message, "settings")
        if not ctx:
            return
        group, actor = ctx
        if not await group_feature_allowed(session, group.id, "reports"):
            await _reply_error(message, "举报通报属于订阅版功能，当前群组未解锁。")
            return
        args = helpers.parse_args(message.text)
        if not args:
            await _reply_error(message, "用法：/reportlink add|del|list [群ID]（对方接收本群通报）")
            return
        sub = args[0].lower()
        if sub == "list":
            links = (
                await session.scalars(select(ReportLink).where(ReportLink.group_id == group.id))
            ).all()
            if not links:
                await message.reply("本群暂无通报订阅者。")
                return
            lines = ["📮 <b>通报订阅者</b>"]
            for link in links:
                target_group = await repo.get_group(session, link.subscriber_id)
                lines.append(
                    f"· <code>{link.subscriber_id}</code> "
                    f"{escape(target_group.title if target_group else '')}"
                )
            await message.reply("\n".join(lines))
            return
        if len(args) < 2 or not args[1].lstrip("-").isdigit():
            await _reply_error(message, "请提供群 ID。")
            return
        other_id = int(args[1])
        if sub == "add":
            try:
                chat = await bot.get_chat(other_id)
                if chat.type not in {"group", "supergroup"}:
                    await _reply_error(message, "目标必须是 Telegram 群组，不能是私聊或频道。")
                    return
                target_group = await repo.ensure_group(
                    session, other_id, title=chat.title or "", username=chat.username or "", chat_type=chat.type
                )
                target_group.report_receive = True
            except TelegramAPIError as exc:
                await _reply_error(message, f"无法获取群组：{exc}")
                return
            exists = await session.scalar(
                select(ReportLink).where(
                    ReportLink.group_id == group.id, ReportLink.subscriber_id == other_id
                )
            )
            if not exists:
                session.add(ReportLink(group_id=group.id, subscriber_id=other_id, created_by=actor))
            await repo.add_log(session, group.id, actor, "reportlink_add", other_id, "")
            await message.reply(f"✅ 已添加通报订阅者 <code>{other_id}</code>。")
        elif sub == "del":
            link = await session.scalar(
                select(ReportLink).where(
                    ReportLink.group_id == group.id, ReportLink.subscriber_id == other_id
                )
            )
            if link:
                await session.delete(link)
            await message.reply("✅ 已删除通报订阅。")
        else:
            await _reply_error(message, "子命令必须是 add、del 或 list。")


# ---------------------------------------------------------------------------
# Global blacklist (bot admins only)
# ---------------------------------------------------------------------------
@router.message(Command("gblacklist", "gbl"))
async def cmd_gblacklist(message: Message, bot: Bot) -> None:
    actor = message.from_user.id if message.from_user else None
    if not is_global_admin(actor):
        await message.reply("⛔ 仅机器人全局管理员可操作全局黑名单。")
        return
    args = helpers.parse_args(message.text)
    if not args:
        await _reply_error(message, "用法：/gblacklist add|del|list [用户] [原因]")
        return
    sub = args[0].lower()
    async with session_scope() as session:
        if sub == "list":
            entries = (
                await session.scalars(
                    select(GlobalBlacklist).order_by(GlobalBlacklist.id.desc()).limit(50)
                )
            ).all()
            if not entries:
                await message.reply("全局黑名单为空。")
                return
            lines = ["🌐 <b>全局黑名单</b>"]
            for entry in entries:
                scope = "平台" if entry.owner_user_id is None else f"用户 {entry.owner_user_id}"
                lines.append(
                    f"· <code>{entry.user_id}</code> [{scope}] · "
                    f"{escape(entry.reason or '无原因')}"
                )
            await message.reply("\n".join(lines))
            return
        target = await helpers.resolve_target(bot, message, args[1:])
        if not target:
            await _reply_error(message, "请提供用户 ID 或回复用户。")
            return
        if sub == "add":
            existing = await session.scalar(
                select(GlobalBlacklist).where(GlobalBlacklist.user_id == target.user_id)
            )
            reason = " ".join(args[2:])
            if existing:
                existing.reason = reason or existing.reason
                # /gblacklist is a platform command: promote any subscriber
                # entry to a platform-wide entry instead of leaving it scoped.
                existing.owner_user_id = None
            else:
                session.add(
                    GlobalBlacklist(
                        user_id=target.user_id,
                        owner_user_id=None,
                        reason=reason,
                        added_by=actor,
                    )
                )
            await repo.add_log(session, message.chat.id, actor, "gblacklist_add", target.user_id, reason)
            await message.reply(f"✅ 已加入全局黑名单：{target.mention}")
        elif sub == "del":
            entry = await session.scalar(
                select(GlobalBlacklist).where(GlobalBlacklist.user_id == target.user_id)
            )
            if entry:
                await session.delete(entry)
            await message.reply(f"✅ 已移出全局黑名单：{target.mention}")
        else:
            await _reply_error(message, "子命令必须是 add、del 或 list。")
