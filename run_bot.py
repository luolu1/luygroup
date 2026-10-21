"""Run only the Telegram bot."""
from __future__ import annotations

import asyncio

from app.bot.main import main

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
