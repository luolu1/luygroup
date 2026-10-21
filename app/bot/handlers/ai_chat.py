"""AI assistant commands."""
from __future__ import annotations

from aiogram import Bot, Router
from aiogram.filters import Command
from aiogram.types import Message

from ...config import is_global_admin
from ...db import session_scope
from ...entitlements import group_feature_allowed, group_owner_user_id
from ...models import AIEvent
from ...services import ai, quota, repo
from .. import helpers

router = Router(name="ai")


@router.message(Command("ask", "ai"))
async def cmd_ask(message: Message, bot: Bot) -> None:
    args = helpers.parse_args(message.text)
    question = " ".join(args).strip()
    if not question:
        await message.reply("用法：/ask 你的问题")
        return

    async with session_scope() as session:
        if message.chat.type == "private":
            if not is_global_admin(message.from_user.id if message.from_user else None):
                await message.reply("私聊 AI 仅限机器人管理员使用。")
                return
            group = None
            group_title = "私聊"
            rules = ""
            history = helpers.history(message.chat.id)
            owner_user_id = None
        else:
            group = await repo.ensure_group(
                session,
                message.chat.id,
                title=message.chat.title or "",
                username=message.chat.username or "",
                chat_type=message.chat.type,
            )
            if not (group.ai_assistant_enabled or group.ai_chat_enabled):
                await message.reply("🤖 本群未开启 AI 助手。")
                return
            if not await group_feature_allowed(session, group.id, "ai"):
                await message.reply("🤖 AI 助手属于订阅版功能，请群组所有者先开通订阅。")
                return
            if not await quota.allow_request(session, group):
                await message.reply("🤖 本群今日 AI 额度已用完。")
                return
            group_title = group.title
            rules = group.rules_text
            history = helpers.history(group.id)
            owner_user_id = await group_owner_user_id(session, group.id)

        if group and group.ai_assistant_enabled:
            result = await ai.assistant_reply(
                session,
                question,
                group_title=group_title,
                rules=rules,
                history=history,
                group_id=group.id if group else message.chat.id,
                owner_user_id=owner_user_id,
            )
            kind = "assistant"
        else:
            result = await ai.chat_reply(
                session,
                question,
                group_title=group_title,
                history=history,
                group_id=group.id if group else message.chat.id,
                owner_user_id=owner_user_id,
            )
            kind = "chat"

        if group and result.attempts:
            await quota.note(session, group, requests=result.attempts)

        if result.ok:
            helpers.remember(message.chat.id, "user", question)
            helpers.remember(message.chat.id, "assistant", result.text)
            session.add(
                AIEvent(
                    group_id=group.id if group else message.chat.id,
                    user_id=message.from_user.id if message.from_user else None,
                    kind=kind,
                    provider=result.provider,
                    prompt=question[:2000],
                    result=result.text[:3000],
                    latency_ms=result.latency_ms,
                )
            )
            await message.reply(result.text[:4000], parse_mode=None)
        else:
            session.add(
                AIEvent(
                    group_id=group.id if group else message.chat.id,
                    user_id=message.from_user.id if message.from_user else None,
                    kind=kind,
                    provider=result.provider,
                    prompt=question[:2000],
                    result=("调用失败：" + result.error)[:3000],
                    latency_ms=result.latency_ms,
                )
            )
            await message.reply(f"🤖 AI 调用失败：{result.error[:200]}")
