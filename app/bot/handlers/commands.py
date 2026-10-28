"""General commands: help, rules, info, warnings lookup, stats."""
from __future__ import annotations

from datetime import timedelta
from html import escape

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, ChatMemberAdministrator, ChatMemberOwner, Message
from sqlalchemy import func, select

from ...config import get_config, is_global_admin
from ...db import session_scope
from ...entitlements import build_for_user
from ...models import (
    AdminLog,
    ConsoleGroupAccess,
    ConsoleUser,
    GlobalBlacklist,
    Group,
    GroupDailyStat,
    KeywordHit,
    Member,
    MemberDailyStat,
    UserNotificationPreference,
    Warning,
    day_key,
    utcnow,
)
from ...web.auth import hash_console_token, new_console_token
from ...services import repo
from .. import helpers, keyboards

router = Router(name="commands")


PRIVATE_MENU_TEXT = (
    "🛡 <b>luygroup · 私聊菜单</b>\n"
    "/start —— 快速开始\n"
    "/console —— 订阅控制台\n"
    "/sub —— 查看订阅\n"
    "/unbanme —— 自助解封\n"
    "/unmuteme —— 自助解除禁言\n"
    "/cjtz —— 开启/关闭抽奖推送\n"
    "/cjhistory —— 查看我创建的抽奖\n"
    "/cjcancel 编号 —— 取消未开奖抽奖\n"
    "/jptz —— 开启/关闭竞拍推送\n\n"
    "群组管理请把机器人设为管理员，并在群内发送 /ads 查看总菜单。"
)

GROUP_MENU_TEXT = (
    "🛡 <b>luygroup · 总菜单</b>\n"
    "/ads —— 总菜单\n"
    "/ban —— 回复广告消息封禁\n"
    "/iunban —— 全局解封并尝试跨群解除限制\n"
    "/jy 用户ID 10m —— 手动禁言\n"
    "/jcjy —— 回复被禁言用户解除禁言\n"
    "/wladd —— 回复用户添加白名单\n"
    "/wlrm —— 回复用户移除白名单\n"
    "/fy —— 查询某人 10 天 / 30 天 / 累计发言\n"
    "/cf —— 查询封禁历史等原因\n"
    "/jf —— 查看自己的积分\n"
    "/id —— 查询用户/群 ID\n"
    "/cj —— 群内管理员发起，私聊配置抽奖\n"
    "/jp 物品 | 起拍价 [加价幅度] [分钟] —— 发起竞拍\n\n"
    "进群验证、反广告、警告、关键词、必关频道等设置由管理员在群内或控制台配置。\n"
    "订阅用户可私聊机器人发送 /console 打开订阅控制台，发送 /sub 查看订阅。"
)


@router.message(CommandStart())
async def cmd_start(message: Message, bot: Bot) -> None:
    if message.chat.type == "private":
        await _ensure_console_user(message)
        greeting = (
            f"👋 你好，我是 <b>@{escape(await helpers.get_bot_username(bot) or 'luygroup')}</b>，luygroup 群组管理机器人。\n"
            "把我拉进群并设为管理员，即可使用进群验证、反广告、警告、积分、抽奖与 AI 功能。\n\n"
            "发送 /console 打开订阅控制台，发送 /sub 查看订阅。\n"
            "群组管理请把机器人设为管理员，并在群内发送 /ads 查看总菜单。"
        )
        await message.answer(greeting)
        return
    async with session_scope() as session:
        group = await repo.ensure_group(
            session,
            message.chat.id,
            title=message.chat.title or "",
            username=message.chat.username or "",
            chat_type=message.chat.type,
        )
        _ = group
    await message.answer(
        "🛡 luygroup 已就位。发送 /ads 查看总菜单，/rules 查看群规。"
    )


@router.message(Command("ads"))
async def cmd_ads(message: Message) -> None:
    """Show the command menu for the current chat type."""
    text = (
        GROUP_MENU_TEXT
        if message.chat.type in {"group", "supergroup"}
        else PRIVATE_MENU_TEXT
    )
    await message.answer(text)


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    text = (
        GROUP_MENU_TEXT
        if message.chat.type in {"group", "supergroup"}
        else PRIVATE_MENU_TEXT
    )
    await message.answer(text)


