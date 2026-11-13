"""Offline regression: .venv/bin/python scripts/smoke_redpacket_publication.py.

Real dispatcher updates exercise publication, rejection, claims and future expiry.
Only synthetic configuration and a unique temporary SQLite database are used.
"""

from __future__ import annotations

import atexit
import json
import os
import re
import sys
from collections.abc import Awaitable, Callable
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import partial
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Final
from unittest.mock import patch

ROOT: Final = Path(__file__).resolve().parents[1]
RUNTIME: Final = ExitStack()
_ = atexit.register(RUNTIME.close)
TEMP: Final = Path(
    RUNTIME.enter_context(
        TemporaryDirectory(
            prefix="redpacket-publication-",
            dir="/tmp/opencode",
        )
    )
)
CONFIG: Final = TEMP / "synthetic-config.json"
_ = CONFIG.write_text(
    json.dumps(
        {
            "bot": {"token": "123456789:REDPACKET_SYNTHETIC_TOKEN", "admin_ids": [], "proxy": ""},
            "database": {"url": f"sqlite+aiosqlite:///{TEMP / 'redpacket.db'}"},
            "web": {"secret_key": "redpacket-synthetic-secret", "base_url": "https://example.test"},
            "ai": {"learning_dir": str(TEMP / "learning"), "providers": []},
            "payments": {"enabled": False},
        }
    ),
    encoding="utf-8",
)
_ = RUNTIME.enter_context(patch.dict(os.environ, {"GROUPGUARD_CONFIG": str(CONFIG)}))
sys.path.insert(0, str(ROOT))
sys.dont_write_bytecode = True

from scripts.offline_support import install_guards

GUARD: Final = install_guards(ROOT, TEMP)
_ = atexit.register(GUARD.finish)

import anyio
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import SendMessage
from aiogram.types import InlineKeyboardMarkup
from sqlalchemy import event, select
from sqlalchemy.engine import Connection

from app.bot.handlers import games_redpacket, games_shared
from app.bot.main import build_dispatcher
from app.config import get_config
from app.db import dispose_db, get_engine, init_db, session_scope
from app.models import Member, PointsTransaction, RedPacket, RedPacketClaim
from app.services import scheduler
from scripts.auction_dispatcher_fixture import Harness, Scene
from scripts.auction_dispatcher_transport import AuctionRequest, callbacks

NOW: Final = datetime(2030, 1, 2, 12)
RUNTIME.callback(get_config.cache_clear)


@dataclass(frozen=True, slots=True)
class PacketState:
    balances: tuple[tuple[int, int], ...]
    packets: tuple[tuple[int, str, int, int, int | None], ...]
    transactions: tuple[tuple[int, int], ...]
    claims: tuple[tuple[int, int, int], ...]


async def state(scene: Scene) -> PacketState:
    async with session_scope() as session:
        balances = await session.execute(
            select(Member.user_id, Member.points)
            .where(Member.group_id == scene.group_id)
            .order_by(Member.user_id)
        )
        packets = await session.execute(
            select(
                RedPacket.id,
                RedPacket.status,
                RedPacket.remaining_amount,
                RedPacket.remaining_count,
                RedPacket.message_id,
            )
            .where(RedPacket.group_id == scene.group_id)
            .order_by(RedPacket.id)
        )
        transactions = await session.execute(
            select(PointsTransaction.user_id, PointsTransaction.amount)
            .where(PointsTransaction.group_id == scene.group_id)
            .order_by(PointsTransaction.id)
        )
        claims = await session.execute(
            select(RedPacketClaim.red_packet_id, RedPacketClaim.user_id, RedPacketClaim.amount)
            .where(RedPacketClaim.user_id.in_((scene.first.id, scene.second.id)))
            .order_by(RedPacketClaim.id)
        )
        return PacketState(
            tuple((user_id, points) for user_id, points in balances),
            tuple(
                (packet_id, status, amount, count, message_id)
                for packet_id, status, amount, count, message_id in packets
            ),
            tuple((user_id, amount) for user_id, amount in transactions),
            tuple((packet_id, user_id, amount) for packet_id, user_id, amount in claims),
        )


