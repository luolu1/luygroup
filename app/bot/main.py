"""Telegram bot bootstrap and router wiring."""
from __future__ import annotations

import asyncio
import logging
import signal
from contextlib import suppress

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.types import (
    BotCommand,
    BotCommandScopeAllGroupChats,
    BotCommandScopeAllPrivateChats,
)

from ..config import get_config
from ..db import dispose_db, init_db
from ..services.scheduler import run_scheduler
from .handlers import admin, ai_chat, commands, games, join, moderation, report

log = logging.getLogger(__name__)
RETRY_INITIAL_SECONDS = 1
RETRY_MAX_SECONDS = 60

PRIVATE_COMMANDS = [
    BotCommand(command="start", description="快速开始"),
    BotCommand(command="console", description="订阅控制台"),
    BotCommand(command="sub", description="查看订阅"),
    BotCommand(command="unbanme", description="自助解封"),
    BotCommand(command="unmuteme", description="自助解除禁言"),
    BotCommand(command="cjtz", description="开启/关闭抽奖推送"),
    BotCommand(command="cjhistory", description="查看我创建的抽奖"),
    BotCommand(command="cjcancel", description="取消未开奖抽奖"),
    BotCommand(command="jptz", description="开启/关闭竞拍推送"),
]

GROUP_COMMANDS = [
    BotCommand(command="ads", description="总菜单"),
    BotCommand(command="ban", description="回复广告消息封禁"),
    BotCommand(command="iunban", description="全局解封并尝试跨群解除限制"),
    BotCommand(command="jy", description="手动禁言"),
    BotCommand(command="jcjy", description="解除禁言"),
    BotCommand(command="wladd", description="回复用户添加白名单"),
    BotCommand(command="wlrm", description="回复用户移除白名单"),
    BotCommand(command="fy", description="查询近 10/30 天与累计发言"),
    BotCommand(command="cf", description="查询封禁历史等原因"),
    BotCommand(command="jf", description="查看自己的积分"),
    BotCommand(command="id", description="查询用户/群 ID"),
    BotCommand(command="cj", description="私聊配置抽奖"),
    BotCommand(command="jp", description="发起竞拍"),
]

# Older code and tests import BOT_COMMANDS as the "default" list.  Keep the
# private menu as that alias; runtime registration uses scoped menus below so
# group chats only see group commands and private chats only see private ones.
BOT_COMMANDS = PRIVATE_COMMANDS

ROUTERS = [
    # Commands and explicit callbacks first. The catch-all group message
    # handler in moderation must stay last.
    ai_chat.router,
    commands.router,
    admin.router,
    join.router,
    report.router,
    games.router,
    moderation.router,
]


def build_bot() -> Bot:
    cfg = get_config()
    session = AiohttpSession(proxy=cfg.bot.proxy) if cfg.bot.proxy else None
    return Bot(
        cfg.bot.token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        session=session,
    )


def build_dispatcher() -> Dispatcher:
    dp = Dispatcher()

    @dp.update.outer_middleware()
    async def _log_update(handler, event, data):
        try:
            obj = getattr(event, event.event_type, None)
            chat = getattr(obj, "chat", None)
            if chat is None and getattr(obj, "message", None) is not None:
                chat = obj.message.chat
            chat_id = getattr(chat, "id", None)
            command = ""
            if event.event_type == "message" and obj is not None:
                text = (obj.text or "").strip()
                if text.startswith("/"):
                    command = " " + text.split()[0][:40]
            if event.event_type in {"my_chat_member", "chat_member"} or command:
                log.info(
                    "Update %s %s chat=%s%s",
                    event.update_id,
                    event.event_type,
                    chat_id,
                    command,
                )
        except Exception:
            log.exception("Failed to log incoming update")
        return await handler(event, data)

    for router in ROUTERS:
        dp.include_router(router)
    return dp


async def run_bot(*, handle_signals: bool = True) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    )
    # httpx logs full request URLs at INFO, which would expose the bot token.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    await init_db()
    # aiogram forbids attaching the same router to a second Dispatcher, so the
    # Dispatcher is built once and shared by every reconnect attempt.
    dp = build_dispatcher()
    loop = asyncio.get_running_loop()
    current_task = asyncio.current_task()
    signal_handlers: list[signal.Signals] = []
    if handle_signals and current_task is not None:
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, current_task.cancel)
            except NotImplementedError:
                continue
            signal_handlers.append(sig)
    active_bot: list[Bot | None] = [None]

    class ActiveBot:
        def __getattr__(self, name: str):
            bot = active_bot[0]
            if bot is None:
                raise RuntimeError("Telegram bot is not connected")
            return getattr(bot, name)

    scheduler: asyncio.Task[None] | None = None
    retry_delay = RETRY_INITIAL_SECONDS
    bot: Bot | None = None
    try:
        scheduler = asyncio.create_task(run_scheduler(ActiveBot()))
        while True:
            try:
                if bot is None:
                    bot = build_bot()
                    active_bot[0] = bot
                await bot.delete_my_commands()
                await bot.set_my_commands(
                    PRIVATE_COMMANDS, scope=BotCommandScopeAllPrivateChats()
                )
                await bot.set_my_commands(
                    GROUP_COMMANDS, scope=BotCommandScopeAllGroupChats()
                )
                me = await bot.me()
                log.info("Bot started as @%s (id=%s)", me.username, me.id)
                await bot.delete_webhook(drop_pending_updates=False)
                retry_delay = RETRY_INITIAL_SECONDS
                await dp.start_polling(
                    bot,
                    allowed_updates=dp.resolve_used_update_types(),
                    handle_signals=False,
                )
                # Only a stop request ends start_polling(); returning keeps
                # shutdown from spinning into another reconnect.
                return
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception(
                    "Telegram attempt failed; retrying in %ss", retry_delay
                )
                if bot is not None:
                    failed_bot = bot
                    bot = None
                    active_bot[0] = None
                    await failed_bot.session.close()
                await asyncio.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, RETRY_MAX_SECONDS)
    finally:
        for sig in signal_handlers:
            loop.remove_signal_handler(sig)
        if scheduler is not None:
            scheduler.cancel()
            with suppress(asyncio.CancelledError):
                await scheduler
        if bot is not None:
            await bot.session.close()
        await dispose_db()


async def main() -> None:
    await run_bot()


if __name__ == "__main__":
    asyncio.run(main())
