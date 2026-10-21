"""Per-group daily AI quotas, capped by the owning user's plan."""
from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from ..entitlements import build_for_user, group_owner_user_id
from ..models import ConsoleUser, Group
from . import repo


async def _owner_limit(
    session: AsyncSession, group: Group, attr: str
) -> int:
    """Return the effective daily limit for ``attr`` (0 means unlimited)."""
    group_limit = int(getattr(group, attr) or 0)
    owner_id = await group_owner_user_id(session, group.id)
    if owner_id is None:
        return group_limit
    owner = await session.get(ConsoleUser, owner_id)
    entitlements = await build_for_user(session, owner)
    plan_limit = getattr(entitlements, attr, None)
    if plan_limit is None:
        # Superadmin-owned groups only use the per-group setting.
        return group_limit
    plan_limit = int(plan_limit)
    if group_limit <= 0:
        return plan_limit
    if plan_limit <= 0:
        return group_limit
    return min(group_limit, plan_limit)


async def allow_request(session: AsyncSession, group: Group) -> bool:
    limit = await _owner_limit(session, group, "ai_daily_quota")
    if limit > 0:
        usage = await repo.get_usage(session, group.id)
        return usage.requests < limit
    return True


async def allow_sample(session: AsyncSession, group: Group) -> bool:
    limit = await _owner_limit(session, group, "sample_daily_quota")
    if limit > 0:
        usage = await repo.get_usage(session, group.id)
        return usage.samples < limit
    return True


async def note(
    session: AsyncSession,
    group: Group,
    *,
    requests: int = 0,
    samples: int = 0,
    hits: int = 0,
) -> None:
    usage = await repo.get_usage(session, group.id)
    if requests:
        usage.requests = (usage.requests or 0) + requests
        await repo.bump_stat(session, group.id, "ai_requests", requests)
    if samples:
        usage.samples = (usage.samples or 0) + samples
    if hits:
        usage.hits = (usage.hits or 0) + hits
        await repo.bump_stat(session, group.id, "ai_hits", hits)
