"""AI provider management routes, registered on the shared panel router."""

from __future__ import annotations

from datetime import timedelta
from fastapi import Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import case, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from .auth import require_admin
from .form_values import bool_value as _bool, int_value as _int, text_value as _text
from ..config import get_config, save_learning_dir
from ..db import get_session
from ..entitlements import Entitlements
from ..models import AIEvent, AIProvider, AIUsage, ConsoleUser, Group, day_key, utcnow
from ..services import ai as ai_service
from .pages import _accessible_group_ids, _provider_labels, _redirect, _render, _require_feature, _require_superadmin, router


# ---------------------------------------------------------------------------
# AI providers
# ---------------------------------------------------------------------------
def _provider_visible(ent: Entitlements, provider: AIProvider) -> bool:
    if ent.is_superadmin:
        return True
    if ent.user is None:
        return False
    return provider.owner_user_id in (None, ent.user.id)


def _provider_editable(ent: Entitlements, provider: AIProvider) -> bool:
    if ent.is_superadmin:
        return True
    return bool(ent.user and provider.owner_user_id == ent.user.id)


def _api_mode(form, *, fallback: str = "chat") -> str:  # type: ignore[no-untyped-def]
    raw = _text(form, "api_mode", 16)
    return _mode_value(raw) if raw else _mode_value(fallback)


def _vision_api_mode(form, *, fallback: str = "chat") -> str:  # type: ignore[no-untyped-def]
    raw = _text(form, "vision_api_mode", 16)
    return _mode_value(raw) if raw else _mode_value(fallback)


def _mode_value(raw: str) -> str:
    mode = (raw or "").strip().lower()
    return mode if mode in {"chat", "responses"} else "chat"


@router.get("/ai/add", response_class=HTMLResponse)
async def ai_add_page(request: Request) -> HTMLResponse:
    _require_feature(request, "ai_providers")
    return _render(
        request,
        "ai_add.html",
        defaults=get_config().ai,
        presets=ai_service.PROVIDER_PRESETS,
    )


