"""General commands: help, rules, info, warnings lookup, stats."""
from __future__ import annotations

from datetime import timedelta
from html import escape

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, ChatMemberAdministrator, ChatMemberOwner, Message
from sqlalchemy import func, select

from ...config import get_config, is_global_admin
from ...db import session_scope
from ...models import ConsoleUser, GroupDailyStat, KeywordHit, Warning, day_key, utcnow
from ...web.auth import hash_console_token, new_console_path, new_console_token
from ...services import repo
from .. import helpers, keyboards

router = Router(name="commands")


HELP_TEXTS = {
    "home": (
        "🛡 <b>群卫 GroupGuard</b>\n"
        "群组安全、成员管理、积分玩法与 AI 助理，一个机器人全搞定。\n\n"
        "选择下面的分类查看命令，或发送 /rules 查看群规。"
    ),
    "admin": (
        "🛡 <b>管理命令</b>\n"
        "/ban 回复或 @用户 —— 封禁\n"
        "/unban —— 解封\n"
        "/kick —— 移出群聊\n"
        "/mute [分钟] —— 禁言\n"
        "/unmute —— 解除禁言\n"
        "/warn [原因] —— 警告\n"
        "/warns [用户] —— 查询警告记录\n"
        "/resetwarns —— 清空警告\n"
        "/del —— 删除被回复的消息\n"
        "/info [用户] —— 成员信息\n"
        "/stats —— 本群统计\n\n"
        "关键词：/addkeyword、/keywords、/delkeyword\n"
        "白名单：/whitelist、/unwhitelist\n"
        "全局黑名单：/gblacklist add|del|list\n"
        "日志：/logs [数量]"
    ),
    "points": (
        "⭐ <b>积分与玩法</b>\n"
        "/points —— 查看我的积分\n"
        "/checkin —— 每日签到\n"
        "/shop —— 积分商城\n"
        "/buy 编号 —— 购买商品\n"
        "/redpacket 总额 个数 [备注] —— 发积分红包\n"
        "/lottery 标题 | 奖品 [参与费] [分钟] —— 发起抽奖\n"
        "/auction 物品 | 起拍价 [加价幅度] [分钟] —— 发起竞拍\n"
        "/bid 金额 —— 竞拍出价"
    ),
    "games": (
        "🎁 <b>抽奖与竞拍</b>\n"
        "抽奖消息上的「参与抽奖」按钮点击即可报名；到期自动开奖。\n"
        "竞拍使用积分出价，被超过后自动退款；到期最高价成交。\n\n"
        "发起命令见「积分玩法」分类。"
    ),
    "ai": (
        "🤖 <b>AI 功能</b>\n"
        "/ask 问题 —— 询问 AI 助理\n"
        "在群里 @机器人 也可以直接提问。\n\n"
        "管理员可在 Web 面板单独开关：\n"
        "· AI 广告检测\n"
        "· AI 管理助理\n"
        "· AI 闲聊\n"
        "并配置每日请求额度与优先/备用接口。"
    ),
    "settings": (
        "⚙️ <b>管理员设置</b>\n"
        "/settings —— 当前设置摘要\n"
        "/setrules 文本 —— 修改群规\n"
        "/setwarn 次数 mute|kick|ban —— 警告阈值\n"
        "/setflood 条数 秒数 mute|kick|ban —— 刷屏限制\n"
        "/setmute 分钟 —— 默认禁言时长\n"
        "/setwelcome 文本 —— 欢迎语，支持 {name} {group}\n"
        "/setverify on|off —— 进群验证\n"
        "/setai ad|assistant|chat on|off —— AI 开关\n"
        "/setquota 请求数 样本数 —— 每日 AI 额度\n"
        "/setcrossban on|off|own|shared —— 跨群封禁\n"
        "/setreport on|off —— 通报开关\n"
        "/channels —— 必关频道\n"
        "/addchannel @频道 [链接] —— 添加必关频道\n"
        "/delchannel @频道 —— 删除必关频道\n"
        "/assistant add|del|list [用户] —— 协助管理员\n"
        "/crosslink add|del|list [群ID] —— 共享联动群\n"
        "/reportlink add|del|list [群ID] —— 通报订阅\n"
        "/panel —— 私聊获取 Web 面板登录地址（群内不公开）\n"
        "/token —— 查看你的控制台 Token（24 小时内保持不变，仅管理员私聊）"
    ),
}


