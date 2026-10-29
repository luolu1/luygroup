"""竞拍出价与红包领取的并发资金安全回归测试。

使用临时配置与临时 SQLite 数据库，不修改真实 config.json / data/，也不访问
Telegram。重点验证 CAS 抢占顺序与「抢占失败必须回滚」语义。

    .venv/bin/python scripts/smoke_games_race.py
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
TMP = Path(tempfile.mkdtemp(prefix="groupguard-games-race-"))
DB = TMP / "games-race.db"
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

from sqlalchemy import func, select  # noqa: E402
from sqlalchemy.exc import OperationalError  # noqa: E402

from app.bot import helpers  # noqa: E402
from app.bot.handlers import games, moderation  # noqa: E402
from app.db import dispose_db, init_db, session_scope  # noqa: E402
from app.models import (  # noqa: E402
    Auction,
    AuctionBid,
    Group,
    Lottery,
    LotteryEntry,
    Member,
    PointsTransaction,
    RedPacket,
    RedPacketClaim,
    utcnow,
)
from app.services import repo, scheduler  # noqa: E402

failures: list[str] = []


def future_draw_time() -> str:
    return (utcnow() + timedelta(days=1)).strftime("%Y-%m-%d %H:%M")


def check(name: str, cond: bool, extra: str = "") -> None:
    if cond:
        print(f"PASS {name}")
    else:
        failures.append(name)
        print(f"FAIL {name} {extra}")


async def with_retry(factory):
    """Test-only retry for SQLite's transient database-is-locked errors."""
    for attempt in range(3):
        try:
            return await factory()
        except OperationalError as exc:
            if "database is locked" not in str(exc).lower() or attempt == 2:
                raise
        await asyncio.sleep(0.05)


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []
        self.membership: dict[tuple[int, int], str] = {}
        self.next_message_id = 700
        self.hold_send = False
        self.send_started = asyncio.Event()
        self.send_release = asyncio.Event()
        self.send_release.set()

    async def send_message(self, chat_id: int, text: str, **kwargs):
        if self.hold_send:
            self.send_started.set()
            await self.send_release.wait()
        self.sent.append((int(chat_id), text))
        self.next_message_id += 1
        return SimpleNamespace(message_id=self.next_message_id)

    async def get_chat_member(self, chat_id: int, user_id: int):
        return SimpleNamespace(
            status=self.membership.get((int(chat_id), int(user_id)), "member")
        )

    async def me(self):
        return SimpleNamespace(username="smokebot")


class FakeUser:
    def __init__(self, user_id: int, first_name: str = "Tester") -> None:
        self.id = user_id
        self.first_name = first_name
        self.username = f"user{user_id}"
        self.is_bot = False


class FakeChat:
    def __init__(self, chat_id: int, title: str = "Games race") -> None:
        self.id = chat_id
        self.title = title
        self.username = ""
        self.type = "supergroup"


class FakeMessage:
    def __init__(self, *, user_id: int, chat_id: int, text: str = "") -> None:
        self.from_user = FakeUser(user_id)
        self.chat = FakeChat(chat_id)
        self.sender_chat = None
        self.forward_from_chat = None
        if chat_id > 0:
            self.chat.type = "private"
        self.text = text
        self.reply_to_message = None
        self.replies: list[str] = []
        self.markup = None
        self.sent_answers: list[tuple[str, object | None]] = []

    async def reply(self, text: str, **kwargs) -> None:
        self.replies.append(text)
        self.markup = kwargs.get("reply_markup")

    async def answer(self, text: str, **kwargs) -> None:
        self.sent_answers.append((text, kwargs.get("reply_markup")))


class FakeCallbackMessage:
    def __init__(self, chat_id: int) -> None:
        self.chat = FakeChat(chat_id)
        if chat_id > 0:
            self.chat.type = "private"
        self.markup_calls: list[object] = []
        self.answers: list[tuple[str, object | None]] = []
        self.edited_text: str | None = None

    async def edit_reply_markup(self, reply_markup=None) -> None:
        self.markup_calls.append(reply_markup)

    async def answer(self, text: str, **kwargs) -> None:
        self.answers.append((text, kwargs.get("reply_markup")))

    async def edit_text(self, text: str, **kwargs) -> None:
        self.edited_text = text
        self.answers.append((text, kwargs.get("reply_markup")))


class FakeCallback:
    def __init__(self, *, data: str, user_id: int, chat_id: int) -> None:
        self.data = data
        self.from_user = FakeUser(user_id)
        self.message = FakeCallbackMessage(chat_id)
        self.answers: list[str] = []

    async def answer(self, text: str = "", show_alert: bool = False) -> None:
        self.answers.append(text)


