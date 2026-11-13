"""Offline verification-timeout removal regression: ``make smoke-scheduler``."""
from __future__ import annotations

import asyncio
import atexit
import json
import os
import sys
import tempfile
from contextlib import ExitStack
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Final
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
RUNTIME: Final = ExitStack()
atexit.register(RUNTIME.close)
TMP = Path(RUNTIME.enter_context(tempfile.TemporaryDirectory(
    prefix="groupguard-scheduler-", dir="/tmp/opencode",
)))
cfg = {
    "bot": {"token": "123456:offline-scheduler", "admin_ids": [], "proxy": ""},
    "web": {"secret_key": "scheduler-smoke-secret", "base_url": "https://example.test"},
    "database": {"url": f"sqlite+aiosqlite:///{TMP / 'scheduler.db'}"},
    "ai": {"learning_dir": str(TMP / "learning"), "providers": []},
    "payments": {"enabled": False},
}
config_file = TMP / "config.json"
config_file.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
RUNTIME.enter_context(patch.dict(os.environ, {"GROUPGUARD_CONFIG": str(config_file)}))

from aiogram.exceptions import TelegramAPIError  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.config import get_config  # noqa: E402
from app.db import dispose_db, init_db, session_scope  # noqa: E402
from app.models import Group, Member, Verification, utcnow  # noqa: E402
from app.services import scheduler  # noqa: E402

RUNTIME.callback(get_config.cache_clear)

CHAT_ID = -100123
USER_ID = 456


class FakeBot:
    def __init__(self, status: str = "member", *, ban_error: bool = False,
                 unban_error: bool = False, lookup_error: bool = False) -> None:
        self.status = status
        self.ban_error = ban_error
        self.unban_error = unban_error
        self.lookup_error = lookup_error
        self.calls: list[str] = []
        self.present = True
        self.deleted: list[tuple[int, int]] = []

    async def get_chat_member(self, chat_id: int, user_id: int):
        self.calls.append("lookup")
        if self.lookup_error:
            raise TelegramAPIError("getChatMember", "lookup failed")
        return SimpleNamespace(status=self.status, is_member=self.present)

    async def ban_chat_member(self, chat_id: int, user_id: int, revoke_messages=False):
        self.calls.append("ban")
        if self.ban_error:
            raise TelegramAPIError("banChatMember", "not enough rights")
        self.status = "kicked"

    async def unban_chat_member(self, chat_id: int, user_id: int, only_if_banned=False):
        assert only_if_banned is True
        self.calls.append("unban")
        if self.unban_error:
            raise TelegramAPIError("unbanChatMember", "unban failed")
        self.status = "left"

    async def restrict_chat_member(self, chat_id: int, user_id: int, **kwargs):
        self.calls.append("restrict")

    async def send_message(self, chat_id: int, text: str, **kwargs):
        self.calls.append("notice")
        return SimpleNamespace(message_id=9001)

    async def delete_message(self, chat_id: int, message_id: int):
        self.calls.append(f"delete:{message_id}")
        self.deleted.append((chat_id, message_id))


async def member_state() -> tuple[bool, bool, bool, bool, bool]:
    async with session_scope() as session:
        member = await session.scalar(
            select(Member).where(Member.group_id == CHAT_ID, Member.user_id == USER_ID)
        )
        assert member is not None
        return (member.is_member, member.is_banned, member.is_muted,
                member.mute_on_rejoin, member.verification_restricted)


async def set_member(*, is_member: bool = True, is_banned: bool = False,
                     is_muted: bool = False, mute_on_rejoin: bool = False,
                     verification_restricted: bool = False) -> None:
    async with session_scope() as session:
        member = await session.scalar(
            select(Member).where(Member.group_id == CHAT_ID, Member.user_id == USER_ID)
        )
        assert member is not None
        member.is_member = is_member
        member.is_banned = is_banned
        member.is_muted = is_muted
        member.mute_on_rejoin = mute_on_rejoin
        member.verification_restricted = verification_restricted