@router.message(Command("rules"))
async def cmd_rules(message: Message) -> None:
    if message.chat.type == "private":
        await message.reply("群规只能在群组中查看。")
        return
    async with session_scope() as session:
        group = await repo.ensure_group(
            session,
            message.chat.id,
            title=message.chat.title or "",
            username=message.chat.username or "",
            chat_type=message.chat.type,
        )
        text = group.rules_text or "本群暂未设置群规。管理员可发送 /setrules 文本 来设置。"
    await message.reply(f"📜 <b>{escape(message.chat.title or '本群')} 群规</b>\n\n{escape(text)}")


@router.message(Command("id"))
async def cmd_id(message: Message) -> None:
    lines = [f"👤 你的 ID：<code>{message.from_user.id}</code>"]
    if message.chat.type != "private":
        lines.append(f"💬 本群 ID：<code>{message.chat.id}</code>")
    if message.reply_to_message and message.reply_to_message.from_user:
        user = message.reply_to_message.from_user
        lines.append(
            f"🎯 被回复用户：<code>{user.id}</code> "
            f"{escape(user.first_name or '')} {('@' + user.username) if user.username else ''}"
        )
    await message.reply("\n".join(lines))


@router.message(Command("console", "panel"))
async def cmd_panel(message: Message, bot: Bot) -> None:
    user_id = message.from_user.id if message.from_user else None
    if message.chat.type != "private":
        # Console addresses and Tokens are never posted in groups or channels.
        if is_global_admin(user_id) or await _has_console_user(user_id):
            await message.reply(
                "🔒 订阅控制台地址不会在群内公开。请私聊 @"
                f"{get_config().bot.username or '机器人'} 并发送 /console 获取。"
            )
        else:
            await message.reply(
                "🔒 订阅控制台仅限私聊使用，群组或频道内不会展示地址。"
                "请私聊机器人发送 /console。"
            )
        return
    if not await _has_console_user(user_id):
        await _ensure_console_user(message)
    await _issue_console_access(message)


async def _issue_console_access(message: Message) -> None:
    user_id = message.from_user.id if message.from_user else None
    if not user_id or message.chat.type != "private":
        return
    async with session_scope() as session:
        user = await session.scalar(
            select(ConsoleUser).where(ConsoleUser.telegram_id == int(user_id))
        )
        if user is None:
            user = await _ensure_console_user(message, session)
        else:
            user.username = (message.from_user.username or user.username or "")[:128]
            user.display_name = (
                message.from_user.full_name
                or message.from_user.first_name
                or user.display_name
                or ""
            )[:255]
        if not user.is_active:
            return
        now = utcnow()
        token = new_console_token()
        user.token_hash = hash_console_token(token)
        user.token_value = None
        user.token_expires_at = now + timedelta(minutes=15)
        entitlements = await build_for_user(session, user)
        bound = int(
            await session.scalar(
                select(func.count(ConsoleGroupAccess.id)).where(
                    ConsoleGroupAccess.user_id == user.id
                )
            )
            or 0
        )
        await session.flush()
        web = get_config().web
        url = f"{web.base_url.rstrip('/')}/console?token={token}"
        group_limit = "不限" if entitlements.group_limit is None else str(entitlements.group_limit)
        if entitlements.subscription is not None:
            plan = entitlements.plan
            plan_name = plan.name if plan else "订阅版"
            expiry = "永久" if entitlements.subscription.expires_at is None else repo.dt(entitlements.subscription.expires_at)
            subscription_text = f"{plan_name}，到期 {expiry}"
        elif entitlements.is_superadmin:
            subscription_text = "超级管理员（长期有效）"
        else:
            subscription_text = "无"
        account = (
            f"{user.telegram_id} (@{user.username})"
            if user.username
            else str(user.telegram_id)
        )
        balance_cents = int(user.balance_cents or 0)
        balance = f"{balance_cents // 100}.{balance_cents % 100:02d}"
        text = (
            f"账号: {escape(account)}\n"
            f"余额: {balance}\n"
            f"绑定上限: {group_limit}，当前已绑定 {bound}\n"
            f"token: <code>{token}</code>\n"
            f"控制台: {escape(web.base_url.rstrip('/'))}/console\n"
            f"当前订阅: {escape(subscription_text)}\n\n"
            "登录码 15 分钟内有效，只能使用一次。\n"
            "登录后可使用已开通的订阅功能并配置自己的 AI 接口。"
        )
    await message.reply(
        text,
        reply_markup=keyboards.console_login_kb(url),
        disable_web_page_preview=True,
    )


