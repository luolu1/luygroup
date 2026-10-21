"""Bot-side permissions, target parsing and moderation primitives."""
from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramForbiddenError
from aiogram.types import Chat, ChatMemberAdministrator, ChatMemberOwner, ChatPermissions, Message, User
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_config, is_global_admin
from ..models import Group, utcnow
from ..services import repo

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Permission checks
# ---------------------------------------------------------------------------
_admin_cache: dict[tuple[int, int], tuple[bool, float]] = {}
_ADMIN_TTL = 60.0


async def is_telegram_admin(bot: Bot, chat_id: int, user_id: int) -> bool:
    key = (int(chat_id), int(user_id))
    now = time.monotonic()
    cached = _admin_cache.get(key)
    if cached and cached[1] > now:
        return cached[0]
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        result = isinstance(member, (ChatMemberAdministrator, ChatMemberOwner))
    except (TelegramAPIError, Exception):
        result = False
    _admin_cache[key] = (result, now + _ADMIN_TTL)
    return result


async def has_permission(
    bot: Bot,
    session: AsyncSession,
    group_id: int,
    user_id: int | None,
    permission: str = "delete",
) -> bool:
    if user_id is None:
        return False
    if is_global_admin(user_id):
        return True
    if await is_telegram_admin(bot, group_id, user_id):
        return True
    admin = await repo.get_admin(session, group_id, user_id)
    if admin is None:
        return False
    if admin.role == "owner":
        return True
    return permission in admin.permission_set


async def admin_context(
    bot: Bot,
    session: AsyncSession,
    message: Message,
    permission: str = "delete",
) -> tuple[Group, int] | None:
    """Return (group, actor_id) when the sender may use an admin command."""
    chat = message.chat
    if chat.type not in {"group", "supergroup"}:
        await message.reply("该命令只能在群组中使用。")
        return None
    if message.from_user is None:
        return None
    group = await repo.ensure_group(
        session,
        chat.id,
        title=chat.title or "",
        username=chat.username or "",
        chat_type=chat.type,
    )
    if not await has_permission(bot, session, chat.id, message.from_user.id, permission):
        await message.reply("⛔ 你没有权限执行此操作。")
        return None
    if group.delete_command_messages and (message.text or "").startswith("/"):
        asyncio.create_task(
            delete_message_safe(bot, chat.id, message.message_id, delay=2)
        )
    return group, message.from_user.id


async def can_moderate_target(bot: Bot, session: AsyncSession, group_id: int, target_id: int) -> tuple[bool, str]:
    if is_global_admin(target_id) or await repo.get_admin(session, group_id, target_id):
        return False, "对方是机器人管理员，不能对其操作。"
    if await is_telegram_admin(bot, group_id, target_id):
        return False, "对方是本群管理员，不能对其操作。"
    return True, ""


# ---------------------------------------------------------------------------
# Target resolution
# ---------------------------------------------------------------------------
@dataclass
class Target:
    user_id: int
    first_name: str = ""
    username: str = ""

    @property
    def label(self) -> str:
        return repo.display_user(self.user_id, self.first_name, self.username)

    @property
    def mention(self) -> str:
        if self.username:
            return f"@{self.username}"
        return f'<a href="tg://user?id={self.user_id}">{self.first_name or self.user_id}</a>'


async def resolve_target(bot: Bot, message: Message, args: list[str]) -> Target | None:
    reply = message.reply_to_message
    if reply and reply.from_user:
        user = reply.from_user
        return Target(user.id, user.first_name or "", user.username or "")

    if not args:
        return None

    raw = args[0].lstrip("@")
    if raw.lstrip("-").isdigit():
        user_id = int(raw)
        try:
            chat = await bot.get_chat(user_id)
            return Target(user_id, chat.first_name or chat.title or "", chat.username or "")
        except TelegramAPIError:
            return Target(user_id)
    if args[0].startswith("@"):
        try:
            chat = await bot.get_chat("@" + raw)
            return Target(chat.id, chat.first_name or chat.title or "", chat.username or "")
        except TelegramAPIError:
            return None
    return None


# ---------------------------------------------------------------------------
# Telegram actions
# ---------------------------------------------------------------------------
_MUTE_DENY = ChatPermissions(
    can_send_messages=False,
    can_send_audios=False,
    can_send_documents=False,
    can_send_photos=False,
    can_send_videos=False,
    can_send_video_notes=False,
    can_send_voice_notes=False,
    can_send_polls=False,
    can_send_other_messages=False,
    can_add_web_page_previews=False,
    can_react_to_messages=False,
)

