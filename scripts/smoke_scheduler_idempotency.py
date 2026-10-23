"""调度器结算幂等性回归测试（抽奖 / 竞拍 / 红包）。

使用临时配置与临时 SQLite 数据库，不修改真实 config.json / data/，也不访问
Telegram。重点验证「扫描后 CAS 抢占」语义：多个 worker 同时看到 open 记录时，
只有一个能完成结算，其余必须跳过公告、日志与退款。

    .venv/bin/python scripts/smoke_scheduler_idempotency.py
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
TMP = Path(tempfile.mkdtemp(prefix="groupguard-scheduler-idempotency-"))
DB = TMP / "scheduler-idempotency.db"
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

from app.db import dispose_db, init_db, session_scope  # noqa: E402
from app.models import (  # noqa: E402
    Auction,
    Group,
    Lottery,
    LotteryEntry,
    Member,
    PointsTransaction,
    RedPacket,
    utcnow,
)
from app.services import scheduler  # noqa: E402

failures: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    if cond:
        print(f"PASS {name}")
    else:
        failures.append(name)
        print(f"FAIL {name} {extra}")


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id: int, text: str, **kwargs) -> None:
        self.sent.append((int(chat_id), text))


def messages_for(bot: FakeBot, chat_id: int, marker: str) -> list[str]:
    return [
        text
        for target, text in bot.sent
        if target == int(chat_id) and marker in text
    ]


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


async def run_tests() -> None:
    # ------------------------------------------------------------------
    # 1. 抽奖只结算一次
    # ------------------------------------------------------------------
    lottery_group = -100810001
    await create_group(lottery_group, "Lottery idempotency")
    past = utcnow() - timedelta(minutes=1)
    async with session_scope() as session:
        lottery = Lottery(
            group_id=lottery_group,
            creator_id=810001,
            title="周末抽奖",
            prize="一个月会员",
            winners_count=1,
            status="open",
            ends_at=past,
        )
        session.add(lottery)
        await session.flush()
        lottery_id = int(lottery.id)
        session.add_all(
            [
                LotteryEntry(lottery_id=lottery_id, user_id=810101),
                LotteryEntry(lottery_id=lottery_id, user_id=810102),
            ]
        )
        empty = Lottery(
            group_id=lottery_group,
            creator_id=810001,
            title="无人抽奖",
            prize="谢谢参与",
            winners_count=1,
            status="open",
            ends_at=past,
        )
        session.add(empty)
        await session.flush()
        empty_id = int(empty.id)

    bot = FakeBot()
    await scheduler.draw_lotteries(bot)
    await scheduler.draw_lotteries(bot)

    async with session_scope() as session:
        settled = await session.get(Lottery, lottery_id)
        empty_settled = await session.get(Lottery, empty_id)
        check(
            "lottery settles exactly once",
            len(messages_for(bot, lottery_group, "抽奖开奖")) == 1,
            str(messages_for(bot, lottery_group, "抽奖开奖")),
        )
        check(
            "empty lottery settles exactly once",
            len(messages_for(bot, lottery_group, "本轮流奖")) == 1,
            str(messages_for(bot, lottery_group, "本轮流奖")),
        )
        check(
            "lottery status drawn after first claim",
            settled is not None and settled.status == "drawn",
            settled.status if settled else "missing",
        )
        check(
            "empty lottery status drawn after first claim",
            empty_settled is not None and empty_settled.status == "drawn",
            empty_settled.status if empty_settled else "missing",
        )

    # ------------------------------------------------------------------
    # 2. 竞拍只结算一次
    # ------------------------------------------------------------------
    auction_group = -100810002
    await create_group(auction_group, "Auction idempotency")
    async with session_scope() as session:
        won = Auction(
            group_id=auction_group,
            creator_id=810002,
            item="稀有头像框",
            start_price=100,
            step=10,
            current_price=120,
            current_bidder=810201,
            status="open",
            ends_at=past,
        )
        unsold = Auction(
            group_id=auction_group,
            creator_id=810002,
            item="无人出价物品",
            start_price=50,
            step=5,
            current_price=50,
            current_bidder=None,
            status="open",
            ends_at=past,
        )
        session.add_all([won, unsold])
        await session.flush()
        won_id = int(won.id)
        unsold_id = int(unsold.id)

    auction_bot = FakeBot()
    await scheduler.finish_auctions(auction_bot)
    await scheduler.finish_auctions(auction_bot)

    async with session_scope() as session:
        won_settled = await session.get(Auction, won_id)
        unsold_settled = await session.get(Auction, unsold_id)
        check(
            "auction with bidder settles exactly once",
            len(messages_for(auction_bot, auction_group, "竞拍结束")) == 1,
            str(messages_for(auction_bot, auction_group, "竞拍结束")),
        )
        check(
            "auction without bidder cancels exactly once",
            len(messages_for(auction_bot, auction_group, "已流拍")) == 1,
            str(messages_for(auction_bot, auction_group, "已流拍")),
        )
        check(
            "auction with bidder status finished",
            won_settled is not None and won_settled.status == "finished",
            won_settled.status if won_settled else "missing",
        )
        check(
            "auction without bidder status cancelled",
            unsold_settled is not None and unsold_settled.status == "cancelled",
            unsold_settled.status if unsold_settled else "missing",
        )

    # ------------------------------------------------------------------
    # 3. 红包只退款一次
    # ------------------------------------------------------------------
    packet_group = -100810003
    sender_id = 810301
    await create_group(packet_group, "Red packet idempotency")
    async with session_scope() as session:
        session.add(
            Member(
                group_id=packet_group,
                user_id=sender_id,
                first_name="Sender",
                username="sender",
                points=0,
            )
        )
        packet = RedPacket(
            group_id=packet_group,
            sender_id=sender_id,
            total=30,
            count=3,
            remaining_amount=30,
            remaining_count=3,
            status="open",
            expires_at=past,
        )
        session.add(packet)
        await session.flush()
        packet_id = int(packet.id)

    packet_bot = FakeBot()
    await scheduler.refund_red_packets(packet_bot)
    await scheduler.refund_red_packets(packet_bot)

    async with session_scope() as session:
        member = await session.scalar(
            select(Member).where(
                Member.group_id == packet_group, Member.user_id == sender_id
            )
        )
        refunds = int(
            await session.scalar(
                select(func.count(PointsTransaction.id)).where(
                    PointsTransaction.group_id == packet_group,
                    PointsTransaction.user_id == sender_id,
                    PointsTransaction.reason.like("红包过期退回%"),
                )
            )
            or 0
        )
        settled_packet = await session.get(RedPacket, packet_id)
        check(
            "red packet refunds exactly once",
            member is not None and member.points == 30,
            str(member.points if member else None),
        )
        check(
            "red packet writes one points transaction",
            refunds == 1,
            str(refunds),
        )
        check(
            "red packet status expired",
            settled_packet is not None and settled_packet.status == "expired",
            settled_packet.status if settled_packet else "missing",
        )
        check(
            "red packet announcement once",
            len(messages_for(packet_bot, packet_group, "已过期")) == 1,
            str(messages_for(packet_bot, packet_group, "已过期")),
        )

    # ------------------------------------------------------------------
    # 4. CAS 互斥：两个 worker 都扫描到 open，只有一个能结算
    # ------------------------------------------------------------------
    race_group = -100810004
    await create_group(race_group, "CAS race")
    async with session_scope() as session:
        race_lottery = Lottery(
            group_id=race_group,
            creator_id=810401,
            title="并发抽奖",
            prize="并发奖品",
            status="open",
            ends_at=past,
        )
        race_packet = RedPacket(
            group_id=race_group,
            sender_id=810402,
            total=25,
            count=2,
            remaining_amount=25,
            remaining_count=2,
            status="open",
            expires_at=past,
        )
        session.add_all([race_lottery, race_packet])
        await session.flush()
        race_lottery_id = int(race_lottery.id)
        race_packet_id = int(race_packet.id)

    async def scan_open_lottery() -> list[int]:
        async with session_scope() as session:
            rows = await session.scalars(
                select(Lottery).where(
                    Lottery.id == race_lottery_id, Lottery.status == "open"
                )
            )
            return [int(row.id) for row in rows.all()]

    a_scan = await scan_open_lottery()
    b_scan = await scan_open_lottery()
    check(
        "both workers scan the same open lottery",
        a_scan == [race_lottery_id] and b_scan == [race_lottery_id],
        f"{a_scan} / {b_scan}",
    )

    async with session_scope() as session:
        check(
            "worker A claims lottery",
            await scheduler._claim_lottery(session, race_lottery_id),
        )
    async with session_scope() as session:
        check(
            "worker B loses lottery claim after A commits",
            not await scheduler._claim_lottery(session, race_lottery_id),
        )

    async with session_scope() as session:
        amount = await scheduler._claim_red_packet(session, race_packet_id)
    check(
        "worker A claims red packet refund",
        amount == 25,
        str(amount),
    )
    async with session_scope() as session:
        second_amount = await scheduler._claim_red_packet(session, race_packet_id)
    check(
        "worker B gets no red packet refund after A commits",
        second_amount is None,
        str(second_amount),
    )

    # ------------------------------------------------------------------
    # 5. 竞拍价格 CAS：扫描后晚到出价时不能按旧价结算
    # ------------------------------------------------------------------
    price_group = -100810005
    await create_group(price_group, "Auction price CAS")
    async with session_scope() as session:
        auction = Auction(
            group_id=price_group,
            creator_id=810501,
            item="价格竞拍",
            start_price=100,
            step=10,
            current_price=100,
            current_bidder=810502,
            status="open",
            ends_at=past,
        )
        session.add(auction)
        await session.flush()
        price_auction_id = int(auction.id)

    async with session_scope() as session:
        snapshot = await session.get(Auction, price_auction_id)
        assert snapshot is not None
        scan_bidder = snapshot.current_bidder
        scan_price = int(snapshot.current_price)

    async with session_scope() as session:
        late = await session.get(Auction, price_auction_id)
        assert late is not None
        late.current_price = 150
        late.current_bidder = 810503

    async with session_scope() as session:
        claimed = await scheduler._claim_auction(
            session, price_auction_id, scan_bidder, scan_price
        )
    check(
        "auction price CAS rejects stale snapshot",
        not claimed,
        str(claimed),
    )

    async with session_scope() as session:
        current = await session.get(Auction, price_auction_id)
        check(
            "late bid keeps auction open for next tick",
            current is not None
            and current.status == "open"
            and int(current.current_price or 0) == 150,
            f"{current.status}/{current.current_price}" if current else "missing",
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