@router.message(Command("token", "panel_token"))
async def cmd_token(message: Message) -> None:
    if message.chat.type != "private":
        if await _has_console_user(message.from_user.id if message.from_user else None):
            await message.reply("🔒 Token 不会在群聊中显示，请私聊机器人发送 /console。")
        return
    if not await _has_console_user(message.from_user.id if message.from_user else None):
        await _ensure_console_user(message)
    await _issue_console_access(message)


async def _require_private(message: Message, command: str) -> bool:
    if message.chat.type == "private":
        return True
    await message.reply(f"🔒 请在私聊中发送 /{command}，群聊不会显示订阅或自助操作结果。")
    return False


async def _private_self_id(message: Message) -> int | None:
    return message.from_user.id if message.from_user else None


@router.message(Command("sub", "subscription"))
async def cmd_sub(message: Message) -> None:
    """Show the caller's current plan and effective entitlements."""
    if not await _require_private(message, "sub"):
        return
    user_id = await _private_self_id(message)
    if user_id is None:
        return
    await _ensure_console_user(message)
    async with session_scope() as session:
        current = await session.scalar(
            select(ConsoleUser).where(ConsoleUser.telegram_id == int(user_id))
        )
        if current is None or not current.is_active:
            await message.reply("⛔ 你的控制台账号已停用，请联系超级管理员。")
            return
        ent = await build_for_user(session, current)
        bound = int(
            await session.scalar(
                select(func.count(ConsoleGroupAccess.id)).where(
                    ConsoleGroupAccess.user_id == current.id
                )
            )
            or 0
        )
        role = ent.role_label
        if ent.is_superadmin:
            plan_label = "超级管理员"
            status = "平台管理，无限制"
            expires = "长期有效"
            group_limit = "不限"
            ai_quota = "不限"
            feature_text = "已解锁全部功能。"
        else:
            plan_label = (
                (ent.plan.label or ent.plan.name)
                if ent.plan
                else ("订阅版" if ent.advanced else "基础版")
            )
            if ent.subscription is not None:
                status = "生效中"
                expires = (
                    "长期有效"
                    if ent.subscription.expires_at is None
                    else repo.dt(ent.subscription.expires_at)
                )
            else:
                status = "未订阅" if not ent.advanced else "订阅已生效"
                expires = "-"
            group_limit = "不限" if ent.group_limit is None else str(ent.group_limit)
            if not ent.advanced:
                ai_quota = "未开通"
                feature_text = (
                    "基础功能：反广告、警告、白名单、关键词、必关频道、"
                    "基础进群验证、成员查询、管理日志。"
                )
            else:
                ai_quota = (
                    "不限"
                    if not ent.ai_daily_quota
                    else f"{ent.ai_daily_quota} 次/天"
                )
                feature_text = (
                    "高级功能：AI 检测/助理/闲聊、跨群封禁、全局黑名单、"
                    "积分/商城/红包、欢迎语、抽奖/竞拍、协助管理员、多群绑定。"
                )
    lines = [
        "📋 <b>我的订阅</b>",
        f"👤 账号：<code>{user_id}</code>",
        f"🏷 角色：{role}",
        f"📦 套餐：{plan_label}",
        f"📅 状态：{status} · 到期：{expires}",
        f"🏘 可绑定群组：{group_limit} · 已绑定：{bound}",
        f"🤖 AI 每日额度：{ai_quota}",
        f"💰 账户余额：¥{int(current.balance_cents or 0) // 100}.{int(current.balance_cents or 0) % 100:02d} CNY",
        "",
        feature_text,
        "",
        "发送 /console 打开控制台，在「账户与订阅」中以 ¥8.00 CNY/月购买或续费。",
    ]
    await message.reply("\n".join(lines))