_UNMUTE = ChatPermissions(
    can_send_messages=True,
    can_send_audios=True,
    can_send_documents=True,
    can_send_photos=True,
    can_send_videos=True,
    can_send_video_notes=True,
    can_send_voice_notes=True,
    can_send_polls=True,
    can_send_other_messages=True,
    can_add_web_page_previews=True,
    can_react_to_messages=True,
)


async def restrict_for_verification(
    bot: Bot, chat_id: int, user_id: int, seconds: int = 600
) -> None:
    await bot.restrict_chat_member(
        chat_id,
        user_id,
        permissions=_MUTE_DENY,
        until_date=int(time.time()) + max(60, seconds),
    )


async def restrict_permanently(bot: Bot, chat_id: int, user_id: int) -> None:
    """Mute a member until an administrator explicitly unmutes them."""
    await bot.restrict_chat_member(
        chat_id,
        user_id,
        permissions=_MUTE_DENY,
    )


async def delete_message_safe(
    bot: Bot, chat_id: int, message_id: int | None, *, delay: float = 0
) -> bool:
    if not message_id:
        return False
    if delay > 0:
        await asyncio.sleep(delay)
    try:
        await bot.delete_message(chat_id, message_id)
        return True
    except (TelegramBadRequest, TelegramForbiddenError):
        return False
    except TelegramAPIError:
        return False


async def mute_user(
    bot: Bot,
    session: AsyncSession,
    group: Group,
    target: Target,
    minutes: int,
    *,
    reason: str = "",
    admin_id: int | None = None,
) -> str:
    until = int(time.time()) + max(1, minutes) * 60
    await bot.restrict_chat_member(group.id, target.user_id, permissions=_MUTE_DENY, until_date=until)
    member = await repo.ensure_member(
        session, group.id, target.user_id, username=target.username, first_name=target.first_name
    )
    member.is_muted = True
    member.muted_until = utcnow() + timedelta(minutes=max(1, minutes))
    await repo.add_log(
        session, group.id, admin_id, "mute", target.user_id, f"{minutes} 分钟；{reason}"
    )
    await repo.bump_stat(session, group.id, "warns")
    return f"已禁言 {target.mention} {minutes} 分钟。{('原因：' + reason) if reason else ''}"


async def unmute_user(
    bot: Bot,
    session: AsyncSession,
    group: Group,
    target: Target,
    *,
    admin_id: int | None = None,
) -> str:
    await bot.restrict_chat_member(group.id, target.user_id, permissions=_UNMUTE)
    member = await repo.get_member(session, group.id, target.user_id)
    if member:
        member.is_muted = False
        member.muted_until = None
        # An explicit unmute is also the administrator's signal that a future
        # rejoin should not be auto-muted again.
        member.mute_on_rejoin = False
    await repo.add_log(session, group.id, admin_id, "unmute", target.user_id, "")
    return f"已解除 {target.mention} 的禁言。"


async def apply_rejoin_mute(
    bot: Bot,
    session: AsyncSession,
    group: Group,
    target: Target,
    *,
    reason: str = "此前被封禁或移出，重新入群后自动禁言",
) -> bool:
    """Keep a previously removed member muted after they rejoin.

    The SQLite flag is persisted before the Telegram call so the console and
    later joins agree even if the API request fails. Returns whether Telegram
    accepted the restriction.
    """
    member = await repo.ensure_member(
        session,
        group.id,
        target.user_id,
        username=target.username,
        first_name=target.first_name,
    )
    member.is_muted = True
    member.muted_until = None
    member.mute_on_rejoin = True
    await repo.add_log(
        session, group.id, None, "rejoin_mute", target.user_id, reason
    )
    try:
        await restrict_permanently(bot, group.id, target.user_id)
        return True
    except TelegramAPIError as exc:
        log.warning(
            "could not apply rejoin mute group=%s user=%s: %s",
            group.id,
            target.user_id,
            exc,
        )
        return False


async def ban_user(
    bot: Bot,
    session: AsyncSession,
    group: Group,
    target: Target,
    *,
    reason: str = "",
    admin_id: int | None = None,
    revoke: bool = False,
) -> str:
    await bot.ban_chat_member(group.id, target.user_id, revoke_messages=revoke)
    member = await repo.ensure_member(
        session, group.id, target.user_id, username=target.username, first_name=target.first_name
    )
    member.is_banned = True
    member.is_member = False
    member.is_muted = False
    member.muted_until = None
    member.mute_on_rejoin = True
    await repo.add_log(session, group.id, admin_id, "ban", target.user_id, reason)
    await repo.bump_stat(session, group.id, "bans")
    return f"已封禁 {target.mention}。{('原因：' + reason) if reason else ''}"