async def test_lottery_private_setup() -> None:
    group_id = -100820099
    user_id = 820099
    await create_group(group_id, "Lottery setup")
    bot = FakeBot()
    bot.membership[(group_id, user_id)] = "administrator"

    start = FakeMessage(user_id=user_id, chat_id=group_id, text="/cj")
    start.chat.title = "Lottery setup"
    await games.cmd_lottery(start, bot)
    assert bot.sent[-1][0] == user_id
    assert "Lottery setup" in bot.sent[0][1]
    assert "第 1/6 步 · 标题" in bot.sent[0][1]
    assert "例如：周末抽奖" in bot.sent[0][1]
    async with session_scope() as session:
        assert await session.scalar(
            select(Lottery.id).where(Lottery.group_id == group_id)
        ) is None

    await games.lottery_private_config(
        FakeMessage(user_id=user_id, chat_id=user_id, text="周末抽奖"),
        bot,
    )
    await games.lottery_private_config(
        FakeMessage(user_id=user_id, chat_id=user_id, text="一个月会员"), bot
    )
    await games.lottery_setup_callback(
        FakeCallback(data="lotsetup:condition:none", user_id=user_id, chat_id=user_id),
        bot,
    )
    await games.lottery_private_config(
        FakeMessage(user_id=user_id, chat_id=user_id, text="2"), bot
    )
    await games.lottery_setup_callback(
        FakeCallback(data="lotsetup:draw:time", user_id=user_id, chat_id=user_id),
        bot,
    )
    await games.lottery_private_config(
        FakeMessage(user_id=user_id, chat_id=user_id, text=future_draw_time()), bot
    )
    configured_draw_at = games._lottery_drafts[user_id].draw_at
    assert configured_draw_at is not None
    confirm = FakeMessage(user_id=user_id, chat_id=user_id, text="@admin")
    await games.lottery_private_config(confirm, bot)
    assert confirm.markup is not None
    callback = FakeCallback(data="lotsetup:create", user_id=user_id, chat_id=user_id)
    await games.lottery_setup_callback(callback, bot)
    async with session_scope() as session:
        lottery = await session.scalar(
            select(Lottery).where(Lottery.group_id == group_id)
        )
        assert lottery is not None
        assert lottery.title == "周末抽奖"
        assert lottery.winners_count == 2
        assert lottery.participation_type == "none"
        assert lottery.draw_type == "time"
        assert lottery.claim_contact == "@admin"
        assert lottery.ends_at is not None
        assert lottery.ends_at == configured_draw_at
    assert any(chat_id == group_id and "周末抽奖" in text for chat_id, text in bot.sent)
    print("PASS group admin walks the private wizard and creates a time lottery")
    games._lottery_drafts.pop(user_id, None)

    games._lottery_drafts[user_id] = replace(
        games._lottery_drafts.get(user_id)
        or games.LotteryDraft(
            user_id=user_id,
            group_id=group_id,
            created_at=utcnow(),
            step="confirm",
            title="Concurrent lottery",
            prize="Prize",
        ),
        step="confirm",
        title="Concurrent lottery",
        prize="Prize",
    )
    bot.hold_send = True
    bot.send_release.clear()
    sent_before = len(bot.sent)
    first_confirm = FakeCallback(
        data="lotsetup:create", user_id=user_id, chat_id=user_id
    )
    second_confirm = FakeCallback(
        data="lotsetup:create", user_id=user_id, chat_id=user_id
    )
    first_task = asyncio.create_task(games.lottery_setup_callback(first_confirm, bot))
    await bot.send_started.wait()
    second_task = asyncio.create_task(games.lottery_setup_callback(second_confirm, bot))
    await second_task
    bot.send_release.set()
    await first_task
    async with session_scope() as session:
        concurrent_count = int(
            await session.scalar(
                select(func.count(Lottery.id)).where(
                    Lottery.group_id == group_id,
                    Lottery.title == "Concurrent lottery",
                )
            )
            or 0
        )
    assert concurrent_count == 1
    assert len(bot.sent) == sent_before + 1
    print("PASS duplicate lottery confirmation creates and publishes once")
    games._lottery_drafts.pop(user_id, None)

    revoked_group_id = -100820097
    revoked_user_id = 820097
    await create_group(revoked_group_id, "Lottery revoked")
    bot.membership[(revoked_group_id, revoked_user_id)] = "administrator"
    await games.cmd_lottery(
        FakeMessage(user_id=revoked_user_id, chat_id=revoked_group_id, text="/cj"),
        bot,
    )
    await games.lottery_private_config(
        FakeMessage(user_id=revoked_user_id, chat_id=revoked_user_id, text="不应发布"),
        bot,
    )
    await games.lottery_private_config(
        FakeMessage(user_id=revoked_user_id, chat_id=revoked_user_id, text="中奖品"), bot
    )
    await games.lottery_setup_callback(
        FakeCallback(
            data="lotsetup:condition:none", user_id=revoked_user_id, chat_id=revoked_user_id
        ),
        bot,
    )
    await games.lottery_private_config(
        FakeMessage(user_id=revoked_user_id, chat_id=revoked_user_id, text="1"), bot
    )
    await games.lottery_setup_callback(
        FakeCallback(
            data="lotsetup:draw:time", user_id=revoked_user_id, chat_id=revoked_user_id
        ),
        bot,
    )
    await games.lottery_private_config(
        FakeMessage(
            user_id=revoked_user_id,
            chat_id=revoked_user_id,
            text=future_draw_time(),
        ),
        bot,
    )
    await games.lottery_private_config(
        FakeMessage(user_id=revoked_user_id, chat_id=revoked_user_id, text="@admin"), bot
    )
    bot.membership[(revoked_group_id, revoked_user_id)] = "member"
    await games.lottery_setup_callback(
        FakeCallback(
            data="lotsetup:create",
            user_id=revoked_user_id,
            chat_id=revoked_user_id,
        ),
        bot,
    )
    async with session_scope() as session:
        assert await session.scalar(
            select(Lottery.id).where(Lottery.group_id == revoked_group_id)
        ) is None
    print("PASS revoked admin cannot confirm lottery")