async def _toggle_push(message: Message, field: str, label: str, command: str) -> None:
    if not await _require_private(message, command):
        return
    user_id = await _private_self_id(message)
    if user_id is None:
        return
    args = helpers.parse_args(message.text)
    raw = (args[0].lower() if args else "").strip()
    if raw not in {"", "on", "off", "开", "关", "开启", "关闭", "1", "0"}:
        await message.reply(
            f"用法：/{command} [on|off]\n"
            f"不带参数时切换当前的{label}状态。"
        )
        return
    async with session_scope() as session:
        pref = await session.scalar(
            select(UserNotificationPreference).where(
                UserNotificationPreference.telegram_id == int(user_id)
            )
        )
        if pref is None:
            pref = UserNotificationPreference(telegram_id=int(user_id))
            session.add(pref)
            await session.flush()
        if raw in {"on", "开", "开启", "1"}:
            enabled = True
        elif raw in {"off", "关", "关闭", "0"}:
            enabled = False
        else:
            enabled = not bool(getattr(pref, field))
        setattr(pref, field, enabled)
    if enabled:
        await message.reply(
            f"✅ 已开启{label}推送。\n有新活动时会私聊通知你，发送 /{command} off 可关闭。"
        )
    else:
        await message.reply(f"🔕 已关闭{label}推送。")


@router.message(Command("cjtz"))
async def cmd_cjtz(message: Message) -> None:
    await _toggle_push(message, "lottery_enabled", "抽奖", "cjtz")


@router.message(Command("jptz"))
async def cmd_jptz(message: Message) -> None:
    await _toggle_push(message, "auction_enabled", "竞拍", "jptz")


async def _ensure_console_user(
    message: Message, session=None
) -> ConsoleUser | None:
    """Create the console identity on the user's first private interaction."""
    telegram_user = message.from_user
    if telegram_user is None or message.chat.type != "private":
        return None

    async def find_or_create(db_session) -> ConsoleUser:
        user = await db_session.scalar(
            select(ConsoleUser).where(ConsoleUser.telegram_id == int(telegram_user.id))
        )
        if user is None:
            user = ConsoleUser(
                telegram_id=int(telegram_user.id),
                username=telegram_user.username or "",
                display_name=(telegram_user.full_name or telegram_user.first_name or "")[:255],
                role="superadmin" if is_global_admin(telegram_user.id) else "user",
                token_hash=hash_console_token(new_console_token()),
            )
            # The first token is issued by /console, so do not expose
            # a credential merely because the user sent /start.
            user.token_value = None
            user.token_expires_at = None
            db_session.add(user)
            await db_session.flush()
        else:
            user.username = telegram_user.username or user.username or ""
            user.display_name = (telegram_user.full_name or telegram_user.first_name or user.display_name or "")[:255]
            if is_global_admin(telegram_user.id):
                user.role = "superadmin"
        return user

    if session is not None:
        return await find_or_create(session)
    async with session_scope() as db_session:
        return await find_or_create(db_session)


async def _has_console_user(user_id: int | None) -> bool:
    """Return whether a Telegram account has an active console identity."""
    if user_id is None:
        return False
    async with session_scope() as session:
        user = await session.scalar(
            select(ConsoleUser).where(
                ConsoleUser.telegram_id == int(user_id),
                ConsoleUser.is_active.is_(True),
            )
        )
    return user is not None


