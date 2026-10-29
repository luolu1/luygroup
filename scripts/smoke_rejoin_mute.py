"""被封禁/移出的成员再次入群后保持禁言的回归测试。

使用临时配置与临时 SQLite 数据库，不修改真实 config.json / data/。
运行：

    .venv/bin/python scripts/smoke_rejoin_mute.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
TMP = Path(tempfile.mkdtemp(prefix="groupguard-rejoin-"))
DB = TMP / "rejoin.db"
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

from app.bot import captcha, helpers  # noqa: E402
from app.bot.handlers import join  # noqa: E402
from app.db import init_db, session_scope  # noqa: E402
from app.models import AdminLog, Group, InviteCode, Member, Verification, utcnow  # noqa: E402

CHAT_ID = -1009876543210
USER_ID = 7788
TOKEN = "rejoin-token"
failures: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    if cond:
        print(f"PASS {name}")
    else:
        failures.append(name)
        print(f"FAIL {name} {extra}")


class FakeUser:
    id = USER_ID
    first_name = "Returning"
    username = "returning"
    is_bot = False

    def mention_html(self) -> str:
        return f'<a href="tg://user?id={self.id}">Returning</a>'


class FakeChat:
    id = CHAT_ID
    title = "Rejoin Test"
    username = "rejoin_test"
    type = "supergroup"


class FakeMessage:
    def __init__(self, message_id: int = 1) -> None:
        self.message_id = message_id

    async def delete(self) -> None:
        pass


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
        self.restrictions: list[tuple[bool | None, int | None]] = []
        self.messages: list[str] = []
        self.deleted: list[int] = []

    async def get_chat_member_count(self, chat_id: int) -> int:
        return 10

    async def restrict_chat_member(
        self, chat_id, user_id, permissions=None, until_date=None, **kwargs
    ) -> None:
        self.calls.append("restrict")
        allowed = getattr(permissions, "can_send_messages", None)
        self.restrictions.append((allowed, until_date))

    async def ban_chat_member(self, chat_id, user_id, revoke_messages=False) -> None:
        self.calls.append("ban")

    async def unban_chat_member(self, chat_id, user_id, only_if_banned=True) -> None:
        self.calls.append("unban")

    async def send_message(self, chat_id, text, **kwargs):
        self.calls.append("send_message")
        self.messages.append(text)
        return SimpleNamespace(message_id=999)

    async def delete_message(self, chat_id, message_id) -> None:
        self.calls.append("delete_message")
        self.deleted.append(message_id)

    async def send_photo(self, chat_id, photo, caption=None, reply_markup=None):
        self.calls.append("send_photo")
        return SimpleNamespace(message_id=1000)


async def member_row() -> Member | None:
    async with session_scope() as session:
        return await session.scalar(
            select(Member).where(
                Member.group_id == CHAT_ID, Member.user_id == USER_ID
            )
        )


async def reset_member(*, mute_on_rejoin: bool, verified: bool = False) -> None:
    async with session_scope() as session:
        member = await session.scalar(
            select(Member).where(
                Member.group_id == CHAT_ID, Member.user_id == USER_ID
            )
        )
        if member is None:
            member = Member(
                group_id=CHAT_ID,
                user_id=USER_ID,
                username="returning",
                first_name="Returning",
            )
            session.add(member)
        member.is_member = False
        member.is_verified = verified
        member.is_banned = False
        member.is_muted = False
        member.muted_until = None
        member.mute_on_rejoin = mute_on_rejoin


async def test_disabled_invite_redemption_keeps_code_usage() -> None:
    async with session_scope() as session:
        group = await session.get(Group, CHAT_ID)
        assert group is not None
        group.invite_redemption = False
        session.add(
            InviteCode(
                group_id=CHAT_ID,
                code="disabled-redemption",
                link="https://t.me/+disabled-redemption",
                uses=0,
                max_uses=1,
                active=True,
            )
        )
    redeemed = await join.redeem_invite(
        FakeBot(), CHAT_ID, USER_ID, "https://t.me/+disabled-redemption", FakeUser()
    )
    async with session_scope() as session:
        code = await session.scalar(
            select(InviteCode).where(InviteCode.code == "disabled-redemption")
        )
        check(
            "disabled invite redemption does not consume code",
            not redeemed and code is not None and code.uses == 0 and code.active,
            str((redeemed, code.uses if code else None, code.active if code else None)),
        )


async def main() -> None:
    await init_db()
    async with session_scope() as session:
        session.add(
            Group(
                id=CHAT_ID,
                title="Rejoin Test",
                username="rejoin_test",
                chat_type="supergroup",
                verify_enabled=False,
                verify_timeout=300,
                welcome_enabled=False,
            )
        )
    await reset_member(mute_on_rejoin=False)
    await test_disabled_invite_redemption_keeps_code_usage()

    # 1. A flagged member rejoining a group without verification is muted
    # immediately and permanently.
    await reset_member(mute_on_rejoin=True)
    bot = FakeBot()
    await join.process_join(bot, FakeChat(), FakeUser())
    row = await member_row()
    check("rejoin is restricted", "restrict" in bot.calls)
    check(
        "rejoin restriction is permanent",
        bool(bot.restrictions) and bot.restrictions[-1] == (False, None),
        str(bot.restrictions),
    )
    check("rejoin member is muted in DB", bool(row and row.is_muted))
    check("rejoin mute has no expiry", bool(row and row.muted_until is None))
    check("rejoin flag is preserved", bool(row and row.mute_on_rejoin))
    check(
        "rejoin mute is logged",
        any("重新入群后已自动禁言" in text for text in bot.messages),
    )
    async with session_scope() as session:
        log_row = await session.scalar(
            select(AdminLog).where(
                AdminLog.group_id == CHAT_ID,
                AdminLog.target_id == USER_ID,
                AdminLog.action == "rejoin_mute",
            )
        )
    check("rejoin mute audit row exists", log_row is not None)

    # 2. Passing CAPTCHA must not unmute a flagged member.
    await reset_member(mute_on_rejoin=True)
    async with session_scope() as session:
        session.add(
            Verification(
                group_id=CHAT_ID,
                user_id=USER_ID,
                token=TOKEN,
                status="pending",
                created_at=utcnow(),
                failed_attempts=0,
                message_id=321,
            )
        )
    bot = FakeBot()
    correct = captcha.captcha_options(TOKEN).index(captcha.captcha_code(TOKEN))
    callback = FakeCallback(f"verify:{TOKEN}:{correct}")
    await join.on_verify(callback, bot)
    row = await member_row()
    check("captcha pass is not unmuting", bool(row and row.is_muted))
    check("captcha pass keeps rejoin flag", bool(row and row.mute_on_rejoin))
    check(
        "captcha pass applies permanent mute",
        bool(bot.restrictions) and bot.restrictions[-1] == (False, None),
        str(bot.restrictions),
    )
    check(
        "captcha alert explains remaining mute",
        any("当前保持禁言" in answer[0] for answer in callback.answers),
        str(callback.answers),
    )
    check("captcha prompt is deleted", 321 in bot.deleted)

    # 3. Moderation helpers set the flag; verification kicks do not.
    await reset_member(mute_on_rejoin=False)
    bot = FakeBot()
    async with session_scope() as session:
        group = await session.get(Group, CHAT_ID)
        assert group is not None
        target = helpers.Target(USER_ID, "Returning", "returning")
        await helpers.kick_user(
            bot, session, group, target, reason="测试移出", admin_id=1
        )
    row = await member_row()
    check("moderation kick flags rejoin mute", bool(row and row.mute_on_rejoin))

    await reset_member(mute_on_rejoin=False)
    async with session_scope() as session:
        group = await session.get(Group, CHAT_ID)
        assert group is not None
        target = helpers.Target(USER_ID, "Returning", "returning")
        await helpers.kick_user(
            bot,
            session,
            group,
            target,
            reason="进群验证连续答错 3 次",
            admin_id=None,
            flag_rejoin=False,
        )
    row = await member_row()
    check(
        "verification kick does not flag rejoin mute",
        bool(row and not row.mute_on_rejoin),
    )

    # 4. Explicit unmute clears the flag.
    await reset_member(mute_on_rejoin=True)
    async with session_scope() as session:
        group = await session.get(Group, CHAT_ID)
        assert group is not None
        target = helpers.Target(USER_ID, "Returning", "returning")
        await helpers.unmute_user(bot, session, group, target, admin_id=1)
    row = await member_row()
    check("explicit unmute clears rejoin flag", bool(row and not row.mute_on_rejoin))

    if failures:
        print("FAILURES:", ", ".join(failures))
        raise SystemExit(1)
    print("FAILURES: none")


if __name__ == "__main__":
    asyncio.run(main())
