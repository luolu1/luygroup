"""Login, console account, recharge and subscription routes."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from fastapi import Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Any
from .auth import current_admin_id, hash_console_token, new_console_token, safe_next
from .form_values import int_value as _int
from ..config import get_config
from ..db import get_session
from ..models import ConsoleUser, ConsoleGroupAccess, Subscription, SubscriptionPlan, AccountLedgerEntry, utcnow
from .pages import _optional_int, _redirect, _render, _require_superadmin, public_router, router


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------
@public_router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request) -> Response:
    if current_admin_id(request) is not None:
        return RedirectResponse("/", status_code=303)
    return _render(
        request,
        "login.html",
        error="",
        admin_id_input="",
        next_url=safe_next(request.query_params.get("next")),
    )


async def _login_with_token(
    request: Request,
    token: str,
    session: AsyncSession,
    next_url: str,
) -> Response:
    token = token.strip()
    if not token:
        return _render(
            request,
            "login.html",
            error="请输入控制台 Token。",
            admin_id_input="",
            next_url=next_url,
        )
    consumed = await session.execute(
        update(ConsoleUser)
        .where(
            ConsoleUser.token_hash == hash_console_token(token),
            ConsoleUser.is_active.is_(True),
            ConsoleUser.token_expires_at.is_not(None),
            ConsoleUser.token_expires_at > utcnow(),
        )
        .values(
            token_hash=hash_console_token(new_console_token()),
            token_value=None,
            token_expires_at=None,
            last_login_at=utcnow(),
        )
        .returning(ConsoleUser.id, ConsoleUser.role)
    )
    identity = consumed.one_or_none()
    if identity is None:
        return _render(
            request,
            "login.html",
            error="登录码无效、已过期或已被使用。",
            admin_id_input="",
            next_url=next_url,
        )
    request.session.clear()
    request.session["admin_id"] = int(identity.id)
    request.session["console_role"] = identity.role
    request.session["console_auth"] = True
    return RedirectResponse(next_url, status_code=303)


@public_router.get("/console", response_class=HTMLResponse)
async def console_code_page(
    request: Request,
    token: str = "",
    session: AsyncSession = Depends(get_session),
) -> Response:
    if not token:
        return RedirectResponse("/login", status_code=303)
    response = await _login_with_token(request, token, session, "/")
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@public_router.post("/login")
async def login_submit(request: Request, session: AsyncSession = Depends(get_session)):
    form = await request.form()
    token = str(form.get("token") or "").strip()
    next_url = safe_next(str(form.get("next") or request.query_params.get("next") or "/"))
    return await _login_with_token(request, token, session, next_url)


@public_router.post("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


async def _users_context(session: AsyncSession, q: str = "") -> dict[str, Any]:
    query = select(ConsoleUser).order_by(ConsoleUser.id)
    q = q.strip()
    if q:
        conditions = [
            ConsoleUser.display_name.ilike(f"%{q}%"),
            ConsoleUser.username.ilike(f"%{q}%"),
        ]
        telegram_id = q.lstrip("-")
        if telegram_id.isdigit():
            conditions.append(ConsoleUser.telegram_id == int(q))
        query = query.where(or_(*conditions))
    users = (await session.scalars(query)).all()
    plans = (
        await session.scalars(
            select(SubscriptionPlan)
            .where(SubscriptionPlan.enabled.is_(True))
            .order_by(SubscriptionPlan.id.asc())
        )
    ).all()
    plan_by_id = {plan.id: plan for plan in plans}
    now = utcnow()
    subscription_by_user: dict[int, Subscription] = {}
    if users:
        rows = (
            await session.scalars(
                select(Subscription)
                .where(
                    Subscription.user_id.in_([u.id for u in users]),
                    Subscription.status == "active",
                    or_(Subscription.expires_at.is_(None), Subscription.expires_at > now),
                )
                .order_by(Subscription.id.desc())
            )
        ).all()
        for item in rows:
            subscription_by_user.setdefault(item.user_id, item)
    binding_count_by_user: dict[int, int] = {}
    if users:
        counts = await session.execute(
            select(ConsoleGroupAccess.user_id, func.count(ConsoleGroupAccess.id))
            .where(ConsoleGroupAccess.user_id.in_([user.id for user in users]))
            .group_by(ConsoleGroupAccess.user_id)
        )
        binding_count_by_user = {int(user_id): int(count) for user_id, count in counts.all()}
    return {
        "users": users,
        "plans": plans,
        "plan_by_id": plan_by_id,
        "subscription_by_user": subscription_by_user,
        "binding_count_by_user": binding_count_by_user,
        "binding_count_total": sum(binding_count_by_user.values()),
        "q": q,
    }


@router.get("/users", response_class=HTMLResponse)
async def users_page(
    request: Request,
    q: str = "",
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    _require_superadmin(request)
    return _render(
        request,
        "users.html",
        **(await _users_context(session, q)),
        issued_url="",
    )


@router.post("/users/{user_id}/role")
async def update_user_role(
    user_id: int,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    _require_superadmin(request)
    q = str(request.query_params.get("q") or "").strip()
    user = await session.get(ConsoleUser, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    if user.role == "superadmin":
        return _redirect("/users", err="不能在这里修改超级管理员角色", q=q)
    form = await request.form()
    role = str(form.get("role") or "user")
    if role not in {"user", "subscriber"}:
        role = "user"
    user.role = role
    return _redirect("/users", msg=f"已更新 {user.display_name or user.id} 的角色", q=q)


@router.post("/users/{user_id}/subscription")
async def update_user_subscription(
    user_id: int,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    _require_superadmin(request)
    q = str(request.query_params.get("q") or "").strip()
    user = await session.get(ConsoleUser, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    if user.role == "superadmin":
        return _redirect("/users", err="超级管理员不需要订阅", q=q)
    form = await request.form()
    action = str(form.get("action") or "activate")
    active_rows = (
        await session.scalars(
            select(Subscription).where(
                Subscription.user_id == user.id,
                Subscription.status == "active",
            )
        )
    ).all()
    if action == "cancel":
        for item in active_rows:
            item.status = "cancelled"
        user.role = "user"
        return _redirect("/users", msg="订阅已取消，用户已恢复为基础权限", q=q)
    plan_id = _optional_int(form, "plan_id")
    plan = await session.get(SubscriptionPlan, plan_id) if plan_id else None
    if plan is None or not plan.enabled:
        return _redirect("/users", err="请选择有效的订阅套餐", q=q)
    days = _int(form, "days", 30, 1, 3650)
    expires_at = utcnow() + timedelta(days=days)
    for item in active_rows:
        item.status = "expired"
    session.add(
        Subscription(
            user_id=user.id,
            plan_id=plan.id,
            status="active",
            starts_at=utcnow(),
            expires_at=expires_at,
        )
    )
    return _redirect(
        "/users",
        msg=f"已为 {user.display_name or user.id} 开通 {plan.label or plan.name}，群组上限为 5，到期 {expires_at:%Y-%m-%d}",
        q=q,
    )


@router.post("/users/{user_id}/toggle")
async def toggle_console_user(
    user_id: int,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    _require_superadmin(request)
    q = str(request.query_params.get("q") or "").strip()
    user = await session.get(ConsoleUser, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    if user.role == "superadmin":
        return _redirect("/users", err="不能停用超级管理员", q=q)
    user.is_active = not bool(user.is_active)
    return _redirect("/users", msg="用户状态已更新", q=q)


@router.post("/users/{user_id}/balance")
async def update_user_balance(
    user_id: int,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    _require_superadmin(request)
    q = str(request.query_params.get("q") or "").strip()
    user = await session.get(ConsoleUser, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    if user.role == "superadmin":
        return _redirect("/users", err="不能修改超级管理员余额", q=q)
    raw = str((await request.form()).get("balance") or "").strip()
    try:
        amount = Decimal(raw)
        if not amount.is_finite() or amount < 0 or amount.as_tuple().exponent < -2:
            raise InvalidOperation
        cents = int((amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    except (InvalidOperation, ValueError):
        return _redirect("/users", err="余额必须是非负金额，最多保留两位小数", q=q)
    previous = int(user.balance_cents or 0)
    if cents == previous:
        return _redirect("/users", msg="用户余额未变化", q=q)
    changed = await session.scalar(
        update(ConsoleUser)
        .where(ConsoleUser.id == user.id, ConsoleUser.balance_cents == previous)
        .values(balance_cents=cents)
        .returning(ConsoleUser.id)
    )
    if changed is None:
        return _redirect("/users", err="余额已被其他操作修改，请刷新后重试", q=q)
    session.add(
        AccountLedgerEntry(
            user_id=user.id,
            kind="adjustment",
            amount_cents=cents - previous,
            balance_after_cents=cents,
            description=f"管理员手动调整余额（操作者 {request.session.get('admin_id', '')}）"[:255],
        )
    )
    return _redirect("/users", msg="用户余额已更新", q=q)


@router.post("/users/{user_id}/token", response_class=HTMLResponse)
async def reset_console_token(
    user_id: int,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    _require_superadmin(request)
    q = str(request.query_params.get("q") or "").strip()
    user = await session.get(ConsoleUser, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    token = new_console_token()
    user.token_hash = hash_console_token(token)
    user.token_value = None
    user.token_expires_at = utcnow() + timedelta(minutes=15)
    url = f"{get_config().web.base_url.rstrip('/')}/console?token={token}"
    return _render(
        request,
        "users.html",
        **(await _users_context(session, q)),
        issued_url=url,
    )