@router.message(Command("unbanme"))
async def cmd_unbanme(message: Message, bot: Bot) -> None:
    """Clear automatic bans across groups without overriding manual bans."""
    if not await _require_private(message, "unbanme"):
        return
    user_id = await _private_self_id(message)
    if user_id is None:
        return

    unlocked: list[str] = []
    manual: list[str] = []
    failed: list[str] = []
    async with session_scope() as session:
        blacklisted = await session.scalar(
            select(GlobalBlacklist.id).where(GlobalBlacklist.user_id == int(user_id)).limit(1)
        )
        if blacklisted is not None:
            await message.reply(
                "⛔ 你仍在全局黑名单中，无法自助解封。请联系超级管理员处理。"
            )
            return
        members = list(
            (
                await session.scalars(
                    select(Member).where(
                        Member.user_id == int(user_id), Member.is_banned.is_(True)
                    )
                )
            ).all()
        )
        if not members:
            await message.reply("✅ 你当前没有被机器人记录的封禁。")
            return
        for member in members:
            group = await session.get(Group, int(member.group_id))
            if group is None or not group.is_active:
                continue
            title = group.title or str(group.id)
            latest = await session.scalar(
                select(AdminLog)
                .where(
                    AdminLog.group_id == group.id,
                    AdminLog.target_id == int(user_id),
                    AdminLog.action.in_(("ban", "crossban")),
                )
                .order_by(AdminLog.created_at.desc(), AdminLog.id.desc())
                .limit(1)
            )
            if latest is not None and latest.admin_id is not None:
                manual.append(title)
                continue
            try:
                await bot.unban_chat_member(int(group.id), int(user_id), only_if_banned=True)
            except TelegramAPIError:
                failed.append(title)
                continue
            member.is_banned = False
            # Self-unban only clears the ban.  A previous ban/kick still
            # carries the rejoin-mute flag; only an explicit admin unmute (or
            # the web "解禁" action) clears that persistent restriction.
            await repo.add_log(
                session, group.id, user_id, "self_unban", user_id, "自助解封"
            )
            unlocked.append(title)

    if not unlocked and not manual and not failed:
        await message.reply("✅ 没有可自助解除的封禁。")
        return
    lines = []
    if unlocked:
        lines.append(f"✅ 已自助解除 {len(unlocked)} 个群组的封禁：")
        lines.extend(f"· {escape(name)}" for name in unlocked[:8])
        if len(unlocked) > 8:
            lines.append(f"…… 其余 {len(unlocked) - 8} 个群组")
    if manual:
        lines.append("\n⛔ 以下群组由管理员手动封禁，需联系管理员解除：")
        lines.extend(f"· {escape(name)}" for name in manual[:8])
        if len(manual) > 8:
            lines.append(f"…… 其余 {len(manual) - 8} 个群组")
    if failed:
        lines.append("\n⚠️ 以下群组解除失败（机器人权限不足）：")
        lines.extend(f"· {escape(name)}" for name in failed[:8])
    await message.reply("\n".join(lines))


@router.message(Command("unmuteme"))
async def cmd_unmuteme(message: Message, bot: Bot) -> None:
    """Clear automatic mutes across groups without overriding manual mutes."""
    if not await _require_private(message, "unmuteme"):
        return
    user_id = await _private_self_id(message)
    if user_id is None:
        return

    unlocked: list[str] = []
    manual: list[str] = []
    failed: list[str] = []
    async with session_scope() as session:
        members = list(
            (
                await session.scalars(
                    select(Member).where(
                        Member.user_id == int(user_id), Member.is_muted.is_(True)
                    )
                )
            ).all()
        )
        if not members:
            await message.reply("✅ 你当前没有被机器人记录的禁言。")
            return
        for member in members:
            group = await session.get(Group, int(member.group_id))
            if group is None or not group.is_active:
                continue
            title = group.title or str(group.id)
            if member.mute_on_rejoin:
                manual.append(f"{title}（入群自动禁言）")
                continue
            latest = await session.scalar(
                select(AdminLog)
                .where(
                    AdminLog.group_id == group.id,
                    AdminLog.target_id == int(user_id),
                    AdminLog.action == "mute",
                )
                .order_by(AdminLog.created_at.desc(), AdminLog.id.desc())
                .limit(1)
            )
            if latest is not None and latest.admin_id is not None:
                manual.append(title)
                continue
            target = helpers.Target(
                int(user_id),
                member.first_name or (message.from_user.first_name if message.from_user else ""),
                member.username or (message.from_user.username if message.from_user else ""),
            )
            if member.is_member:
                try:
                    await helpers.unmute_user(
                        bot, session, group, target, admin_id=user_id
                    )
                except TelegramAPIError:
                    failed.append(title)
                    continue
            else:
                member.is_muted = False
                member.muted_until = None
                member.mute_on_rejoin = False
                await repo.add_log(
                    session, group.id, user_id, "self_unmute", user_id, "自助解除禁言"
                )
            unlocked.append(title)

    if not unlocked and not manual and not failed:
        await message.reply("✅ 没有可自助解除的禁言。")
        return
    lines = []
    if unlocked:
        lines.append(f"✅ 已自助解除 {len(unlocked)} 个群组的禁言：")
        lines.extend(f"· {escape(name)}" for name in unlocked[:8])
        if len(unlocked) > 8:
            lines.append(f"…… 其余 {len(unlocked) - 8} 个群组")
    if manual:
        lines.append("\n⛔ 以下群组需管理员解除：")
        lines.extend(f"· {escape(name)}" for name in manual[:8])
        if len(manual) > 8:
            lines.append(f"…… 其余 {len(manual) - 8} 个群组")
    if failed:
        lines.append("\n⚠️ 以下群组解除失败（机器人权限不足）：")
        lines.extend(f"· {escape(name)}" for name in failed[:8])
    await message.reply("\n".join(lines))


