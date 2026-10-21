"""Join verification, welcome messages, required channels and invite tracking."""
from __future__ import annotations

import logging
import secrets
import time
from datetime import timedelta
from functools import wraps
from html import escape

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    Chat,
    ChatMemberUpdated,
    Message,
    User,
)
from sqlalchemy import or_, select

from ...db import session_scope
from ...entitlements import group_feature_allowed, group_owner_user_id
from ...models import (
    GlobalBlacklist,
    Group,
    InviteCode,
    RequiredChannel,
    Verification,
    utcnow,
)
from ...services import repo
from .. import captcha, helpers, keyboards

router = Router(name="join")

log = logging.getLogger(__name__)

_processed: dict[tuple[int, int], float] = {}
_TTL = 20.0
_CAPTCHA_MAX_ATTEMPTS = 3
_verification_inflight: set[str] = set()


async def _answer(
    callback: CallbackQuery, text: str, *, show_alert: bool = False
) -> None:
    """Answer a callback without aborting security-critical work on API errors."""
    try:
        await callback.answer(text, show_alert=show_alert)
    except TelegramAPIError:
        pass


def _single_attempt(func):
    """Reject concurrent clicks on the same CAPTCHA.

    Telegram may deliver several callback queries for the old inline keyboard
    at once. Without this guard a bot could submit every option concurrently
    before the first answer expires the token.
    """

    @wraps(func)
    async def wrapper(callback: CallbackQuery, bot: Bot):
        parts = (callback.data or "").split(":")
        token = parts[1] if len(parts) >= 2 else ""
        key = f"{callback.from_user.id}:{token}" if token else ""
        if key and key in _verification_inflight:
            await _answer(callback, "该验证正在处理，请稍候。", show_alert=True)
            return
        if key:
            _verification_inflight.add(key)
        try:
            return await func(callback, bot)
        finally:
            if key:
                _verification_inflight.discard(key)

    return wrapper


def _claim(chat_id: int, user_id: int) -> bool:
    now = time.monotonic()
    key = (int(chat_id), int(user_id))
    last = _processed.get(key, 0.0)
    if now - last < _TTL:
        return False
    _processed[key] = now
    if len(_processed) > 5000:
        for k in list(_processed):
            if now - _processed[k] > 120:
                _processed.pop(k, None)
    return True


async def _group_channels(session, group_id: int) -> list[RequiredChannel]:
    result = await session.scalars(
        select(RequiredChannel).where(RequiredChannel.group_id == group_id)
    )
    return list(result.all())


async def missing_channels(bot: Bot, group_id: int, user_id: int) -> list[RequiredChannel]:
    async with session_scope() as session:
        channels = await _group_channels(session, group_id)
    missing: list[RequiredChannel] = []
    for channel in channels:
        try:
            member = await bot.get_chat_member(channel.channel_id, user_id)
            status = getattr(member.status, "value", str(member.status))
            is_member = getattr(member, "is_member", None)
            if is_member is None:
                is_member = status in {"creator", "administrator", "member"}
            if not is_member:
                missing.append(channel)
        except TelegramAPIError:
            missing.append(channel)
    return missing


def _bio_hit(group: Group, user: User) -> str | None:
    keywords = repo.parse_csv(group.bio_block_keywords)
    if not keywords:
        return None
    # Bot API does not expose user bios; best-effort check of names/username.
    haystack = " ".join(
        filter(None, [user.first_name or "", getattr(user, "last_name", "") or "", user.username or ""])
    ).lower()
    for keyword in keywords:
        if keyword.lower() in haystack:
            return keyword
    return None


async def _announce_rejoin_mute(bot: Bot, group: Group, user: User) -> None:
    try:
        await bot.send_message(
            group.id,
            f"🔇 {user.mention_html()} 此前曾被移出，重新入群后已自动禁言，请联系管理员解除。",
        )
    except TelegramAPIError:
        pass