@router.message(CommandStart())
async def cmd_start(message: Message, bot: Bot) -> None:
    if message.chat.type == "private":
        await _ensure_console_user(message)
        greeting = (
            "👋 你好，我是 <b>群卫 GroupGuard</b>。\n"
            "把我拉进群并设为管理员，即可使用进群验证、反广告、警告、积分与抽奖等功能。\n\n"
            "发送 /help 查看全部命令。"
        )
        if await _has_console_user(message.from_user.id if message.from_user else None):
            greeting += "\n\n发送 /panel 获取控制台登录地址，发送 /token 查看 Token。"
        await message.answer(greeting, reply_markup=keyboards.main_menu())
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
        "🛡 群卫已就位。发送 /help 查看命令，/rules 查看群规。",
        reply_markup=keyboards.main_menu(),
    )


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(HELP_TEXTS["home"], reply_markup=keyboards.main_menu())


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


@router.message(Command("panel"))
async def cmd_panel(message: Message, bot: Bot) -> None:
    user_id = message.from_user.id if message.from_user else None
    if message.chat.type != "private":
        # The panel URL is never posted in groups or channels.
        if is_global_admin(user_id) or await _has_console_user(user_id):
            await message.reply(
                "🔒 面板地址不会在群内公开。请私聊 @"
                f"{get_config().bot.username or '机器人'} 并发送 /panel 获取。"
            )
        else:
            await message.reply(
                "🔒 面板仅限已开通控制台账号的用户私聊使用，群组或频道内不会展示地址。"
            )
        return
    if not await _has_console_user(user_id):
        await _ensure_console_user(message)
    await _issue_console_access(message)


async def _issue_console_access(message: Message) -> None:
    """Show the user's current token, rotating it every 24 hours."""
    user_id = message.from_user.id if message.from_user else None
    if not user_id or message.chat.type != "private":
        return
    async with session_scope() as session:
        user = await session.scalar(
            select(ConsoleUser).where(ConsoleUser.telegram_id == int(user_id))
        )
        if user is None:
            user = await _ensure_console_user(message, session)
        if not user.is_active:
            return
        now = utcnow()
        # Repeated /token calls within the validity window must return the
        # same credential. Legacy users without a stored value are rotated
        # once so the URL can be displayed safely.
        if not user.token_value or not user.token_expires_at or user.token_expires_at <= now:
            token = new_console_token()
            user.token_hash = hash_console_token(token)
            user.token_value = token
            user.token_expires_at = now + timedelta(days=1)
        else:
            token = user.token_value
        await session.flush()
        web = get_config().web
        url = f"{web.base_url.rstrip('/')}/console/{user.console_path}?token={token}"
    await message.reply(
        "🌐 <b>GroupGuard 控制台</b>\n\n"
        "以下是你的控制台登录地址（24 小时内重复发送 /token 不会变化）：\n"
        f"<code>{escape(url)}</code>\n\n"
        "⚠️ Token 有效期 24 小时，过期后再次发送 /token 将自动生成新的 Token。"
    )


@router.message(Command("token", "panel_token"))
async def cmd_token(message: Message) -> None:
    if message.chat.type != "private":
        if await _has_console_user(message.from_user.id if message.from_user else None):
            await message.reply("🔒 Token 不会在群聊中显示，请私聊机器人发送 /token。")
        return
    if not await _has_console_user(message.from_user.id if message.from_user else None):
        await _ensure_console_user(message)
    await _issue_console_access(message)


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
                console_path=new_console_path(),
            )
            # The first token is issued by /token or /panel, so do not expose
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
    if key == "home":
        text = HELP_TEXTS["home"]
        markup = keyboards.main_menu()
    else:
        text = HELP_TEXTS.get(key, HELP_TEXTS["home"])
        markup = keyboards.back_menu()
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