async def test_rollback_when_publication_rejected(harness: Harness, reply_succeeds: bool) -> None:
    scene = await harness.seed(f"RPFAIL{int(reply_succeeds)}")
    before = await state(scene)
    assert before == PacketState(((scene.first.id, 500), (scene.second.id, 500)), (), (), ())
    events: list[str] = []

    def on_rollback(_connection: Connection) -> None:
        events.append("rollback")

    async def reject_card(request: AuctionRequest) -> None:
        assert isinstance(request, SendMessage)
        markup = request.reply_markup
        assert markup is None or isinstance(markup, InlineKeyboardMarkup)
        if any(re.fullmatch(r"rp:\d+", data) for data in callbacks(markup)):
            events.append("card")
            raise TelegramBadRequest(method=request, message="synthetic publication rejection")
        assert request.reply_parameters is not None
        events.append("reply")
        if not reply_succeeds:
            raise TelegramBadRequest(method=request, message="synthetic error-reply rejection")

    engine = get_engine().sync_engine
    event.listen(engine, "rollback", on_rollback)
    harness.session.on_request = reject_card
    try:
        try:
            await harness.say(scene.group_id, scene.first, "/redpacket 100 1")
        except TelegramBadRequest as error:
            assert not reply_succeeds
            assert (
                isinstance(error.method, SendMessage) and error.method.reply_parameters is not None
            )
        else:
            assert reply_succeeds
    finally:
        event.remove(engine, "rollback", on_rollback)
        harness.session.on_request = None
    after_rejection = await state(scene)
    with patch.object(scheduler, "utcnow", return_value=NOW + timedelta(days=2)):
        await scheduler.refund_red_packets(harness.bot)
        await scheduler.refund_red_packets(harness.bot)
    after_expiry = await state(scene)

    assert after_rejection == before, (after_rejection, after_expiry)
    assert after_expiry == before, after_expiry
    assert events == ["card", "rollback", "reply"], events
    delivered = [
        request
        for request, _message in harness.session.sent_messages
        if request.chat_id == scene.group_id
    ]
    assert len(delivered) == int(reply_succeeds), delivered


async def test_claimable_when_publication_succeeds(harness: Harness) -> None:
    scene = await harness.seed("RPOK")

    await harness.say(scene.group_id, scene.first, "/redpacket 100 1")
    published = await state(scene)

    assert len(published.packets) == 1, published
    packet_id, status, amount, count, message_id = published.packets[0]
    assert (status, amount, count) == ("open", 100, 1)
    assert dict(published.balances) == {scene.first.id: 400, scene.second.id: 500}
    assert published.transactions == ((scene.first.id, -100),)
    cards = [
        message
        for request, message in harness.session.sent_messages
        if request.chat_id == scene.group_id
        and callbacks(message.reply_markup) == {f"rp:{packet_id}"}
    ]
    assert len(cards) == 1 and cards[0].message_id == message_id

    await harness.click(scene.second, cards[0], f"rp:{packet_id}")
    claimed = await state(scene)

    assert dict(claimed.balances) == {scene.first.id: 400, scene.second.id: 600}
    assert claimed.transactions == ((scene.first.id, -100), (scene.second.id, 100))
    assert claimed.claims == ((packet_id, scene.second.id, 100),)
    assert claimed.packets == ((packet_id, "finished", 0, 0, message_id),)
    with patch.object(scheduler, "utcnow", return_value=NOW + timedelta(days=2)):
        await scheduler.refund_red_packets(harness.bot)
    assert await state(scene) == claimed


async def main() -> None:
    harness = Harness(build_dispatcher())
    cases: tuple[tuple[str, Callable[[], Awaitable[None]]], ...] = (
        ("publication_rejected", partial(test_rollback_when_publication_rejected, harness, True)),
        (
            "error_reply_also_rejected",
            partial(test_rollback_when_publication_rejected, harness, False),
        ),
        ("published_packet_claimable", partial(test_claimable_when_publication_succeeds, harness)),
    )
    failures: list[str] = []
    try:
        await init_db()
        with patch.object(games_redpacket, "utcnow", return_value=NOW), patch.object(games_shared, "utcnow", return_value=NOW):
            for name, case in cases:
                try:
                    await case()
                except AssertionError as error:
                    failures.append(name)
                    print(f"FAIL {name}: {error}")
                else:
                    print(f"PASS {name}")
                finally:
                    await harness.settle()
    finally:
        await harness.close()
        await dispose_db()
    assert not GUARD.violations, GUARD.violations
    print(f"RESULT {len(cases) - len(failures)} passed; {len(failures)} failed")
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    try:
        anyio.run(main)
    finally:
        RUNTIME.close()
