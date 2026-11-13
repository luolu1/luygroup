from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..entitlements import GroupAccess, build_for_user, group_owner_user_id
from ..models import AIProvider, AIUsage, ConsoleGroupAccess, ConsoleUser, Group, utcnow
from . import ai
from . import repo


async def allow_request(
    session: AsyncSession,
    group: Group,
    *,
    access: GroupAccess | None = None,
    provider_id: int | None = None,
    image: bool = False,
) -> bool:
    provider = await _selected_provider(
        session, group, provider_id=provider_id, image=image, access=access
    )
    if provider is None or provider.owner_user_id is not None:
        return True
    attr = "sample_daily_quota" if image else "ai_daily_quota"
    owner_id = await group_owner_user_id(session, group.id, access=access)
    if owner_id is not None:
        owner = await session.get(ConsoleUser, owner_id)
        entitlements = (
            access.entitlements if access is not None else await build_for_user(session, owner)
        )
        custom_limit = (
            owner.ai_image_daily_quota
            if image and owner is not None
            else owner.ai_text_daily_quota
            if owner is not None
            else None
        )
        plan_limit = getattr(entitlements, attr, None)
        if custom_limit is None or custom_limit == 0:
            limit = plan_limit
        elif plan_limit is None or plan_limit <= 0:
            limit = custom_limit
        else:
            limit = min(custom_limit, int(plan_limit))
        if limit is None:
            return True
        limit = int(limit)
    else:
        limit = int(getattr(group, attr) or 0)
    if limit > 0:
        if owner_id is not None:
            total = await session.scalar(
                select(
                    func.coalesce(
                        func.sum(
                            getattr(
                                AIUsage,
                                "image_requests" if image else "text_requests",
                            )
                        ),
                        0,
                    )
                )
                .select_from(AIUsage)
                .join(ConsoleGroupAccess, ConsoleGroupAccess.group_id == AIUsage.group_id)
                .where(
                    ConsoleGroupAccess.user_id == owner_id,
                    ConsoleGroupAccess.role == "owner",
                    AIUsage.day == utcnow().strftime("%Y-%m-%d"),
                )
            )
            return int(total or 0) < limit
        usage = await repo.get_usage(session, group.id)
        return (usage.image_requests if image else usage.text_requests) < limit
    return True


async def allow_sample(
    session: AsyncSession, group: Group, *, access: GroupAccess | None = None
) -> bool:
    return await allow_request(session, group, access=access, image=True)


async def _selected_provider(
    session: AsyncSession,
    group: Group,
    *,
    provider_id: int | None,
    image: bool,
    access: GroupAccess | None,
) -> AIProvider | None:
    providers = await ai.enabled_providers(
        session,
        access.owner_id if access is not None else await group_owner_user_id(session, group.id),
        provider_id=provider_id,
    )
    if image:
        providers = [provider for provider in providers if provider.vision_model]
    return providers[0] if providers else None


async def reserve_quota(
    session: AsyncSession,
    group: Group,
    *,
    image: bool = False,
    access: GroupAccess | None = None,
    provider_id: int | None = None,
) -> bool:
    """Atomically reserve one quota slot. Returns True if allowed."""
    provider = await _selected_provider(
        session, group, provider_id=provider_id, image=image, access=access
    )
    if provider is None or provider.owner_user_id is not None:
        return True
    
    attr = "sample_daily_quota" if image else "ai_daily_quota"
    owner_id = await group_owner_user_id(session, group.id, access=access)
    if owner_id is not None:
        owner = await session.get(ConsoleUser, owner_id)
        entitlements = (
            access.entitlements if access is not None else await build_for_user(session, owner_id)
        )
        custom_limit = (
            owner.ai_image_daily_quota
            if image and owner is not None
            else owner.ai_text_daily_quota
            if owner is not None
            else None
        )
        plan_limit = getattr(entitlements, attr, None)
        if custom_limit is None or custom_limit == 0:
            limit = plan_limit
        elif plan_limit is None or plan_limit <= 0:
            limit = custom_limit
        else:
            limit = min(custom_limit, int(plan_limit))
        if limit is None or limit <= 0:
            return False
        
        usage = await repo.get_usage(session, group.id)
        field = "image_requests" if image else "text_requests"
        current = getattr(usage, field) or 0
        if current >= limit:
            return False
        
        # Atomically increment
        setattr(usage, field, current + 1)
        usage.requests = (usage.requests or 0) + 1
        await session.flush()
        return True
    return True


async def release_quota(
    session: AsyncSession,
    group: Group,
    *,
    image: bool = False,
) -> None:
    """Release a reserved quota slot when the request was not made."""
    usage = await repo.get_usage(session, group.id)
    field = "image_requests" if image else "text_requests"
    current = getattr(usage, field) or 0
    if current > 0:
        setattr(usage, field, current - 1)
    if (usage.requests or 0) > 0:
        usage.requests = (usage.requests or 0) - 1


async def note(
    session: AsyncSession,
    group: Group,
    *,
    requests: int = 0,
    image: bool = False,
    samples: int = 0,
    hits: int = 0,
) -> None:
    """Record additional usage metrics. Requests count is already incremented by reserve_quota."""
    usage = await repo.get_usage(session, group.id)
    if requests:
        # Requests already counted by reserve_quota, only update stats
        await repo.bump_stat(session, group.id, "ai_requests", requests)
    if samples:
        usage.samples = (usage.samples or 0) + samples
    if hits:
        usage.hits = (usage.hits or 0) + hits
        await repo.bump_stat(session, group.id, "ai_hits", hits)