@router.get("/ai/providers/{provider_id}", response_class=HTMLResponse)
async def ai_provider_edit_page(
    provider_id: int,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    ent = _require_feature(request, "ai_providers")
    provider = await session.get(AIProvider, provider_id)
    if provider is None or not _provider_visible(ent, provider):
        raise HTTPException(status_code=404, detail="接口不存在")
    if not _provider_editable(ent, provider):
        raise HTTPException(status_code=403, detail="不能编辑其他用户或系统内置接口")
    return _render(
        request,
        "ai_provider_edit.html",
        provider=provider,
        defaults=get_config().ai,
        presets=ai_service.PROVIDER_PRESETS,
    )


@router.get("/ai", response_class=HTMLResponse)
async def ai_page(request: Request, session: AsyncSession = Depends(get_session)) -> HTMLResponse:
    ent = _require_feature(request, "ai_providers")
    provider_stmt = select(AIProvider)
    if not ent.is_superadmin and ent.user is not None:
        provider_stmt = provider_stmt.where(
            or_(AIProvider.owner_user_id.is_(None), AIProvider.owner_user_id == ent.user.id)
        )
    providers = (
        await session.scalars(
            provider_stmt.order_by(AIProvider.priority.asc(), AIProvider.id.asc())
        )
    ).all()
    gids = await _accessible_group_ids(session, request)
    start = (utcnow() - timedelta(days=13)).strftime("%Y-%m-%d")
    usage_stmt = select(AIUsage).where(AIUsage.day >= start).order_by(AIUsage.day.desc())
    event_stmt = select(AIEvent).order_by(AIEvent.id.desc()).limit(100)
    if gids is not None:
        usage_stmt = usage_stmt.where(AIUsage.group_id.in_(gids))
        event_stmt = event_stmt.where(AIEvent.group_id.in_(gids))
    usage = (await session.scalars(usage_stmt)).all()
    events = (await session.scalars(event_stmt)).all()
    provider_usage_stmt = (
        select(
            AIEvent.provider,
            func.count(AIEvent.id),
            func.coalesce(func.sum(case((AIEvent.hit.is_(True), 1), else_=0)), 0),
        )
        .where(AIEvent.created_at >= utcnow() - timedelta(days=13))
        .group_by(AIEvent.provider)
        .order_by(func.count(AIEvent.id).desc(), AIEvent.provider.asc())
    )
    if gids is not None:
        provider_usage_stmt = provider_usage_stmt.where(AIEvent.group_id.in_(gids))
    provider_usage = (await session.execute(provider_usage_stmt)).all()
    today = day_key()
    today_stmt = select(
        func.coalesce(func.sum(AIUsage.requests), 0),
        func.coalesce(func.sum(AIUsage.samples), 0),
        func.coalesce(func.sum(AIUsage.hits), 0),
    ).where(AIUsage.day == today)
    if gids is not None:
        today_stmt = today_stmt.where(AIUsage.group_id.in_(gids))
    today_requests, today_samples, today_hits = (await session.execute(today_stmt)).one()
    owner = ent.user
    request_limit = (
        owner.ai_text_daily_quota
        if owner is not None and owner.ai_text_daily_quota is not None
        else ent.ai_daily_quota
    )
    sample_limit = (
        owner.ai_image_daily_quota
        if owner is not None and owner.ai_image_daily_quota is not None
        else ent.sample_daily_quota
    )
    if ent.user is not None and not ent.is_superadmin:
        request_limit = (
            min(int(request_limit or 0), int(ent.ai_daily_quota or 0))
            if ent.ai_daily_quota
            else request_limit
        )
        sample_limit = (
            min(int(sample_limit or 0), int(ent.sample_daily_quota or 0))
            if ent.sample_daily_quota
            else sample_limit
        )
    ai_summary = {
        "day": today,
        "requests": int(today_requests or 0),
        "text_requests": int(sum(row.text_requests or 0 for row in usage if row.day == today)),
        "image_requests": int(sum(row.image_requests or 0 for row in usage if row.day == today)),
        "samples": int(today_samples or 0),
        "hits": int(today_hits or 0),
        "request_limit": request_limit,
        "sample_limit": sample_limit,
    }
    group_ids = {item.group_id for item in usage} | {item.group_id for item in events}
    groups = (
        (await session.scalars(select(Group).where(Group.id.in_(group_ids)))).all()
        if group_ids
        else []
    )
    owner_ids = {p.owner_user_id for p in providers if p.owner_user_id}
    owners = (
        (await session.scalars(select(ConsoleUser).where(ConsoleUser.id.in_(owner_ids)))).all()
        if owner_ids
        else []
    )
    return _render(
        request,
        "ai.html",
        providers=providers,
        owner_map={u.id: u for u in owners},
        provider_editable=lambda p: _provider_editable(ent, p),
        usage=usage,
        events=events,
        group_map={g.id: g for g in groups},
        defaults=get_config().ai,
        presets=ai_service.PROVIDER_PRESETS,
        learning=ai_service.learning_store.stats(),
        ai_summary=ai_summary,
        provider_usage=provider_usage,
        provider_labels=await _provider_labels(session),
    )


@router.post("/ai/models")
async def ai_models(
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    """Return the supplier model list for the panel's model picker."""
    ent = _require_feature(request, "ai_providers")
    try:
        payload = await request.json()
    except ValueError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    base_url = str(payload.get("base_url") or "").strip()
    api_key = str(payload.get("api_key") or "").strip()
    provider_id = payload.get("provider_id")
    if provider_id:
        try:
            provider = await session.get(AIProvider, int(provider_id))
        except (TypeError, ValueError):
            provider = None
        if provider is None or not _provider_visible(ent, provider):
            return JSONResponse({"ok": False, "error": "接口不存在"}, status_code=404)
        if _provider_editable(ent, provider):
            base_url = base_url or provider.base_url
            api_key = api_key or provider.api_key
        else:
            base_url = provider.base_url
            api_key = provider.api_key
    kind = str(payload.get("kind") or "text").strip().lower()
    if kind not in {"text", "vision"}:
        kind = "text"
    try:
        ai_service.validated_base_url(base_url, allow_internal=ent.is_superadmin)
        models = await ai_service.fetch_models(base_url, api_key, kind=kind)
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    return JSONResponse({"ok": True, "models": models})


@router.post("/ai/learning/dir")
async def change_ai_learning_dir(
    request: Request,
    admin_id: int = Depends(require_admin),
):
    _require_superadmin(request)
    form = await request.form()
    raw = _text(form, "directory", 1024)
    try:
        path = save_learning_dir(raw)
    except (OSError, ValueError) as exc:
        return _redirect("/ai", err=f"学习数据目录设置失败：{exc}")
    try:
        await ai_service.learning_store.set_directory(path)
    except OSError as exc:
        return _redirect("/ai", err=f"目录已保存，但迁移学习数据失败：{exc}")
    return _redirect("/ai", msg=f"AI 学习数据目录已切换为 {path}")


@router.post("/ai/providers/add")
async def add_ai_provider(
    request: Request,
    admin_id: int = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    ent = _require_feature(request, "ai_providers")
    form = await request.form()
    name = _text(form, "name", 64)
    if not name:
        return _redirect("/ai/add", err="接口名称不能为空")
    exists = await session.scalar(select(AIProvider.id).where(AIProvider.name == name))
    if exists:
        return _redirect("/ai/add", err="接口名称已存在")
    raw_base_url = _text(form, "base_url", 512)
    model = _text(form, "model", 128)
    if not raw_base_url or not model:
        return _redirect("/ai/add", err="接口地址和模型不能为空")
    try:
        base_url = ai_service.validated_base_url(raw_base_url, allow_internal=ent.is_superadmin)
    except ValueError as exc:
        return _redirect("/ai/add", err=str(exc))
    session.add(
        AIProvider(
            name=name,
            label=name,
            base_url=base_url,
            api_key=_text(form, "api_key", 512),
            model=model,
            vision_model=_text(form, "vision_model", 128),
            api_mode=_api_mode(form),
            vision_api_mode=_vision_api_mode(form),
            priority=_int(form, "priority", 100, 0, 100000),
            enabled=_bool(form, "enabled"),
            builtin=False,
            owner_user_id=None if ent.is_superadmin else ent.user.id,
        )
    )
    return _redirect("/ai", msg="AI 接口已添加")


@router.post("/ai/providers/{provider_id}/update")
async def update_ai_provider(
    provider_id: int,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    ent = _require_feature(request, "ai_providers")
    provider = await session.get(AIProvider, provider_id)
    if provider is None:
        raise HTTPException(status_code=404, detail="接口不存在")
    if not _provider_editable(ent, provider):
        raise HTTPException(status_code=403, detail="不能修改其他用户或系统内置接口")
    form = await request.form()
    name = _text(form, "name", 64, provider.name or provider.label or "")
    if not name:
        return _redirect("/ai", err="接口名称不能为空")
    duplicate = await session.scalar(
        select(AIProvider.id).where(AIProvider.name == name, AIProvider.id != provider_id)
    )
    if duplicate is not None:
        return _redirect("/ai", err="接口名称已存在")
    base_url = _text(form, "base_url", 512, provider.base_url or "")
    model = _text(form, "model", 128, provider.model or "")
    if not base_url or not model:
        return _redirect("/ai", err="接口地址和模型不能为空")
    try:
        base_url = ai_service.validated_base_url(base_url, allow_internal=ent.is_superadmin)
    except ValueError as exc:
        return _redirect("/ai", err=str(exc))
    new_key = _text(form, "api_key", 512)
    vision_model = _text(form, "vision_model", 128, provider.vision_model or "")
    priority = _int(form, "priority", provider.priority or 100, 0, 100000)
    enabled = _bool(form, "enabled")
    old_name = provider.name
    provider.name = name
    provider.label = name
    provider.base_url = base_url
    if new_key:
        provider.api_key = new_key
    provider.model = model
    provider.vision_model = vision_model
    provider.api_mode = _api_mode(form, fallback=provider.api_mode or "chat")
    provider.vision_api_mode = _vision_api_mode(
        form, fallback=provider.vision_api_mode or "chat"
    )
    provider.priority = priority
    provider.enabled = enabled
    if old_name != name:
        await session.execute(
            update(AIEvent).where(AIEvent.provider == old_name).values(provider=name)
        )
    return _redirect("/ai", msg="接口配置已更新")


@router.post("/ai/providers/{provider_id}/delete")
async def delete_ai_provider(
    provider_id: int,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    ent = _require_feature(request, "ai_providers")
    provider = await session.get(AIProvider, provider_id)
    if provider is None:
        raise HTTPException(status_code=404, detail="接口不存在")
    if not _provider_editable(ent, provider):
        raise HTTPException(status_code=403, detail="不能删除其他用户或系统内置接口")
    await session.delete(provider)
    return _redirect("/ai", msg="接口已删除")


@router.post("/ai/providers/{provider_id}/test")
async def test_ai_provider(
    provider_id: int,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    ent = _require_feature(request, "ai_providers")
    provider = await session.get(AIProvider, provider_id)
    if provider is None or not _provider_visible(ent, provider):
        return _redirect("/ai", err="接口不存在")
    form = await request.form()
    vision = _text(form, "kind", 8).strip().lower() == "vision"
    result = await ai_service.test_provider(provider, vision=vision)
    label = "图像模型" if vision else "文本模型"
    if result.ok:
        return _redirect(
            "/ai",
            msg=f"{provider.name} {label}连通正常（{result.latency_ms}ms）：{result.text[:80]}",
        )
    return _redirect("/ai", err=f"{provider.name} {label}测试失败：{result.error[:200]}")
