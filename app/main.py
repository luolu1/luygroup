"""Combined entry point: Telegram bot polling + web control panel."""
from __future__ import annotations

import asyncio
import logging
from contextlib import suppress

import uvicorn

from .bot.main import run_bot
from .config import get_config

log = logging.getLogger(__name__)


async def _run_bot_safely() -> None:
    try:
        await run_bot()
    except asyncio.CancelledError:
        raise
    except Exception:
        # Keep the panel online so the operator can inspect logs/settings even
        # when Telegram is unreachable or the token is rejected.
        log.exception("Telegram bot stopped unexpectedly; panel stays online")


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    )
    # httpx logs full request URLs at INFO, which would expose the bot token.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    cfg = get_config()
    server = uvicorn.Server(
        uvicorn.Config(
            "app.web.main:app",
            host=cfg.web.host,
            port=cfg.web.port,
            log_level="info",
            access_log=False,
        )
    )

    bot_task = asyncio.create_task(_run_bot_safely(), name="groupguard-bot")
    try:
        await server.serve()
    finally:
        bot_task.cancel()
        with suppress(asyncio.CancelledError):
            await bot_task


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
