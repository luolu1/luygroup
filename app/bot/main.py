"""Telegram bot bootstrap and router wiring."""
from __future__ import annotations

import asyncio
import logging
from contextlib import suppress

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.types import BotCommand

from ..config import get_config
from ..db import dispose_db, init_db
from ..services.scheduler import run_scheduler
from .handlers import admin, ai_chat, commands, games, join, moderation, report

log = logging.getLogger(__name__)

BOT_COMMANDS = [
    BotCommand(command="start", description="开始使用 / 打开面板"),
    BotCommand(command="help", description="命令面板"),
    BotCommand(command="rules", description="查看群规"),
    BotCommand(command="report", description="回复消息举报"),
    BotCommand(command="points", description="查看我的积分"),
    BotCommand(command="checkin", description="每日签到"),
    BotCommand(command="shop", description="积分商城"),
    BotCommand(command="redpacket", description="发积分红包"),
    BotCommand(command="lottery", description="发起抽奖"),
    BotCommand(command="auction", description="发起竞拍"),
    BotCommand(command="bid", description="竞拍出价"),
    BotCommand(command="ask", description="询问 AI 助理"),
    BotCommand(command="info", description="成员信息"),
    BotCommand(command="warns", description="警告记录"),
    BotCommand(command="stats", description="群组统计"),
    BotCommand(command="panel", description="私聊获取 Web 面板地址"),
]

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


async def run_bot() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    )
    # httpx logs full request URLs at INFO, which would expose the bot token.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    await init_db()
    bot = build_bot()
    dp = build_dispatcher()
    scheduler = asyncio.create_task(run_scheduler(bot))
    try:
        await bot.set_my_commands(BOT_COMMANDS)
        me = await bot.me()
        log.info("Bot started as @%s (id=%s)", me.username, me.id)
        await bot.delete_webhook(drop_pending_updates=False)
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        scheduler.cancel()
        with suppress(asyncio.CancelledError):
            await scheduler
        await bot.session.close()
        await dispose_db()


async def main() -> None:
    await run_bot()


if __name__ == "__main__":
    asyncio.run(main())
