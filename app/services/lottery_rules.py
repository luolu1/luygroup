"""Participation rules shared by group messages and lottery entry."""
from __future__ import annotations

from sqlalchemy import delete, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Lottery, LotteryEntry, LotteryQualification, Member, utcnow
from . import repo

async def open_keyword_lotteries(
    session: AsyncSession, group_id: int
) -> list[tuple[int, str]]:
    key = int(group_id)
    rows = (
        await session.execute(
            select(Lottery.id, Lottery.participation_value).where(
                Lottery.group_id == key,
                Lottery.status == "open",
                Lottery.participation_type == "keyword",
            )
        )
    ).all()
    return [
        (int(row[0]), str(row[1] or "").strip().casefold())
        for row in rows
        if str(row[1] or "").strip()
    ]


async def record_keyword_matches(
    session: AsyncSession, group_id: int, user_id: int, text: str
) -> list[str]:
    lotteries = await open_keyword_lotteries(session, group_id)
    if not lotteries:
        return []
    normalized_text = text.strip().casefold()
    auto_entered: list[str] = []
    for lottery_id, keyword in lotteries:
        if keyword != normalized_text:
            continue
        lottery = await session.get(Lottery, lottery_id)
        if (
            lottery is None
            or lottery.status != "open"
            or (lottery.ends_at is not None and lottery.ends_at <= utcnow())
        ):
            continue
        inserted = (
            await session.execute(
                sqlite_insert(LotteryEntry)
                .values(lottery_id=lottery_id, user_id=int(user_id))
                .on_conflict_do_nothing(index_elements=["lottery_id", "user_id"])
                .returning(LotteryEntry.id)
            )
        ).first() is not None
        if inserted and lottery.entry_fee > 0:
            points = await repo.adjust_points(
                session,
                group_id,
                user_id,
                -lottery.entry_fee,
                f"抽奖参与费：{lottery.title}",
            )
            if points is None:
                await session.execute(
                    delete(LotteryEntry).where(
                        LotteryEntry.lottery_id == lottery_id,
                        LotteryEntry.user_id == int(user_id),
                    )
                )
                continue
        await session.execute(
            sqlite_insert(LotteryQualification)
            .values(lottery_id=lottery_id, user_id=int(user_id))
            .on_conflict_do_nothing(index_elements=["lottery_id", "user_id"])
        )
        if inserted:
            auto_entered.append(lottery.title or f"#{lottery_id}")
    return auto_entered


async def eligible(
    session: AsyncSession, lottery: Lottery, user_id: int
) -> tuple[bool, str]:
    kind = (lottery.participation_type or "none").lower()
    if kind == "keyword":
        keyword = (lottery.participation_value or "").strip()
        found = await session.scalar(
            select(LotteryQualification.id).where(
                LotteryQualification.lottery_id == lottery.id,
                LotteryQualification.user_id == int(user_id),
            )
        )
        if found is not None:
            return True, ""
        return False, f"需要先在群内发送包含「{keyword}」的消息才能参与。"
    if kind in {"points", "message_count"}:
        member = await session.scalar(
            select(Member).where(
                Member.group_id == int(lottery.group_id),
                Member.user_id == int(user_id),
            )
        )
        required = int(lottery.participation_value or 0)
        if kind == "points":
            current = int(member.points or 0) if member is not None else 0
            label = "积分"
        else:
            current = int(member.message_count or 0) if member is not None else 0
            label = "累计发言"
        if current >= required:
            return True, ""
        return False, f"需要至少 {required} {label}才能参与（当前 {current}）。"
    return True, ""
