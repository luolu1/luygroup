"""验证超时移出的回归测试（不需要真实群组或 Bot Token）。

运行：``make smoke-scheduler``
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ROOT = Path(__file__).resolve().parents[1]
TMP = Path(tempfile.mkdtemp(prefix="groupguard-scheduler-"))
config_path = ROOT / "config.json"
cfg = json.loads(config_path.read_text(encoding="utf-8"))
cfg["database"]["url"] = f"sqlite+aiosqlite:///{TMP / 'scheduler.db'}"
cfg.setdefault("ai", {})["learning_dir"] = str(TMP / "learning")
config_file = TMP / "config.json"
config_file.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
os.environ["GROUPGUARD_CONFIG"] = str(config_file)

from aiogram.exceptions import TelegramAPIError  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.services import scheduler  # noqa: E402
from app.db import init_db, session_scope  # noqa: E402
from app.models import Group, Member  # noqa: E402

CHAT_ID = -100123
USER_ID = 456


class FakeBot:
    def __init__(
        self,
        status: str = "member",
        *,
        ban_error: bool = False,
        ban_applied_on_error: bool = False,
        unban_errors: int = 0,
    ) -> None:
        self.status = status
        self.ban_error = ban_error
        self.ban_applied_on_error = ban_applied_on_error
        self.unban_errors = unban_errors
        self.calls: list[str] = []

    async def get_chat_member(self, chat_id: int, user_id: int):
        return type("Member", (), {"status": self.status})()

    async def ban_chat_member(self, chat_id: int, user_id: int, revoke_messages=False):
        self.calls.append("ban")
        if self.ban_error:
            if self.ban_applied_on_error:
                self.status = "kicked"
            raise TelegramAPIError("test", "ban failed")
        self.status = "kicked"

    async def unban_chat_member(self, chat_id: int, user_id: int, only_if_banned=True):
        self.calls.append("unban")
        if self.unban_errors:
            self.unban_errors -= 1
            raise TelegramAPIError("test", "unban failed")
        self.status = "left"

    async def restrict_chat_member(self, chat_id: int, user_id: int, permissions=None, until_date=None):
        self.calls.append("restrict")
        if self.ban_error:
            raise TelegramAPIError("restrictChatMember", "Bad Request: not enough rights")

    async def send_message(self, chat_id, text, **kwargs):
        self.calls.append("send_message")
        return type("Message", (), {"message_id": 9001})()

    async def delete_message(self, chat_id, message_id):
        self.calls.append(f"delete:{message_id}")


async def main() -> None:
    await init_db()
    async with session_scope() as session:
        session.add(Group(id=CHAT_ID, title="测试群", chat_type="supergroup"))
        session.add(Member(group_id=CHAT_ID, user_id=USER_ID, is_member=True))
    notices: list[str] = []
    logs: list[tuple[str, str]] = []
    marks: list[tuple[int, int]] = []

    async def fake_record(chat_id: int, user_id: int, action: str, detail: str) -> None:
        logs.append((action, detail))

    async def fake_notice(bot, chat_id: int, text: str) -> None:
        notices.append(text)

    async def fake_mark(chat_id: int, user_id: int) -> None:
        marks.append((chat_id, user_id))

    scheduler._record_kick = fake_record  # type: ignore[assignment]
    scheduler._notice = fake_notice  # type: ignore[assignment]
    scheduler._mark_left = fake_mark  # type: ignore[assignment]

    def reset() -> None:
        notices.clear()
        logs.clear()
        marks.clear()

    reset()
    bot = FakeBot()
    assert await scheduler._kick_timed_out(bot, CHAT_ID, USER_ID, "测试群")
    assert bot.calls == ["restrict"], bot.calls
    assert "永久禁言" in notices[-1]
    assert logs[-1][0] == "verify_timeout_restrict"
    assert not marks
    async with session_scope() as session:
        member = await session.scalar(
            select(Member).where(Member.group_id == CHAT_ID, Member.user_id == USER_ID)
        )
        assert member is not None and member.verification_restricted and member.muted_until is None
    print("PASS 验证超时永久禁言")

    reset()
    bot = FakeBot(ban_error=True)
    assert not await scheduler._kick_timed_out(bot, CHAT_ID, USER_ID, "测试群")
    assert "永久禁言失败" in notices[-1]
    assert "restrict" in bot.calls
    assert not marks
    assert logs[-1][0] == "verify_timeout_restrict_failed"
    print("PASS 永久禁言失败只提示")

    reset()
    bot = FakeBot(ban_error=False, ban_applied_on_error=True)
    assert await scheduler._kick_timed_out(bot, CHAT_ID, USER_ID, "测试群")
    assert "永久禁言" in notices[-1] and "ban" not in bot.calls
    assert not marks
    print("PASS 验证超时不再执行封禁踢出")

    reset()
    bot = FakeBot(unban_errors=2)
    assert await scheduler._kick_timed_out(bot, CHAT_ID, USER_ID, "测试群")
    assert "永久禁言" in notices[-1]
    assert logs[-1][0] == "verify_timeout_restrict"
    print("PASS 超时限制保持永久")

    reset()
    bot = FakeBot(status="left")
    assert await scheduler._kick_timed_out(bot, CHAT_ID, USER_ID, "测试群")
    assert "restrict" in bot.calls
    assert "永久禁言" in notices[-1]
    print("PASS 超时处理不依赖先踢出")

    reset()
    bot = FakeBot(status="administrator")
    assert await scheduler._kick_timed_out(bot, CHAT_ID, USER_ID, "测试群")
    assert "永久禁言" in notices[-1]
    print("PASS 超时不执行管理员移出")

    # 7. 权限不足时提示检查权限
    reset()
    bot = FakeBot()

    async def no_rights(chat_id, user_id, **kwargs):
        bot.calls.append("restrict")
        raise TelegramAPIError("banChatMember", "Bad Request: not enough rights")

    bot.restrict_chat_member = no_rights  # type: ignore[assignment]
    assert not await scheduler._kick_timed_out(bot, CHAT_ID, USER_ID, "测试群")
    assert "永久禁言失败" in notices[-1]
    print("PASS 永久禁言权限不足时提示")


if __name__ == "__main__":
    asyncio.run(main())
    print("FAILURES: none")