async def unban_user(
    bot: Bot,
    session: AsyncSession,
    group: Group,
    target: Target,
    *,
    admin_id: int | None = None,
) -> str:
    await bot.unban_chat_member(group.id, target.user_id, only_if_banned=True)
    member = await repo.get_member(session, group.id, target.user_id)
    if member:
        member.is_banned = False
    await repo.add_log(session, group.id, admin_id, "unban", target.user_id, "")
    return f"已解封 {target.mention}。"


async def kick_user(
    bot: Bot,
    session: AsyncSession,
    group: Group,
    target: Target,
    *,
    reason: str = "",
    admin_id: int | None = None,
    flag_rejoin: bool = True,
) -> str:
    await bot.ban_chat_member(group.id, target.user_id, revoke_messages=False)
    await bot.unban_chat_member(group.id, target.user_id, only_if_banned=True)
    member = await repo.get_member(session, group.id, target.user_id)
    if member:
        member.is_member = False
        member.is_verified = False
        member.is_muted = False
        member.muted_until = None
        # Verification failures should not permanently mute a user who later
        # completes a fresh CAPTCHA. Moderation kicks keep the flag.
        if flag_rejoin:
            member.mute_on_rejoin = True
    await repo.add_log(session, group.id, admin_id, "kick", target.user_id, reason)
    await repo.bump_stat(session, group.id, "bans")
    return f"已将 {target.mention} 移出群聊。{('原因：' + reason) if reason else ''}"


async def warn_user(
    bot: Bot,
    session: AsyncSession,
    group: Group,
    target: Target,
    reason: str,
    admin_id: int | None = None,
    *,
    source: str = "manual",
    content: str = "",
) -> tuple[int, str]:
    from ..models import Warning

    now = utcnow()
    base_code = now.strftime("%Y%m%d%H%M")
    code = base_code
    suffix = 1
    while await session.scalar(
        select(Warning.id).where(Warning.group_id == group.id, Warning.code == code)
    ) is not None:
        suffix += 1
        code = f"{base_code}-{suffix}"

    warning = Warning(
        group_id=group.id,
        user_id=target.user_id,
        admin_id=admin_id,
        code=code,
        reason=reason[:500],
        content=(content or "")[:4000],
        source=(source or "manual")[:16],
        review_status="pending" if source == "ai" else "",
        action="warn",
        active=True,
        created_at=now,
    )
    session.add(warning)
    await session.flush()
    count = await repo.warning_count(session, group.id, target.user_id)
    await repo.add_log(session, group.id, admin_id, "warn", target.user_id, reason)
    await repo.bump_stat(session, group.id, "warns")

    extra = ""
    if group.warn_limit > 0 and count >= group.warn_limit:
        action = group.warn_action or "mute"
        try:
            if action == "mute":
                extra = "\n" + await mute_user(
                    bot, session, group, target, group.mute_minutes, reason="警告达到上限", admin_id=admin_id
                )
                warning.action = action
            elif action == "kick":
                extra = "\n" + await kick_user(
                    bot, session, group, target, reason="警告达到上限", admin_id=admin_id
                )
                warning.action = action
            elif action == "ban":
                extra = "\n" + await ban_user(
                    bot, session, group, target, reason="警告达到上限", admin_id=admin_id
                )
                warning.action = action
        except TelegramAPIError as exc:
            extra = f"\n自动处罚失败：{exc}"

    return count, f"已警告 {target.mention}（{count}/{group.warn_limit or '∞'}）。原因：{reason}{extra}"


# ---------------------------------------------------------------------------
# Cross-group ban linkage
# ---------------------------------------------------------------------------
async def _same_tenant_scope(session: AsyncSession, group: Group) -> list:
    """SQL conditions that keep linkage inside one console user's groups."""
    from sqlalchemy import select

    from ..entitlements import group_owner_user_id
    from ..models import ConsoleGroupAccess

    owner_id = await group_owner_user_id(session, group.id)
    if owner_id is None:
        # Platform-managed groups may link to other unbound groups, never to
        # a subscriber's private groups.
        return [~Group.id.in_(select(ConsoleGroupAccess.group_id))]
    return [
        Group.id.in_(
            select(ConsoleGroupAccess.group_id).where(
                ConsoleGroupAccess.user_id == owner_id
            )
        )
    ]


