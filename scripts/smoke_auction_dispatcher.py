"""Offline auction regressions: .venv/bin/python scripts/smoke_auction_dispatcher.py.

Only a synthetic token/config and a unique temporary SQLite database are used.
The real dispatcher is built once; only session.make_request is intercepted.
Pass test names to select cases, or --show-cards to inspect emitted card payloads.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import traceback
from collections.abc import Awaitable, Callable
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import aiogram
from aiogram.exceptions import TelegramAPIError
from aiogram.methods import SendMessage
from pydantic import ValidationError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("cases", nargs="*", help="Exact test function names; default: all")
    parser.add_argument("--show-cards", action="store_true")
    options = parser.parse_args()
    with TemporaryDirectory(prefix="auction-dispatcher-", dir="/tmp/opencode") as directory:
        temp = Path(directory)
        config = temp / "synthetic-config.json"
        config.write_text(json.dumps({
            "bot": {"token": "123456789:AUCTION_DISPATCHER_SYNTHETIC_TOKEN", "admin_ids": [], "proxy": ""},
            "database": {"url": f"sqlite+aiosqlite:///{temp / 'auction.db'}"},
            "web": {"secret_key": "auction-dispatcher-synthetic-secret", "base_url": "https://example.test"},
            "ai": {"learning_dir": str(temp / "learning"), "providers": []},
            "payments": {"enabled": False},
        }), encoding="utf-8")
        with patch.dict(os.environ, {"GROUPGUARD_CONFIG": str(config)}):
            from app.bot.main import build_dispatcher
            from app.config import get_config
            from app.db import dispose_db, init_db
            import auction_dispatcher_bid_cases as bids
            import auction_dispatcher_cancel_cases as cancellations
            import auction_dispatcher_failure_cases as failures
            import auction_dispatcher_query_cases as queries
            import auction_dispatcher_race_cases as races
            import auction_dispatcher_setup_cases as setup
            import auction_dispatcher_lifecycle_cases as lifecycle
            from auction_dispatcher_fixture import Harness

            cases: list[tuple[str, Callable[[Harness], Awaitable[None]]]] = [
                (name, value) for module in (setup, bids, queries, failures, races, cancellations, lifecycle)
                for name, value in vars(module).items() if name.startswith("test_") and callable(value)
            ]
            selected = [(name, case) for name, case in cases if not options.cases or name in options.cases]
            unknown = set(options.cases) - {name for name, _case in cases}
            if unknown:
                parser.error(f"Unknown cases: {', '.join(sorted(unknown))}")
            harness = Harness(build_dispatcher())
            failed: list[str] = []
            print(f"aiogram={aiogram.__version__}; dispatcher builds=1; offline cases={len(selected)}")
            try:
                await init_db()
                for name, case in selected:
                    try:
                        await case(harness)
                    except (AssertionError, ValidationError, TelegramAPIError, TypeError) as error:
                        failed.append(name)
                        frame = traceback.extract_tb(error.__traceback__)[-1]
                        print(f"FAIL {name} at {Path(frame.filename).name}:{frame.lineno}: {type(error).__name__}: {error}")
                    else:
                        print(f"PASS {name}")
                    finally:
                        await harness.settle()
                if options.show_cards:
                    for request in harness.session.calls:
                        if isinstance(request, SendMessage) and any(
                            title in (request.text or "") for title in (
                                "<b>拍品最新出价</b>", "<b>竞拍出价情况</b>", "<b>本群竞拍列表</b>"
                            )
                        ):
                            print(f"CARD chat={request.chat_id}\n{request.text}\nMARKUP {request.reply_markup.model_dump_json() if request.reply_markup else None}")
            finally:
                await harness.close()
                await dispose_db()
                get_config.cache_clear()
            print(f"RESULT {len(selected) - len(failed)} passed; {len(failed)} failed")
            if failed:
                raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