@router.message(Command("ping"))
async def cmd_ping(message: Message) -> None:
    await message.reply("🏓 pong")


@router.message(Command("info", "me"))
async def cmd_info(message: Message, bot: Bot) -> None:
    if message.chat.type not in {"group", "supergroup"}:
        await message.reply("请在群组中使用 /info。")
        return
    args = helpers.parse_args(message.text)
    target = await helpers.resolve_target(bot, message, args)
    self_id = message.from_user.id if message.from_user else 0
    if target is None:
        target = helpers.Target(
            self_id,
            message.from_user.first_name if message.from_user else "",
            message.from_user.username if message.from_user else "",
        )
    if target.user_id != self_id and not is_global_admin(self_id):
        async with session_scope() as session:
            allowed = await helpers.has_permission(bot, session, message.chat.id, self_id, "info")
        if not allowed:
            await message.reply("⛔ 查询他人信息需要管理员权限。")
            return

    async with session_scope() as session:
        group = await repo.ensure_group(
            session,
            message.chat.id,
            title=message.chat.title or "",
            username=message.chat.username or "",
            chat_type=message.chat.type,
        )
        member = await repo.get_member(session, group.id, target.user_id)
        warns = await repo.warning_count(session, group.id, target.user_id)
        whitelisted = await repo.is_whitelisted(session, group.id, target.user_id)
        admin = await repo.get_admin(session, group.id, target.user_id)

    status = "普通成员"
    try:
        tg_member = await bot.get_chat_member(message.chat.id, target.user_id)
        if isinstance(tg_member, ChatMemberOwner):
            status = "群主"
        elif isinstance(tg_member, ChatMemberAdministrator):
            status = "管理员"
        else:
            status = getattr(tg_member.status, "value", str(tg_member.status))
    except Exception:
        status = "未知"

    lines = [
        f"👤 <b>{escape(target.first_name or '未知')}</b>"
        f"{(' @' + escape(target.username)) if target.username else ''}",
        f"🆔 <code>{target.user_id}</code>",
        f"🏷 群内身份：{status}" + (" · 机器人协助管理员" if admin else ""),
    ]
    if member:
        lines += [
            f"📅 加入时间：{repo.dt(member.joined_at)}",
            f"💬 发言数：{member.message_count}",
            f"⭐ {escape(group.points_name or '积分')}：{member.points}",
            f"🔇 禁言状态：{'禁言至 ' + repo.dt(member.muted_until) if member.is_muted else '正常'}",
        ]
    else:
        lines.append("📅 暂无本地记录（加入后会自动建立）")
    lines += [
        f"⚠️ 有效警告：{warns} / {group.warn_limit}",
        f"✅ 白名单：{'是' if whitelisted else '否'}",
        f"🚫 封禁记录：{'是' if member and member.is_banned else '否'}",
    ]
    await message.reply("\n".join(lines))


@router.message(Command("warns"))
async def cmd_warns(message: Message, bot: Bot) -> None:
    if message.chat.type not in {"group", "supergroup"}:
        await message.reply("请在群组中使用 /warns。")
        return
    args = helpers.parse_args(message.text)
    self_id = message.from_user.id if message.from_user else 0
    target = await helpers.resolve_target(bot, message, args)
    if target is None:
        target = helpers.Target(
            self_id,
            message.from_user.first_name if message.from_user else "",
            message.from_user.username if message.from_user else "",
        )
    if target.user_id != self_id:
        async with session_scope() as session:
            allowed = await helpers.has_permission(bot, session, message.chat.id, self_id, "warn")
        if not allowed:
            await message.reply("⛔ 查询他人警告记录需要管理员权限。")
            return
    async with session_scope() as session:
        group = await repo.ensure_group(session, message.chat.id)
        warnings = await repo.active_warnings(session, group.id, target.user_id)
    if not warnings:
        await message.reply(f"✅ {target.mention} 没有有效警告记录。")
        return
    lines = [f"⚠️ <b>{escape(target.label)} 的警告记录</b>（{len(warnings)} 条）"]
    for warning in warnings[:15]:
        lines.append(
            f"· {repo.dt(warning.created_at)} · {escape(warning.reason or '未填写原因')}"
        )
    if len(warnings) > 15:
        lines.append(f"…… 其余 {len(warnings) - 15} 条请在 Web 面板查看")
    await message.reply("\n".join(lines))