async def send_welcome(bot: Bot, group: Group, user: User, session=None) -> None:
    if not group.welcome_enabled:
        return
    if session is not None and not await group_feature_allowed(
        session, group.id, "welcome"
    ):
        return
    template = group.welcome_text or "欢迎 {name} 加入 {group}！"
    text = (
        template.replace("{name}", escape(user.first_name or user.username or str(user.id)))
        .replace("{group}", escape(group.title or "本群"))
        .replace("{id}", str(user.id))
    )
    try:
        await bot.send_message(group.id, text)
    except TelegramAPIError:
        pass


async def _send_verify_prompt(
    bot: Bot,
    group: Group,
    user: User,
    token: str,
    channels: list[RequiredChannel],
    missing: list[RequiredChannel],
    require_username: bool | None = None,
) -> int | None:
    code = captcha.captcha_code(token)
    options = captcha.captcha_options(token)
    image = captcha.render_captcha(code)

    group_title = (group.title or "本群")[:64]
    lines = [
        f"🛡 {user.mention_html()}，欢迎加入 <b>{escape(group_title)}</b>！",
        f"\n请阅读图片中的 {len(code)} 位验证码，点击下方对应按钮。每个验证码仅可尝试一次，选错后会立即更换。",
    ]
    if channels:
        missing_ids = {channel.channel_id for channel in missing}
        labels = []
        for channel in channels[:5]:
            flag = "❌" if channel.channel_id in missing_ids else "✅"
            label = _channel_label(channel)
            if len(label) > 30:
                label = label[:30] + "…"
            labels.append(f"{flag} {escape(label)}")
        if len(channels) > 5:
            labels.append("…")
        lines.append("\n📢 必关频道：" + "、".join(labels))
    wants_username = group.require_username if require_username is None else require_username
    if wants_username and not user.username:
        lines.append("\n⚠️ 你还没有设置用户名，请先在 Telegram 设置 @username。")
    timeout_minutes = max(1, int(group.verify_timeout or 300) // 60)
    lines.append(
        f"\n⏱️ {timeout_minutes} 分钟内有效，累计选错 {_CAPTCHA_MAX_ATTEMPTS} 次将被移出群组。"
    )

    caption = "\n".join(lines)
    links = [
        (
            _channel_label(channel),
            channel.invite_link
            or (f"https://t.me/{channel.username}" if channel.username else ""),
        )
        for channel in channels
    ]
    try:
        sent = await bot.send_photo(
            group.id,
            BufferedInputFile(image, filename="groupguard-captcha.png"),
            caption=caption,
            reply_markup=keyboards.verification_kb(token, links, options),
        )
        return sent.message_id
    except TelegramAPIError:
        return None


def _channel_label(channel: RequiredChannel) -> str:
    return channel.title or channel.username or str(channel.channel_id)


async def _start_verification(
    bot: Bot,
    session,
    group: Group,
    user: User,
    missing: list[RequiredChannel],
    *,
    failed_attempts: int | None = None,
) -> Verification:
    # A restricted member cannot type an answer, so the CAPTCHA is answered
    # with inline buttons.  The mute can stay in place during verification.
    # Previously removed members stay permanently muted instead of getting the
    # temporary verification restriction that would expire on its own.
    member = await repo.get_member(session, group.id, user.id)
    try:
        if member and member.mute_on_rejoin:
            await helpers.restrict_permanently(bot, group.id, user.id)
        else:
            await helpers.restrict_for_verification(
                bot, group.id, user.id, (group.verify_timeout or 300) + 120
            )
    except TelegramAPIError:
        pass
    # Rotate the challenge while carrying the failure count and the original
    # deadline. A wrong answer must not become a fresh 3-attempt session.
    old = (
        await session.scalars(
            select(Verification).where(
                Verification.group_id == group.id,
                Verification.user_id == user.id,
                Verification.status == "pending",
            )
        )
    ).all()
    old_message_ids = [item.message_id for item in old if item.message_id]
    now = utcnow()
    timeout = max(60, int(group.verify_timeout or 300))
    if failed_attempts is None:
        failed_attempts = max(
            (item.failed_attempts or 0 for item in old), default=0
        )
    active_old = [
        item
        for item in old
        if item.created_at is not None
        and item.created_at + timedelta(seconds=timeout) > now
    ]
    created_at = min((item.created_at for item in active_old), default=now)
    for item in old:
        item.status = "expired"
    token = secrets.token_urlsafe(18)
    verification = Verification(
        group_id=group.id,
        user_id=user.id,
        token=token,
        status="pending",
        created_at=created_at,
        failed_attempts=failed_attempts,
    )
    session.add(verification)
    await session.flush()
    channels = await _group_channels(session, group.id)
    advanced_join = await group_feature_allowed(session, group.id, "verify_advanced")
    message_id = await _send_verify_prompt(
        bot,
        group,
        user,
        token,
        channels,
        missing,
        require_username=bool(advanced_join and group.require_username),
    )
    if message_id:
        verification.message_id = message_id
        for old_message_id in old_message_ids:
            if old_message_id == message_id:
                continue
            if not await helpers.delete_message_safe(
                bot, group.id, old_message_id
            ):
                log.debug(
                    "could not delete old verification prompt group=%s message=%s",
                    group.id,
                    old_message_id,
                )
    return verification


async def redeem_invite(
    bot: Bot,
    group_id: int,
    user_id: int,
    invite_link: str,
    user: User | None = None,
) -> bool:
    """Track invite usage; returns True when the invite bypassed verification."""
    async with session_scope() as session:
        group = await repo.get_group(session, group_id)
        if group is None:
            return False
        code = await session.scalar(
            select(InviteCode).where(InviteCode.group_id == group.id, InviteCode.link == invite_link)
        )
        if code is None:
            return False
        now = utcnow()
        if not code.active or (code.expires_at and code.expires_at < now):
            return False
        if code.max_uses and code.uses >= code.max_uses:
            return False
        code.uses = (code.uses or 0) + 1
        if code.max_uses and code.uses >= code.max_uses:
            code.active = False
        await repo.add_log(session, group.id, None, "invite_used", user_id, f"code {code.code}")

        if not group.invite_redemption:
            return False
        member = await repo.ensure_member(
            session,
            group.id,
            user_id,
            username=(user.username if user else "") or "",
            first_name=(user.first_name if user else "") or "",
        )
        was_verified = bool(member.is_verified)
        member.is_verified = True
        member.is_member = True
        member.is_banned = False
        for verification in (
            await session.scalars(
                select(Verification).where(
                    Verification.group_id == group.id,
                    Verification.user_id == user_id,
                    Verification.status == "pending",
                )
            )
        ).all():
            verification.status = "passed"
            verification.passed_at = now
        target = helpers.Target(
            user_id,
            user.first_name if user else "",
            user.username if user else "",
        )
        stayed_muted = bool(member.mute_on_rejoin)
        if stayed_muted:
            mute_applied = await helpers.apply_rejoin_mute(
                bot, session, group, target
            )
        else:
            mute_applied = False
            try:
                await helpers.unmute_user(bot, session, group, target, admin_id=None)
            except TelegramAPIError:
                pass
        if not was_verified and user is not None:
            await send_welcome(bot, group, user, session)
            if stayed_muted:
                suffix = (
                    "验证通过，但此前曾被移出，当前保持禁言。"
                    if mute_applied
                    else "验证通过，但自动禁言失败，请联系管理员处理。"
                )
            else:
                suffix = "验证通过。"
            try:
                await bot.send_message(
                    group.id,
                    f"🎟 {user.mention_html()} 的邀请码已核销，{suffix}",
                )
            except TelegramAPIError:
                pass
        return True


async def process_join(
    bot: Bot,
    chat: Chat,
    user: User,
    *,
    invite_redeemed: bool = False,
) -> None:
    async with session_scope() as session:
        group = await repo.ensure_group(
            session,
            chat.id,
            title=chat.title or "",
            username=chat.username or "",
            chat_type=chat.type,
        )
        member = await repo.ensure_member(
            session,
            group.id,
            user.id,
            username=user.username or "",
            first_name=user.first_name or "",
        )
        stayed_muted = bool(member.mute_on_rejoin)
        member.joined_at = utcnow()
        member.is_member = True
        member.is_banned = False
        await repo.bump_stat(session, group.id, "joins")
        try:
            count = await bot.get_chat_member_count(group.id)
            group.member_count = count
        except TelegramAPIError:
            group.member_count = (group.member_count or 0) + 1

        if invite_redeemed:
            member.is_verified = True
            return

        # Global blacklist: entered via a public link or leftover account.
        # Platform-wide entries (owner_user_id IS NULL) apply everywhere;
        # subscriber entries only apply to the groups they own.
        owner_id = await group_owner_user_id(session, group.id)
        blacklist_stmt = select(GlobalBlacklist).where(
            GlobalBlacklist.user_id == user.id
        )
        if owner_id is None:
            blacklist_stmt = blacklist_stmt.where(
                GlobalBlacklist.owner_user_id.is_(None)
            )
        else:
            blacklist_stmt = blacklist_stmt.where(
                or_(
                    GlobalBlacklist.owner_user_id.is_(None),
                    GlobalBlacklist.owner_user_id == owner_id,
                )
            )
        blacklisted = await session.scalar(blacklist_stmt)
        if blacklisted:
            try:
                await bot.ban_chat_member(group.id, user.id, revoke_messages=True)
                member.is_banned = True
                await repo.add_log(
                    session,
                    group.id,
                    None,
                    "global_blacklist_ban",
                    user.id,
                    blacklisted.reason or "",
                )
                await repo.bump_stat(session, group.id, "bans")
                await bot.send_message(
                    group.id,
                    f"🚫 {user.mention_html()} 在全局黑名单中，已自动封禁。",
                )
            except TelegramAPIError:
                pass
            return

        # Best-effort profile inspection (subscription-only advanced check).
        advanced_join = await group_feature_allowed(
            session, group.id, "verify_advanced"
        )
        hit = _bio_hit(group, user) if advanced_join else None
        if hit:
            try:
                await bot.ban_chat_member(group.id, user.id, revoke_messages=True)
                member.is_banned = True
                await repo.add_log(session, group.id, None, "bio_block", user.id, hit)
                await repo.bump_stat(session, group.id, "bans")
                await bot.send_message(
                    group.id,
                    f"🚫 {user.mention_html()} 的资料命中违规关键词，已自动封禁。",
                )
            except TelegramAPIError:
                pass
            return

        missing = await missing_channels(bot, group.id, user.id)
        need_username = bool(
            advanced_join and group.require_username and not user.username
        )
        needs_verification = group.verify_enabled or bool(missing) or need_username

        if needs_verification:
            await _start_verification(bot, session, group, user, missing)
            return

        member.is_verified = True
        if stayed_muted:
            target = helpers.Target(user.id, user.first_name or "", user.username or "")
            if await helpers.apply_rejoin_mute(bot, session, group, target):
                await _announce_rejoin_mute(bot, group, user)
        await send_welcome(bot, group, user, session)


@router.message(F.new_chat_members)
async def on_new_members(message: Message, bot: Bot) -> None:
    me = await bot.me()
    for user in message.new_chat_members:
        if user.id == me.id:
            async with session_scope() as session:
                group = await repo.ensure_group(
                    session,
                    message.chat.id,
                    title=message.chat.title or "",
                    username=message.chat.username or "",
                    chat_type=message.chat.type,
                )
                try:
                    group.member_count = await bot.get_chat_member_count(group.id)
                except TelegramAPIError:
                    pass
                await repo.add_log(session, group.id, user.id, "bot_added", None, "机器人进群")
            # my_chat_member may arrive together with this service message.
            if not _claim(message.chat.id, user.id):
                continue
            try:
                await message.answer(
                    "👋 我是 <b>群卫 GroupGuard</b>。\n"
                    "请把我设为管理员，并授予删除消息、封禁用户、邀请用户权限。\n\n"
                    "管理员发送 /help 查看命令；面板地址请私聊我发送 /console。"
                )
            except TelegramAPIError:
                pass
            continue
        if user.is_bot:
            continue
        if _claim(message.chat.id, user.id):
            await process_join(bot, message.chat, user)


@router.my_chat_member()
async def on_my_chat_member(update: ChatMemberUpdated, bot: Bot) -> None:
    """Register the group and send onboarding when the bot itself is added."""
    chat = update.chat
    if chat.type not in {"group", "supergroup"}:
        return
    old = getattr(update.old_chat_member.status, "value", str(update.old_chat_member.status))
    new = getattr(update.new_chat_member.status, "value", str(update.new_chat_member.status))
    joined = old in {"left", "kicked"} and new in {"member", "administrator"}
    promoted = old in {"member", "restricted"} and new == "administrator"
    if not (joined or promoted):
        return
    me = await bot.me()
    if update.new_chat_member.user.id != me.id:
        return

    async with session_scope() as session:
        group = await repo.ensure_group(
            session,
            chat.id,
            title=chat.title or "",
            username=chat.username or "",
            chat_type=chat.type,
        )
        try:
            group.member_count = await bot.get_chat_member_count(group.id)
        except TelegramAPIError:
            pass
        await repo.add_log(
            session,
            group.id,
            update.from_user.id if update.from_user else None,
            "bot_added" if joined else "bot_promoted",
            me.id,
            "机器人进群" if joined else "机器人被设为管理员",
        )

    if joined and not _claim(chat.id, me.id):
        return
    lines = [
        "👋 我是 <b>群卫 GroupGuard</b>，已加入本群。",
        "管理员发送 /help 查看命令；面板地址请私聊我发送 /console。",
    ]
    if new == "administrator":
        lines.append("\n✅ 已获得管理员权限，封禁、禁言、删除消息与进群验证功能可正常使用。")
    else:
        lines.append(
            "\n⚠️ 我还没有管理员权限，请将我设为管理员，并授予删除消息、封禁用户、邀请用户权限。"
        )
    try:
        await bot.send_message(chat.id, "\n".join(lines))
    except TelegramAPIError:
        pass


@router.chat_member()
async def on_chat_member(update: ChatMemberUpdated, bot: Bot) -> None:
    old = getattr(update.old_chat_member.status, "value", str(update.old_chat_member.status))
    new = getattr(update.new_chat_member.status, "value", str(update.new_chat_member.status))
    joined = old in {"left", "kicked"} and new in {"member", "restricted", "administrator", "creator"}
    left = old in {"member", "restricted", "administrator", "creator"} and new in {"left", "kicked"}

    async with session_scope() as session:
        group = await repo.ensure_group(
            session,
            update.chat.id,
            title=update.chat.title or "",
            username=update.chat.username or "",
            chat_type=update.chat.type,
        )
        try:
            group.member_count = await bot.get_chat_member_count(group.id)
        except TelegramAPIError:
            pass
        invite_allowed = await group_feature_allowed(
            session, group.id, "verify_advanced"
        )

    if update.new_chat_member.user.is_bot:
        return

    if joined:
        invite_redeemed = False
        if invite_allowed and update.invite_link and update.invite_link.invite_link:
            invite_redeemed = await redeem_invite(
                bot,
                update.chat.id,
                update.new_chat_member.user.id,
                update.invite_link.invite_link,
                update.new_chat_member.user,
            )
        if _claim(update.chat.id, update.new_chat_member.user.id):
            await process_join(
                bot,
                update.chat,
                update.new_chat_member.user,
                invite_redeemed=invite_redeemed,
            )
    elif left:
        pending_messages: list[int] = []
        async with session_scope() as session:
            member = await repo.get_member(session, group.id, update.new_chat_member.user.id)
            if member:
                member.is_member = False
                member.is_verified = False
                member.is_banned = new == "kicked"
                member.is_muted = False
                member.muted_until = None
            pending = (
                await session.scalars(
                    select(Verification).where(
                        Verification.group_id == group.id,
                        Verification.user_id == update.new_chat_member.user.id,
                        Verification.status == "pending",
                    )
                )
            ).all()
            for verification in pending:
                verification.status = "expired"
                if verification.message_id:
                    pending_messages.append(verification.message_id)
            await repo.bump_stat(session, group.id, "leaves")
        for message_id in pending_messages:
            if not await helpers.delete_message_safe(bot, group.id, message_id):
                log.debug(
                    "could not delete verification prompt after leave "
                    "group=%s message=%s",
                    group.id,
                    message_id,
                )


@router.callback_query(F.data.startswith("verify:"))
@_single_attempt
async def on_verify(callback: CallbackQuery, bot: Bot) -> None:
    parts = (callback.data or "").split(":")
    if len(parts) < 3 or not parts[2].isdigit():
        await _answer(callback, "验证按钮已更新，请发送 /verify 获取新的图片验证码。", show_alert=True)
        return
    token = parts[1]
    option_index = int(parts[2])
    async with session_scope() as session:
        verification = await session.scalar(
            select(Verification).where(Verification.token == token)
        )
        if verification is None:
            await _answer(callback, "验证信息不存在或已失效。", show_alert=True)
            return
        if callback.from_user.id != verification.user_id:
            await _answer(callback, "这不是你的验证按钮。", show_alert=True)
            return
        if verification.status != "pending":
            await _answer(callback, "该验证已处理。", show_alert=True)
            return
        group = await repo.get_group(session, verification.group_id)
        if group is None:
            await _answer(callback, "群组信息不存在。", show_alert=True)
            return
        if verification.created_at + timedelta(seconds=group.verify_timeout) < utcnow():
            await _answer(callback, "验证已过期，请等待管理员处理。", show_alert=True)
            return

        missing = await missing_channels(bot, group.id, callback.from_user.id)
        if missing:
            names = "、".join(_channel_label(channel) for channel in missing)
            await _answer(callback, f"还未关注：{names}", show_alert=True)
            return
        advanced_join = await group_feature_allowed(
            session, group.id, "verify_advanced"
        )
        if advanced_join and group.require_username and not callback.from_user.username:
            await _answer(callback, "请先在 Telegram 设置用户名后再验证。", show_alert=True)
            return
        hit = _bio_hit(group, callback.from_user) if advanced_join else None
        if hit:
            await _answer(callback, "资料未通过检测。", show_alert=True)
            return

        expected = captcha.captcha_code(token)
        options = captcha.captcha_options(token)
        if option_index < 0 or option_index >= len(options):
            await _answer(callback, "验证码选项无效，请发送 /verify 重新获取。", show_alert=True)
            return
        if options[option_index] != expected:
            verification.failed_attempts = (verification.failed_attempts or 0) + 1
            attempts = verification.failed_attempts
            if attempts < _CAPTCHA_MAX_ATTEMPTS:
                remaining = _CAPTCHA_MAX_ATTEMPTS - attempts
                new_verification = await _start_verification(
                    bot,
                    session,
                    group,
                    callback.from_user,
                    missing,
                    failed_attempts=attempts,
                )
                if new_verification.message_id:
                    await _answer(
                        callback,
                        f"❌ 验证码错误，已更换新验证码。还可尝试 {remaining} 次。",
                        show_alert=True,
                    )
                else:
                    await _answer(
                        callback,
                        "❌ 验证码错误，但新验证码发送失败，请发送 /verify 重新获取。",
                        show_alert=True,
                    )
                return

            verification.status = "kicked"
            await repo.add_log(
                session,
                group.id,
                None,
                "verify_failed_kick",
                callback.from_user.id,
                f"连续答错 {attempts} 次",
            )
            await _answer(
                callback,
                "❌ 已连续答错 3 次，正在将你移出群组。",
                show_alert=True,
            )
            target = helpers.Target(
                callback.from_user.id,
                callback.from_user.first_name or "",
                callback.from_user.username or "",
            )
            try:
                await helpers.kick_user(
                    bot,
                    session,
                    group,
                    target,
                    reason="进群验证连续答错 3 次",
                    admin_id=None,
                    flag_rejoin=False,
                )
            except TelegramAPIError as exc:
                log.warning(
                    "captcha kick failed for group=%s user=%s: %s",
                    group.id,
                    callback.from_user.id,
                    exc,
                )
                try:
                    await bot.send_message(
                        group.id,
                        f"⚠️ {callback.from_user.mention_html()} 验证失败次数达到上限，"
                        f"但自动移出失败：{escape(str(exc))[:200]}，请管理员手动处理。",
                    )
                except TelegramAPIError:
                    pass
            else:
                try:
                    await bot.send_message(
                        group.id,
                        f"🚫 {callback.from_user.mention_html()} 连续答错验证码 3 次，已被移出群组。",
                    )
                except TelegramAPIError:
                    pass
            prompt_message_id = verification.message_id or getattr(
                callback.message, "message_id", None
            )
            if prompt_message_id and not await helpers.delete_message_safe(
                bot, group.id, prompt_message_id
            ):
                log.debug(
                    "could not delete kicked user's verification prompt "
                    "group=%s message=%s",
                    group.id,
                    prompt_message_id,
                )
            return

        verification.status = "passed"
        verification.passed_at = utcnow()
        member = await repo.ensure_member(
            session,
            group.id,
            callback.from_user.id,
            username=callback.from_user.username or "",
            first_name=callback.from_user.first_name or "",
        )
        member.is_verified = True
        target = helpers.Target(
            callback.from_user.id,
            callback.from_user.first_name or "",
            callback.from_user.username or "",
        )
        stayed_muted = bool(member.mute_on_rejoin)
        if stayed_muted:
            applied = await helpers.apply_rejoin_mute(
                bot, session, group, target
            )
            if applied:
                await _announce_rejoin_mute(bot, group, callback.from_user)
            answer_text = (
                "✅ 验证通过。你此前曾被移出，当前保持禁言，请联系管理员解除。"
                if applied
                else "✅ 验证通过，但自动禁言失败，请联系管理员处理。"
            )
        else:
            try:
                await helpers.unmute_user(bot, session, group, target, admin_id=None)
            except TelegramAPIError:
                pass
            answer_text = "✅ 验证通过，欢迎加入！"
        await repo.add_log(
            session, group.id, None, "verify_passed", callback.from_user.id, ""
        )
        await send_welcome(bot, group, callback.from_user, session)
        await _answer(callback, answer_text, show_alert=stayed_muted)
        prompt_message_id = verification.message_id or getattr(
            callback.message, "message_id", None
        )
        if prompt_message_id and not await helpers.delete_message_safe(
            bot, group.id, prompt_message_id
        ):
            log.debug(
                "could not delete passed verification prompt group=%s message=%s",
                group.id,
                prompt_message_id,
            )


@router.callback_query(F.data.startswith("verifynew:"))
@_single_attempt
async def on_verify_new(callback: CallbackQuery, bot: Bot) -> None:
    token = (callback.data or "").split(":", 1)[1]
    async with session_scope() as session:
        verification = await session.scalar(
            select(Verification).where(Verification.token == token)
        )
        if verification is None:
            await _answer(callback, "验证信息不存在或已失效。", show_alert=True)
            return
        if callback.from_user.id != verification.user_id:
            await _answer(callback, "这不是你的验证按钮。", show_alert=True)
            return
        if verification.status != "pending":
            await _answer(callback, "该验证已处理。", show_alert=True)
            return
        group = await repo.get_group(session, verification.group_id)
        if group is None:
            await _answer(callback, "群组信息不存在。", show_alert=True)
            return
        if verification.created_at + timedelta(seconds=group.verify_timeout) < utcnow():
            await _answer(callback, "验证已过期，请等待管理员处理。", show_alert=True)
            return

        missing = await missing_channels(bot, group.id, callback.from_user.id)
        new_verification = await _start_verification(
            bot, session, group, callback.from_user, missing
        )
        if new_verification.message_id:
            await _answer(callback, "🔄 已更换验证码。")
        else:
            await _answer(callback, "新验证码发送失败，请发送 /verify 重新获取。", show_alert=True)


@router.message(Command("verify"))
async def cmd_verify(message: Message, bot: Bot) -> None:
    if message.chat.type == "private" or not message.from_user:
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
        member = await repo.get_member(session, group.id, message.from_user.id)
        if member and member.is_verified and not group.verify_enabled:
            await message.reply("你已通过验证。")
            return
        missing = await missing_channels(bot, group.id, message.from_user.id)
        await _start_verification(bot, session, group, message.from_user, missing)
    try:
        await message.delete()
    except TelegramAPIError:
        pass


@router.message(Command("invite", "invites"))
async def cmd_invite(message: Message, bot: Bot) -> None:
    if message.chat.type == "private":
        await message.reply("请在群组中使用。")
        return
    async with session_scope() as session:
        ctx = await helpers.admin_context(bot, session, message, "settings")
        if not ctx:
            return
        group, actor = ctx
        args = helpers.parse_args(message.text)
        if message.text and message.text.split()[0].split("@")[0] == "/invites":
            codes = (
                await session.scalars(
                    select(InviteCode)
                    .where(InviteCode.group_id == group.id)
                    .order_by(InviteCode.id.desc())
                    .limit(20)
                )
            ).all()
            if not codes:
                await message.reply("暂无邀请链接。使用 /invite [次数] [有效分钟] 创建。")
                return
            lines = ["🎟 <b>邀请链接</b>"]
            for code in codes:
                status = "有效" if code.active else "已失效"
                lines.append(
                    f"· <code>{code.code}</code> · {code.uses}/{code.max_uses or '∞'} · {status}\n  {code.link}"
                )
            await message.reply("\n".join(lines))
            return

        max_uses = int(args[0]) if args and args[0].isdigit() else 0
        minutes = int(args[1]) if len(args) > 1 and args[1].isdigit() else 0
        code = secrets.token_urlsafe(9)
        try:
            link = await bot.create_chat_invite_link(
                group.id,
                name=f"gg-{code}"[:32],
                member_limit=max_uses or None,
                expire_date=int(time.time()) + minutes * 60 if minutes > 0 else None,
            )
        except TelegramAPIError as exc:
            await message.reply(f"创建邀请链接失败：{exc}")
            return
        session.add(
            InviteCode(
                group_id=group.id,
                code=code,
                link=link.invite_link,
                created_by=actor,
                max_uses=max_uses,
                expires_at=utcnow() + timedelta(minutes=minutes) if minutes > 0 else None,
            )
        )
        await repo.add_log(session, group.id, actor, "invite_create", None, code)
    await message.reply(
        f"🎟 邀请链接已创建（次数 {max_uses or '不限'}，有效期 {minutes or '不限'} 分钟）：\n{link.invite_link}"
    )
