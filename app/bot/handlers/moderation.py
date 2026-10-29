"""Real-time message moderation, flood control and AI mention replies."""
from __future__ import annotations

from datetime import datetime, timedelta

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.types import Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ...config import is_global_admin
from ...db import session_scope
from ...entitlements import GroupAccessCache
from ...models import AIEvent, Group, Keyword, utcnow
from ...services import ai, detect, lottery_rules, quota, repo
from .. import helpers

router = Router(name="moderation")

_flood: dict[tuple[int, int], list[datetime]] = {}


def _flood_add(chat_id: int, user_id: int) -> list[datetime]:
    now = utcnow()
    key = (chat_id, user_id)
    items = _flood.setdefault(key, [])
    items.append(now)
    cutoff = now - timedelta(seconds=120)
    items[:] = [ts for ts in items if ts >= cutoff]
    if len(_flood) > 20000:
        for k in list(_flood):
            _flood[k] = [ts for ts in _flood[k] if ts >= cutoff]
            if not _flood[k]:
                _flood.pop(k, None)
    return items


async def _notice(bot: Bot, chat_id: int, text: str, delay: int = 12) -> None:
    await helpers.send_temp_notice(bot, chat_id, text, delay=delay)


@router.message(F.chat.type.in_(["group", "supergroup"]))
async def on_group_message(message: Message, bot: Bot) -> None:
    user = message.from_user
    if not user or user.is_bot:
        return
    # Anonymous group admins post as the group itself; never moderate them.
    if message.sender_chat is not None:
        return

    text = message.text or message.caption or ""
    async with session_scope() as session:
        group = await repo.ensure_group(
            session,
            message.chat.id,
            title=message.chat.title or "",
            username=message.chat.username or "",
            chat_type=message.chat.type,
        )
        if not group.is_active:
            return
        member = await repo.ensure_member(
            session,
            group.id,
            user.id,
            username=user.username or "",
            first_name=user.first_name or "",
        )
        access = GroupAccessCache(session, group.id)

        # ---- counters / points ----
        member.is_member = True
        member.message_count = (member.message_count or 0) + 1
        member.last_seen = utcnow()
        group.message_count = (group.message_count or 0) + 1
        await repo.bump_stat(session, group.id, "messages")
        await repo.bump_member_daily(session, group.id, user.id)
        if (
            group.points_enabled
            and group.points_per_message
            and await access.can("points")
        ):
            now = utcnow()
            if not member.last_points_at or (now - member.last_points_at).total_seconds() >= (group.points_cooldown or 0):
                await repo.adjust_points(
                    session, group.id, user.id, group.points_per_message, "发言奖励"
                )
                member.last_points_at = now

        if not text:
            return

        if not text.startswith("/"):
            if not user.is_bot:
                entered = await lottery_rules.record_keyword_matches(
                    session, group.id, user.id, text
                )
            else:
                entered = []
            if entered:
                await _notice(
                    bot,
                    group.id,
                    f"🎟 关键词匹配，已自动参与：{'、'.join(entered)}。",
                    8,
                )

        # ---- AI mention reply (works for admins too) ----
        bot_username = await helpers.get_bot_username(bot)
        mentioned = bool(bot_username) and f"@{bot_username.lower()}" in text.lower()
        if mentioned and (group.ai_assistant_enabled or group.ai_chat_enabled):
            if not await access.can("ai"):
                await _notice(bot, group.id, "🤖 AI 助手属于订阅版功能，请群组所有者先开通订阅。", 60)
                return
            if not await quota.allow_request(session, group, access=await access.resolve()):
                await _notice(bot, group.id, "🤖 本群今日 AI 额度已用完，请联系管理员调整。", 10)
                return
            owner_user_id = (await access.resolve()).owner_id
            question = text
            if bot_username:
                question = question.replace(f"@{bot_username}", "").replace(f"@{bot_username.lower()}", "")
            question = question.strip() or "你好"
            if group.ai_assistant_enabled:
                result = await ai.assistant_reply(
                    session,
                    question,
                    group_title=group.title,
                    rules=group.rules_text,
                    history=helpers.history(group.id),
                    group_id=group.id,
                    owner_user_id=owner_user_id,
                )
                kind = "assistant"
            else:
                result = await ai.chat_reply(
                    session,
                    question,
                    group_title=group.title,
                    history=helpers.history(group.id),
                    group_id=group.id,
                    owner_user_id=owner_user_id,
                )
                kind = "chat"
            if result.attempts:
                await quota.note(session, group, requests=result.attempts)
            if result.ok:
                helpers.remember(group.id, "user", question)
                helpers.remember(group.id, "assistant", result.text)
                session.add(
                    AIEvent(
                        group_id=group.id,
                        user_id=user.id,
                        kind=kind,
                        provider=result.provider,
                        prompt=question[:2000],
                        result=result.text[:3000],
                        latency_ms=result.latency_ms,
                    )
                )
                try:
                    await message.reply(result.text[:4000], parse_mode=None)
                except TelegramAPIError:
                    pass
            else:
                session.add(
                    AIEvent(
                        group_id=group.id,
                        user_id=user.id,
                        kind=kind,
                        provider=result.provider,
                        prompt=question[:2000],
                        result=("调用失败：" + result.error)[:3000],
                        latency_ms=result.latency_ms,
                    )
                )
                if group.ai_assistant_enabled:
                    await _notice(bot, group.id, f"🤖 AI 暂时不可用：{result.error[:120]}", 10)
            return

        if text.startswith("/"):
            return

        # ---- immunity ----
        if is_global_admin(user.id):
            return
        if await repo.is_whitelisted(session, group.id, user.id):
            return
        if await helpers.is_telegram_admin(bot, group.id, user.id):
            return

        detection = detect.Detection()

        # ---- flood ----
        if group.anti_spam_enabled:
            times = _flood_add(group.id, user.id)
            detection = detect.check_flood(
                message_times=times,
                limit=group.flood_limit,
                window_seconds=group.flood_window,
                action=group.flood_action,
            )
            if detection.hit:
                _flood[(group.id, user.id)] = []

        # ---- keywords (always active) then local anti-ad rules ----
        if not detection.hit:
            keywords = [
                (k.pattern, k.is_regex, k.action, k.reply_text or "")
                for k in (
                    await session.scalars(
                        select(Keyword).where(
                            Keyword.group_id == group.id, Keyword.enabled.is_(True)
                        )
                    )
                ).all()
            ]
            if keywords:
                detection = detect.check_keywords(text, keywords)
            if not detection.hit and group.anti_ad_enabled:
                detection = detect.check_local(
                    text=text,
                    group_id=group.id,
                    user_id=user.id,
                    allowed_domains=repo.parse_csv(group.allowed_domains),
                    max_length=group.max_message_length or 0,
                    forwarded_from_channel=message.forward_from_chat is not None,
                    block_forwarded=group.block_forwarded,
                )

        # ---- AI ad detection ----
        if (
            not detection.hit
            and group.ai_ad_enabled
            and len(text) >= 4
            and await access.can("ai")
            and await quota.allow_sample(session, group, access=await access.resolve())
            and await quota.allow_request(session, group, access=await access.resolve())
        ):
            try:
                owner_user_id = (await access.resolve()).owner_id
                is_ad, reason, result = await ai.check_ad(
                    session,
                    text,
                    group.title,
                    group.id,
                    owner_user_id=owner_user_id,
                )
                await quota.note(
                    session,
                    group,
                    requests=max(1, result.attempts) if result.attempts else 0,
                    samples=1,
                    hits=1 if is_ad else 0,
                )
                result_text = result.text if result.ok else f"调用失败：{result.error}"
                session.add(
                    AIEvent(
                        group_id=group.id,
                        user_id=user.id,
                        kind="moderation",
                        provider=result.provider,
                        prompt=text[:2000],
                        result=(result_text or reason)[:1000],
                        hit=is_ad,
                        latency_ms=result.latency_ms,
                    )
                )
                if is_ad:
                    detection = detect.Detection(
                        hit=True,
                        source="ai",
                        reason=f"AI 判定为广告：{reason}" if reason else "AI 判定为广告",
                        action="warn",
                    )
            except Exception:
                pass

        if not detection.hit:
            return

        await _apply_detection(bot, session, group, message, user, detection)


