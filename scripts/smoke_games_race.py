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

from app.bot.handlers import games  # noqa: E402
from app.db import dispose_db, init_db, session_scope  # noqa: E402
from app.models import (  # noqa: E402
    Auction,
    AuctionBid,
    Group,
    Member,
    PointsTransaction,
    RedPacket,
    RedPacketClaim,
    utcnow,
)
from app.services import repo, scheduler  # noqa: E402

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


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id: int, text: str, **kwargs) -> None:
        self.sent.append((int(chat_id), text))


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
        self.text = text
        self.reply_to_message = None
        self.replies: list[str] = []

    async def reply(self, text: str, **kwargs) -> None:
        self.replies.append(text)


class FakeCallbackMessage:
    def __init__(self, chat_id: int) -> None:
        self.chat = FakeChat(chat_id)
        self.markup_calls: list[object] = []

    async def edit_reply_markup(self, reply_markup=None) -> None:
        self.markup_calls.append(reply_markup)


class FakeCallback:
    def __init__(self, *, data: str, user_id: int, chat_id: int) -> None:
        self.data = data
        self.from_user = FakeUser(user_id)
        self.message = FakeCallbackMessage(chat_id)
        self.answers: list[str] = []

    async def answer(self, text: str, show_alert: bool = False) -> None:
        self.answers.append(text)


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
