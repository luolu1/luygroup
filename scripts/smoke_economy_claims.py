"""签到、商城与抽奖参与的并发资金/库存安全回归测试。

使用临时配置与临时 SQLite 数据库，不修改真实 config.json / data/，也不访问
Telegram。重点验证「先原子占位，后扣费」语义与失败分支必须回滚。

    .venv/bin/python scripts/smoke_economy_claims.py
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TMP = Path(tempfile.mkdtemp(prefix="groupguard-economy-claims-"))
DB = TMP / "economy-claims.db"
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

from app.bot.handlers import games  # noqa: E402
from app.db import dispose_db, init_db, session_scope  # noqa: E402
from app.models import (  # noqa: E402
    DailyCheckin,
    Group,
    Lottery,
    LotteryEntry,
    Member,
    PointsTransaction,
    ShopItem,
    ShopOrder,
    utcnow,
)

failures: list[str] = []


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


class FakeUser:
    def __init__(self, user_id: int, first_name: str = "Tester") -> None:
        self.id = user_id
        self.first_name = first_name
        self.username = f"user{user_id}"
        self.is_bot = False


class FakeChat:
    def __init__(self, chat_id: int, title: str = "Economy claims") -> None:
        self.id = chat_id
        self.title = title
        self.username = ""
        self.type = "supergroup"


class FakeMessage:
    def __init__(self, *, user_id: int, chat_id: int, text: str = "") -> None:
        self.from_user = FakeUser(user_id)
        self.chat = FakeChat(chat_id)
        self.text = text
        self.reply_to_message = None
        self.replies: list[str] = []

    async def reply(self, text: str, **kwargs) -> None:
        self.replies.append(text)


class FakeCallbackMessage:
    def __init__(self, chat_id: int) -> None:
        self.chat = FakeChat(chat_id)

    async def edit_reply_markup(self, reply_markup=None) -> None:
        return None


class FakeCallback:
    def __init__(self, *, data: str, user_id: int, chat_id: int) -> None:
        self.data = data
        self.from_user = FakeUser(user_id)
        self.message = FakeCallbackMessage(chat_id)
        self.answers: list[str] = []

    async def answer(self, text: str, show_alert: bool = False) -> None:
        self.answers.append(text)


async def create_group(
    group_id: int,
    title: str,
    *,
    daily_bonus: int = 5,
    points_enabled: bool = True,
) -> None:
    async with session_scope() as session:
        session.add(
            Group(
                id=int(group_id),
                title=title,
                username="",
                chat_type="supergroup",
                is_active=True,
                points_enabled=points_enabled,
                daily_bonus=int(daily_bonus),
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


async def create_item(
    group_id: int,
    *,
    name: str,
    cost: int,
    stock: int,
    enabled: bool = True,
) -> int:
    async with session_scope() as session:
        item = ShopItem(
            group_id=int(group_id),
            name=name,
            cost=int(cost),
            stock=int(stock),
            enabled=enabled,
        )
        session.add(item)
        await session.flush()
        return int(item.id)


async def create_lottery(
    group_id: int,
    *,
    title: str,
    entry_fee: int,
    ends_in: int = 3600,
) -> int:
    async with session_scope() as session:
        lottery = Lottery(
            group_id=int(group_id),
            creator_id=900000,
            title=title,
            prize="测试奖品",
            entry_fee=int(entry_fee),
            winners_count=1,
            status="open",
            ends_at=utcnow() + timedelta(seconds=ends_in),
        )
        session.add(lottery)
        await session.flush()
        return int(lottery.id)


async def get_points(group_id: int, user_id: int) -> int:
    async with session_scope() as session:
        return int(
            await session.scalar(
                select(Member.points).where(
                    Member.group_id == int(group_id),
                    Member.user_id == int(user_id),
                )
            )
            or 0
        )


async def get_sum_points(group_id: int, user_ids: list[int]) -> int:
    async with session_scope() as session:
        return int(
            await session.scalar(
                select(func.coalesce(func.sum(Member.points), 0)).where(
                    Member.group_id == int(group_id),
                    Member.user_id.in_([int(uid) for uid in user_ids]),
                )
            )
            or 0
        )


async def count_rows(model, *where) -> int:
    async with session_scope() as session:
        return int(
            await session.scalar(select(func.count(model.id)).where(*where)) or 0
        )


async def run_tests() -> None:
    # ------------------------------------------------------------------
    # 1. 同一用户并发签到只发一次奖励
    # ------------------------------------------------------------------
    group1 = -100830001
    user1 = 830101
    await create_group(group1, "Concurrent checkin", daily_bonus=7)
    msgs = [FakeMessage(user_id=user1, chat_id=group1, text="/checkin") for _ in range(10)]
    await asyncio.gather(
        *[with_retry(lambda msg=msg: games.cmd_checkin(msg)) for msg in msgs]
    )

    tx1 = await count_rows(
        PointsTransaction,
        PointsTransaction.group_id == group1,
        PointsTransaction.user_id == user1,
        PointsTransaction.reason == "每日签到",
    )
    day1 = await count_rows(
        DailyCheckin,
        DailyCheckin.group_id == group1,
        DailyCheckin.user_id == user1,
    )
    balance1 = await get_points(group1, user1)
    replies1 = [text for msg in msgs for text in msg.replies]
    check("checkin concurrent writes exactly one transaction", tx1 == 1, str(tx1))
    check("checkin concurrent writes exactly one daily row", day1 == 1, str(day1))
    check("checkin concurrent credits bonus once", balance1 == 7, str(balance1))
    check(
        "checkin concurrent replies one success and nine duplicates",
        sum(1 for text in replies1 if "签到成功" in text) == 1
        and sum(1 for text in replies1 if "已经签到" in text) == 9,
        str(replies1),
    )

    # ------------------------------------------------------------------
    # 2. 顺序再次签到不重复发奖励
    # ------------------------------------------------------------------
    again = FakeMessage(user_id=user1, chat_id=group1, text="/checkin")
    await games.cmd_checkin(again)
    check(
        "repeated checkin keeps balance",
        await get_points(group1, user1) == 7,
        str(await get_points(group1, user1)),
    )
    check(
        "repeated checkin replies already signed",
        len(again.replies) == 1 and "已经签到" in again.replies[0],
        str(again.replies),
    )

    # ------------------------------------------------------------------
    # 3. 有限库存并发抢购不超卖
    # ------------------------------------------------------------------
    group3 = -100830003
    buyers = [830301, 830302, 830303, 830304, 830305]
    await create_group(group3, "Limited stock")
    for buyer in buyers:
        await set_points(group3, buyer, 500)
    item3 = await create_item(group3, name="限量商品", cost=50, stock=1)
    buy_msgs = [
        FakeMessage(user_id=buyer, chat_id=group3, text=f"/buy {item3}")
        for buyer in buyers
    ]
    await asyncio.gather(
        *[with_retry(lambda msg=msg: games.cmd_buy(msg)) for msg in buy_msgs]
    )

    async with session_scope() as session:
        item = await session.get(ShopItem, item3)
        stock3 = int(item.stock or 0) if item else -1
    orders3 = await count_rows(ShopOrder, ShopOrder.item_id == item3)
    points3 = {buyer: await get_points(group3, buyer) for buyer in buyers}
    charged = [uid for uid, value in points3.items() if value == 450]
    total3 = await get_sum_points(group3, buyers)
    check("limited stock writes exactly one order", orders3 == 1, str(orders3))
    check("limited stock ends at zero", stock3 == 0, str(stock3))
    check("limited stock charges exactly one buyer", len(charged) == 1, str(points3))
    check(
        "limited stock leaves other buyers untouched",
        sorted(points3.values()) == [450, 500, 500, 500, 500],
        str(points3),
    )
    check("limited stock conserves points (2450)", total3 == 2450, str(total3))

    # ------------------------------------------------------------------
    # 4. 无限库存并发购买各自扣费
    # ------------------------------------------------------------------
    group4 = -100830004
    buyers4 = [830401, 830402, 830403]
    await create_group(group4, "Unlimited stock")
    for buyer in buyers4:
        await set_points(group4, buyer, 500)
    item4 = await create_item(group4, name="无限商品", cost=50, stock=-1)
    await asyncio.gather(
        *[
            with_retry(
                lambda buyer=buyer: games.cmd_buy(
                    FakeMessage(user_id=buyer, chat_id=group4, text=f"/buy {item4}")
                )
            )
            for buyer in buyers4
        ]
    )

    async with session_scope() as session:
        item = await session.get(ShopItem, item4)
        stock4 = int(item.stock if item and item.stock is not None else 0)
    orders4 = await count_rows(ShopOrder, ShopOrder.item_id == item4)
    points4 = {buyer: await get_points(group4, buyer) for buyer in buyers4}
    check("unlimited stock writes three orders", orders4 == 3, str(orders4))
    check("unlimited stock stays at -1", stock4 == -1, str(stock4))
    check(
        "unlimited stock charges each buyer once",
        all(value == 450 for value in points4.values()),
        str(points4),
    )

    # ------------------------------------------------------------------
    # 5. 积分不足时库存回滚
    # ------------------------------------------------------------------
    group5 = -100830005
    buyer5 = 830501
    await create_group(group5, "Insufficient points")
    await set_points(group5, buyer5, 10)
    item5 = await create_item(group5, name="高价商品", cost=50, stock=2)
    msg5 = FakeMessage(user_id=buyer5, chat_id=group5, text=f"/buy {item5}")
    await games.cmd_buy(msg5)

    async with session_scope() as session:
        item = await session.get(ShopItem, item5)
        stock5 = int(item.stock or 0) if item else -1
    orders5 = await count_rows(ShopOrder, ShopOrder.item_id == item5)
    check("insufficient points restores stock", stock5 == 2, str(stock5))
    check("insufficient points writes no order", orders5 == 0, str(orders5))
    check(
        "insufficient points keeps balance",
        await get_points(group5, buyer5) == 10,
        str(await get_points(group5, buyer5)),
    )
    check(
        "insufficient points replies failure",
        len(msg5.replies) == 1 and "积分不足" in msg5.replies[0],
        str(msg5.replies),
    )

    # ------------------------------------------------------------------
    # 6. 同一用户并发送礼参与只收一次费
    # ------------------------------------------------------------------
    group6 = -100830006
    winner6 = 830601
    await create_group(group6, "Concurrent lottery join")
    await set_points(group6, winner6, 100)
    lottery6 = await create_lottery(group6, title="并发抽奖", entry_fee=20)
    cbs6 = [
        FakeCallback(data=f"lot:{lottery6}", user_id=winner6, chat_id=group6)
        for _ in range(5)
    ]
    await asyncio.gather(
        *[with_retry(lambda cb=cb: games.on_lottery_join(cb)) for cb in cbs6]
    )

    entries6 = await count_rows(
        LotteryEntry,
        LotteryEntry.lottery_id == lottery6,
        LotteryEntry.user_id == winner6,
    )
    fees6 = await count_rows(
        PointsTransaction,
        PointsTransaction.group_id == group6,
        PointsTransaction.user_id == winner6,
        PointsTransaction.reason.like("抽奖参与费%"),
    )
    answers6 = [text for cb in cbs6 for text in cb.answers]
    check("concurrent lottery join writes one entry", entries6 == 1, str(entries6))
    check("concurrent lottery join charges once", fees6 == 1, str(fees6))
    check(
        "concurrent lottery join keeps balance",
        await get_points(group6, winner6) == 80,
        str(await get_points(group6, winner6)),
    )
    check(
        "concurrent lottery join answers each callback once",
        all(len(cb.answers) == 1 for cb in cbs6)
        and sum(1 for text in answers6 if "参与成功" in text) == 1,
        str(answers6),
    )

    # ------------------------------------------------------------------
    # 7. 参与费不足不写入口、不扣费
    # ------------------------------------------------------------------
    group7 = -100830007
    poor7 = 830701
    await create_group(group7, "Lottery insufficient points")
    await set_points(group7, poor7, 5)
    lottery7 = await create_lottery(group7, title="高价抽奖", entry_fee=20)
    cb7 = FakeCallback(data=f"lot:{lottery7}", user_id=poor7, chat_id=group7)
    await games.on_lottery_join(cb7)

    entries7 = await count_rows(
        LotteryEntry,
        LotteryEntry.lottery_id == lottery7,
        LotteryEntry.user_id == poor7,
    )
    fees7 = await count_rows(
        PointsTransaction,
        PointsTransaction.group_id == group7,
        PointsTransaction.user_id == poor7,
    )
    check("lottery insufficient points writes no entry", entries7 == 0, str(entries7))
    check("lottery insufficient points writes no transaction", fees7 == 0, str(fees7))
    check(
        "lottery insufficient points keeps balance",
        await get_points(group7, poor7) == 5,
        str(await get_points(group7, poor7)),
    )
    check(
        "lottery insufficient points answers failure",
        len(cb7.answers) == 1 and "积分不足" in cb7.answers[0],
        str(cb7.answers),
    )

    # ------------------------------------------------------------------
    # 8. 不同用户并发参与各自成功
    # ------------------------------------------------------------------
    group8 = -100830008
    players8 = [830801, 830802, 830803]
    await create_group(group8, "Lottery multiple users")
    for player in players8:
        await set_points(group8, player, 100)
    lottery8 = await create_lottery(group8, title="多人抽奖", entry_fee=20)
    cbs8 = [
        FakeCallback(data=f"lot:{lottery8}", user_id=player, chat_id=group8)
        for player in players8
    ]
    await asyncio.gather(
        *[with_retry(lambda cb=cb: games.on_lottery_join(cb)) for cb in cbs8]
    )

    entries8 = await count_rows(LotteryEntry, LotteryEntry.lottery_id == lottery8)
    fees8 = await count_rows(
        PointsTransaction,
        PointsTransaction.group_id == group8,
        PointsTransaction.reason.like("抽奖参与费%"),
    )
    points8 = {player: await get_points(group8, player) for player in players8}
    check("multi-user lottery writes one entry each", entries8 == 3, str(entries8))
    check("multi-user lottery charges one fee each", fees8 == 3, str(fees8))
    check(
        "multi-user lottery deducts each player once",
        all(value == 80 for value in points8.values()),
        str(points8),
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
