from __future__ import annotations

import calendar
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import ConsoleUser, Subscription, SubscriptionPlan, utcnow

MONTHLY_PRICE_CENTS = 800


def add_calendar_month(value: datetime) -> datetime:
    month_index = value.year * 12 + value.month
    year, month_zero = divmod(month_index, 12)
    month = month_zero + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return value.replace(year=year, month=month, day=day)


async def purchase_month(
    session: AsyncSession, user_id: int
) -> tuple[str, datetime | None, int | None]:
    """Charge CNY 8 and extend one calendar month in the current transaction."""
    now = utcnow()
    new_balance = await session.scalar(
        update(ConsoleUser)
        .where(
            ConsoleUser.id == int(user_id),
            ConsoleUser.is_active.is_(True),
            ConsoleUser.balance_cents >= MONTHLY_PRICE_CENTS,
        )
        .values(balance_cents=ConsoleUser.balance_cents - MONTHLY_PRICE_CENTS)
        .returning(ConsoleUser.balance_cents)
    )
    if new_balance is None:
        exists = await session.scalar(
            select(ConsoleUser.id).where(
                ConsoleUser.id == int(user_id), ConsoleUser.is_active.is_(True)
            )
        )
        return ("insufficient_balance" if exists is not None else "account_unavailable", None, None)
    user_id = int(user_id)

    plan = await session.scalar(
        select(SubscriptionPlan)
        .where(SubscriptionPlan.name == "pro", SubscriptionPlan.enabled.is_(True))
        .limit(1)
    )
    if plan is None:
        await session.rollback()
        return "plan_unavailable", None, None

    rows = (
        await session.scalars(
            select(Subscription)
            .where(Subscription.user_id == user_id, Subscription.status == "active")
            .order_by(Subscription.id.desc())
        )
    ).all()
    permanent = next((row for row in rows if row.expires_at is None), None)
    if permanent is not None:
        await session.rollback()
        return "permanent_subscription", None, None
    current = rows[0] if rows else None
    for duplicate in rows[1:]:
        duplicate.status = "expired"

    base = now
    if current is not None and current.expires_at is not None and current.expires_at > now:
        base = current.expires_at
    expires_at = add_calendar_month(base)
    if current is None:
        session.add(
            Subscription(
                user_id=user_id,
                plan_id=plan.id,
                status="active",
                starts_at=now,
                expires_at=expires_at,
            )
        )
    else:
        current.plan_id = plan.id
        current.status = "active"
        current.starts_at = now
        current.expires_at = expires_at
    return "ok", expires_at, int(new_balance)