async def test_lottery_wizard_back_and_expiry() -> None:
    group_id = -100820088
    user_id = 820088
    await create_group(group_id, "Lottery edit")
    bot = FakeBot()
    bot.membership[(group_id, user_id)] = "administrator"
    await games.cmd_lottery(
        FakeMessage(user_id=user_id, chat_id=group_id, text="/cj"), bot
    )
    started = games._lottery_drafts[user_id].created_at
    await games.lottery_private_config(
        FakeMessage(user_id=user_id, chat_id=user_id, text="Original title"), bot
    )
    back = FakeCallback(
        data="lotsetup:back:title", user_id=user_id, chat_id=user_id
    )
    await games.lottery_setup_callback(back, bot)
    assert games._lottery_drafts[user_id].step == "title"
    assert games._lottery_drafts[user_id].created_at == started
    await games.lottery_private_config(
        FakeMessage(user_id=user_id, chat_id=user_id, text="Updated title"), bot
    )
    assert games._lottery_drafts[user_id].title == "Updated title"
    print("PASS lottery wizard returns to title without extending draft lifetime")

    games._lottery_drafts[user_id] = replace(
        games._lottery_drafts[user_id],
        created_at=utcnow() - timedelta(minutes=5),
    )
    expired = FakeMessage(user_id=user_id, chat_id=user_id, text="Late prize")
    await games.lottery_private_config(expired, bot)
    assert user_id not in games._lottery_drafts
    assert expired.replies
    print("PASS lottery wizard draft expires after five minutes")

    confirm_group = -100820087
    confirm_user = 820087
    await create_group(confirm_group, "Lottery confirmation expiry")
    bot.membership[(confirm_group, confirm_user)] = "administrator"
    await games.cmd_lottery(
        FakeMessage(user_id=confirm_user, chat_id=confirm_group, text="/cj"), bot
    )
    games._lottery_drafts[confirm_user] = replace(
        games._lottery_drafts[confirm_user],
        step="confirm",
        title="Expired draft",
        prize="Prize",
        created_at=utcnow() - timedelta(minutes=5),
    )
    await games.lottery_setup_callback(
        FakeCallback(data="lotsetup:create", user_id=confirm_user, chat_id=confirm_user),
        bot,
    )
    async with session_scope() as session:
        assert await session.scalar(
            select(Lottery.id).where(Lottery.group_id == confirm_group)
        ) is None
    print("PASS expired lottery draft cannot be confirmed")


async def test_lottery_participation_rules() -> None:
    group_id = -100820094
    admin_id = 820094
    member_id = 820095
    await create_group(group_id, "Lottery rules")
    bot = FakeBot()
    bot.membership[(group_id, admin_id)] = "administrator"
    await games.cmd_lottery(
        FakeMessage(user_id=admin_id, chat_id=group_id, text="/cj"), bot
    )
    await games.lottery_private_config(
        FakeMessage(user_id=admin_id, chat_id=admin_id, text="关键词抽奖"), bot
    )
    await games.lottery_private_config(
        FakeMessage(user_id=admin_id, chat_id=admin_id, text="奖品A"), bot
    )
    await games.lottery_setup_callback(
        FakeCallback(data="lotsetup:condition:keyword", user_id=admin_id, chat_id=admin_id),
        bot,
    )
    await games.lottery_private_config(
        FakeMessage(user_id=admin_id, chat_id=admin_id, text="抽奖"), bot
    )
    await games.lottery_private_config(
        FakeMessage(user_id=admin_id, chat_id=admin_id, text="1"), bot
    )
    await games.lottery_setup_callback(
        FakeCallback(data="lotsetup:draw:time", user_id=admin_id, chat_id=admin_id), bot
    )
    await games.lottery_private_config(
        FakeMessage(user_id=admin_id, chat_id=admin_id, text=future_draw_time()), bot
    )
    await games.lottery_private_config(
        FakeMessage(user_id=admin_id, chat_id=admin_id, text="@admin"), bot
    )
    await games.lottery_setup_callback(
        FakeCallback(data="lotsetup:create", user_id=admin_id, chat_id=admin_id), bot
    )
    async with session_scope() as session:
        keyword_lottery = await session.scalar(
            select(Lottery).where(Lottery.group_id == group_id)
        )
        assert keyword_lottery is not None
        assert keyword_lottery.participation_type == "keyword"
        keyword_lottery_id = int(keyword_lottery.id)

    denied = FakeCallback(
        data=f"lot:{keyword_lottery_id}", user_id=member_id, chat_id=group_id
    )
    await games.on_lottery_join(denied)
    assert any("需要先在群内发送" in answer for answer in denied.answers), denied.answers
    print("PASS keyword lottery rejects member without qualifying message")

    auto_message = FakeMessage(user_id=member_id, chat_id=group_id, text="我要参加抽奖")
    auto_message.chat.title = "Lottery rules"
    await moderation.on_group_message(auto_message, bot)
    async with session_scope() as session:
        entry_count = int(await session.scalar(
            select(func.count(LotteryEntry.id)).where(
                LotteryEntry.lottery_id == keyword_lottery_id,
                LotteryEntry.user_id == member_id,
            )
        ) or 0)
        assert entry_count == 0
    exact_message = FakeMessage(user_id=member_id, chat_id=group_id, text="抽奖")
    exact_message.chat.title = "Lottery rules"
    await moderation.on_group_message(exact_message, bot)
    async with session_scope() as session:
        entry_count = int(await session.scalar(
            select(func.count(LotteryEntry.id)).where(
                LotteryEntry.lottery_id == keyword_lottery_id,
                LotteryEntry.user_id == member_id,
            )
        ) or 0)
        assert entry_count == 1
    history = FakeMessage(user_id=admin_id, chat_id=admin_id, text="/cjhistory")
    await games.lottery_history(history)
    assert any("我创建的抽奖" in text for text, _markup in history.sent_answers)
    list_markup = next(
        markup for _text, markup in history.sent_answers if markup is not None
    )
    assert any("关键词抽奖" in button.text for row in list_markup.inline_keyboard for button in row)
    print("PASS keyword message automatically enters member and appears in history")
    allowed = FakeCallback(
        data=f"lot:{keyword_lottery_id}", user_id=member_id, chat_id=group_id
    )
    await games.on_lottery_join(allowed)
    assert any("你已经参与过了" in answer for answer in allowed.answers), allowed.answers
    print("PASS keyword lottery button recognizes automatic entry")

    admin_bot = FakeBot()
    admin_bot.membership[(group_id, admin_id)] = "administrator"
    admin_message = FakeMessage(user_id=admin_id, chat_id=group_id, text="抽奖")
    admin_message.chat.title = "Lottery rules"
    await moderation.on_group_message(admin_message, admin_bot)
    async with session_scope() as session:
        admin_entry_count = int(
            await session.scalar(
                select(func.count(LotteryEntry.id)).where(
                    LotteryEntry.lottery_id == keyword_lottery_id,
                    LotteryEntry.user_id == admin_id,
                )
            )
            or 0
        )
    assert admin_entry_count == 1
    print("PASS group admin keyword message automatically enters lottery")

    points_group = -100820093
    points_admin = 820092
    points_member = 820091
    await create_group(points_group, "Lottery points")
    bot.membership[(points_group, points_admin)] = "administrator"
    await set_points(points_group, points_member, 5)
    async with session_scope() as session:
        points_lottery = Lottery(
            group_id=points_group,
            creator_id=points_admin,
            title="积分抽奖",
            prize="奖品B",
            winners_count=1,
            participation_type="points",
            participation_value="100",
            draw_type="time",
            status="open",
            ends_at=utcnow() + timedelta(minutes=30),
        )
        session.add(points_lottery)
        await session.flush()
        points_lottery_id = int(points_lottery.id)

    poor = FakeCallback(
        data=f"lot:{points_lottery_id}", user_id=points_member, chat_id=points_group
    )
    await games.on_lottery_join(poor)
    assert any("需要至少 100 积分" in answer for answer in poor.answers), poor.answers
    print("PASS points lottery rejects member below threshold")

    async with session_scope() as session:
        member = await session.scalar(
            select(Member).where(
                Member.group_id == points_group, Member.user_id == points_member
            )
        )
        assert member is not None
        member.points = 200
    rich = FakeCallback(
        data=f"lot:{points_lottery_id}", user_id=points_member, chat_id=points_group
    )
    await games.on_lottery_join(rich)
    assert any("参与成功" in answer for answer in rich.answers), rich.answers
    print("PASS points lottery accepts member at threshold")

    message_group = -100820092
    message_member = 820090
    await create_group(message_group, "Lottery message count")
    async with session_scope() as session:
        session.add(
            Member(
                group_id=message_group,
                user_id=message_member,
                message_count=20,
            )
        )
        message_lottery = Lottery(
            group_id=message_group,
            creator_id=820089,
            title="发言数抽奖",
            prize="奖品D",
            winners_count=1,
            participation_type="message_count",
            participation_value="20",
            draw_type="time",
            status="open",
            ends_at=utcnow() + timedelta(minutes=30),
        )
        session.add(message_lottery)
        await session.flush()
        message_lottery_id = int(message_lottery.id)
    active_member = FakeCallback(
        data=f"lot:{message_lottery_id}",
        user_id=message_member,
        chat_id=message_group,
    )
    await games.on_lottery_join(active_member)
    assert any("参与成功" in answer for answer in active_member.answers)
    print("PASS message-count lottery accepts member at threshold")