async def _apply_detection(
    bot: Bot,
    session: AsyncSession,
    group: Group,
    message: Message,
    user,
    detection: detect.Detection,
) -> None:
    target = helpers.Target(user.id, user.first_name or "", user.username or "")
    deleted = False
    delete_error: str | None = None
    if group.auto_delete_messages and detection.action != "reply":
        deleted, delete_error = await helpers.delete_message_diagnostic(
            bot, group.id, message.message_id
        )
    if deleted:
        await repo.bump_stat(session, group.id, "deletes")
    elif group.auto_delete_messages and detection.action != "reply":
        await repo.add_log(
            session,
            group.id,
            None,
            "auto_delete_failed",
            user.id,
            f"[{detection.source}] message={message.message_id} "
            f"error={delete_error or 'unknown'}",
        )
    await repo.record_hit(
        session,
        group.id,
        user.id,
        keyword=detection.match or detection.reason,
        content=message.text or message.caption or "",
        action=detection.action,
        source=detection.source,
    )
    await repo.add_log(
        session,
        group.id,
        None,
        f"auto_{detection.action}",
        user.id,
        f"[{detection.source}] {detection.reason}",
    )

    text = ""
    try:
        if detection.action == "delete":
            text = f"🧹 已删除 {target.mention} 的一条违规消息：{detection.reason}"
        elif detection.action == "warn":
            _count, result = await helpers.warn_user(
                bot,
                session,
                group,
                target,
                detection.reason,
                admin_id=None,
                source=detection.source,
                content=message.text or message.caption or "",
            )
            text = f"⚠️ {result}"
        elif detection.action == "mute":
            text = await helpers.mute_user(
                bot,
                session,
                group,
                target,
                group.mute_minutes,
                reason=detection.reason,
                admin_id=None,
            )
        elif detection.action == "kick":
            text = await helpers.kick_user(
                bot, session, group, target, reason=detection.reason, admin_id=None
            )
        elif detection.action == "ban":
            text = await helpers.ban_user(
                bot, session, group, target, reason=detection.reason, admin_id=None
            )
            if group.crossban_enabled:
                linked = await helpers.apply_crossban(
                    bot, session, group, target, reason=detection.reason, admin_id=None
                )
                if linked:
                    text += f"\n🔗 已联动封禁：{'、'.join(linked)}"
        elif detection.action == "reply" and detection.reply_text:
            await message.reply(detection.reply_text[:4000], parse_mode=None)
    except TelegramAPIError as exc:
        text = f"⚠️ 自动处理失败：{exc}"

    if text:
        await _notice(bot, group.id, text, 15)
    await helpers.send_group_log(
        bot,
        group,
        f"🛡 自动处理 · {detection.source} · {detection.action}\n"
        f"用户：{target.label}\n原因：{detection.reason}",
    )
