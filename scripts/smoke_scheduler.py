"""验证超时移出的回归测试（不需要真实群组或 Bot Token）。

运行：``make smoke-scheduler``
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aiogram.exceptions import TelegramAPIError  # noqa: E402

from app.services import scheduler  # noqa: E402

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


async def main() -> None:
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

    # 1. 正常移出
    reset()
    bot = FakeBot()
    assert await scheduler._kick_timed_out(bot, CHAT_ID, USER_ID, "测试群")
    assert bot.calls == ["ban", "unban"], bot.calls
    assert "已被移出" in notices[-1]
    assert logs[-1][0] == "verify_timeout_kick"
    assert marks == [(CHAT_ID, USER_ID)]
    print("PASS 正常移出并提示")

    # 2. 封禁失败：只提示失败，不再禁言或进入重试
    reset()
    bot = FakeBot(ban_error=True)
    assert not await scheduler._kick_timed_out(bot, CHAT_ID, USER_ID, "测试群")
    assert "自动移出失败" in notices[-1]
    assert "restrict" not in bot.calls
    assert not marks
    assert logs[-1][0] == "verify_timeout_kick_failed"
    print("PASS 封禁失败只提示，不再禁言或重试")

    # 3. 封禁请求报错但用户实际已被移出
    reset()
    bot = FakeBot(ban_error=True, ban_applied_on_error=True)
    assert await scheduler._kick_timed_out(bot, CHAT_ID, USER_ID, "测试群")
    assert "已被移出" in notices[-1] and bot.status == "left"
    assert marks == [(CHAT_ID, USER_ID)]
    print("PASS 请求超时但实际已移出时按成功处理")

    # 4. 已移出但解除封禁失败
    reset()
    bot = FakeBot(unban_errors=2)
    assert await scheduler._kick_timed_out(bot, CHAT_ID, USER_ID, "测试群")
    assert "解除封禁失败" in notices[-1]
    assert logs[-1][0] == "verify_timeout_kick_unban_failed"
    print("PASS 解除封禁失败会提醒管理员")

    # 5. 用户已自行离开：不发送消息，但顺带把面板状态纠正为已离开
    reset()
    bot = FakeBot(status="left")
    assert await scheduler._kick_timed_out(bot, CHAT_ID, USER_ID, "测试群")
    assert not bot.calls and not notices
    assert marks == [(CHAT_ID, USER_ID)]
    print("PASS 用户已离开时不发送消息并纠正成员状态")

    # 6. 管理员无法移出
    reset()
    bot = FakeBot(status="administrator")
    assert not await scheduler._kick_timed_out(bot, CHAT_ID, USER_ID, "测试群")
    assert "自动移出失败" in notices[-1]
    print("PASS 管理员无法移出时提示")

    # 7. 权限不足时提示检查权限
    reset()
    bot = FakeBot()

    async def no_rights(chat_id, user_id, revoke_messages=False):
        bot.calls.append("ban")
        raise TelegramAPIError("banChatMember", "Bad Request: not enough rights")

    bot.ban_chat_member = no_rights  # type: ignore[assignment]
    assert not await scheduler._kick_timed_out(bot, CHAT_ID, USER_ID, "测试群")
    assert "封禁用户" in notices[-1]
    print("PASS 权限不足时提示检查权限")


if __name__ == "__main__":
    asyncio.run(main())
    print("FAILURES: none")