async def test_lottery_creator_management() -> None:
    group_id = -100820086
    creator_id = 820086
    other_id = 820085
    await create_group(group_id, "Lottery management")
    async with session_scope() as session:
        session.add_all(
            [
                Lottery(
                    group_id=group_id,
                    creator_id=creator_id,
                    title="可取消",
                    prize="奖品",
                    status="open",
                    ends_at=utcnow() + timedelta(hours=1),
                ),
                Lottery(
                    group_id=group_id,
                    creator_id=other_id,
                    title="他人的抽奖",
                    prize="奖品",
                    status="open",
                    ends_at=utcnow() + timedelta(hours=1),
                ),
            ]
        )
        await session.flush()
        creator_lottery = await session.scalar(
            select(Lottery).where(Lottery.creator_id == creator_id)
        )
        other_lottery = await session.scalar(
            select(Lottery).where(Lottery.creator_id == other_id)
        )
        assert creator_lottery is not None and other_lottery is not None
        creator_lottery_id = int(creator_lottery.id)
    history = FakeMessage(user_id=creator_id, chat_id=creator_id, text="/cjhistory")
    await games.lottery_history(history)
    listed = [text for text, _markup in history.sent_answers]
    assert any("我创建的抽奖" in text for text in listed)
    assert any(markup is not None for _text, markup in history.sent_answers)
    view = FakeCallback(
        data=f"lotmanage:view:{creator_lottery_id}",
        user_id=creator_id,
        chat_id=creator_id,
    )
    await games.lottery_management_callback(view, FakeBot())
    assert view.message.answers
    selection = FakeCallback(
        data=f"lotmanage:select:{creator_lottery_id}",
        user_id=creator_id,
        chat_id=creator_id,
    )
    await games.lottery_management_callback(selection, FakeBot())
    assert selection.message.answers
    assert selection.message.answers[-1][1] is not None
    edit = FakeCallback(
        data=f"lotmanage:field:{creator_lottery_id}:title",
        user_id=creator_id,
        chat_id=creator_id,
    )
    await games.lottery_management_callback(edit, FakeBot())
    edit_bot = FakeBot()
    edit_bot.membership[(group_id, creator_id)] = "administrator"
    await games.lottery_private_config(
        FakeMessage(user_id=creator_id, chat_id=creator_id, text="Edited title"), edit_bot
    )
    await games.lottery_setup_callback(
        FakeCallback(data="lotsetup:create", user_id=creator_id, chat_id=creator_id),
        edit_bot,
    )
    async with session_scope() as session:
        edited = await session.get(Lottery, creator_lottery_id)
        total_created = int(
            await session.scalar(
                select(func.count(Lottery.id)).where(Lottery.creator_id == creator_id)
            )
            or 0
        )
        assert edited is not None and edited.title == "Edited title", (
            edited.title if edited is not None else "missing"
        )
        assert total_created == 1
    print("PASS edit flow updates existing lottery without duplicating it")

    cancel = FakeCallback(
        data=f"lotmanage:cancel:{creator_lottery_id}",
        user_id=creator_id,
        chat_id=creator_id,
    )
    await games.lottery_management_callback(cancel, FakeBot())
    assert any("已取消" in answer for answer in cancel.answers)
    async with session_scope() as session:
        cancelled = await session.get(Lottery, creator_lottery_id)
        assert cancelled is not None and cancelled.status == "cancelled"
    cancelled_history = FakeMessage(
        user_id=creator_id, chat_id=creator_id, text="/cjhistory"
    )
    await games.lottery_history(cancelled_history)
    assert len(cancelled_history.sent_answers) == 1
    assert "已取消" in cancelled_history.sent_answers[0][1].inline_keyboard[0][0].text
    print("PASS creator can list, view, edit and cancel with management buttons")

    denied = FakeMessage(
        user_id=other_id, chat_id=other_id, text=f"/cjcancel {creator_lottery_id}"
    )
    await games.lottery_cancel_created(denied)
    assert "找不到" in denied.replies[-1]
    print("PASS other creator cannot cancel lottery")