async def main() -> None:
    await init_db()
    async with session_scope() as session:
        session.add(Group(id=CHAT_ID, title="测试群", chat_type="supergroup", verify_enabled=True))
        session.add(Member(group_id=CHAT_ID, user_id=USER_ID, is_member=True))

    notices: list[str] = []
    logs: list[str] = []

    async def fake_record(chat_id: int, user_id: int, action: str, detail: str) -> None:
        logs.append(action)

    async def fake_notice(bot: FakeBot, chat_id: int, text: str) -> None:
        notices.append(text)

    with patch.object(scheduler, "_record_kick", fake_record), patch.object(
        scheduler, "_notice", fake_notice
    ):
        bot = FakeBot()
        assert await scheduler._kick_timed_out(bot, CHAT_ID, USER_ID, "测试群")
        assert bot.calls == ["lookup", "ban", "unban"] and bot.status == "left"
        assert await member_state() == (False, False, False, False, False)
        assert logs[-1] == "verify_timeout_kick" and "移出" in notices[-1]
        print("PASS timeout removes member and permits rejoin")

        for status, state in (("left", (False, False, False, False, False)),
                              ("kicked", (False, True, False, True, False)),
                              ("administrator", (True, False, False, False, False)),
                              ("creator", (True, False, False, False, False))):
            await set_member(is_member=state[0], is_banned=state[1],
                             mute_on_rejoin=state[3])
            bot = FakeBot(status)
            assert not await scheduler._kick_timed_out(bot, CHAT_ID, USER_ID, "测试群")
            assert bot.calls == ["lookup"], (status, bot.calls)
            assert await member_state() == state, status
        print("PASS absent, banned, administrator and owner skipped")

        await set_member(is_member=True, is_banned=True, mute_on_rejoin=True)
        bot = FakeBot()
        assert not await scheduler._kick_timed_out(bot, CHAT_ID, USER_ID, "测试群")
        assert bot.calls == ["lookup"]
        assert await member_state() == (True, True, False, True, False)
        print("PASS local administrative ban prevents timeout unban")

        await set_member(is_member=True)
        bot = FakeBot("restricted")
        bot.present = False
        assert not await scheduler._kick_timed_out(bot, CHAT_ID, USER_ID, "测试群")
        assert bot.calls == ["lookup"]
        assert await member_state() == (False, False, False, False, False)
        print("PASS absent restricted member is not kicked")

        for bot in (FakeBot(ban_error=True), FakeBot(lookup_error=True)):
            await set_member(is_member=True)
            assert not await scheduler._kick_timed_out(bot, CHAT_ID, USER_ID, "测试群")
            assert await member_state() == (True, False, False, False, False)
            assert "失败" in notices[-1] and "移出" not in notices[-1]
            assert logs[-1] == "verify_timeout_kick_failed"
        print("PASS missing permissions and status lookup failure report failure")

        await set_member(is_member=True, is_muted=True, mute_on_rejoin=True,
                         verification_restricted=True)
        bot = FakeBot(unban_error=True)
        assert not await scheduler._kick_timed_out(bot, CHAT_ID, USER_ID, "测试群")
        assert bot.calls == ["lookup", "ban", "unban"] and bot.status == "kicked"
        assert await member_state() == (False, True, True, True, False)
        assert "解封失败" in notices[-1] and logs[-1] == "verify_timeout_unban_failed"
        print("PASS unban failure records banned state without success claim")

        await set_member(is_member=True)
        bot = FakeBot(unban_error=True)
        assert not await scheduler._kick_timed_out(bot, CHAT_ID, USER_ID, "测试群")
        assert await member_state() == (False, True, False, False, False)
        print("PASS unban failure does not introduce rejoin mute")

        await set_member(is_member=True, is_muted=True, mute_on_rejoin=True,
                         verification_restricted=True)
        bot = FakeBot()
        assert await scheduler._kick_timed_out(bot, CHAT_ID, USER_ID, "测试群")
        assert await member_state() == (False, False, True, True, False)
        print("PASS existing moderation mute survives timeout kick")

        await set_member(is_member=True)
        async with session_scope() as session:
            session.add(Verification(
                group_id=CHAT_ID, user_id=USER_ID, token="expired-test", status="pending",
                created_at=utcnow() - timedelta(minutes=10), message_id=70,
                message_chat_id=USER_ID,
            ))
        bot = FakeBot("restricted")
        await scheduler.expire_verifications(bot)
        assert bot.calls == ["lookup", "ban", "unban", "delete:70"]
        assert bot.deleted == [(USER_ID, 70)]
        assert await member_state() == (False, False, False, False, False)
        async with session_scope() as session:
            verification = await session.scalar(
                select(Verification).where(Verification.token == "expired-test")
            )
            assert verification is not None and verification.status == "timeout"
        print("PASS expired verification removes member and deletes private prompt")

        await scheduler.expire_verifications(bot)
        assert bot.calls == ["lookup", "ban", "unban", "delete:70"]
        print("PASS repeated expiry does not repeat removal or cleanup")

        for failure_bot in (FakeBot(ban_error=True), FakeBot(unban_error=True)):
            await set_member(is_member=True)
            async with session_scope() as session:
                verification = await session.scalar(
                    select(Verification).where(Verification.token == "expired-test")
                )
                assert verification is not None
                verification.status = "pending"
            await scheduler.expire_verifications(failure_bot)
            assert failure_bot.deleted == [(USER_ID, 70)]
            assert "失败" in notices[-1]
            assert logs[-1] != "verify_timeout_kick"
        print("PASS ban and unban failures still clean expired prompts")


if __name__ == "__main__":
    with RUNTIME, asyncio.Runner() as runner:
        try:
            runner.run(main())
        finally:
            runner.run(dispose_db())
    print("FAILURES: none")
