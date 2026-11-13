"""Deterministic auction reminder regressions, with no production config or network.

Run: .venv/bin/python -B scripts/smoke_auction_reminders.py

The outer config stays active across late helper imports. Restart workers borrow
their parent fixture's config and never own or remove its files.
"""
from __future__ import annotations

import asyncio
import atexit
import json
import os
import sys
from contextlib import ExitStack
from datetime import datetime, timedelta
from enum import StrEnum
from html import escape
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Final, assert_never
from unittest.mock import patch

from aiogram.methods import SendMessage
from aiogram.types import InlineKeyboardMarkup

RUNTIME: Final = ExitStack()
if __name__ == "__main__":
    atexit.register(RUNTIME.close)
    if "--restart-worker" in sys.argv[1:2]:
        worker_config = Path(os.environ["GROUPGUARD_CONFIG"]).resolve()
        worker_parent = (
            Path(os.environ["GROUPGUARD_OFFLINE_STORAGE"]) / "tmp"
            if os.environ.get("GROUPGUARD_OFFLINE_PID") == str(os.getpid())
            else Path("/tmp/opencode")
        )
        assert worker_config.parent.parent == worker_parent
        assert worker_config.parent.name.startswith("auction-reminders-")
    else:
        temp = Path(RUNTIME.enter_context(TemporaryDirectory(
            prefix="auction-reminders-suite-", dir="/tmp/opencode",
        )))
        config = temp / "config.json"
        config.write_text(json.dumps({
            "bot": {"token": "123456:offline-reminders", "admin_ids": [], "proxy": ""},
            "web": {"secret_key": "reminders-smoke-secret", "base_url": "https://example.test"},
            "database": {"url": f"sqlite+aiosqlite:///{temp / 'reminders.db'}"},
            "ai": {"learning_dir": str(temp / "learning"), "providers": []},
            "payments": {"enabled": False},
        }), encoding="utf-8")
        RUNTIME.enter_context(patch.dict(os.environ, {"GROUPGUARD_CONFIG": str(config)}))

from auction_reminder_fixtures import (
    GROUP_ID,
    NOW,
    WorkerReceipt,
    auction_at,
    captured_bot,
    financial_rows,
    isolated_db,
    persist_auction,
    reminder_rows,
)

from app.config import get_config
from app.db import dispose_db, init_db, session_scope
from app.models import Member
from app.services import scheduler


class Suite(StrEnum):
    ALL = "--all"
    REGRESSION = "--regression-only"
    CLAIMS = "--claims-only"
    EXTENSIONS = "--extensions-only"
    DELIVERY = "--delivery-only"
    PROOF = "--prove-regressions"
    WORKER = "--restart-worker"
    HELP = "--help"


async def regression_windows() -> None:
    failures: list[tuple[int, int]] = []
    for seconds, expected in ((179, 0), (119, 1), (59, 1)):
        async with isolated_db(), captured_bot() as (bot, transport):
            await persist_auction(auction_at(NOW + timedelta(seconds=seconds)))
            await scheduler._send_auction_reminders(bot, NOW)
            actual = len(transport.sends)
            print(f"{'PASS' if actual == expected else 'FAIL'} remaining={seconds}s sends={actual} expected={expected}")
            if actual != expected:
                failures.append((seconds, actual))
    assert not failures, f"Final-minute reminder windows missing: {failures}"


async def boundaries_when_deadline_is_nearby() -> None:
    for seconds, expected in (
        (181, None), (180, None), (179, None), (121, None), (120, 2), (119, 2),
        (61, 2), (60, 3), (59, 3), (1, 3), (0, None), (-1, None),
    ):
        async with isolated_db(), captured_bot() as (bot, transport):
            auction = auction_at(NOW + timedelta(seconds=seconds))
            await persist_auction(auction)
            before = await financial_rows()

            await scheduler._send_auction_reminders(bot, NOW)

            rows = await reminder_rows()
            if expected is None:
                assert rows == [] and transport.methods == [], (seconds, rows, transport.methods)
            else:
                assert len(rows) == 1 and len(transport.methods) == 1, seconds
                row = rows[0]
                assert (row.auction_id, row.ends_at, row.window, row.status, row.message_id) == (
                    auction.id, auction.ends_at, expected, "sent", 9001,
                ), seconds
                assert row.claimed_at == row.sent_at == NOW
                assert isinstance(transport.methods[0], SendMessage)
                assert transport.pins == []
                keyboard = transport.sends[0].reply_markup
                assert isinstance(keyboard, InlineKeyboardMarkup)
                assert {button.callback_data for line in keyboard.inline_keyboard for button in line} == {
                    f"bid:{auction.id}", f"aucquery:{auction.id}",
                }
                assert f"第{('一', '二')[expected - 2]}次" in transport.sends[0].text
            assert await financial_rows() == before
            print(f"PASS boundary remaining={seconds}s window={expected} sends={len(transport.sends)}")


async def skipped_when_closed_or_deadline_missing() -> None:
    for status, ends_at in (("finished", NOW + timedelta(seconds=59)), ("cancelled", NOW + timedelta(seconds=59)), ("open", None)):
        async with isolated_db(), captured_bot() as (bot, transport):
            auction = auction_at(ends_at)
            auction.status = status
            await persist_auction(auction)

            await scheduler._send_auction_reminders(bot, NOW)

            assert transport.methods == [] and await reminder_rows() == []
    print("PASS closed and undated auctions never claim or dispatch")