async def test_lottery_requires_admin() -> None:
    group_id = -100820098
    user_id = 820098
    await create_group(group_id, "Lottery non-admin")
    bot = FakeBot()
    start = FakeMessage(user_id=user_id, chat_id=group_id, text="/cj")
    await games.cmd_lottery(start, bot)
    assert not bot.sent
    assert start.replies
    assert user_id not in games._lottery_drafts
    print("PASS non-admin cannot start lottery setup")

    anonymous = SimpleNamespace(
        chat=FakeChat(group_id),
        sender_chat=FakeChat(group_id),
        from_user=None,
        text="/cj",
        message_id=702,
        replies=[],
    )

    async def reply(text: str, **_kwargs) -> None:
        anonymous.replies.append(text)

    anonymous.reply = reply
    await games.cmd_lottery(anonymous, bot)
    assert any("匿名管理员身份无法接收私聊" in text for text in anonymous.replies)
    print("PASS anonymous admin gets private-DM limitation instead of permission denial")


async def test_anonymous_admin_context() -> None:
    group_id = -100820096
    await create_group(group_id, "Anonymous admin")
    async with session_scope() as session:
        group = await session.get(Group, group_id)
        assert group is not None
        group.delete_command_messages = False
        await session.flush()
        chat = FakeChat(group_id, "Anonymous admin")

        anonymous_message = SimpleNamespace(
            chat=chat,
            from_user=None,
            sender_chat=chat,
            text="/ban",
            message_id=701,
        )
        async def fake_reply(_text: str) -> None:
            return None

        anonymous_message.reply = fake_reply
        result = await helpers.admin_context(
            FakeBot(), session, anonymous_message, "ban"
        )
        assert result is not None and result[1] == group_id
        anonymous_message.sender_chat = FakeChat(-100820095)
        denied = await helpers.admin_context(
            FakeBot(), session, anonymous_message, "ban"
        )
        assert denied is None
    print("PASS same-group anonymous admin is recognized by admin context")


async def create_group(group_id: int, title: str) -> None:
    async with session_scope() as session:
        session.add(
            Group(
                id=int(group_id),
                title=title,
                username="",
                chat_type="supergroup",
                is_active=True,
            )
        )


async def set_points(group_id: int, user_id: int, points: int) -> None:
    async with session_scope() as session:
        session.add(
            Member(
                group_id=int(group_id),
                user_id=int(user_id),
                first_name=f"User{user_id}",
                username=f"user{user_id}",
                points=int(points),
            )
        )


async def get_points(group_id: int, user_id: int) -> int:
    async with session_scope() as session:
        member = await session.scalar(
            select(Member).where(
                Member.group_id == int(group_id), Member.user_id == int(user_id)
            )
        )
        return int(member.points or 0) if member else 0


async def create_auction(
    group_id: int,
    *,
    item: str,
    start_price: int,
    step: int,
    current_price: int,
    current_bidder: int | None,
) -> int:
    async with session_scope() as session:
        auction = Auction(
            group_id=int(group_id),
            creator_id=900000,
            item=item,
            start_price=int(start_price),
            step=int(step),
            current_price=int(current_price),
            current_bidder=current_bidder,
            status="open",
            ends_at=utcnow() + timedelta(hours=1),
        )
        session.add(auction)
        await session.flush()
        return int(auction.id)


async def create_packet(
    group_id: int,
    *,
    sender_id: int,
    total: int,
    count: int,
    status: str = "open",
    expires_in: int = 3600,
) -> int:
    async with session_scope() as session:
        packet = RedPacket(
            group_id=int(group_id),
            sender_id=int(sender_id),
            total=int(total),
            count=int(count),
            remaining_amount=int(total),
            remaining_count=int(count),
            status=status,
            expires_at=utcnow() + timedelta(seconds=expires_in),
        )
        session.add(packet)
        await session.flush()
        return int(packet.id)


