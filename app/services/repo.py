"""Small repository helpers shared by bot handlers and the web panel."""
from __future__ import annotations

from datetime import datetime
from collections.abc import Iterable

from sqlalchemy import func, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_config
from ..models import (
    Admin,
    AdminLog,
    AIUsage,
    Group,
    GroupDailyStat,
    KeywordHit,
    Member,
    MemberDailyStat,
    PointsTransaction,
    Warning,
    Whitelist,
    day_key,
    utcnow,
)

STAT_FIELDS = {
    "messages",
    "joins",
    "leaves",
    "warns",
    "bans",
    "deletes",
    "ai_requests",
    "ai_hits",
}


def display_user(user_id: int | None, first_name: str | None = None, username: str | None = None) -> str:
    if first_name:
        if username:
            return f"{first_name} (@{username})"
        return first_name
    if username:
        return f"@{username}"
    return f"ID {user_id}" if user_id else "未知用户"


def dt(value: datetime | None) -> str:
    return value.strftime("%Y-%m-%d %H:%M") if value else "-"


async def get_group(session: AsyncSession, group_id: int) -> Group | None:
    return await session.get(Group, int(group_id))


async def ensure_group(
    session: AsyncSession,
    group_id: int,
    *,
    title: str | None = None,
    username: str | None = None,
    chat_type: str = "supergroup",
    owner_id: int | None = None,
    member_count: int | None = None,
) -> Group:
    # Telegram 群组/超级群组 ID 为负数；正数通常是私聊用户 ID。
    # 防止私聊命令误把用户创建成“群组”记录。
    if int(group_id) >= 0:
        raise ValueError("只能为 Telegram 群组建立记录")
    group = await session.get(Group, int(group_id))
    if group is None:
        defaults = get_config().defaults
        group = Group(
            id=int(group_id),
            title=title or "",
            username=username or "",
            chat_type=chat_type,
            owner_id=owner_id,
            member_count=member_count or 0,
            warn_limit=defaults.warn_limit,
            warn_action=defaults.warn_action,
            mute_minutes=defaults.mute_minutes,
            flood_limit=defaults.flood_limit,
            flood_window=defaults.flood_window,
            flood_action=defaults.flood_action,
            ai_daily_quota=defaults.ai_daily_quota,
            sample_daily_quota=defaults.sample_daily_quota,
            points_per_message=defaults.points_per_message,
            points_cooldown=defaults.points_cooldown,
            daily_bonus=defaults.daily_bonus,
        )
        session.add(group)
        await session.flush()
    else:
        changed = False
        if title is not None and title != group.title:
            group.title = title
            changed = True
        if username is not None and username != group.username:
            group.username = username
            changed = True
        if owner_id is not None and group.owner_id != owner_id:
            group.owner_id = owner_id
            changed = True
        if member_count is not None and member_count >= 0 and group.member_count != member_count:
            group.member_count = member_count
            changed = True
        if changed:
            group.updated_at = utcnow()
    return group


async def ensure_member(
    session: AsyncSession,
    group_id: int,
    user_id: int,
    *,
    username: str = "",
    first_name: str = "",
) -> Member:
    member = await session.scalar(
        select(Member).where(Member.group_id == int(group_id), Member.user_id == int(user_id))
    )
    if member is None:
        member = Member(
            group_id=int(group_id),
            user_id=int(user_id),
            username=username or "",
            first_name=first_name or "",
        )
        session.add(member)
        await session.flush()
    else:
        if username and member.username != username:
            member.username = username
        if first_name and member.first_name != first_name:
            member.first_name = first_name
    return member


async def get_member(session: AsyncSession, group_id: int, user_id: int) -> Member | None:
    return await session.scalar(
        select(Member).where(Member.group_id == int(group_id), Member.user_id == int(user_id))
    )


async def bump_stat(session: AsyncSession, group_id: int, field: str, delta: int = 1) -> None:
    if field not in STAT_FIELDS or not delta:
        return
    column = getattr(GroupDailyStat, field)
    statement = (
        sqlite_insert(GroupDailyStat)
        .values(group_id=int(group_id), day=day_key(), **{field: int(delta)})
        .on_conflict_do_update(
            index_elements=["group_id", "day"],
            set_={field: func.coalesce(column, 0) + int(delta)},
        )
    )
    await session.execute(statement)


async def bump_member_daily(
    session: AsyncSession, group_id: int, user_id: int, delta: int = 1
) -> None:
    """Track a member's messages for the current UTC day.

    Uses SQLite's atomic upsert so concurrent messages from the same member
    cannot race into a unique-constraint failure.
    """
    if not delta:
        return
    today = day_key()
    statement = (
        sqlite_insert(MemberDailyStat)
        .values(
            group_id=int(group_id),
            user_id=int(user_id),
            day=today,
            messages=delta,
        )
        .on_conflict_do_update(
            index_elements=["group_id", "user_id", "day"],
            set_={"messages": MemberDailyStat.messages + delta},
        )
    )
    await session.execute(statement)


