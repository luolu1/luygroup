"""命令菜单、学习数据分目录、功能门槛与自助命令回归测试。

使用临时配置与临时 SQLite 数据库，不修改真实 config.json / data/。

    .venv/bin/python scripts/smoke_commands.py
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import sys
import tempfile
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
TMP = Path(tempfile.mkdtemp(prefix="groupguard-commands-"))
DB = TMP / "commands.db"
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

from app.bot.handlers import admin as admin_handlers, commands, report as report_handlers  # noqa: E402
from app.bot.helpers import has_permission  # noqa: E402
from app.bot.keyboards import console_login_kb, main_menu  # noqa: E402
from app.bot.main import GROUP_COMMANDS, PRIVATE_COMMANDS  # noqa: E402
from app.db import init_db, session_scope  # noqa: E402
from app.entitlements import group_feature_allowed  # noqa: E402
from app.models import (  # noqa: E402
    AdminLog,
    ConsoleGroupAccess,
    ConsoleUser,
    Group,
    Member,
    MemberDailyStat,
    UserNotificationPreference,
    utcnow,
)
from app.services import ai_learning, repo  # noqa: E402

PRIVATE_EXPECTED = [
    "start",
    "console",
    "sub",
    "unbanme",
    "unmuteme",
    "cjtz",
    "cjhistory",
    "cjcancel",
    "jptz",
]
GROUP_EXPECTED = [
    "ads",
    "ban",
    "iunban",
    "jy",
    "jcjy",
    "wladd",
    "wlrm",
    "fy",
    "cf",
    "jf",
    "id",
    "cj",
    "jp",
]

failures: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    if cond:
        print(f"PASS {name}")
    else:
        failures.append(name)
        print(f"FAIL {name} {extra}")


class FakeUser:
    def __init__(self, user_id: int, first_name: str = "Tester") -> None:
        self.id = user_id
        self.first_name = first_name
        self.full_name = first_name
        self.username = f"user{user_id}"
        self.is_bot = False

    def mention_html(self) -> str:
        return f'<a href="tg://user?id={self.id}">{self.first_name}</a>'


class FakeChat:
    def __init__(self, chat_id: int, chat_type: str = "supergroup", title: str = "Test") -> None:
        self.id = chat_id
        self.type = chat_type
        self.title = title
        self.username = ""


class FakeMessage:
    def __init__(
        self,
        *,
        user_id: int,
        chat_id: int,
        chat_type: str = "supergroup",
        text: str = "",
    ) -> None:
        self.from_user = FakeUser(user_id)
        self.chat = FakeChat(chat_id, chat_type)
        self.text = text
        self.reply_to_message = None
        self.answers: list[str] = []
        self.replies: list[str] = []
        self.reply_kwargs: dict[str, object] = {}
        self.message_id = 7001

    async def answer(self, text: str, **kwargs) -> None:
        self.answers.append(text)

    async def reply(self, text: str, **kwargs) -> None:
        self.replies.append(text)
        self.reply_kwargs = kwargs


class FakeBot:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.unbanned: list[tuple[int, int]] = []
        self.restricted: list[tuple[int, int]] = []

    async def unban_chat_member(self, chat_id, user_id, only_if_banned=True) -> None:
        self.calls.append("unban")
        self.unbanned.append((int(chat_id), int(user_id)))

    async def restrict_chat_member(self, chat_id, user_id, permissions=None, **kwargs) -> None:
        self.calls.append("restrict")
        self.restricted.append((int(chat_id), int(user_id)))

    async def ban_chat_member(self, chat_id, user_id, **kwargs) -> None:
        self.calls.append(f"ban:{chat_id}:{user_id}")

    async def get_chat_member(self, chat_id, user_id):
        return SimpleNamespace(status="member")

    async def me(self):
        return SimpleNamespace(id=999999)

    async def send_message(self, *args, **kwargs):
        return None

    async def delete_message(self, chat_id, message_id):
        self.calls.append(f"delete_message:{message_id}")


def command_names_from_menu(text: str) -> set[str]:
    return set(re.findall(r"^/([a-z]+)\b", text, flags=re.MULTILINE))


async def create_group(group_id: int, title: str = "Bound") -> None:
    async with session_scope() as session:
        session.add(
            Group(
                id=group_id,
                title=title,
                username="",
                chat_type="supergroup",
                is_active=True,
            )
        )


async def create_console_user(telegram_id: int, role: str = "user") -> int:
    async with session_scope() as session:
        raw = f"smoke-{telegram_id}"
        user = ConsoleUser(
            telegram_id=telegram_id,
            username=f"user{telegram_id}",
            display_name="Smoke User",
            token_hash=hashlib.sha256(raw.encode()).hexdigest(),
            role=role,
        )
        session.add(user)
        await session.flush()
        return int(user.id)


async def get_console_user(telegram_id: int) -> ConsoleUser | None:
    async with session_scope() as session:
        return await session.scalar(
            select(ConsoleUser).where(ConsoleUser.telegram_id == int(telegram_id))
        )


async def bind_group(user_id: int, group_id: int) -> None:
    async with session_scope() as session:
        session.add(
            ConsoleGroupAccess(user_id=int(user_id), group_id=int(group_id), role="owner")
        )


async def create_member(group_id: int, user_id: int, **kwargs) -> None:
    async with session_scope() as session:
        member = Member(
            group_id=int(group_id),
            user_id=int(user_id),
            first_name=kwargs.pop("first_name", "Member"),
            username=kwargs.pop("username", f"user{user_id}"),
            **kwargs,
        )
        session.add(member)


async def get_member(group_id: int, user_id: int) -> Member | None:
    async with session_scope() as session:
        return await session.scalar(
            select(Member).where(
                Member.group_id == int(group_id), Member.user_id == int(user_id)
            )
        )


async def set_member(group_id: int, user_id: int, **fields) -> None:
    async with session_scope() as session:
        member = await session.scalar(
            select(Member).where(
                Member.group_id == int(group_id), Member.user_id == int(user_id)
            )
        )
        if member is None:
            raise AssertionError("member missing")
        for name, value in fields.items():
            setattr(member, name, value)


async def add_log(
    group_id: int,
    user_id: int,
    action: str,
    admin_id: int | None,
    detail: str = "",
) -> None:
    async with session_scope() as session:
        session.add(
            AdminLog(
                group_id=int(group_id),
                admin_id=admin_id,
                action=action,
                target_id=int(user_id),
                detail=detail,
            )
        )


async def run_tests() -> None:
    # ------------------------------------------------------------------
    # Exact command catalogs and menu text
    # ------------------------------------------------------------------
    private_names = [item.command for item in PRIVATE_COMMANDS]
    group_names = [item.command for item in GROUP_COMMANDS]
    check("private command catalog exact", private_names == PRIVATE_EXPECTED, str(private_names))
    check("group command catalog exact", group_names == GROUP_EXPECTED, str(group_names))

    private_menu = command_names_from_menu(commands.PRIVATE_MENU_TEXT)
    group_menu = command_names_from_menu(commands.GROUP_MENU_TEXT)
    check("private menu exact", private_menu == set(PRIVATE_EXPECTED), str(private_menu))
    check("group menu exact", group_menu == set(GROUP_EXPECTED), str(group_menu))

    menu = main_menu()
    callbacks = {
        button.callback_data
        for row in menu.inline_keyboard
        for button in row
        if button.callback_data
    }
    check(
        "inline menu has group/private choices",
        {"help:group", "help:private", "panel:close"}.issubset(callbacks),
        str(callbacks),
    )
    login_kb = console_login_kb("https://example.test/console?token=code")
    login_button = login_kb.inline_keyboard[0][0]
    check(
        "console login keyboard uses URL button",
        login_button.url == "https://example.test/console?token=code",
        str(login_button),
    )

    command_group_id = -100915000
    command_user_id = 915000
    command_user_pk = await create_console_user(command_user_id, "subscriber")
    await create_group(command_group_id, "Command cleanup")
    await bind_group(command_user_pk, command_group_id)
    command_msg = FakeMessage(
        user_id=command_user_id,
        chat_id=command_group_id,
        chat_type="supergroup",
        text="/ban",
    )
    command_bot = FakeBot()
    command_msg.reply_to_message = SimpleNamespace(
        message_id=7000,
        from_user=FakeUser(915099, "Target"),
    )
    await admin_handlers.cmd_ban(command_msg, command_bot)
    await asyncio.sleep(2.1)
    check(
        "/ban deletes its command message when enabled",
        "ban:-100915000:915099" in command_bot.calls
        and "delete_message:7000" in command_bot.calls
        and "delete_message:7001" in command_bot.calls,
        str(command_bot.calls),
    )

    token_user_id = 915001
    token_user_pk = await create_console_user(token_user_id, "subscriber")
    await create_group(-100915001, "Token summary")
    await create_group(-100915002, "Token summary 2")
    await bind_group(token_user_pk, -100915001)
    await bind_group(token_user_pk, -100915002)
    token_msg = FakeMessage(
        user_id=token_user_id, chat_id=token_user_id, chat_type="private", text="/token"
    )
    token_msg.from_user.username = "token_tester"
    await commands.cmd_token(token_msg)
    token_text = "\n".join(token_msg.replies)
    check(
        "token summary includes account and balance",
        f"{token_user_id} (@token_tester)" in token_text and "余额: 0.00" in token_text,
        token_text,
    )
    check(
        "token summary includes binding and one-time expiry",
        "绑定上限: 5，当前已绑定 2" in token_text
        and "15 分钟内有效，只能使用一次" in token_text,
        token_text,
    )
    sub_msg = FakeMessage(
        user_id=token_user_id,
        chat_id=token_user_id,
        chat_type="private",
        text="/sub",
    )
    await commands.cmd_sub(sub_msg)
    sub_text = "\n".join(sub_msg.replies)
    check(
        "/sub includes CNY balance and monthly purchase instructions",
        "账户余额：¥0.00 CNY" in sub_text
        and "¥8.00 CNY/月" in sub_text
        and "账户与订阅" in sub_text,
        sub_text,
    )
    token_url = token_msg.reply_kwargs["reply_markup"].inline_keyboard[0][0].url
    login_token = token_text.split("token: <code>")[1].split("</code>")[0]
    async with session_scope() as session:
        token_user = await session.get(ConsoleUser, token_user_pk)
        check(
            "token expires in fifteen minutes and is hash-only",
            token_user is not None
            and token_user.token_expires_at is not None
            and 0 < (token_user.token_expires_at - utcnow()).total_seconds() <= 900
            and token_user.token_value is None
            and token_url.endswith("/console?token=" + login_token),
            token_url,
        )

    # ------------------------------------------------------------------
    # `/ads` switches menu by chat type
    # ------------------------------------------------------------------
    private_msg = FakeMessage(user_id=910001, chat_id=910001, chat_type="private", text="/ads")
    await commands.cmd_ads(private_msg)
    check(
        "private /ads shows private menu",
        private_msg.answers and "私聊菜单" in private_msg.answers[-1],
        str(private_msg.answers),
    )
    group_msg = FakeMessage(user_id=910002, chat_id=-100910002, text="/ads")
    await commands.cmd_ads(group_msg)
    check(
        "group /ads shows group menu",
        group_msg.answers and "总菜单" in group_msg.answers[-1],
        str(group_msg.answers),
    )

    # ------------------------------------------------------------------
    # Learning data is separated by purpose
    # ------------------------------------------------------------------
    store = ai_learning.LearningStore(TMP / "learning-split")
    await store.put("ad", 123, "buy cheap followers", '{"is_ad": true}')
    await store.put("assistant", 123, "how to verify", "Use the verify button.")
    await store.put("chat", 123, "hello", "Hi!")
    split_stats = store.stats()
    check(
        "learning writes ad_detection directory",
        (TMP / "learning-split" / "ad_detection" / "group_123.jsonl").exists(),
    )
    check(
        "learning writes assistant directory",
        (TMP / "learning-split" / "assistant" / "group_123.jsonl").exists(),
    )
    check(
        "learning writes chat directory",
        (TMP / "learning-split" / "chat" / "group_123.jsonl").exists(),
    )
    check(
        "learning stats split by kind",
        split_stats["by_kind"] == {"ad_detection": 1, "assistant": 1, "chat": 1},
        str(split_stats),
    )

    legacy = TMP / "learning-legacy"
    legacy.mkdir(parents=True)
    (legacy / "group_9.jsonl").write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "key": "legacy-ad",
                        "kind": "ad",
                        "group_id": 9,
                        "question": "ad",
                        "answer": "yes",
                    },
                    ensure_ascii=False,
                ),
                json.dumps(
                    {
                        "key": "legacy-chat",
                        "kind": "chat",
                        "group_id": 9,
                        "question": "hi",
                        "answer": "hello",
                    },
                    ensure_ascii=False,
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    legacy_store = ai_learning.LearningStore(legacy)
    legacy_stats = legacy_store.stats()
    check(
        "legacy root files split by kind",
        not (legacy / "group_9.jsonl").exists()
        and (legacy / "ad_detection" / "group_9.jsonl").exists()
        and (legacy / "chat" / "group_9.jsonl").exists(),
    )
    check(
        "legacy migration keeps all entries",
        legacy_stats["by_kind"] == {"ad_detection": 1, "chat": 1},
        str(legacy_stats),
    )

    # ------------------------------------------------------------------
    # Group features follow the bound owner's plan
    # ------------------------------------------------------------------
    await create_group(-100920001, "Basic bound")
    await create_group(-100920002, "Unbound")
    basic_id = await create_console_user(920001, "user")
    await bind_group(basic_id, -100920001)
    async with session_scope() as session:
        check(
            "unbound group keeps legacy features",
            await group_feature_allowed(session, -100920002, "games"),
        )
        check(
            "basic bound group denies games",
            not await group_feature_allowed(session, -100920001, "games"),
        )
        check(
            "basic bound group denies points",
            not await group_feature_allowed(session, -100920001, "points"),
        )
        check(
            "basic bound group denies ai",
            not await group_feature_allowed(session, -100920001, "ai"),
        )
        check(
            "basic bound group keeps settings",
            await group_feature_allowed(session, -100920001, "settings"),
        )
        check(
            "bound group owner can manage group despite Telegram lookup failure",
            await has_permission(FakeBot(), session, -100920001, 920001, "ban"),
        )
        check(
            "unbound user cannot manage another group",
            not await has_permission(FakeBot(), session, -100920001, 920099, "ban"),
        )
    async with session_scope() as session:
        user = await session.get(ConsoleUser, basic_id)
        assert user is not None
        user.role = "subscriber"
    async with session_scope() as session:
        check(
            "subscriber bound group allows games",
            await group_feature_allowed(session, -100920001, "games"),
        )
        check(
            "subscriber bound group allows ai",
            await group_feature_allowed(session, -100920001, "ai"),
        )

    report_group_id = -100920010
    await create_group(report_group_id, "Report learning")
    report_owner_id = await create_console_user(920010, "subscriber")
    await bind_group(report_owner_id, report_group_id)
    reporter = FakeMessage(
        user_id=920011, chat_id=report_group_id, text="/report 诈骗广告"
    )
    reporter.reply_to_message = SimpleNamespace(
        from_user=FakeUser(920012, "Target"),
        chat=reporter.chat,
        text="投资返利，点击 example.invalid",
        caption=None,
        message_id=77,
    )
    await report_handlers.cmd_report(reporter, FakeBot())
    report_entry = ai_learning.learning_store.get(
        "ad", report_group_id, "投资返利，点击 example.invalid"
    )
    check(
        "member ad report is learned immediately with provenance",
        report_entry is not None
        and report_entry.get("is_ad") is True
        and report_entry.get("provider") == "member-report"
        and report_entry.get("report_id") is not None,
        str(report_entry),
    )

    # Atomic daily counter keeps concurrent messages from colliding.
    daily_group = -100920003
    daily_user = 920003
    await create_group(daily_group, "Daily counter")
    await create_member(daily_group, daily_user)
    async with session_scope() as session:
        await repo.bump_member_daily(session, daily_group, daily_user)
        await repo.bump_member_daily(session, daily_group, daily_user)
    async with session_scope() as session:
        daily = await session.scalar(
            select(MemberDailyStat).where(
                MemberDailyStat.group_id == daily_group,
                MemberDailyStat.user_id == daily_user,
            )
        )
        check(
            "daily counter upserts without duplicate rows",
            daily is not None and daily.messages == 2,
            str(daily.messages if daily else None),
        )

    # ------------------------------------------------------------------
    # Notification toggles are private and autonomous
    # ------------------------------------------------------------------
    notify_msg = FakeMessage(
        user_id=930001, chat_id=930001, chat_type="private", text="/cjtz on"
    )
    await commands.cmd_cjtz(notify_msg)
    async with session_scope() as session:
        pref = await session.scalar(
            select(UserNotificationPreference).where(
                UserNotificationPreference.telegram_id == 930001
            )
        )
        check(
            "lottery notification turns on",
            pref is not None and pref.lottery_enabled and not pref.auction_enabled,
            str(pref),
        )
    off_msg = FakeMessage(
        user_id=930001, chat_id=930001, chat_type="private", text="/cjtz off"
    )
    await commands.cmd_cjtz(off_msg)
    auction_msg = FakeMessage(
        user_id=930001, chat_id=930001, chat_type="private", text="/jptz on"
    )
    await commands.cmd_jptz(auction_msg)
    async with session_scope() as session:
        pref = await session.scalar(
            select(UserNotificationPreference).where(
                UserNotificationPreference.telegram_id == 930001
            )
        )
        check(
            "notification toggles persist independently",
            pref is not None
            and not pref.lottery_enabled
            and pref.auction_enabled,
            str(pref),
        )
    group_toggle = FakeMessage(
        user_id=930001, chat_id=-100930001, chat_type="supergroup", text="/jptz off"
    )
    await commands.cmd_jptz(group_toggle)
    check(
        "notification toggle refuses group chat",
        group_toggle.replies
        and "请在私聊" in group_toggle.replies[-1],
        str(group_toggle.replies),
    )

    # ------------------------------------------------------------------
    # `/fy` and `/cf`
    # ------------------------------------------------------------------
    fy_group = -100940001
    fy_user = 940001
    await create_group(fy_group, "Stats group")
    await create_member(fy_group, fy_user, message_count=50)
    async with session_scope() as session:
        today = utcnow()
        session.add_all(
            [
                MemberDailyStat(
                    group_id=fy_group,
                    user_id=fy_user,
                    day=today.strftime("%Y-%m-%d"),
                    messages=5,
                ),
                MemberDailyStat(
                    group_id=fy_group,
                    user_id=fy_user,
                    day=(today - timedelta(days=15)).strftime("%Y-%m-%d"),
                    messages=7,
                ),
                MemberDailyStat(
                    group_id=fy_group,
                    user_id=fy_user,
                    day=(today - timedelta(days=40)).strftime("%Y-%m-%d"),
                    messages=100,
                ),
            ]
        )
    fy_msg = FakeMessage(user_id=fy_user, chat_id=fy_group, text="/fy")
    await commands.cmd_fy(fy_msg, FakeBot())
    fy_text = "\n".join(fy_msg.replies)
    check(
        "/fy 10-day count",
        "近 10 天：<b>5</b>" in fy_text,
        fy_text[:300],
    )
    check(
        "/fy 30-day count",
        "近 30 天：<b>12</b>" in fy_text,
        fy_text[:300],
    )
    check(
        "/fy all-time count",
        "累计发言：<b>50</b>" in fy_text,
        fy_text[:300],
    )

    await set_member(fy_group, fy_user, is_banned=True)
    await add_log(fy_group, fy_user, "ban", None, "广告刷屏")
    cf_msg = FakeMessage(user_id=fy_user, chat_id=fy_group, text="/cf")
    await commands.cmd_cf(cf_msg, FakeBot())
    cf_text = "\n".join(cf_msg.replies)
    check("/cf shows current state", "封禁中" in cf_text, cf_text[:300])
    check("/cf shows automatic actor", "自动处理" in cf_text, cf_text[:300])
    check("/cf shows reason", "广告刷屏" in cf_text, cf_text[:300])
    check(
        "/cf labels global unban",
        commands._ACTION_LABELS.get("global_unban") == "全局解封",
    )

    # ------------------------------------------------------------------
    # Self-unban keeps the rejoin-mute flag; manual bans are blocked
    # ------------------------------------------------------------------
    auto_group = -100950001
    auto_user = 950001
    await create_group(auto_group, "Auto ban")
    await create_member(
        auto_group, auto_user, is_banned=True, is_member=False, mute_on_rejoin=True
    )
    await add_log(auto_group, auto_user, "ban", None, "自动反广告")
    bot = FakeBot()
    unban_msg = FakeMessage(
        user_id=auto_user, chat_id=auto_user, chat_type="private", text="/unbanme"
    )
    await commands.cmd_unbanme(unban_msg, bot)
    member = await get_member(auto_group, auto_user)
    check("self-unban calls Telegram", bot.unbanned == [(auto_group, auto_user)], str(bot.unbanned))
    check("self-unban clears ban", bool(member and not member.is_banned))
    check(
        "self-unban keeps rejoin mute",
        bool(member and member.mute_on_rejoin),
        str(member.mute_on_rejoin if member else None),
    )
    check(
        "self-unban replies",
        any("已自助解除" in item for item in unban_msg.replies),
        str(unban_msg.replies),
    )

    manual_group = -100950002
    await create_group(manual_group, "Manual ban")
    await create_member(manual_group, auto_user, is_banned=True, is_member=False)
    await add_log(manual_group, auto_user, "ban", 999999, "管理员手动封禁")
    manual_msg = FakeMessage(
        user_id=auto_user, chat_id=auto_user, chat_type="private", text="/unbanme"
    )
    await commands.cmd_unbanme(manual_msg, FakeBot())
    member = await get_member(manual_group, auto_user)
    check("self-unban blocks manual ban", bool(member and member.is_banned))
    check(
        "self-unban explains manual ban",
        any("管理员手动封禁" in item for item in manual_msg.replies),
        str(manual_msg.replies),
    )

    # ------------------------------------------------------------------
    # Self-unmute: automatic only, never manual or rejoin mute
    # ------------------------------------------------------------------
    mute_group = -100960001
    mute_user = 960001
    await create_group(mute_group, "Auto mute")
    await create_member(mute_group, mute_user, is_muted=True, mute_on_rejoin=False)
    await add_log(mute_group, mute_user, "mute", None, "自动禁言")
    mute_bot = FakeBot()
    unmute_msg = FakeMessage(
        user_id=mute_user, chat_id=mute_user, chat_type="private", text="/unmuteme"
    )
    await commands.cmd_unmuteme(unmute_msg, mute_bot)
    member = await get_member(mute_group, mute_user)
    check("self-unmute calls Telegram", mute_bot.restricted == [(mute_group, mute_user)])
    check("self-unmute clears automatic mute", bool(member and not member.is_muted))

    await set_member(mute_group, mute_user, is_muted=True, mute_on_rejoin=False)
    await add_log(mute_group, mute_user, "mute", 999999, "管理员手动禁言")
    manual_mute = FakeMessage(
        user_id=mute_user, chat_id=mute_user, chat_type="private", text="/unmuteme"
    )
    await commands.cmd_unmuteme(manual_mute, FakeBot())
    member = await get_member(mute_group, mute_user)
    check("self-unmute blocks manual mute", bool(member and member.is_muted))
    check(
        "self-unmute explains manual restriction",
        any("需管理员解除" in item for item in manual_mute.replies),
        str(manual_mute.replies),
    )

    await set_member(mute_group, mute_user, is_muted=True, mute_on_rejoin=True)
    rejoin_msg = FakeMessage(
        user_id=mute_user, chat_id=mute_user, chat_type="private", text="/unmuteme"
    )
    await commands.cmd_unmuteme(rejoin_msg, FakeBot())
    member = await get_member(mute_group, mute_user)
    check("self-unmute blocks rejoin mute", bool(member and member.is_muted))
    check(
        "self-unmute explains rejoin mute",
        any("入群自动禁言" in item for item in rejoin_msg.replies),
        str(rejoin_msg.replies),
    )

    # ------------------------------------------------------------------
    # /iunban: superadmin acts across all groups, and empty groups are skipped
    # ------------------------------------------------------------------
    current_group = -100970001
    other_group = -100970002
    iunban_user = 970001
    await create_group(current_group, "Current group")
    await create_group(other_group, "Other restricted group")
    await create_member(other_group, iunban_user, is_banned=True, is_member=False)
    iunban_bot = FakeBot()
    iunban_msg = FakeMessage(
        user_id=7122485076,
        chat_id=current_group,
        chat_type="supergroup",
        text="/iunban",
    )
    iunban_msg.reply_to_message = SimpleNamespace(from_user=FakeUser(iunban_user))
    await admin_handlers.cmd_iunban(iunban_msg, iunban_bot)
    check(
        "/iunban only processes restricted groups",
        iunban_bot.unbanned == [(other_group, iunban_user)],
        str(iunban_bot.unbanned),
    )
    check(
        "/iunban does not claim empty current group",
        any("已处理 1 个群组" in item for item in iunban_msg.replies),
        str(iunban_msg.replies),
    )
    member = await get_member(other_group, iunban_user)
    check(
        "/iunban clears restriction flags",
        bool(
            member
            and not member.is_banned
            and not member.is_muted
            and not member.mute_on_rejoin
        ),
    )


async def main() -> None:
    await init_db()
    try:
        await run_tests()
    finally:
        if failures:
            print("FAILURES:", failures)
            raise SystemExit(1)
        print("FAILURES: none")


if __name__ == "__main__":
    asyncio.run(main())