async def repeated_ticks_when_deadline_unchanged() -> None:
    async with isolated_db(), captured_bot() as (bot, transport):
        auction = auction_at(NOW + timedelta(seconds=180))
        await persist_auction(auction)
        before = await financial_rows()

        for elapsed in (0, 1, 60, 61, 120, 121, 179, 180, 181):
            await scheduler._send_auction_reminders(bot, NOW + timedelta(seconds=elapsed))

        rows = await reminder_rows()
        assert [(row.window, row.status, row.message_id) for row in rows] == [
            (2, "sent", 9001), (3, "sent", 9002),
        ], f"windows={[(row.window, row.message_id) for row in rows]} sends={len(transport.sends)} pins={len(transport.pins)}"
        assert [row.ends_at for row in rows] == [auction.ends_at] * 2
        assert len(transport.sends) == len(transport.delivered) == 2
        assert transport.pins == []
        assert await financial_rows() == before
        print("PASS repeated ticks: windows=[2,3] claims=2 sends=2 pins=0; no ledger writes")


async def message_contract_when_units_or_bidder_differ() -> None:
    for auction_type, unit in (("points", "积分"), ("currency", "CNY")):
        async with isolated_db(), captured_bot() as (bot, transport):
            auction = auction_at(NOW + timedelta(seconds=59))
            auction.auction_type = auction_type
            auction.current_price = 145
            auction.step = 7
            auction.extend_minutes = 0
            auction.current_bidder = 819001
            await persist_auction(auction)
            async with session_scope() as session:
                member = await session.get(Member, 1)
                assert member is not None
                member.first_name = "Bidder <tag> & One"

            await scheduler._send_auction_reminders(bot, NOW)

            assert len(transport.sends) == len(await reminder_rows()) == 1
            send = transport.sends[0]
            assert send.chat_id == GROUP_ID
            for value in (escape(auction.item), escape(auction.description), auction.code,
                          "Bidder &lt;tag&gt; &amp; One", f"145 {unit}", f"7 {unit}",
                          "2026-11-09 09:03:02", "出价后 3 分钟"):
                assert value in send.text, (value, send.text)
            assert isinstance(send.reply_markup, InlineKeyboardMarkup)
            assert {button.callback_data for line in send.reply_markup.inline_keyboard for button in line} == {
                "bid:1", "aucquery:1",
            }
    print("PASS points/CNY content, escaped bidder/item, Beijing cutoff, rule and live bid/query methods")


async def tick_when_only_reminder_is_due() -> None:
    async with isolated_db(), captured_bot() as (bot, transport):
        await persist_auction(auction_at(NOW + timedelta(seconds=119)))
        before = await financial_rows()

        with patch.object(scheduler, "utcnow", return_value=NOW):
            await scheduler.tick(bot)

        assert len(transport.sends) == len(await reminder_rows()) == 1
        assert transport.pins == []
        assert await financial_rows() == before
    print("PASS full scheduler tick remains wired to reminders without financial mutation")


async def restart_worker(now: datetime) -> None:
    config_path = Path(os.environ["GROUPGUARD_CONFIG"])
    assert config_path.parent.name.startswith("auction-reminders-")
    try:
        await init_db()
        async with captured_bot() as (bot, transport):
            await scheduler._send_auction_reminders(bot, now)
            print(WorkerReceipt(
                sends=len(transport.sends), pins=len(transport.pins), claims=len(await reminder_rows())
            ).model_dump_json())
    finally:
        await dispose_db()


async def main() -> None:
    try:
        suite = Suite(sys.argv[1] if len(sys.argv) > 1 else Suite.ALL)
    except ValueError:
        raise SystemExit("Unknown suite; use --help") from None
    match suite:
        case Suite.REGRESSION:
            await regression_windows()
        case Suite.CLAIMS:
            from auction_reminder_claim_tests import run_claim_tests
            await run_claim_tests()
        case Suite.EXTENSIONS:
            from auction_reminder_extension_tests import run_extension_tests
            await run_extension_tests()
        case Suite.DELIVERY:
            from auction_reminder_delivery_tests import run_delivery_tests
            await run_delivery_tests()
        case Suite.PROOF:
            from auction_reminder_negative_controls import prove_regressions
            await prove_regressions()
        case Suite.WORKER:
            await restart_worker(datetime.fromisoformat(sys.argv[2]))
        case Suite.HELP:
            print("Offline auction reminder smoke: --all (default), --regression-only, --claims-only, --extensions-only, --delivery-only, --prove-regressions")
        case Suite.ALL:
            await regression_windows()
            await boundaries_when_deadline_is_nearby()
            await skipped_when_closed_or_deadline_missing()
            await repeated_ticks_when_deadline_unchanged()
            await message_contract_when_units_or_bidder_differ()
            await tick_when_only_reminder_is_due()
            from auction_reminder_claim_tests import run_claim_tests
            from auction_reminder_extension_tests import run_extension_tests
            from auction_reminder_delivery_tests import run_delivery_tests
            await run_claim_tests()
            await run_extension_tests()
            await run_delivery_tests()
            print("PASS all auction reminder regressions")
        case unexpected:
            assert_never(unexpected)


if __name__ == "__main__":
    RUNTIME.callback(get_config.cache_clear)
    with RUNTIME:
        asyncio.run(main())