async def linked_groups(session: AsyncSession, group: Group) -> list[Group]:
    from sqlalchemy import or_, select

    from ..entitlements import group_feature_allowed
    from ..models import CrossLink

    if not group.crossban_enabled:
        return []
    if not await group_feature_allowed(session, group.id, "linkage"):
        return []
    scope = await _same_tenant_scope(session, group)
    if group.crossban_mode == "own":
        result = await session.scalars(
            select(Group).where(
                Group.id != group.id,
                Group.is_active.is_(True),
                Group.crossban_enabled.is_(True),
                *scope,
            )
        )
        return list(result.all())
    links = await session.scalars(
        select(CrossLink).where(
            or_(CrossLink.group_id == group.id, CrossLink.linked_group_id == group.id)
        )
    )
    ids = set()
    for link in links.all():
        ids.add(link.linked_group_id if link.group_id == group.id else link.group_id)
    if not ids:
        return []
    result = await session.scalars(
        select(Group).where(
            Group.id.in_(ids),
            Group.id != group.id,
            Group.is_active.is_(True),
            Group.crossban_enabled.is_(True),
            *scope,
        )
    )
    return list(result.all())


async def apply_crossban(
    bot: Bot,
    session: AsyncSession,
    source: Group,
    target: Target,
    *,
    reason: str,
    admin_id: int | None,
) -> list[str]:
    results: list[str] = []
    for group in await linked_groups(session, source):
        try:
            await bot.ban_chat_member(group.id, target.user_id, revoke_messages=False)
            await repo.add_log(
                session,
                group.id,
                admin_id,
                "crossban",
                target.user_id,
                f"来自群 {source.id}：{reason}",
            )
            await repo.bump_stat(session, group.id, "bans")
            results.append(str(group.title or group.id))
        except TelegramAPIError:
            continue
    return results


# ---------------------------------------------------------------------------
# Logs / notices
# ---------------------------------------------------------------------------
async def send_group_log(bot: Bot, group: Group, text: str) -> None:
    if not group.log_channel_id:
        return
    try:
        await bot.send_message(group.log_channel_id, text[:4000])
    except TelegramAPIError:
        pass


async def notify_admins(bot: Bot, group_id: int, text: str) -> None:
    """Best-effort notice in the group itself."""
    try:
        await bot.send_message(group_id, text[:4000])
    except TelegramAPIError:
        pass


# ---------------------------------------------------------------------------
# Recent chat history for AI context
# ---------------------------------------------------------------------------
_history: dict[int, deque[dict[str, str]]] = defaultdict(lambda: deque(maxlen=16))


def remember(chat_id: int, role: str, content: str) -> None:
    if content:
        _history[int(chat_id)].append({"role": role, "content": content[:1500]})


def history(chat_id: int) -> list[dict[str, str]]:
    return list(_history.get(int(chat_id), []))


# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------
def user_from_message(message: Message) -> User | None:
    return message.from_user


def chat_title(message: Message) -> str:
    chat: Chat | None = message.chat
    return (chat.title if chat and chat.title else "") or "本群"


def parse_args(text: str | None) -> list[str]:
    if not text:
        return []
    parts = text.split()
    return parts[1:] if parts and parts[0].startswith("/") else parts


async def bot_is_admin(bot: Bot, chat_id: int) -> bool:
    try:
        me = await bot.get_chat_member(chat_id, (await bot.me()).id)
        return isinstance(me, (ChatMemberAdministrator, ChatMemberOwner))
    except TelegramAPIError:
        return False


def now_str() -> str:
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")


_username_cache: str | None = None


async def get_bot_username(bot: Bot) -> str:
    global _username_cache
    configured = get_config().bot.username
    if configured:
        return configured.lstrip("@")
    if _username_cache:
        return _username_cache
    try:
        me = await bot.me()
        _username_cache = me.username or ""
    except TelegramAPIError:
        _username_cache = ""
    return _username_cache


def chunks(items: Iterable[str], size: int = 30) -> list[list[str]]:
    batch: list[str] = []
    out: list[list[str]] = []
    for item in items:
        batch.append(item)
        if len(batch) >= size:
            out.append(batch)
            batch = []
    if batch:
        out.append(batch)
    return out
