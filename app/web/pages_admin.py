"""Global blacklist, report review and statistics routes, registered on the shared panel router."""

from __future__ import annotations

from datetime import timedelta
from fastapi import Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from .auth import require_admin
from .form_values import text_value as _text
from ..db import get_session
from ..models import AIUsage, GlobalBlacklist, Group, GroupDailyStat, KeywordHit, Report, utcnow
from ..services import ai as ai_service, repo
from .pages import _accessible_group_ids, _optional_int, _redirect, _render, _require_feature, router


# ---------------------------------------------------------------------------
# Global blacklist / reports / statistics
# ---------------------------------------------------------------------------
@router.get("/blacklist", response_class=HTMLResponse)
async def blacklist_page(
    request: Request,
    q: str = "",
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    ent = _require_feature(request, "global_blacklist")
    stmt = select(GlobalBlacklist).order_by(GlobalBlacklist.id.desc())
    if not ent.is_superadmin and ent.user:
        stmt = stmt.where(
            or_(
                GlobalBlacklist.owner_user_id.is_(None),
                GlobalBlacklist.owner_user_id == ent.user.id,
            )
        )
    q = q.strip()
    if q:
        if q.lstrip("-").isdigit():
            stmt = stmt.where(GlobalBlacklist.user_id == int(q))
        else:
            stmt = stmt.where(GlobalBlacklist.reason.ilike(f"%{q}%"))
    entries = (await session.scalars(stmt.limit(300))).all()
    return _render(request, "blacklist.html", entries=entries, q=q)


@router.post("/blacklist/add")
async def add_blacklist(
    request: Request,
    admin_id: int = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    ent = _require_feature(request, "global_blacklist")
    form = await request.form()
    user_id = _optional_int(form, "user_id")
    if not user_id:
        return _redirect("/blacklist", err="用户 ID 无效")
    reason = _text(form, "reason", 500)
    owner_id = None if ent.is_superadmin else (ent.user.id if ent.user else None)
    existing = await session.scalar(
        select(GlobalBlacklist).where(GlobalBlacklist.user_id == user_id)
    )
    if existing:
        if not ent.is_superadmin and existing.owner_user_id != owner_id:
            return _redirect("/blacklist", err="该用户已在平台通用黑名单或其他用户名单中")
        existing.reason = reason or existing.reason
    else:
        session.add(
            GlobalBlacklist(
                user_id=user_id,
                owner_user_id=owner_id,
                reason=reason,
                added_by=admin_id,
            )
        )
    return _redirect("/blacklist", msg="已加入黑名单")


@router.post("/blacklist/{entry_id}/delete")
async def delete_blacklist(
    entry_id: int,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    ent = _require_feature(request, "global_blacklist")
    entry = await session.get(GlobalBlacklist, entry_id)
    if entry is None:
        return _redirect("/blacklist", err="记录不存在")
    if not ent.is_superadmin and (ent.user is None or entry.owner_user_id != ent.user.id):
        return _redirect("/blacklist", err="只能删除自己添加的黑名单记录")
    await session.delete(entry)
    return _redirect("/blacklist", msg="已移出黑名单")


@router.get("/reports", response_class=HTMLResponse)
async def reports_page(
    request: Request,
    group_id: int | None = None,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    _require_feature(request, "reports")
    allowed = await _accessible_group_ids(session, request)
    stmt = select(Report).order_by(Report.id.desc())
    if allowed is not None:
        stmt = stmt.where(Report.group_id.in_(allowed))
    if group_id:
        stmt = stmt.where(Report.group_id == group_id)
    reports = (await session.scalars(stmt.limit(300))).all()
    gids = {item.group_id for item in reports}
    groups = (await session.scalars(select(Group).where(Group.id.in_(gids)))).all() if gids else []
    group_stmt = select(Group).order_by(Group.title.asc())
    if allowed is not None:
        group_stmt = group_stmt.where(Group.id.in_(allowed))
    all_groups = (await session.scalars(group_stmt)).all()
    return _render(
        request,
        "reports.html",
        reports=reports,
        group_map={g.id: g for g in groups},
        all_groups=all_groups,
        group_id=group_id,
    )


@router.post("/reports/{entry_id}/delete")
async def delete_report(
    entry_id: int,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    _require_feature(request, "reports")
    entry = await session.get(Report, entry_id)
    if entry is None:
        return _redirect("/reports", msg="举报记录已删除")
    allowed = await _accessible_group_ids(session, request)
    if allowed is not None and entry.group_id not in allowed:
        raise HTTPException(status_code=403, detail="无权删除其他群组的举报")
    await session.delete(entry)
    return _redirect("/reports", msg="举报记录已删除")


@router.post("/reports/{entry_id}/review")
async def review_report(
    entry_id: int,
    request: Request,
    admin_id: int = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    _require_feature(request, "reports")
    report = await session.get(Report, entry_id)
    if report is None:
        raise HTTPException(status_code=404, detail="举报记录不存在")
    allowed = await _accessible_group_ids(session, request)
    if allowed is not None and report.group_id not in allowed:
        raise HTTPException(status_code=403, detail="无权审核其他群组的举报")
    form = await request.form()
    decision = _text(form, "decision", 16)
    note = _text(form, "note", 500)
    if decision not in {"confirmed", "rejected"}:
        return _redirect("/reports", err="未知审核操作")
    if report.review_status != "pending":
        return _redirect("/reports", err="该举报已审核")
    excerpt = (report.excerpt or "").strip()
    if decision == "confirmed" and not excerpt:
        return _redirect("/reports", err="举报没有原文内容，无法写入广告学习")
    report.review_status = decision
    report.reviewed_by = admin_id
    report.reviewed_at = utcnow()
    report.review_note = note
    if decision == "confirmed":
        await ai_service.learning_store.put(
            "ad",
            report.group_id,
            excerpt,
            note or "管理员确认举报为广告",
            meta={
                "provider": "admin-report-review",
                "is_ad": True,
                "reason": note or "管理员确认举报为广告",
                "reviewed_by": admin_id,
                "report_id": report.id,
            },
        )
    elif excerpt:
        await ai_service.learning_store.put(
            "ad",
            report.group_id,
            excerpt,
            note or "管理员驳回举报，判定为正常消息",
            meta={
                "provider": "admin-report-review",
                "is_ad": False,
                "reason": note or "管理员驳回举报，判定为正常消息",
                "reviewed_by": admin_id,
                "report_id": report.id,
            },
        )
    await repo.add_log(
        session,
        report.group_id,
        admin_id,
        f"report_{decision}",
        report.target_user_id,
        f"举报 #{report.id} {note}".strip(),
    )
    return _redirect(
        "/reports",
        msg="已确认并写入 AI 广告学习" if decision == "confirmed" else "已驳回举报",
    )


@router.get("/stats", response_class=HTMLResponse)
async def stats_page(
    request: Request, session: AsyncSession = Depends(get_session)
) -> HTMLResponse:
    _require_feature(request, "stats_global")
    allowed = await _accessible_group_ids(session, request)
    days = 30
    start_dt = utcnow() - timedelta(days=days - 1)
    start = start_dt.strftime("%Y-%m-%d")
    stat_stmt = select(GroupDailyStat).where(GroupDailyStat.day >= start)
    if allowed is not None:
        stat_stmt = stat_stmt.where(GroupDailyStat.group_id.in_(allowed))
    rows = (await session.scalars(stat_stmt)).all()
    by_day: dict[str, dict[str, int]] = {}
    for offset in range(days - 1, -1, -1):
        key = (utcnow() - timedelta(days=offset)).strftime("%Y-%m-%d")
        by_day[key] = {"messages": 0, "joins": 0, "leaves": 0, "bans": 0, "ai": 0, "hits": 0}
    per_group: dict[int, int] = {}
    for item in rows:
        bucket = by_day.setdefault(
            item.day, {"messages": 0, "joins": 0, "leaves": 0, "bans": 0, "ai": 0, "hits": 0}
        )
        bucket["messages"] += item.messages or 0
        bucket["joins"] += item.joins or 0
        bucket["leaves"] += item.leaves or 0
        bucket["bans"] += item.bans or 0
        bucket["ai"] += item.ai_requests or 0
        bucket["hits"] += item.ai_hits or 0
        per_group[item.group_id] = per_group.get(item.group_id, 0) + (item.messages or 0)
    series = [{"day": key, "label": key[5:], **value} for key, value in by_day.items()]
    max_messages = max([point["messages"] for point in series] or [1]) or 1

    top_ids = sorted(per_group, key=per_group.get, reverse=True)[:10]
    top_groups = (
        (await session.scalars(select(Group).where(Group.id.in_(top_ids)))).all() if top_ids else []
    )
    top_map = {g.id: g for g in top_groups}
    top_rows = [
        {"group": top_map.get(gid), "messages": per_group[gid]}
        for gid in top_ids
        if top_map.get(gid)
    ]

    keyword_cond = [KeywordHit.created_at >= start_dt]
    ai_cond = [AIUsage.day >= start]
    if allowed is not None:
        keyword_cond.append(KeywordHit.group_id.in_(allowed))
        ai_cond.append(AIUsage.group_id.in_(allowed))
    source_rows = await session.execute(
        select(KeywordHit.source, func.count(KeywordHit.id))
        .where(*keyword_cond)
        .group_by(KeywordHit.source)
    )
    sources = [
        {"source": source or "未分类", "count": int(count)} for source, count in source_rows.all()
    ]
    total_hits = int(
        await session.scalar(select(func.count(KeywordHit.id)).where(*keyword_cond)) or 0
    )
    ai_rows = (
        await session.scalars(select(AIUsage).where(*ai_cond).order_by(AIUsage.day.desc()))
    ).all()
    ai_requests = sum(item.requests or 0 for item in ai_rows)
    ai_hits = sum(item.hits or 0 for item in ai_rows)
    ai_samples = sum(item.samples or 0 for item in ai_rows)
    return _render(
        request,
        "stats.html",
        series=series,
        max_messages=max_messages,
        total_messages=sum(p["messages"] for p in series),
        total_joins=sum(p["joins"] for p in series),
        total_leaves=sum(p["leaves"] for p in series),
        total_bans=sum(p["bans"] for p in series),
        top_rows=top_rows,
        sources=sources,
        total_hits=total_hits,
        ai_requests=ai_requests,
        ai_hits=ai_hits,
        ai_samples=ai_samples,
        days=days,
    )


@router.get("/favicon.svg", include_in_schema=False)
async def favicon() -> Response:
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
        '<rect width="64" height="64" rx="14" fill="#0b1020"/>'
        '<path d="M32 8l20 8v16c0 13-8 20-20 24C20 52 12 45 12 32V16z" '
        'fill="none" stroke="#4f8cff" stroke-width="4"/>'
        '<path d="M22 32l7 7 14-15" fill="none" stroke="#22d3a6" stroke-width="5" '
        'stroke-linecap="round" stroke-linejoin="round"/></svg>'
    )
    return Response(content=svg, media_type="image/svg+xml")