async def run_tests() -> None:
    await test_lottery_private_setup()
    await test_lottery_wizard_back_and_expiry()
    await test_lottery_participation_rules()
    await test_lottery_creator_management()
    await test_lottery_requires_admin()
    await test_anonymous_admin_context()
    # ------------------------------------------------------------------
    # 1. 竞拍正常路径
    # ------------------------------------------------------------------
    group1 = -100820001
    await create_group(group1, "Bid happy path")
    await set_points(group1, 820101, 500)
    auction1 = await create_auction(
        group1,
        item="头像框",
        start_price=100,
        step=10,
        current_price=100,
        current_bidder=None,
    )
    bid_msg = FakeMessage(user_id=820101, chat_id=group1, text="/bid 150")
    await games.cmd_bid(bid_msg, SimpleNamespace())

    async with session_scope() as session:
        auction = await session.get(Auction, auction1)
        member = await session.scalar(
            select(Member).where(
                Member.group_id == group1, Member.user_id == 820101
            )
        )
        bid_rows = int(
            await session.scalar(
                select(func.count(AuctionBid.id)).where(
                    AuctionBid.auction_id == auction1
                )
            )
            or 0
        )
        check(
            "bid happy path deducts new bidder",
            member is not None and int(member.points or 0) == 350,
            str(member.points if member else None),
        )
        check(
            "bid happy path records price and bidder",
            auction is not None
            and int(auction.current_price or 0) == 150
            and auction.current_bidder == 820101,
            f"{auction.current_price}/{auction.current_bidder}" if auction else "missing",
        )
        check("bid happy path writes one bid row", bid_rows == 1, str(bid_rows))
    check(
        "bid happy path replies success",
        len(bid_msg.replies) == 1 and "出价成功" in bid_msg.replies[0],
        str(bid_msg.replies),
    )

    # ------------------------------------------------------------------
    # 2. 并发出价不重复退款
    # ------------------------------------------------------------------
    group2 = -100820002
    await create_group(group2, "Concurrent bids")
    current_bidder = 820201
    bidder_b = 820202
    bidder_c = 820203
    await set_points(group2, current_bidder, 0)
    await set_points(group2, bidder_b, 500)
    await set_points(group2, bidder_c, 500)
    auction2 = await create_auction(
        group2,
        item="并发奖品",
        start_price=100,
        step=10,
        current_price=100,
        current_bidder=current_bidder,
    )
    msg_b = FakeMessage(user_id=bidder_b, chat_id=group2, text="/bid 110")
    msg_c = FakeMessage(user_id=bidder_c, chat_id=group2, text="/bid 110")
    await asyncio.gather(
        games.cmd_bid(msg_b, SimpleNamespace()),
        games.cmd_bid(msg_c, SimpleNamespace()),
    )

    async with session_scope() as session:
        auction = await session.get(Auction, auction2)
        bid_rows = int(
            await session.scalar(
                select(func.count(AuctionBid.id)).where(
                    AuctionBid.auction_id == auction2
                )
            )
            or 0
        )
        refund_rows = int(
            await session.scalar(
                select(func.count(PointsTransaction.id)).where(
                    PointsTransaction.group_id == group2,
                    PointsTransaction.user_id == current_bidder,
                    PointsTransaction.reason.like("竞拍被超过退款%"),
                    PointsTransaction.amount == 100,
                )
            )
            or 0
        )
        members = {
            int(m.user_id): int(m.points or 0)
            for m in (
                await session.scalars(
                    select(Member).where(Member.group_id == group2)
                )
            ).all()
        }
        total = sum(members.values())
        check(
            "concurrent bids keep only one winner price",
            auction is not None and int(auction.current_price or 0) == 110,
            str(auction.current_price if auction else None),
        )
        check("concurrent bids write exactly one bid row", bid_rows == 1, str(bid_rows))
        check(
            "concurrent bids refund previous bidder exactly once",
            refund_rows == 1,
            str(refund_rows),
        )
        check(
            "concurrent bids deduct exactly one new bidder",
            sorted([members.get(bidder_b, 0), members.get(bidder_c, 0)]) == [390, 500],
            str({bidder_b: members.get(bidder_b), bidder_c: members.get(bidder_c)}),
        )
        check(
            "concurrent bids conserve points (990)",
            total == 990,
            str(members),
        )
    replies = msg_b.replies + msg_c.replies
    check(
        "concurrent bids report one success and one rejection",
        sum(1 for text in replies if "出价成功" in text) == 1,
        str(replies),
    )
    check(
        "concurrent bid loser gets a race or minimum message",
        sum(
            1
            for text in replies
            if "抢先" in text or "出价太低" in text
        )
        == 1,
        str(replies),
    )

    # ------------------------------------------------------------------
    # 3. _claim_auction_slot 互斥
    # ------------------------------------------------------------------
    group3 = -100820003
    await create_group(group3, "Auction slot CAS")
    auction3 = await create_auction(
        group3,
        item="CAS 竞拍",
        start_price=100,
        step=10,
        current_price=100,
        current_bidder=None,
    )
    now = utcnow()
    async with session_scope() as session:
        won = await games._claim_auction_slot(
            session, auction3, 100, None, 820301, 120, now
        )
    async with session_scope() as session:
        lost = await games._claim_auction_slot(
            session, auction3, 100, None, 820302, 120, now
        )
    async with session_scope() as session:
        auction = await session.get(Auction, auction3)
        check("auction slot A wins", won, str(won))
        check("auction slot B loses after A commits", not lost, str(lost))
        check(
            "auction slot mutates price and bidder once",
            auction is not None
            and int(auction.current_price or 0) == 120
            and auction.current_bidder == 820301,
            f"{auction.current_price}/{auction.current_bidder}" if auction else "missing",
        )

    # ------------------------------------------------------------------
    # 4. 红包并发领取不透支
    # ------------------------------------------------------------------
    group4 = -100820004
    await create_group(group4, "Concurrent packet claims")
    claimer1 = 820401
    claimer2 = 820402
    await set_points(group4, claimer1, 0)
    await set_points(group4, claimer2, 0)
    packet4 = await create_packet(
        group4, sender_id=820400, total=30, count=3, expires_in=3600
    )
    cb1 = FakeCallback(data=f"rp:{packet4}", user_id=claimer1, chat_id=group4)
    cb2 = FakeCallback(data=f"rp:{packet4}", user_id=claimer2, chat_id=group4)
    await asyncio.gather(
        games.on_claim_packet(cb1),
        games.on_claim_packet(cb2),
    )

    async with session_scope() as session:
        packet = await session.get(RedPacket, packet4)
        claim_rows = int(
            await session.scalar(
                select(func.count(RedPacketClaim.id)).where(
                    RedPacketClaim.red_packet_id == packet4
                )
            )
            or 0
        )
        per_user = {
            int(user_id): int(count)
            for user_id, count in (
                await session.execute(
                    select(
                        RedPacketClaim.user_id,
                        func.count(RedPacketClaim.id),
                    )
                    .where(RedPacketClaim.red_packet_id == packet4)
                    .group_by(RedPacketClaim.user_id)
                )
            ).all()
        }
        points1 = int(
            await session.scalar(
                select(Member.points).where(
                    Member.group_id == group4, Member.user_id == claimer1
                )
            )
            or 0
        )
        points2 = int(
            await session.scalar(
                select(Member.points).where(
                    Member.group_id == group4, Member.user_id == claimer2
                )
            )
            or 0
        )
        remaining_amount = int(packet.remaining_amount or 0) if packet else -1
        remaining_count = int(packet.remaining_count or 0) if packet else -1
        check(
            "concurrent packet claims never overdraw",
            remaining_amount >= 0
            and points1 + points2 + remaining_amount == 30,
            f"points={points1 + points2} remaining={remaining_amount}",
        )
        check(
            "concurrent packet count matches claim rows",
            remaining_count == 3 - claim_rows,
            f"count={remaining_count} claims={claim_rows}",
        )
        check(
            "concurrent packet each user claims at most once",
            all(count <= 1 for count in per_user.values()),
            str(per_user),
        )
        check(
            "concurrent packet writes matching claim rows",
            claim_rows == sum(per_user.values()) and claim_rows >= 1,
            f"{claim_rows}/{per_user}",
        )

    # ------------------------------------------------------------------
    # 5. 过期后领取无效
    # ------------------------------------------------------------------
    group5 = -100820005
    await create_group(group5, "Expired packet")
    await set_points(group5, 820501, 0)
    packet5 = await create_packet(
        group5, sender_id=820500, total=30, count=3, status="expired", expires_in=-60
    )
    cb5 = FakeCallback(data=f"rp:{packet5}", user_id=820501, chat_id=group5)
    await games.on_claim_packet(cb5)

    async with session_scope() as session:
        claims = int(
            await session.scalar(
                select(func.count(RedPacketClaim.id)).where(
                    RedPacketClaim.red_packet_id == packet5
                )
            )
            or 0
        )
    check("expired packet gives no points", await get_points(group5, 820501) == 0)
    check("expired packet writes no claim row", claims == 0, str(claims))
    check(
        "expired packet answers expired or finished",
        len(cb5.answers) == 1
        and ("过期" in cb5.answers[0] or "领完" in cb5.answers[0]),
        str(cb5.answers),
    )

    # ------------------------------------------------------------------
    # 6. _claim_packet_slot 互斥 + 陈旧快照
    # ------------------------------------------------------------------
    group6 = -100820006
    await create_group(group6, "Packet slot CAS")
    packet6 = await create_packet(
        group6, sender_id=820600, total=30, count=3, expires_in=3600
    )
    now = utcnow()
    async with session_scope() as session:
        won = await games._claim_packet_slot(session, packet6, 3, 30, 10, now)
    async with session_scope() as session:
        lost = await games._claim_packet_slot(session, packet6, 3, 30, 10, now)
    async with session_scope() as session:
        packet = await session.get(RedPacket, packet6)
        check("packet slot A wins", won, str(won))
        check("packet slot B loses on stale snapshot", not lost, str(lost))
        check(
            "packet slot mutates remainder once",
            packet is not None
            and int(packet.remaining_amount or 0) == 20
            and int(packet.remaining_count or 0) == 2,
            f"{packet.remaining_amount}/{packet.remaining_count}" if packet else "missing",
        )

    # ------------------------------------------------------------------
    # 7. 重复领取
    # ------------------------------------------------------------------
    group7 = -100820007
    await create_group(group7, "Duplicate claim")
    await set_points(group7, 820701, 0)
    packet7 = await create_packet(
        group7, sender_id=820700, total=30, count=3, expires_in=3600
    )
    first_cb = FakeCallback(data=f"rp:{packet7}", user_id=820701, chat_id=group7)
    await games.on_claim_packet(first_cb)
    balance_after_first = await get_points(group7, 820701)
    second_cb = FakeCallback(data=f"rp:{packet7}", user_id=820701, chat_id=group7)
    await games.on_claim_packet(second_cb)

    async with session_scope() as session:
        claims = int(
            await session.scalar(
                select(func.count(RedPacketClaim.id)).where(
                    RedPacketClaim.red_packet_id == packet7,
                    RedPacketClaim.user_id == 820701,
                )
            )
            or 0
        )
    check(
        "duplicate claim keeps points unchanged",
        await get_points(group7, 820701) == balance_after_first,
        str(await get_points(group7, 820701)),
    )
    check("duplicate claim keeps one claim row", claims == 1, str(claims))
    check(
        "duplicate claim answers already claimed",
        len(second_cb.answers) == 1 and "已经领过" in second_cb.answers[0],
        str(second_cb.answers),
    )

    # ------------------------------------------------------------------
    # 8. 过期红包被点击后不能吞掉发送者退款
    # ------------------------------------------------------------------
    group8 = -100820008
    await create_group(group8, "Expired click keeps refund")
    sender8 = 820800
    claimer8 = 820801
    await set_points(group8, sender8, 0)
    await set_points(group8, claimer8, 0)
    packet8 = await create_packet(
        group8, sender_id=sender8, total=42, count=2, expires_in=-60
    )
    cb8 = FakeCallback(data=f"rp:{packet8}", user_id=claimer8, chat_id=group8)
    await games.on_claim_packet(cb8)

    async with session_scope() as session:
        packet = await session.get(RedPacket, packet8)
        claims = int(
            await session.scalar(
                select(func.count(RedPacketClaim.id)).where(
                    RedPacketClaim.red_packet_id == packet8
                )
            )
            or 0
        )
        check("expired click gives no points", await get_points(group8, claimer8) == 0)
        check("expired click writes no claim row", claims == 0, str(claims))
        check(
            "expired click keeps packet open for scheduler refund",
            packet is not None and packet.status == "open",
            packet.status if packet else "missing",
        )

    await scheduler.refund_red_packets(FakeBot())

    async with session_scope() as session:
        packet = await session.get(RedPacket, packet8)
        refund_rows = int(
            await session.scalar(
                select(func.count(PointsTransaction.id)).where(
                    PointsTransaction.group_id == group8,
                    PointsTransaction.user_id == sender8,
                    PointsTransaction.reason.like("红包过期退回%"),
                )
            )
            or 0
        )
        check(
            "scheduler refunds expired packet remainder",
            await get_points(group8, sender8) == 42,
            str(await get_points(group8, sender8)),
        )
        check(
            "scheduler marks clicked expired packet expired",
            packet is not None and packet.status == "expired",
            packet.status if packet else "missing",
        )
        check(
            "expired packet refund writes one transaction",
            refund_rows == 1,
            str(refund_rows),
        )

    # ------------------------------------------------------------------
    # 9. adjust_points 并发累加不丢更新
    # ------------------------------------------------------------------
    group9 = -100820009
    user9 = 820901
    await create_group(group9, "Atomic increments")
    await set_points(group9, user9, 0)

    async def add_one():
        async def run():
            async with session_scope() as session:
                return await repo.adjust_points(
                    session, group9, user9, 1, "并发+1"
                )

        return await with_retry(run)

    results9 = await asyncio.gather(*[add_one() for _ in range(20)])
    async with session_scope() as session:
        points = int(
            await session.scalar(
                select(Member.points).where(
                    Member.group_id == group9, Member.user_id == user9
                )
            )
            or 0
        )
        tx_count = int(
            await session.scalar(
                select(func.count(PointsTransaction.id)).where(
                    PointsTransaction.group_id == group9,
                    PointsTransaction.user_id == user9,
                )
            )
            or 0
        )
        balances = {
            int(value)
            for value in (
                await session.scalars(
                    select(PointsTransaction.balance_after).where(
                        PointsTransaction.group_id == group9,
                        PointsTransaction.user_id == user9,
                    )
                )
            ).all()
        }
        check(
            "concurrent increments keep all points",
            points == 20 and all(result is not None for result in results9),
            f"{points}/{len([r for r in results9 if r is not None])}",
        )
        check("concurrent increments write 20 transactions", tx_count == 20, str(tx_count))
        check(
            "concurrent increments record balances 1..20",
            balances == set(range(1, 21)),
            str(sorted(balances)),
        )

    # ------------------------------------------------------------------
    # 10. adjust_points 并发扣减不透支
    # ------------------------------------------------------------------
    group10 = -100820010
    user10 = 820911
    await create_group(group10, "Atomic decrements")
    await set_points(group10, user10, 50)

    async def deduct_ten():
        async def run():
            async with session_scope() as session:
                return await repo.adjust_points(
                    session, group10, user10, -10, "并发-10"
                )

        return await with_retry(run)

    results10 = await asyncio.gather(*[deduct_ten() for _ in range(10)])
    succeeded = [result for result in results10 if result is not None]
    rejected = [result for result in results10 if result is None]
    final10 = await get_points(group10, user10)
    check("concurrent deductions never go negative", final10 >= 0, str(final10))
    check(
        "concurrent deductions conserve points",
        len(succeeded) * 10 + final10 == 50,
        f"{len(succeeded)}/{final10}",
    )
    check(
        "concurrent deductions reject exactly five",
        len(rejected) == 5,
        str(len(rejected)),
    )

    # ------------------------------------------------------------------
    # 11. 跨路径并发加减
    # ------------------------------------------------------------------
    group11 = -100820011
    user11 = 820921
    await create_group(group11, "Atomic mixed deltas")
    await set_points(group11, user11, 100)

    async def change_points(delta: int, reason: str):
        async def run():
            async with session_scope() as session:
                return await repo.adjust_points(
                    session, group11, user11, delta, reason
                )

        return await with_retry(run)

    await asyncio.gather(
        change_points(30, "跨路径+30"),
        change_points(-20, "跨路径-20"),
    )
    check(
        "concurrent mixed deltas keep 110",
        await get_points(group11, user11) == 110,
        str(await get_points(group11, user11)),
    )


async def main() -> None:
    await init_db()
    try:
        await run_tests()
    finally:
        await dispose_db()
    if failures:
        print("FAILURES:", failures)
        raise SystemExit(1)
    print("FAILURES: none")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