@router.message(Command("fy"))
async def cmd_fy(message: Message, bot: Bot) -> None:
    """Show 10-day, 30-day and all-time message counts for a member."""
    if message.chat.type not in {"group", "supergroup"}:
        await message.reply("请在群组中使用 /fy。")
        return
    if message.from_user is None:
        return
    self_id = message.from_user.id
    target = await helpers.resolve_target(bot, message, helpers.parse_args(message.text))
    if target is None:
        target = helpers.Target(
            self_id, message.from_user.first_name or "", message.from_user.username or ""
        )

    async with session_scope() as session:
        group = await repo.ensure_group(
            session,
            message.chat.id,
            title=message.chat.title or "",
            username=message.chat.username or "",
            chat_type=message.chat.type,
        )
        if target.user_id != self_id and not await helpers.has_permission(
            bot, session, group.id, self_id, "info"
        ):
            await message.reply("⛔ 查询他人发言统计需要管理员权限。")
            return
        member = await repo.get_member(session, group.id, target.user_id)
        since10 = (utcnow() - timedelta(days=9)).strftime("%Y-%m-%d")
        since30 = (utcnow() - timedelta(days=29)).strftime("%Y-%m-%d")
        total10 = int(
            await session.scalar(
                select(func.sum(MemberDailyStat.messages)).where(
                    MemberDailyStat.group_id == group.id,
                    MemberDailyStat.user_id == target.user_id,
                    MemberDailyStat.day >= since10,
                )
            )
            or 0
        )
        total30 = int(
            await session.scalar(
                select(func.sum(MemberDailyStat.messages)).where(
                    MemberDailyStat.group_id == group.id,
                    MemberDailyStat.user_id == target.user_id,
                    MemberDailyStat.day >= since30,
                )
            )
            or 0
        )
        all_time = int(member.message_count or 0) if member else 0

    lines = [
        f"📊 <b>{target.mention} 的发言统计</b>",
        f"近 10 天：<b>{total10}</b> 条",
        f"近 30 天：<b>{total30}</b> 条",
        f"累计发言：<b>{all_time}</b> 条",
        "",
        "近 10/30 天数据从每日计数功能启用后开始记录，累计发言不受影响。",
    ]
    await message.reply("\n".join(lines))


_ACTION_LABELS = {
    "ban": "封禁",
    "auto_ban": "自动封禁",
    "crossban": "跨群封禁",
    "unban": "解封",
    "global_unban": "全局解封",
    "self_unban": "自助解封",
    "kick": "移出群聊",
    "auto_kick": "自动移出",
    "mute": "禁言",
    "auto_mute": "自动禁言",
    "unmute": "解除禁言",
    "self_unmute": "自助解除禁言",
    "rejoin_mute": "入群自动禁言",
    "warn": "警告",
    "auto_warn": "自动警告",
    "gblacklist_add": "加入全局黑名单",
    "blacklist": "黑名单处理",
    "bio_block": "简介拦截",
}


