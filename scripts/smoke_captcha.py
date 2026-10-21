"""入群验证码刷新与三次失败移出回归测试。

使用临时配置与临时 SQLite 数据库，不修改真实 config.json / data/。
运行：

    .venv/bin/python scripts/smoke_captcha.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
TMP = Path(tempfile.mkdtemp(prefix="groupguard-captcha-"))
DB = TMP / "captcha.db"
TMP_CONFIG = TMP / "config.json"

config_path = ROOT / "config.json"
if not config_path.exists():
    print("未找到 config.json，请先复制 config.example.json 并填写。")
    sys.exit(2)

cfg = json.loads(config_path.read_text(encoding="utf-8"))
cfg["database"]["url"] = f"sqlite+aiosqlite:///{DB}"
cfg.setdefault("ai", {})["learning_dir"] = str(TMP / "learning")
TMP_CONFIG.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
os.environ["GROUPGUARD_CONFIG"] = str(TMP_CONFIG)
sys.path.insert(0, str(ROOT))

from sqlalchemy import select  # noqa: E402

from app.bot import captcha  # noqa: E402
from app.bot.handlers import join  # noqa: E402
from app.db import init_db, session_scope  # noqa: E402
from app.models import Group, Verification, utcnow  # noqa: E402
from app.services import scheduler  # noqa: E402

CHAT_ID = -1001234567890
USER_ID = 456
failures: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    if cond:
        print(f"PASS {name}")
    else:
        failures.append(name)
        print(f"FAIL {name} {extra}")


class FakeUser:
    id = USER_ID
    first_name = "Tester"
    username = "tester"
    is_bot = False

    def mention_html(self) -> str:
        return f'<a href="tg://user?id={self.id}">Tester</a>'


class FakeMessage:
    def __init__(self) -> None:
        self.deleted = False

    async def delete(self) -> None:
        self.deleted = True


class FakeCallback:
    def __init__(self, data: str) -> None:
        self.data = data
        self.from_user = FakeUser()
        self.message = FakeMessage()
        self.answers: list[tuple[str, bool]] = []

    async def answer(self, text: str, show_alert: bool = False) -> None:
        self.answers.append((text, show_alert))


class FakeBot:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.captions: list[str] = []
        self.messages: list[str] = []
        self.deleted: list[int] = []
        self.next_id = 100

    async def restrict_chat_member(self, *args, **kwargs) -> None:
        self.calls.append("restrict")

    async def send_photo(self, chat_id, photo, caption=None, reply_markup=None):
        self.calls.append("send_photo")
        self.captions.append(caption or "")
        self.next_id += 1
        return SimpleNamespace(message_id=self.next_id)

    async def ban_chat_member(self, *args, **kwargs) -> None:
        self.calls.append("ban")

    async def get_chat_member(self, chat_id, user_id):
        return SimpleNamespace(status="member", is_member=True)

    async def unban_chat_member(self, *args, **kwargs) -> None:
        self.calls.append("unban")

    async def send_message(self, chat_id, text, **kwargs):
        self.calls.append("send_message")
        self.messages.append(text)
        return SimpleNamespace(message_id=999)

    async def delete_message(self, chat_id, message_id):
        self.calls.append("delete_message")
        self.deleted.append(message_id)


async def pending_row() -> Verification | None:
    async with session_scope() as session:
        return await session.scalar(
            select(Verification)
            .where(
                Verification.group_id == CHAT_ID,
                Verification.user_id == USER_ID,
                Verification.status == "pending",
            )
            .order_by(Verification.id.desc())
        )


def wrong_option(token: str) -> int:
    expected = captcha.captcha_code(token)
    options = captcha.captcha_options(token)
    return next(i for i, option in enumerate(options) if option != expected)


async def main() -> None:
    await init_db()
    async with session_scope() as session:
        session.add(
            Group(
                id=CHAT_ID,
                title="Captcha Test",
                username="captcha_test",
                chat_type="supergroup",
                verify_enabled=True,
                verify_timeout=300,
                welcome_enabled=False,
            )
        )
        session.add(
            Verification(
                group_id=CHAT_ID,
                user_id=USER_ID,
                token="initial-token",
                status="pending",
                created_at=utcnow(),
                failed_attempts=0,
                message_id=50,
            )
        )

    bot = FakeBot()
    first = await pending_row()
    assert first is not None
    original_created_at = first.created_at

    # First wrong answer: rotate immediately, keep the count and deadline.
    cb1 = FakeCallback(f"verify:{first.token}:{wrong_option(first.token)}")
    await join.on_verify(cb1, bot)
    second = await pending_row()
    check("wrong answer rotates challenge", second is not None and second.token != first.token)
    check("first wrong answer count is 1", bool(second and second.failed_attempts == 1))
    check(
        "rotation keeps original deadline",
        bool(second and second.created_at == original_created_at),
        f"{second.created_at if second else None} != {original_created_at}",
    )
    check("first wrong answer tells remaining attempts", "还可尝试 2 次" in cb1.answers[-1][0])
    check("first wrong answer deletes old prompt", 50 in bot.deleted)
    check("first wrong answer sends new image", bot.calls.count("send_photo") == 1)
    check(
        "captcha text says one attempt",
        "每个验证码仅可尝试一次" in bot.captions[-1],
    )
    check(
        "captcha text says three failures remove user",
        "累计选错 3 次将被移出群组" in bot.captions[-1],
    )

    # Second wrong answer.
    assert second is not None
    cb2 = FakeCallback(f"verify:{second.token}:{wrong_option(second.token)}")
    await join.on_verify(cb2, bot)
    third = await pending_row()
    check("second wrong answer count is 2", bool(third and third.failed_attempts == 2))
    check("second wrong answer rotates again", third is not None and third.token != second.token)
    check(
        "second rotation keeps original deadline",
        bool(third and third.created_at == original_created_at),
    )
    check("second wrong answer tells one remaining attempt", "还可尝试 1 次" in cb2.answers[-1][0])
    assert second is not None
    check("second wrong answer deletes old prompt", second.message_id in bot.deleted)

    # Manual refresh must not reset the failure counter.
    assert third is not None
    cb_refresh = FakeCallback(f"verifynew:{third.token}")
    await join.on_verify_new(cb_refresh, bot)
    refreshed = await pending_row()
    check("manual refresh keeps count", bool(refreshed and refreshed.failed_attempts == 2))
    check("manual refresh keeps deadline", bool(refreshed and refreshed.created_at == original_created_at))
    check("manual refresh answers success", "已更换验证码" in cb_refresh.answers[-1][0])
    assert third is not None
    check("manual refresh deletes old prompt", third.message_id in bot.deleted)

    # Third wrong answer: kick and do not issue another challenge.
    assert refreshed is not None
    before_third = bot.calls.count("send_photo")
    cb3 = FakeCallback(f"verify:{refreshed.token}:{wrong_option(refreshed.token)}")
    await join.on_verify(cb3, bot)
    check("third wrong answer leaves no pending challenge", await pending_row() is None)
    check("third wrong answer does not rotate again", bot.calls.count("send_photo") == before_third)
    check("third wrong answer kicks", "ban" in bot.calls and "unban" in bot.calls)
    check("third wrong answer tells user about removal", "正在将你移出" in cb3.answers[-1][0])
    check(
        "third wrong answer posts removal notice",
        any("连续答错验证码 3 次，已被移出群组" in text for text in bot.messages),
    )
    check("third wrong answer deletes prompt", refreshed.message_id in bot.deleted)
    check("inflight guard is released", not join._verification_inflight)

    # Concurrent clicks on the same challenge must not allow multiple guesses.
    async with session_scope() as session:
        session.add(
            Verification(
                group_id=CHAT_ID,
                user_id=USER_ID,
                token="concurrent-token",
                status="pending",
                created_at=utcnow(),
                failed_attempts=0,
                message_id=60,
            )
        )
    cb_a = FakeCallback(f"verify:concurrent-token:{wrong_option('concurrent-token')}")
    cb_b = FakeCallback(f"verify:concurrent-token:{wrong_option('concurrent-token')}")
    await asyncio.gather(join.on_verify(cb_a, bot), join.on_verify(cb_b, bot))
    all_answers = cb_a.answers + cb_b.answers
    check(
        "concurrent duplicate click is rejected",
        sum("正在处理" in answer[0] for answer in all_answers) == 1,
        str(all_answers),
    )
    check(
        "only one concurrent click consumes the challenge",
        sum("还可尝试 2 次" in answer[0] for answer in all_answers) == 1,
        str(all_answers),
    )
    concurrent_row = await pending_row()
    check(
        "concurrent click creates exactly one new challenge",
        bool(concurrent_row and concurrent_row.failed_attempts == 1),
    )
    check("concurrent click deletes old prompt", 60 in bot.deleted)

    old_cb = FakeCallback(f"verify:concurrent-token:{wrong_option('concurrent-token')}")
    await join.on_verify(old_cb, bot)
    check(
        "old image cannot be answered again",
        any("该验证已处理" in answer[0] for answer in old_cb.answers),
        str(old_cb.answers),
    )

    # Correct answer must pass and delete the active prompt.
    assert concurrent_row is not None
    correct = captcha.captcha_options(concurrent_row.token).index(
        captcha.captcha_code(concurrent_row.token)
    )
    pass_cb = FakeCallback(f"verify:{concurrent_row.token}:{correct}")
    await join.on_verify(pass_cb, bot)
    check(
        "correct answer passes verification",
        await pending_row() is None,
    )
    check(
        "correct answer deletes prompt",
        concurrent_row.message_id in bot.deleted,
        str(bot.deleted),
    )

    # Timeline expiry must remove the prompt as well as kick the user.
    async with session_scope() as session:
        session.add(
            Verification(
                group_id=CHAT_ID,
                user_id=USER_ID,
                token="timeout-token",
                status="pending",
                created_at=utcnow() - timedelta(seconds=600),
                failed_attempts=0,
                message_id=70,
            )
        )
    await scheduler.expire_verifications(bot)
    check("timeout deletes prompt", 70 in bot.deleted, str(bot.deleted))

    if failures:
        print("FAILURES:", ", ".join(failures))
        raise SystemExit(1)
    print("FAILURES: none")


if __name__ == "__main__":
    asyncio.run(main())