async def get_usage(session: AsyncSession, group_id: int) -> AIUsage:
    today = day_key()
    usage = await session.scalar(
        select(AIUsage).where(AIUsage.group_id == int(group_id), AIUsage.day == today)
    )
    if usage is None:
        usage = AIUsage(group_id=int(group_id), day=today)
        session.add(usage)
        await session.flush()
    return usage


async def add_log(
    session: AsyncSession,
    group_id: int,
    admin_id: int | None,
    action: str,
    target_id: int | None = None,
    detail: str = "",
) -> None:
    session.add(
        AdminLog(
            group_id=int(group_id),
            admin_id=admin_id,
            action=action,
            target_id=target_id,
            detail=detail[:4000],
        )
    )


async def record_hit(
    session: AsyncSession,
    group_id: int,
    user_id: int,
    *,
    keyword: str,
    content: str,
    action: str,
    source: str,
) -> None:
    session.add(
        KeywordHit(
            group_id=int(group_id),
            user_id=int(user_id),
            keyword=keyword[:255],
            content=(content or "")[:4000],
            action=action,
            source=source,
        )
    )


async def warning_count(session: AsyncSession, group_id: int, user_id: int) -> int:
    return int(
        await session.scalar(
            select(func.count(Warning.id)).where(
                Warning.group_id == int(group_id),
                Warning.user_id == int(user_id),
                Warning.active.is_(True),
            )
        )
        or 0
    )


async def active_warnings(session: AsyncSession, group_id: int, user_id: int) -> list[Warning]:
    result = await session.scalars(
        select(Warning)
        .where(
            Warning.group_id == int(group_id),
            Warning.user_id == int(user_id),
            Warning.active.is_(True),
        )
        .order_by(Warning.created_at.desc())
    )
    return list(result.all())


async def is_whitelisted(session: AsyncSession, group_id: int, user_id: int) -> bool:
    exists = await session.scalar(
        select(Whitelist.id).where(
            Whitelist.group_id == int(group_id), Whitelist.user_id == int(user_id)
        )
    )
    return exists is not None


async def get_admin(session: AsyncSession, group_id: int, user_id: int) -> Admin | None:
    return await session.scalar(
        select(Admin).where(Admin.group_id == int(group_id), Admin.user_id == int(user_id))
    )


async def adjust_points(
    session: AsyncSession,
    group_id: int,
    user_id: int,
    amount: int,
    reason: str,
    *,
    allow_negative: bool = False,
    username: str = "",
    first_name: str = "",
) -> tuple[Member, int] | None:
    """Change a member's points. Returns (member, new_balance) or None if the
    member would go negative and negatives are not allowed."""
    member = await ensure_member(
        session, group_id, user_id, username=username, first_name=first_name
    )
    # 原子累加，避免并发读写同一余额时丢更新。
    stmt = (
        update(Member)
        .where(Member.id == member.id)
        .values(points=func.coalesce(Member.points, 0) + int(amount))
        .returning(Member.points)
        .execution_options(synchronize_session=False)
    )
    if not allow_negative:
        stmt = stmt.where(func.coalesce(Member.points, 0) + int(amount) >= 0)
    row = (await session.execute(stmt)).first()
    if row is None:
        return None
    new_balance = int(row[0] or 0)
    member.points = new_balance
    session.add(
        PointsTransaction(
            group_id=int(group_id),
            user_id=int(user_id),
            amount=int(amount),
            balance_after=new_balance,
            reason=reason[:255],
        )
    )
    return member, new_balance


async def expire_warnings(session: AsyncSession) -> int:
    result = await session.execute(
        update(Warning)
        .where(
            Warning.active.is_(True),
            Warning.expires_at.is_not(None),
            Warning.expires_at <= utcnow(),
        )
        .values(active=False)
        .execution_options(synchronize_session=False)
    )
    return int(result.rowcount or 0)


def parse_csv(value: str | None) -> list[str]:
    if not value:
        return []
    items: Iterable[str] = value.replace("\n", ",").split(",")
    return [item.strip() for item in items if item.strip()]


async def top_members(session: AsyncSession, group_id: int, limit: int = 10) -> list[Member]:
    result = await session.scalars(
        select(Member)
        .where(Member.group_id == int(group_id))
        .order_by(Member.message_count.desc())
        .limit(limit)
    )
    return list(result.all())


async def top_points(session: AsyncSession, group_id: int, limit: int = 10) -> list[Member]:
    result = await session.scalars(
        select(Member)
        .where(Member.group_id == int(group_id))
        .order_by(Member.points.desc())
        .limit(limit)
    )
    return list(result.all())