@router.message(Command("cf"))
async def cmd_cf(message: Message, bot: Bot) -> None:
    """Show moderation history and current restriction state for a member."""
    if message.chat.type not in {"group", "supergroup"}:
        await message.reply("请在群组中使用 /cf。")
        return
    if message.from_user is None:
        return
    self_id = message.from_user.id
    target = await helpers.resolve_target(bot, message, helpers.parse_args(message.text))
    if target is None:
        target = helpers.Target(
            self_id, message.from_user.first_name or "", message.from_user.username or ""
        )

    async with session_scope() as session:
        group = await repo.ensure_group(
            session,
            message.chat.id,
            title=message.chat.title or "",
            username=message.chat.username or "",
            chat_type=message.chat.type,
        )
        if target.user_id != self_id and not await helpers.has_permission(
            bot, session, group.id, self_id, "logs"
        ):
            await message.reply("⛔ 查询他人处置记录需要管理员日志权限。")
            return
        member = await repo.get_member(session, group.id, target.user_id)
        warns = await repo.warning_count(session, group.id, target.user_id)
        logs = list(
            (
                await session.scalars(
                    select(AdminLog)
                    .where(
                        AdminLog.group_id == group.id,
                        AdminLog.target_id == target.user_id,
                    )
                    .order_by(AdminLog.created_at.desc(), AdminLog.id.desc())
                    .limit(20)
                )
            ).all()
        )

    if member is None:
        status = "暂无本地记录"
    elif member.is_banned:
        status = "封禁中"
    elif member.is_muted:
        status = "禁言中"
    else:
        status = "正常"
    if member and member.mute_on_rejoin:
        status += " · 重新入群会自动禁言"

    lines = [
        f"📋 <b>{target.mention} 的处置记录</b>",
        f"当前状态：{status} · 有效警告：{warns}",
    ]
    if not logs:
        lines.append("暂无封禁、禁言或其他处置记录。")
    else:
        lines.append(f"最近 {len(logs)} 条记录：")
        for entry in logs:
            label = _ACTION_LABELS.get(entry.action, entry.action)
            if entry.admin_id is None:
                actor = "自动处理"
            elif int(entry.admin_id) == int(target.user_id):
                actor = "本人"
            else:
                actor = f"管理员 <code>{entry.admin_id}</code>"
            detail = escape(entry.detail or "")
            line = f"· {repo.dt(entry.created_at)} · {label} · {actor}"
            if detail:
                line += f"\n  {detail}"
            lines.append(line)
    await message.reply("\n".join(lines))


@router.message(Command("stats"))
async def cmd_stats(message: Message, bot: Bot) -> None:
    if message.chat.type == "private":
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
            await message.reply("⛔ 群统计仅管理员可查看。")
            return
        today = day_key()
        stat = await session.scalar(
            select(GroupDailyStat).where(
                GroupDailyStat.group_id == group.id, GroupDailyStat.day == today
            )
        )
        total_warn = int(
            await session.scalar(
                select(func.count(Warning.id)).where(
                    Warning.group_id == group.id, Warning.active.is_(True)
                )
            )
            or 0
        )
        total_hits = int(
            await session.scalar(
                select(func.count(KeywordHit.id)).where(KeywordHit.group_id == group.id)
            )
            or 0
        )
        top = await repo.top_members(session, group.id, 5)
        usage = await repo.get_usage(session, group.id)

    lines = [
        f"📊 <b>{escape(group.title or '本群')} 统计</b>",
        f"👥 成员：{group.member_count}",
        f"💬 累计消息：{group.message_count}",
        f"📅 今日消息：{stat.messages if stat else 0} · 进群 {stat.joins if stat else 0} · 退群 {stat.leaves if stat else 0}",
        f"⚠️ 有效警告：{total_warn}",
        f"🧹 违规命中：{total_hits}",
        f"🤖 今日 AI：请求 {usage.requests}/{group.ai_daily_quota} · 拦截 {usage.hits}",
    ]
    if top:
        lines.append("\n🏆 <b>发言榜</b>")
        for idx, member in enumerate(top, 1):
            lines.append(
                f"{idx}. {escape(member.first_name or member.username or str(member.user_id))} · {member.message_count}"
            )
    await message.reply("\n".join(lines))


@router.callback_query(F.data.startswith("help:"))
async def on_help_menu(callback: CallbackQuery) -> None:
    key = (callback.data or "").split(":", 1)[-1] or "home"
    if key == "group":
        text = GROUP_MENU_TEXT
        markup = keyboards.back_menu()
    elif key == "private":
        text = PRIVATE_MENU_TEXT
        markup = keyboards.back_menu()
    else:
        text = "🛡 <b>luygroup</b>\n请选择要查看的命令菜单。"
        markup = keyboards.main_menu()
    if callback.message:
        try:
            await callback.message.edit_text(text, reply_markup=markup)
        except Exception:
            try:
                await callback.message.answer(text, reply_markup=markup)
            except Exception:
                pass
    await callback.answer()


@router.callback_query(F.data == "panel:close")
async def on_panel_close(callback: CallbackQuery) -> None:
    if callback.message:
        try:
            await callback.message.delete()
        except Exception:
            pass
    await callback.answer("已关闭")
