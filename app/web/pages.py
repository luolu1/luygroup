"""Server-rendered control panel routes."""
from __future__ import annotations

import json
import logging
import re
import time
from datetime import timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlsplit

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)
from fastapi.templating import Jinja2Templates
from sqlalchemy import case, delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_config, save_learning_dir
from ..db import get_session
from ..entitlements import (
    EMPTY_ENTITLEMENTS,
    FEATURE_LABELS,
    FIELD_FEATURES,
    TAB_FEATURES,
    Entitlements,
    group_owner_user_id,
)
from ..models import (
    Admin,
    AdminLog,
    AccountLedgerEntry,
    AIEvent,
    AIProvider,
    AIUsage,
    Auction,
    CrossLink,
    ConsoleUser,
    ConsoleGroupAccess,
    GlobalBlacklist,
    Group,
    GroupDailyStat,
    Keyword,
    KeywordHit,
    Lottery,
    LotteryEntry,
    LotteryWinner,
    Member,
    PaymentChannel,
    PaymentOrder,
    RedPacket,
    Report,
    ReportLink,
    RequiredChannel,
    ShopItem,
    Subscription,
    SubscriptionPlan,
    Verification,
    Warning,
    Whitelist,
    day_key,
    utcnow,
)
from ..services import ai as ai_service
from ..services import repo
from ..services.payments import service as payment_service
from ..services.subscriptions import MONTHLY_PRICE_CENTS, purchase_month
from .auth import LoginRequired, current_admin_id, hash_console_token, new_console_token, require_admin, safe_next
from .utils import fmt_date, fmt_dt, fmt_money, n, short, user_label

TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
log = logging.getLogger(__name__)
templates.env.filters["dt"] = fmt_dt
templates.env.filters["date"] = fmt_date
templates.env.filters["money"] = fmt_money
templates.env.filters["short"] = short
templates.env.filters["n"] = n
templates.env.globals["short"] = short
templates.env.globals["n"] = n
templates.env.globals["user_label"] = user_label
templates.env.globals["provider_preset_label"] = ai_service.provider_preset_label
templates.env.globals["provider_preset"] = ai_service.provider_preset

public_router = APIRouter(tags=["auth"])
router = APIRouter(tags=["panel"], dependencies=[Depends(require_admin)])

TABS = [
    ("settings", "⚙️ 基础设置"),
    ("moderation", "🛡 反广告与警告"),
    ("ai", "🤖 AI"),
    ("join", "🚪 进群验证"),
    ("admins", "👮 协助管理"),
    ("members", "👥 成员"),
    ("warnings", "⚠️ 警告"),
    ("keywords", "🔑 关键词"),
    ("whitelist", "✅ 白名单"),
    ("channels", "📢 必关频道"),
    ("links", "🔗 联动与通报"),
    ("games", "🎁 积分玩法"),
    ("logs", "📋 管理日志"),
    ("reports", "📮 举报"),
]

# Each tab posts only its own fields. Missing fields are left untouched instead
# of being reset, so a partial form can never wipe unrelated settings.
SETTING_SCOPES: dict[str, set[str]] = {
    "general": {
        "is_active",
        "delete_command_messages",
        "auto_delete_messages",
        "rules_text",
        "welcome_enabled",
        "welcome_text",
        "log_channel_id",
        "report_enabled",
        "report_receive",
        "report_chat_id",
        "crossban_enabled",
        "crossban_mode",
        "points_enabled",
        "points_name",
        "points_per_message",
        "points_cooldown",
        "daily_bonus",
    },
    "moderation": {
        "anti_ad_enabled",
        "anti_spam_enabled",
        "block_forwarded",
        "max_message_length",
        "allowed_domains",
        "warn_limit",
        "warn_action",
        "mute_minutes",
        "flood_limit",
        "flood_window",
        "flood_action",
    },
    "ai": {
        "ai_ad_enabled",
        "ai_assistant_enabled",
        "ai_chat_enabled",
        "ai_daily_quota",
        "sample_daily_quota",
    },
    "join": {
        "verify_enabled",
        "verify_timeout",
        "require_username",
        "bio_block_keywords",
        "invite_redemption",
    },
}
SETTING_SCOPES["full"] = set().union(*SETTING_SCOPES.values())
SCOPE_TABS = {"general": "settings", "moderation": "moderation", "ai": "ai", "join": "join"}


def _current_entitlements(request: Request) -> Entitlements:
    ent = getattr(request.state, "entitlements", None)
    return ent if isinstance(ent, Entitlements) else EMPTY_ENTITLEMENTS


def _visible_tabs(ent: Entitlements) -> list[tuple[str, str]]:
    return [
        (key, label)
        for key, label in TABS
        if ent.can(TAB_FEATURES.get(key, ""))
    ]


def _render(request: Request, name: str, **context: Any) -> HTMLResponse:
    ent = _current_entitlements(request)
    context.setdefault("admin_id", current_admin_id(request))
    context.setdefault("tab", "")
    context.setdefault("tabs", _visible_tabs(ent))
    context.setdefault("ent", ent)
    context.setdefault("console_user", ent.user)
    context.setdefault("role_label", ent.role_label)
    context.setdefault("can", ent.can)
    context.setdefault(
        "available_balance",
        fmt_money(ent.user.balance_cents if ent.user is not None else 0),
    )
    return templates.TemplateResponse(request, name, context)


def _require_feature(request: Request, feature: str) -> Entitlements:
    ent = _current_entitlements(request)
    if not ent.can(feature):
        label = FEATURE_LABELS.get(feature, feature)
        raise HTTPException(
            status_code=403,
            detail=f"「{label}」为订阅版功能，请先开通订阅。",
        )
    return ent


def _require_superadmin(request: Request) -> Entitlements:
    ent = _current_entitlements(request)
    if not ent.is_superadmin:
        raise HTTPException(status_code=403, detail="仅超级管理员可执行此操作")
    return ent


def _subscription_message(status: str) -> str:
    return {
        "insufficient_balance": "余额不足，需要 ¥8.00 才能购买一个月订阅",
        "account_unavailable": "当前账号不可用",
        "plan_unavailable": "订阅套餐暂不可用，请稍后再试",
        "permanent_subscription": "当前账号已是长期订阅，无需重复购买",
    }.get(status, "订阅购买失败，请稍后再试")


async def _accessible_group_ids(
    session: AsyncSession, request: Request
) -> set[int] | None:
    """Return the groups a non-superadmin may see; ``None`` means all."""
    ent = _current_entitlements(request)
    if ent.is_superadmin:
        return None
    if ent.user is None:
        return set()
    rows = await session.scalars(
        select(ConsoleGroupAccess.group_id).where(
            ConsoleGroupAccess.user_id == ent.user.id
        )
    )
    return {int(gid) for gid in rows.all()}


def _redirect(url: str, *, msg: str = "", err: str = "", **params: Any) -> RedirectResponse:
    data = {k: v for k, v in params.items() if v not in (None, "")}
    if msg:
        data["msg"] = msg
    if err:
        data["err"] = err
    if data:
        url = f"{url}?{urlencode(data)}"
    return RedirectResponse(url, status_code=303)


def _text(form: Any, key: str, limit: int = 4000, default: str = "") -> str:
    value = form.get(key)
    if value is None:
        return default
    return str(value).strip()[:limit]


def _int(form: Any, key: str, default: int, low: int = 0, high: int = 2_000_000_000) -> int:
    raw = form.get(key)
    try:
        value = int(str(raw))
    except (TypeError, ValueError):
        return default
    return max(low, min(high, value))


def _optional_int(form: Any, key: str) -> int | None:
    raw = str(form.get(key) or "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _bool(form: Any, key: str) -> bool:
    return form.get(key) is not None


async def _group_or_404(session: AsyncSession, group_id: int) -> Group:
    group = await session.scalar(
        select(Group).where(
            Group.id == int(group_id),
            Group.chat_type.in_(("group", "supergroup")),
        )
    )
    if group is None:
        raise HTTPException(status_code=404, detail="群组不存在")
    return group


async def _require_group_access(
    session: AsyncSession, request: Request, group: Group
) -> None:
    ent = _current_entitlements(request)
    if ent.is_superadmin:
        return
    user_id = ent.user.id if ent.user else None
    access = await session.scalar(
        select(ConsoleGroupAccess).where(
            ConsoleGroupAccess.user_id == user_id,
            ConsoleGroupAccess.group_id == group.id,
        )
    )
    if access is None:
        raise HTTPException(status_code=404, detail="群组不存在")


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------
@public_router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request) -> Response:
    if current_admin_id(request) is not None:
        return RedirectResponse("/", status_code=303)
    return _render(request, "login.html", error="", admin_id_input="", next_url=safe_next(request.query_params.get("next")))


async def _login_with_token(
    request: Request,
    token: str,
    session: AsyncSession,
    next_url: str,
) -> Response:
    token = token.strip()
    if not token:
        return _render(request, "login.html", error="请输入控制台 Token。", admin_id_input="", next_url=next_url)
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
        return _render(request, "login.html", error="登录码无效、已过期或已被使用。", admin_id_input="", next_url=next_url)
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
        binding_count_by_user = {
            int(user_id): int(count) for user_id, count in counts.all()
        }
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
    user.role = "subscriber"
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
    user.balance_cents = cents
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


# ---------------------------------------------------------------------------
# Dashboard / groups
# ---------------------------------------------------------------------------
@router.get("/account", response_class=HTMLResponse)
async def account_page(
    request: Request, session: AsyncSession = Depends(get_session)
) -> HTMLResponse:
    ent = _current_entitlements(request)
    if ent.user is None:
        raise LoginRequired
    await payment_service.expire_pending_orders(session, ent.user.id)
    await session.commit()
    subscription = await session.scalar(
        select(Subscription)
        .where(Subscription.user_id == ent.user.id, Subscription.status == "active")
        .order_by(Subscription.id.desc())
        .limit(1)
    )
    plan = await session.get(SubscriptionPlan, subscription.plan_id) if subscription else None
    pay_cfg = payment_service.payment_config()
    bills = await session.scalars(
        select(AccountLedgerEntry)
        .where(AccountLedgerEntry.user_id == ent.user.id)
        .order_by(AccountLedgerEntry.id.desc())
        .limit(500)
    )
    orders = await session.scalars(
        select(PaymentOrder)
        .where(PaymentOrder.user_id == ent.user.id)
        .order_by(PaymentOrder.id.desc())
        .limit(200)
    )
    lottery_rows = (
        await session.execute(
            select(Lottery, Group.title)
            .outerjoin(Group, Group.id == Lottery.group_id)
            .where(
                Lottery.creator_id == int(ent.user.id),
                Lottery.hidden_at.is_(None),
            )
            .order_by(Lottery.id.desc())
            .limit(200)
        )
    ).all()
    lottery_ids = [int(lottery.id) for lottery, _group_title in lottery_rows]
    entry_counts: dict[int, int] = {}
    winner_ids: dict[int, list[int]] = {}
    if lottery_ids:
        entry_counts = {
            int(lottery_id): int(count)
            for lottery_id, count in (
                await session.execute(
                    select(LotteryEntry.lottery_id, func.count(LotteryEntry.id))
                    .where(LotteryEntry.lottery_id.in_(lottery_ids))
                    .group_by(LotteryEntry.lottery_id)
                )
            ).all()
        }
        for lottery_id, user_id in (
            await session.execute(
                select(LotteryWinner.lottery_id, LotteryWinner.user_id).where(
                    LotteryWinner.lottery_id.in_(lottery_ids)
                )
            )
        ).all():
            winner_ids.setdefault(int(lottery_id), []).append(int(user_id))
    my_lotteries = [
        {
            "lottery": lottery,
            "group_title": group_title or str(lottery.group_id),
            "entry_count": entry_counts.get(int(lottery.id), 0),
            "winner_ids": winner_ids.get(int(lottery.id), []),
        }
        for lottery, group_title in lottery_rows
    ]
    payments_enabled = await payment_service.checkout_enabled(session)
    return _render(
        request,
        "account.html",
        subscription=subscription,
        plan=plan,
        now_utc=utcnow(),
        monthly_price_cents=MONTHLY_PRICE_CENTS,
        bills=list(bills.all()),
        orders=list(orders.all()),
        my_lotteries=my_lotteries,
        payments_enabled=payments_enabled,
        min_recharge=pay_cfg.min_amount_cents,
        max_recharge=pay_cfg.max_amount_cents,
    )


@router.post("/account/subscribe")
async def account_subscribe(
    request: Request, session: AsyncSession = Depends(get_session)
) -> RedirectResponse:
    ent = _current_entitlements(request)
    if ent.user is None:
        raise LoginRequired
    if ent.is_superadmin:
        return _redirect("/account", err="超级管理员无需购买订阅")
    status, expires_at, new_balance = await purchase_month(session, ent.user.id)
    if status != "ok":
        return _redirect("/account", err=_subscription_message(status))
    return _redirect(
        "/account",
        msg=f"订阅已购买/续费至 {expires_at:%Y-%m-%d}，扣款 ¥8.00，余额 ¥{new_balance / 100:.2f}",
    )


def _parse_amount_cents(raw: str) -> int | None:
    try:
        raw_amount = Decimal(raw.strip())
        if not raw_amount.is_finite():
            return None
        amount = raw_amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, AttributeError):
        return None
    if amount <= 0 or amount != raw_amount:
        return None
    return int(amount * 100)


@router.post("/account/recharge")
async def account_recharge(
    request: Request, session: AsyncSession = Depends(get_session)
) -> RedirectResponse:
    ent = _current_entitlements(request)
    if ent.user is None:
        raise LoginRequired
    if ent.is_superadmin:
        return _redirect("/account", err="超级管理员无需充值")
    raw = str((await request.form()).get("amount") or "")
    cents = _parse_amount_cents(raw)
    if cents is None:
        return _redirect("/account", err="请输入有效的充值金额，最多两位小数")
    order, error = await payment_service.create_recharge_order(session, ent.user.id, cents)
    if order is None:
        return _redirect("/account", err=error)
    await session.commit()
    provider = await payment_service.provider_for_order(session, order)
    notify_url, return_url = await payment_service.checkout_urls_for_order(session, order)
    creation, error = await payment_service.start_checkout(
        provider,
        order,
        request.client.host if request.client else "0.0.0.0",
        notify_url,
        return_url,
    )
    if creation is None:
        await session.commit()
        return _redirect("/account", err=f"发起支付失败：{error}")
    await session.commit()
    return RedirectResponse(creation.pay_url, status_code=303)


@router.get("/account/recharge/return")
async def account_recharge_return(
    request: Request, session: AsyncSession = Depends(get_session)
) -> RedirectResponse:
    ent = _current_entitlements(request)
    if ent.user is None:
        raise LoginRequired
    order_no = str(request.query_params.get("out_trade_no") or "").strip()
    if not order_no:
        return _redirect("/account", err="缺少订单号")
    order = await session.scalar(
        select(PaymentOrder).where(
            PaymentOrder.order_no == order_no,
            PaymentOrder.user_id == ent.user.id,
        )
    )
    if order is None:
        return _redirect("/account", err="订单不存在")
    if order.status == "paid":
        return _redirect("/account", msg=f"充值 ¥{order.amount_cents / 100:.2f} 已到账")
    return _redirect(
        "/account",
        msg="支付结果以支付平台异步通知为准，到账后余额会自动更新，请稍后刷新。",
    )


@router.post("/account/orders/{order_no}/pay")
async def account_order_pay(
    order_no: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    ent = _current_entitlements(request)
    if ent.user is None:
        raise LoginRequired
    await payment_service.expire_pending_orders(session, ent.user.id)
    order = await session.scalar(
        select(PaymentOrder).where(
            PaymentOrder.order_no == order_no,
            PaymentOrder.user_id == ent.user.id,
        )
    )
    if order is None:
        return _redirect("/account", err="订单不存在")
    if order.status != "pending":
        return _redirect("/account", err="订单已支付或已过期，不能重复付款")
    if order.expires_at is None or order.expires_at <= utcnow():
        order.status = "expired"
        await session.commit()
        return _redirect("/account", err="订单已过期，请重新发起支付")
    try:
        provider = await payment_service.provider_for_order(session, order)
        notify_url, return_url = await payment_service.checkout_urls_for_order(session, order)
        creation, error = await payment_service.start_checkout(
            provider,
            order,
            request.client.host if request.client else "0.0.0.0",
            notify_url,
            return_url,
        )
    except ValueError as exc:
        return _redirect("/account", err=str(exc))
    if creation is None:
        await session.commit()
        return _redirect("/account", err=f"发起支付失败：{error}")
    await session.commit()
    return RedirectResponse(creation.pay_url, status_code=303)


@router.post("/account/subscribe/pay")
async def account_subscribe_pay(
    request: Request, session: AsyncSession = Depends(get_session)
) -> RedirectResponse:
    ent = _current_entitlements(request)
    if ent.user is None:
        raise LoginRequired
    if ent.is_superadmin:
        return _redirect("/account", err="超级管理员无需购买订阅")
    order, error = await payment_service.create_subscription_order(session, ent.user.id)
    if order is None:
        return _redirect("/account", err=error)
    await session.commit()
    provider = await payment_service.provider_for_order(session, order)
    notify_url, return_url = await payment_service.checkout_urls_for_order(session, order)
    creation, error = await payment_service.start_checkout(
        provider,
        order,
        request.client.host if request.client else "0.0.0.0",
        notify_url,
        return_url,
    )
    if creation is None:
        await session.commit()
        return _redirect("/account", err=f"发起支付失败：{error}")
    await session.commit()
    return RedirectResponse(creation.pay_url, status_code=303)


@public_router.api_route("/payments/epay/notify", methods=["GET", "POST"])
async def epay_notify(
    request: Request, session: AsyncSession = Depends(get_session)
) -> Response:
    if request.method == "POST":
        form = await request.form()
        form_items = list(form.multi_items())
    else:
        form_items = list(request.query_params.multi_items())
    if len({key for key, _value in form_items}) != len(form_items):
        return PlainTextResponse("fail", status_code=400)
    params = {str(k): str(v) for k, v in form_items}
    try:
        status, _order = await payment_service.handle_notification(
            session, None, params
        )
    except ValueError as exc:
        log.info("Rejected Epay notification: %s", exc)
        return PlainTextResponse("fail", status_code=400)
    except Exception:
        await session.rollback()
        log.exception("Epay notification processing failed")
        return PlainTextResponse("fail", status_code=400)
    if status in {"paid", "already_paid"}:
        return PlainTextResponse("success")
    return PlainTextResponse("fail", status_code=400)


def _payment_channel_fields(form: Any, *, existing: PaymentChannel | None = None) -> dict[str, object]:
    name = _text(form, "name", 64)
    gateway = _text(form, "gateway", 255).rstrip("/")
    merchant_id = _text(form, "merchant_id", 64)
    merchant_key = _text(form, "merchant_key", 255)
    if not name or not gateway or not merchant_id:
        raise ValueError("渠道名称、支付网关和商户 ID 必填")
    parsed_gateway = urlsplit(gateway)
    if parsed_gateway.scheme not in {"http", "https"} or not parsed_gateway.netloc:
        raise ValueError("支付网关必须是完整的 http:// 或 https:// 地址")
    if parsed_gateway.username or parsed_gateway.password:
        raise ValueError("支付网关地址不能包含用户名或密码")
    if not merchant_key and existing is None:
        raise ValueError("商户密钥必填")
    version = _text(form, "version", 8, "V1").upper()
    if version not in {"V1", "V2"}:
        raise ValueError("仅支持 V1 或 V2")
    channel_type = _text(form, "channel_type", 32, "epay")
    if channel_type != "epay":
        raise ValueError("当前仅支持 Epay 易支付渠道")
    pay_type = _text(form, "pay_type", 32, "alipay")
    if pay_type not in {"alipay", "wxpay", "qqpay", "bank", "usdt"}:
        raise ValueError("不支持的支付方式")
    interact_type = _text(form, "interact_type", 16, "qrcode")
    if interact_type not in {"qrcode", "redirect"}:
        raise ValueError("不支持的交互方式")
    currency = _text(form, "currency", 8, "CNY").upper() or "CNY"
    rate_base = _text(form, "rate_base", 16)
    rate = Decimal(rate_base) if rate_base else None
    if rate is not None and (not rate.is_finite() or rate <= 0):
        raise ValueError("汇率基数必须是大于 0 的数字")
    try:
        fee_percent = Decimal(_text(form, "fee_percent", 16, "0") or "0")
        fee_fixed = Decimal(_text(form, "fee_fixed", 16, "0") or "0")
    except InvalidOperation as exc:
        raise ValueError("手续费必须是有效数字") from exc
    if not fee_percent.is_finite() or fee_percent < 0 or fee_percent > 100:
        raise ValueError("百分比手续费须在 0 到 100 之间")
    if not fee_fixed.is_finite() or fee_fixed < 0:
        raise ValueError("固定手续费不能小于 0")
    icon = existing.icon if existing is not None else ""
    upload = form.get("icon")
    if isinstance(upload, UploadFile) and upload.filename:
        payload = upload.file.read(payment_service.ICON_MAX_BYTES + 1)
        icon = payment_service.save_icon(
            name, upload.content_type or "", payload
        )
    if _text(form, "remove_icon", 8) == "on":
        icon = ""
    return {
        "name": name,
        "icon": icon,
        "channel_type": channel_type,
        "pay_type": pay_type,
        "interact_type": interact_type,
        "sort_weight": _int(form, "sort_weight", 10, 0, 100000),
        "fee_percent": int((fee_percent * 100).quantize(Decimal("1"))),
        "fee_fixed_cents": int((fee_fixed * 100).quantize(Decimal("1"))),
        "enabled": _bool(form, "enabled"),
        "version": version,
        "gateway": gateway,
        "merchant_id": merchant_id,
        "merchant_key": merchant_key or (existing.merchant_key if existing else ""),
        "notify_url": _text(form, "notify_url", 255),
        "return_url": _text(form, "return_url", 255),
        "currency": currency,
        "rate_base": rate_base,
    }


@router.get("/payments", response_class=HTMLResponse)
async def payment_channels_page(
    request: Request,
    channel_type: str = "",
    pay_type: str = "",
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    _require_superadmin(request)
    stmt = select(PaymentChannel)
    if channel_type:
        stmt = stmt.where(PaymentChannel.channel_type == channel_type)
    if pay_type:
        stmt = stmt.where(PaymentChannel.pay_type == pay_type)
    channels = (
        await session.scalars(
            stmt.order_by(PaymentChannel.sort_weight.asc(), PaymentChannel.id.asc())
        )
    ).all()
    return _render(
        request,
        "payment_channels.html",
        channels=channels,
        channel_type=channel_type,
        pay_type=pay_type,
        channel_type_options=[("epay", "Epay 易支付")],
        pay_type_options=[
            ("alipay", "支付宝"),
            ("wxpay", "微信支付"),
            ("qqpay", "QQ 钱包"),
            ("bank", "网银支付"),
            ("usdt", "USDT"),
        ],
    )


@router.get("/payments/orders", response_class=HTMLResponse)
async def payment_orders_page(
    request: Request,
    q: str = "",
    status: str = "",
    purpose: str = "",
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    _require_superadmin(request)
    payment_stmt = (
        select(PaymentOrder, ConsoleUser)
        .join(ConsoleUser, ConsoleUser.id == PaymentOrder.user_id)
        .order_by(PaymentOrder.id.desc())
    )
    ledger_stmt = (
        select(AccountLedgerEntry, ConsoleUser)
        .join(ConsoleUser, ConsoleUser.id == AccountLedgerEntry.user_id)
        .where(AccountLedgerEntry.kind == "subscription_balance")
        .order_by(AccountLedgerEntry.id.desc())
    )
    q = q.strip()
    if q:
        user_match = (
            ConsoleUser.display_name.ilike(f"%{q}%")
            | ConsoleUser.username.ilike(f"%{q}%")
        )
        payment_stmt = payment_stmt.where(
            PaymentOrder.order_no.ilike(f"%{q}%") | user_match
        )
        ledger_stmt = ledger_stmt.where(user_match)
    include_ledger = True
    if status in {"pending", "paid", "failed", "expired"}:
        payment_stmt = payment_stmt.where(PaymentOrder.status == status)
        include_ledger = status == "paid"
    else:
        status = ""
    if purpose in {"recharge", "subscription"}:
        payment_stmt = payment_stmt.where(PaymentOrder.purpose == purpose)
        include_ledger = include_ledger and purpose == "subscription"
    else:
        purpose = ""
    payment_rows = (await session.execute(payment_stmt.limit(1000))).all()
    ledger_rows = (
        (await session.execute(ledger_stmt.limit(1000))).all() if include_ledger else []
    )
    orders: list[dict[str, Any]] = [
        {
            "user": user,
            "order_no": order.order_no,
            "purpose": order.purpose,
            "amount_cents": order.amount_cents,
            "fee_cents": order.fee_cents,
            "charge_cents": order.charge_cents or order.amount_cents + order.fee_cents,
            "charge_currency": order.charge_currency,
            "provider": order.provider,
            "pay_type": order.pay_type,
            "status": order.status,
            "created_at": order.created_at,
            "paid_at": order.paid_at,
        }
        for order, user in payment_rows
    ]
    orders.extend(
        {
            "user": user,
            "order_no": f"余额-{entry.id}",
            "purpose": "subscription_balance",
            "amount_cents": -entry.amount_cents,
            "fee_cents": 0,
            "charge_cents": -entry.amount_cents,
            "charge_currency": "CNY",
            "provider": "余额抵扣",
            "pay_type": "余额",
            "status": "paid",
            "created_at": entry.created_at,
            "paid_at": entry.created_at,
        }
        for entry, user in ledger_rows
    )
    orders.sort(key=lambda item: item["created_at"], reverse=True)
    orders = orders[:1000]
    return _render(
        request,
        "payment_orders.html",
        orders=orders,
        q=q,
        status=status,
        purpose=purpose,
    )


@router.get("/payments/new", response_class=HTMLResponse)
@router.get("/payments/{channel_id}/edit", response_class=HTMLResponse)
async def payment_channel_form(
    request: Request,
    channel_id: int | None = None,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    _require_superadmin(request)
    channel = await session.get(PaymentChannel, channel_id) if channel_id else None
    if channel_id and channel is None:
        raise HTTPException(status_code=404, detail="支付渠道不存在")
    cfg = get_config()
    return _render(
        request,
        "payment_channel_form.html",
        channel=channel,
        channel_type_options=[("epay", "Epay 易支付")],
        pay_type_options=[
            ("alipay", "支付宝"),
            ("wxpay", "微信支付"),
            ("qqpay", "QQ 钱包"),
            ("bank", "网银支付"),
            ("usdt", "USDT"),
        ],
        interact_type_options=[("qrcode", "二维码"), ("redirect", "跳转支付")],
        default_notify=f"{cfg.web.base_url.rstrip('/')}/payments/epay/notify",
        default_return=f"{cfg.web.base_url.rstrip('/')}/account/recharge/return",
    )


@router.post("/payments/new")
async def payment_channel_create(
    request: Request, session: AsyncSession = Depends(get_session)
) -> RedirectResponse:
    _require_superadmin(request)
    form = await request.form()
    try:
        fields = _payment_channel_fields(form)
    except (ValueError, InvalidOperation) as exc:
        return _redirect("/payments/new", err=str(exc))
    await payment_service.create_channel(session, fields)
    return _redirect("/payments", msg="支付渠道已新增")


@router.post("/payments/{channel_id}/update")
async def payment_channel_update(
    channel_id: int,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    _require_superadmin(request)
    channel = await session.get(PaymentChannel, channel_id)
    if channel is None:
        raise HTTPException(status_code=404, detail="支付渠道不存在")
    form = await request.form()
    try:
        fields = _payment_channel_fields(form, existing=channel)
    except (ValueError, InvalidOperation) as exc:
        return _redirect(f"/payments/{channel_id}/edit", err=str(exc))
    await payment_service.update_channel(session, channel_id, fields)
    return _redirect("/payments", msg="支付渠道已更新")


@router.post("/payments/{channel_id}/toggle")
async def payment_channel_toggle(
    channel_id: int,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    _require_superadmin(request)
    channel = await session.get(PaymentChannel, channel_id)
    if channel is None:
        raise HTTPException(status_code=404, detail="支付渠道不存在")
    channel.enabled = not channel.enabled
    return _redirect("/payments", msg="渠道状态已更新")


@router.post("/payments/{channel_id}/delete")
async def payment_channel_delete(
    channel_id: int,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    _require_superadmin(request)
    try:
        deleted = await payment_service.delete_channel(session, channel_id)
    except ValueError as exc:
        return _redirect("/payments", err=str(exc))
    if not deleted:
        raise HTTPException(status_code=404, detail="支付渠道不存在")
    return _redirect("/payments", msg="支付渠道已删除")


@public_router.get("/payments/icons/{filename}")
async def payment_channel_icon(filename: str) -> FileResponse:
    if Path(filename).name != filename:
        raise HTTPException(status_code=404, detail="图标不存在")
    path = payment_service.icons_dir() / filename
    if not path.is_file():
        raise HTTPException(status_code=404, detail="图标不存在")
    return FileResponse(path, headers={"Cache-Control": "public, max-age=86400"})


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, session: AsyncSession = Depends(get_session)) -> HTMLResponse:
    ent = _current_entitlements(request)
    gids = await _accessible_group_ids(session, request)
    group_filter = [] if gids is None else [Group.id.in_(gids)]
    member_filter = [] if gids is None else [Member.group_id.in_(gids)]
    warning_filter = [] if gids is None else [Warning.group_id.in_(gids)]

    def group_stmt(stmt: Any) -> Any:
        return stmt.where(*group_filter) if group_filter else stmt

    groups_total = int(await session.scalar(group_stmt(select(func.count(Group.id)))) or 0)
    active_groups = int(
        await session.scalar(
            group_stmt(
                select(func.count(Group.id)).where(Group.is_active.is_(True))
            )
        )
        or 0
    )
    members_total = int(
        await session.scalar(
            select(func.count(Member.id)).where(*member_filter)
            if member_filter
            else select(func.count(Member.id))
        )
        or 0
    )
    messages_total = int(
        await session.scalar(group_stmt(select(func.sum(Group.message_count)))) or 0
    )
    warnings_total = int(
        await session.scalar(
            select(func.count(Warning.id)).where(
                Warning.active.is_(True), *warning_filter
            )
        )
        or 0
    )
    if ent.can("reports"):
        reports_total = int(
            await session.scalar(
                select(func.count(Report.id)).where(*(
                    [] if gids is None else [Report.group_id.in_(gids)]
                ))
            )
            or 0
        )
    else:
        reports_total = 0
    if ent.can("global_blacklist"):
        blacklist_stmt = select(func.count(GlobalBlacklist.id))
        if not ent.is_superadmin and ent.user:
            blacklist_stmt = blacklist_stmt.where(
                or_(
                    GlobalBlacklist.owner_user_id.is_(None),
                    GlobalBlacklist.owner_user_id == ent.user.id,
                )
            )
        blacklist_total = int(await session.scalar(blacklist_stmt) or 0)
    else:
        blacklist_total = 0

    today = day_key()
    today_stmt = select(GroupDailyStat).where(GroupDailyStat.day == today)
    if gids is not None:
        today_stmt = today_stmt.where(GroupDailyStat.group_id.in_(gids))
    today_rows = (await session.scalars(today_stmt)).all()
    today_messages = sum(item.messages or 0 for item in today_rows)
    today_joins = sum(item.joins or 0 for item in today_rows)
    today_bans = sum(item.bans or 0 for item in today_rows)
    today_ai = sum(item.ai_requests or 0 for item in today_rows) if ent.can("ai") else 0
    today_hits = sum(item.ai_hits or 0 for item in today_rows) if ent.can("ai") else 0

    recent_groups = (
        await session.scalars(
            group_stmt(
                select(Group).order_by(Group.updated_at.desc(), Group.id.desc()).limit(6)
            )
        )
    ).all()
    log_stmt = select(AdminLog).order_by(AdminLog.id.desc()).limit(10)
    if gids is not None:
        log_stmt = log_stmt.where(AdminLog.group_id.in_(gids))
    recent_logs = (await session.scalars(log_stmt)).all()

    start = (utcnow() - timedelta(days=13)).strftime("%Y-%m-%d")
    stat_stmt = select(GroupDailyStat).where(GroupDailyStat.day >= start)
    if gids is not None:
        stat_stmt = stat_stmt.where(GroupDailyStat.group_id.in_(gids))
    stat_rows = (await session.scalars(stat_stmt)).all()
    by_day: dict[str, dict[str, int]] = {}
    for offset in range(13, -1, -1):
        key = (utcnow() - timedelta(days=offset)).strftime("%Y-%m-%d")
        by_day[key] = {"messages": 0, "joins": 0, "bans": 0, "ai": 0}
    for item in stat_rows:
        bucket = by_day.setdefault(
            item.day, {"messages": 0, "joins": 0, "bans": 0, "ai": 0}
        )
        bucket["messages"] += item.messages or 0
        bucket["joins"] += item.joins or 0
        bucket["bans"] += item.bans or 0
        bucket["ai"] += item.ai_requests or 0
    series = [
        {"day": key, "label": key[5:], **value}
        for key, value in by_day.items()
    ]
    max_messages = max([p["messages"] for p in series] or [1]) or 1

    return _render(
        request,
        "dashboard.html",
        groups_total=groups_total,
        active_groups=active_groups,
        members_total=members_total,
        messages_total=messages_total,
        warnings_total=warnings_total,
        reports_total=reports_total,
        blacklist_total=blacklist_total,
        today_messages=today_messages,
        today_joins=today_joins,
        today_bans=today_bans,
        today_ai=today_ai,
        today_hits=today_hits,
        recent_groups=recent_groups,
        recent_logs=recent_logs,
        series=series,
        max_messages=max_messages,
    )


@router.get("/groups", response_class=HTMLResponse)
async def groups_page(
    request: Request,
    q: str = "",
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    # 只把 Telegram 群组放进“群组管理”。私聊 chat_id 是正数，不能当作群组展示。
    stmt = select(Group).where(Group.chat_type.in_(("group", "supergroup")))
    gids = await _accessible_group_ids(session, request)
    if gids is not None:
        stmt = stmt.where(Group.id.in_(gids))
    stmt = stmt.order_by(Group.updated_at.desc(), Group.id.desc())
    q = q.strip()
    if q:
        stmt = stmt.where(or_(Group.title.ilike(f"%{q}%"), Group.username.ilike(f"%{q}%")))
    groups = (await session.scalars(stmt.limit(300))).all()
    counts: dict[int, int] = {}
    owners: dict[int, ConsoleUser] = {}
    if groups:
        rows = await session.execute(
            select(Member.group_id, func.count(Member.id))
            .where(Member.group_id.in_([g.id for g in groups]))
            .group_by(Member.group_id)
        )
        counts = {int(gid): int(count) for gid, count in rows.all()}
        owner_rows = (
            await session.execute(
                select(ConsoleGroupAccess.group_id, ConsoleUser)
                .join(ConsoleUser, ConsoleUser.id == ConsoleGroupAccess.user_id)
                .where(
                    ConsoleGroupAccess.group_id.in_([g.id for g in groups]),
                    ConsoleGroupAccess.role == "owner",
                )
                .order_by(ConsoleGroupAccess.id.asc())
            )
        ).all()
        for group_id, owner in owner_rows:
            owners.setdefault(int(group_id), owner)
    return _render(
        request,
        "groups.html",
        groups=groups,
        counts=counts,
        owners=owners,
        q=q,
        bot_username=get_config().bot.username,
    )


async def _telegram_call(
    method: str,
    params: dict[str, Any],
    *,
    timeout: float = 15,
) -> dict[str, Any]:
    """Call the Bot API and normalize errors for the panel."""
    token = get_config().bot.token
    url = f"https://api.telegram.org/bot{token}/{method}"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(url, params=params)
    except httpx.HTTPError as exc:
        raise ValueError("无法连接 Telegram，请检查服务器网络") from exc
    try:
        data = response.json()
    except ValueError as exc:
        raise ValueError("Telegram 返回了无效响应") from exc
    if not data.get("ok"):
        description = data.get("description") or "Telegram 查询失败"
        if "chat not found" in description.lower():
            raise ValueError("机器人不在该群，或群 ID/用户名不正确。请先把机器人拉进群组")
        raise ValueError(description)
    return data.get("result") or {}


_MUTE_PERMISSIONS: dict[str, bool] = {
    "can_send_messages": False,
    "can_send_audios": False,
    "can_send_documents": False,
    "can_send_photos": False,
    "can_send_videos": False,
    "can_send_video_notes": False,
    "can_send_voice_notes": False,
    "can_send_polls": False,
    "can_send_other_messages": False,
    "can_add_web_page_previews": False,
    "can_change_info": False,
    "can_invite_users": False,
    "can_pin_messages": False,
}
_UNMUTE_PERMISSIONS: dict[str, bool] = {
    "can_send_messages": True,
    "can_send_audios": True,
    "can_send_documents": True,
    "can_send_photos": True,
    "can_send_videos": True,
    "can_send_video_notes": True,
    "can_send_voice_notes": True,
    "can_send_polls": True,
    "can_send_other_messages": True,
    "can_add_web_page_previews": True,
    "can_change_info": False,
    "can_invite_users": True,
    "can_pin_messages": False,
}


async def _telegram_action(
    method: str,
    payload: dict[str, Any],
    *,
    timeout: float = 15,
) -> dict[str, Any]:
    """Call a mutating Bot API method using a JSON body."""
    token = get_config().bot.token
    url = f"https://api.telegram.org/bot{token}/{method}"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(url, json=payload)
    except httpx.HTTPError as exc:
        raise ValueError("无法连接 Telegram，请检查服务器网络") from exc
    try:
        data = response.json()
    except ValueError as exc:
        raise ValueError("Telegram 返回了无效响应") from exc
    if not data.get("ok"):
        raise ValueError(data.get("description") or "Telegram 操作失败")
    return data.get("result") or {}


def _bot_user_id() -> int:
    token = (get_config().bot.token or "").strip()
    try:
        return int(token.split(":", 1)[0])
    except (ValueError, IndexError) as exc:
        raise ValueError("机器人 Token 格式无效") from exc


async def _fetch_telegram_group(ref: str) -> dict[str, Any]:
    """Resolve a group ID or @username through the Bot API."""
    return await _telegram_call("getChat", {"chat_id": ref})


async def _fetch_bot_membership(chat_id: int) -> dict[str, Any]:
    """Return the bot's own chat member record for a group."""
    return await _telegram_call(
        "getChatMember",
        {"chat_id": int(chat_id), "user_id": _bot_user_id()},
        timeout=8,
    )


async def _fetch_user_membership(chat_id: int, user_id: int) -> dict[str, Any]:
    return await _telegram_call(
        "getChatMember",
        {"chat_id": int(chat_id), "user_id": int(user_id)},
        timeout=8,
    )


def _member_in_chat(member: dict[str, Any]) -> bool:
    status = str(member.get("status") or "")
    if status in {"creator", "administrator", "member"}:
        return True
    return status == "restricted" and bool(member.get("is_member"))


def _member_is_admin(member: dict[str, Any]) -> bool:
    return str(member.get("status") or "") in {"creator", "administrator"}


@router.post("/groups/add")
async def add_group(
    request: Request,
    admin_id: int = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    form = await request.form()
    ref = str(form.get("chat") or "").strip()
    if not ref:
        return _redirect("/groups", err="请输入群组 ID 或 @用户名")
    try:
        chat = await _fetch_telegram_group(ref)
    except ValueError as exc:
        return _redirect("/groups", err=f"无法添加群组：{exc}")

    if chat.get("type") not in {"group", "supergroup"}:
        return _redirect("/groups", err="目标必须是 Telegram 群组，频道或私聊不能添加")
    group_id = int(chat["id"])
    try:
        membership = await _fetch_bot_membership(group_id)
    except ValueError as exc:
        return _redirect("/groups", err=f"无法添加群组：{exc}")
    if not _member_in_chat(membership):
        status = str(membership.get("status") or "unknown")
        return _redirect(
            "/groups",
            err=(
                f"机器人当前不在「{chat.get('title') or group_id}」中（状态：{status}），"
                "无法接收入群事件。请先把机器人拉进该群并设为管理员。"
            ),
        )
    ent = _current_entitlements(request)
    if not ent.is_superadmin:
        telegram_id = ent.user.telegram_id if ent.user else None
        if not telegram_id:
            return _redirect("/groups", err="当前控制台账号未绑定 Telegram 身份")
        try:
            user_membership = await _fetch_user_membership(group_id, int(telegram_id))
        except ValueError as exc:
            return _redirect("/groups", err=f"无法验证你的群组权限：{exc}")
        if not _member_is_admin(user_membership):
            return _redirect(
                "/groups",
                err="只有该 Telegram 群组的管理员才能绑定群组",
            )
    existing_access = await session.scalar(
        select(ConsoleGroupAccess).where(
            ConsoleGroupAccess.user_id == admin_id,
            ConsoleGroupAccess.group_id == group_id,
        )
    )
    if not ent.is_superadmin:
        other_access = await session.scalar(
            select(ConsoleGroupAccess.id).where(
                ConsoleGroupAccess.group_id == group_id,
                ConsoleGroupAccess.user_id != admin_id,
            )
        )
        if other_access is not None:
            return _redirect("/groups", err="该群组已绑定其他控制台用户，不能重复绑定")
    if not ent.is_superadmin and existing_access is None:
        bound = int(
            await session.scalar(
                select(func.count(ConsoleGroupAccess.id)).where(
                    ConsoleGroupAccess.user_id == admin_id
                )
            )
            or 0
        )
        if ent.group_limit is not None and bound >= ent.group_limit:
            return _redirect(
                "/groups",
                err=(
                    f"当前套餐最多绑定 {ent.group_limit} 个群组。"
                    "请升级订阅或先在用户管理中调整套餐。"
                ),
            )

    group = await repo.ensure_group(
        session,
        group_id,
        title=chat.get("title") or "",
        username=chat.get("username") or "",
        chat_type=chat.get("type") or "supergroup",
    )
    if existing_access is None:
        session.add(ConsoleGroupAccess(user_id=admin_id, group_id=group.id, role="owner"))
    group.is_active = True
    await repo.add_log(
        session,
        group.id,
        admin_id,
        "web_group_add",
        group.id,
        f"手动添加群组：{group.title or group.id}",
    )
    if not _member_is_admin(membership):
        return _redirect(
            f"/groups/{group.id}",
            msg=(
                f"已添加群组：{group.title or group.id}。"
                "但机器人还不是管理员，请将其设为管理员后再启用进群验证、封禁等功能。"
            ),
        )
    return _redirect("/groups", msg=f"已添加群组：{group.title or group.id}")


@router.get("/groups/{group_id}", response_class=HTMLResponse)
async def group_detail(
    group_id: int,
    request: Request,
    tab: str = "settings",
    q: str = "",
    act: str = "",
    src: str = "",
    review: str = "",
    show_left: str = "",
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    group = await _group_or_404(session, group_id)
    await _require_group_access(session, request, group)
    ent = _current_entitlements(request)
    if tab not in TAB_FEATURES:
        tab = "settings"
    if not ent.can(TAB_FEATURES[tab]):
        label = FEATURE_LABELS.get(TAB_FEATURES[tab], "该功能")
        return _redirect(
            f"/groups/{group.id}",
            err=f"「{label}」为订阅版功能，请先开通订阅。",
            tab="settings",
        )
    q = (q or "").strip()
    show_left_flag = show_left == "1"
    context: dict[str, Any] = {
        "group": group,
        "tab": tab,
        "q": q,
        "show_left": show_left_flag,
        "bot_username": get_config().bot.username,
    }
    try:
        context["bot_member"] = await _fetch_bot_membership(group.id)
    except ValueError as exc:
        context["bot_membership_error"] = str(exc)

    if tab == "settings":
        pass
    elif tab == "admins":
        admins = (
            await session.scalars(
                select(Admin).where(Admin.group_id == group.id).order_by(Admin.id.asc())
            )
        ).all()
        uids = {item.user_id for item in admins}
        members = (
            await session.scalars(
                select(Member).where(Member.group_id == group.id, Member.user_id.in_(uids))
            )
        ).all() if uids else []
        context.update(admins=admins, member_map={m.user_id: m for m in members})
    elif tab == "members":
        stmt = select(Member).where(Member.group_id == group.id)
        if not show_left_flag:
            # Keep banned users visible so they can be unbanned; hide plain
            # leavers, who are not group members any more.  NULL is treated as
            # "in group" for rows created before the column existed.
            stmt = stmt.where(
                or_(
                    Member.is_member.is_(True),
                    Member.is_member.is_(None),
                    Member.is_banned.is_(True),
                )
            )
        if q:
            stmt = stmt.where(
                or_(
                    Member.first_name.ilike(f"%{q}%"),
                    Member.username.ilike(f"%{q}%"),
                    Member.user_id == int(q) if q.lstrip("-").isdigit() else False,
                )
            )
        context["members"] = (
            await session.scalars(stmt.order_by(Member.last_seen.desc()).limit(200))
        ).all()
        context["tracked_total"] = int(
            await session.scalar(
                select(func.count(Member.id)).where(
                    Member.group_id == group.id,
                    or_(Member.is_member.is_(True), Member.is_member.is_(None)),
                )
            )
            or 0
        )
    elif tab == "warnings":
        stmt = select(Warning).where(Warning.group_id == group.id)
        act = act if act in {"warn", "mute", "kick", "ban"} else ""
        src = src if src in {
            "manual",
            "keyword",
            "builtin",
            "ai",
            "flood",
            "forward",
            "length",
            "repeat",
        } else ""
        review = review if review in {"pending", "false_positive", "confirmed"} else ""
        if q:
            conditions = [
                Warning.code.ilike(f"%{q}%"),
                Warning.reason.ilike(f"%{q}%"),
                Warning.content.ilike(f"%{q}%"),
            ]
            if q.lstrip("-").isdigit():
                conditions.append(Warning.user_id == int(q))
            member_ids = (
                await session.scalars(
                    select(Member.user_id).where(
                        Member.group_id == group.id,
                        or_(
                            Member.first_name.ilike(f"%{q}%"),
                            Member.username.ilike(f"%{q}%"),
                        ),
                    )
                )
            ).all()
            if member_ids:
                conditions.append(Warning.user_id.in_(member_ids))
            stmt = stmt.where(or_(*conditions))
        if act:
            stmt = stmt.where(Warning.action == act)
        if src:
            stmt = stmt.where(Warning.source == src)
        if review == "pending":
            stmt = stmt.where(
                Warning.source == "ai",
                or_(
                    Warning.review_status == "",
                    Warning.review_status == "pending",
                    Warning.review_status.is_(None),
                ),
            )
        elif review:
            stmt = stmt.where(Warning.review_status == review)
        rows = (
            await session.scalars(stmt.order_by(Warning.id.desc()).limit(200))
        ).all()
        uids = {item.user_id for item in rows}
        members = (
            await session.scalars(
                select(Member).where(Member.group_id == group.id, Member.user_id.in_(uids))
            )
        ).all() if uids else []
        context.update(
            warnings=rows,
            member_map={m.user_id: m for m in members},
            act=act,
            src=src,
            review=review,
        )
    elif tab == "keywords":
        context["keywords"] = (
            await session.scalars(
                select(Keyword)
                .where(Keyword.group_id == group.id)
                .order_by(Keyword.id.desc())
            )
        ).all()
    elif tab == "whitelist":
        rows = (
            await session.scalars(
                select(Whitelist)
                .where(Whitelist.group_id == group.id)
                .order_by(Whitelist.id.desc())
            )
        ).all()
        uids = {item.user_id for item in rows}
        members = (
            await session.scalars(
                select(Member).where(Member.group_id == group.id, Member.user_id.in_(uids))
            )
        ).all() if uids else []
        context.update(whitelist=rows, member_map={m.user_id: m for m in members})
    elif tab == "channels":
        context["channels"] = (
            await session.scalars(
                select(RequiredChannel)
                .where(RequiredChannel.group_id == group.id)
                .order_by(RequiredChannel.id.asc())
            )
        ).all()
    elif tab == "logs":
        context["logs"] = (
            await session.scalars(
                select(AdminLog)
                .where(AdminLog.group_id == group.id)
                .order_by(AdminLog.id.desc())
                .limit(200)
            )
        ).all()
    elif tab == "links":
        cross = (
            await session.scalars(
                select(CrossLink).where(
                    or_(CrossLink.group_id == group.id, CrossLink.linked_group_id == group.id)
                )
            )
        ).all()
        other_ids = {
            item.linked_group_id if item.group_id == group.id else item.group_id
            for item in cross
        }
        others = (
            await session.scalars(select(Group).where(Group.id.in_(other_ids)))
        ).all() if other_ids else []
        other_map = {g.id: g for g in others}
        cross_items = [
            {
                "link": item,
                "other_id": item.linked_group_id if item.group_id == group.id else item.group_id,
                "title": (other_map.get(
                    item.linked_group_id if item.group_id == group.id else item.group_id
                ).title if other_map.get(
                    item.linked_group_id if item.group_id == group.id else item.group_id
                ) else ""),
            }
            for item in cross
        ]
        report_links = (
            await session.scalars(
                select(ReportLink)
                .where(ReportLink.group_id == group.id)
                .order_by(ReportLink.id.desc())
            )
        ).all()
        subscriber_ids = {item.subscriber_id for item in report_links}
        subscribers = (
            await session.scalars(select(Group).where(Group.id.in_(subscriber_ids)))
        ).all() if subscriber_ids else []
        context.update(
            cross_items=cross_items,
            report_links=report_links,
            subscriber_map={g.id: g for g in subscribers},
        )
    elif tab == "games":
        shop_items = (
            await session.scalars(
                select(ShopItem)
                .where(or_(ShopItem.group_id == group.id, ShopItem.group_id.is_(None)))
                .order_by(ShopItem.id.desc())
            )
        ).all()
        packets = (
            await session.scalars(
                select(RedPacket)
                .where(RedPacket.group_id == group.id)
                .order_by(RedPacket.id.desc())
                .limit(50)
            )
        ).all()
        lotteries = (
            await session.scalars(
                select(Lottery)
                .where(Lottery.group_id == group.id)
                .order_by(Lottery.id.desc())
                .limit(50)
            )
        ).all()
        auctions = (
            await session.scalars(
                select(Auction)
                .where(Auction.group_id == group.id)
                .order_by(Auction.id.desc())
                .limit(50)
            )
        ).all()
        context.update(
            shop_items=shop_items,
            packets=packets,
            lotteries=lotteries,
            auctions=auctions,
        )
    elif tab == "reports":
        context["reports"] = (
            await session.scalars(
                select(Report)
                .where(Report.group_id == group.id)
                .order_by(Report.id.desc())
                .limit(100)
            )
        ).all()
    elif tab == "ai":
        usage = (
            await session.scalars(
                select(AIUsage)
                .where(AIUsage.group_id == group.id)
                .order_by(AIUsage.day.desc())
                .limit(30)
            )
        ).all()
        events = (
            await session.scalars(
                select(AIEvent)
                .where(AIEvent.group_id == group.id)
                .order_by(AIEvent.id.desc())
                .limit(100)
            )
        ).all()
        owner_id = await group_owner_user_id(session, group.id)
        provider_stmt = select(func.count()).select_from(AIProvider).where(
            AIProvider.enabled.is_(True)
        )
        if owner_id is not None:
            provider_stmt = provider_stmt.where(
                or_(AIProvider.owner_user_id.is_(None), AIProvider.owner_user_id == owner_id)
            )
        else:
            provider_stmt = provider_stmt.where(AIProvider.owner_user_id.is_(None))
        provider_count = await session.scalar(provider_stmt)
        context.update(
            usage=usage,
            events=events,
            ai_provider_count=int(provider_count or 0),
        )
    elif tab == "moderation":
        stats = (
            await session.scalars(
                select(GroupDailyStat)
                .where(GroupDailyStat.group_id == group.id)
                .order_by(GroupDailyStat.day.desc())
                .limit(30)
            )
        ).all()
        context["stats"] = stats
    elif tab == "join":
        pending = (
            await session.scalars(
                select(Verification)
                .where(Verification.group_id == group.id)
                .order_by(Verification.id.desc())
                .limit(100)
            )
        ).all()
        context["verifications"] = pending
    else:
        tab = "settings"
        context["tab"] = tab
    return _render(request, "group.html", **context)


# ---------------------------------------------------------------------------
# Group settings / actions
# ---------------------------------------------------------------------------
@router.post("/groups/{group_id}/settings")
async def save_group_settings(
    group_id: int,
    request: Request,
    admin_id: int = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    group = await _group_or_404(session, group_id)
    await _require_group_access(session, request, group)
    ent = _current_entitlements(request)
    form = await request.form()
    scope = str(form.get("_scope") or "general")
    if scope not in {"general", "moderation", "ai", "join"}:
        scope = "general"
    if scope == "ai":
        _require_feature(request, "ai")
    wanted = SETTING_SCOPES.get(scope, SETTING_SCOPES["general"])

    def want(name: str) -> bool:
        if name not in wanted:
            return False
        feature = FIELD_FEATURES.get(name)
        return feature is None or ent.can(feature)

    bool_fields = [
        "is_active",
        "anti_ad_enabled",
        "anti_spam_enabled",
        "block_forwarded",
        "ai_ad_enabled",
        "ai_assistant_enabled",
        "ai_chat_enabled",
        "verify_enabled",
        "require_username",
        "invite_redemption",
        "welcome_enabled",
        "delete_command_messages",
        "auto_delete_messages",
        "crossban_enabled",
        "report_enabled",
        "report_receive",
        "points_enabled",
    ]
    for field in bool_fields:
        if want(field):
            setattr(group, field, _bool(form, field))

    if want("allowed_domains"):
        group.allowed_domains = _text(form, "allowed_domains", 4000)
    if want("bio_block_keywords"):
        group.bio_block_keywords = _text(form, "bio_block_keywords", 2000)
    if want("rules_text"):
        group.rules_text = _text(form, "rules_text", 8000)
    if want("welcome_text"):
        group.welcome_text = _text(form, "welcome_text", 4000, group.welcome_text or "")
    if want("points_name"):
        group.points_name = _text(form, "points_name", 32, "积分") or "积分"
    if want("crossban_mode"):
        group.crossban_mode = _text(form, "crossban_mode", 16, "shared")
        if group.crossban_mode not in {"shared", "own"}:
            group.crossban_mode = "shared"
    if want("warn_action"):
        group.warn_action = _text(form, "warn_action", 16, "mute")
        if group.warn_action not in {"mute", "kick", "ban"}:
            group.warn_action = "mute"
    if want("flood_action"):
        group.flood_action = _text(form, "flood_action", 16, "mute")
        if group.flood_action not in {"mute", "kick", "ban"}:
            group.flood_action = "mute"

    if want("max_message_length"):
        group.max_message_length = _int(
            form, "max_message_length", group.max_message_length or 0, 0, 100000
        )
    if want("warn_limit"):
        group.warn_limit = _int(form, "warn_limit", group.warn_limit or 3, 0, 1000)
    if want("mute_minutes"):
        group.mute_minutes = _int(form, "mute_minutes", group.mute_minutes or 60, 1, 100000)
    if want("flood_limit"):
        group.flood_limit = _int(form, "flood_limit", group.flood_limit or 8, 1, 100000)
    if want("flood_window"):
        group.flood_window = _int(form, "flood_window", group.flood_window or 10, 1, 86400)
    if want("ai_daily_quota"):
        group.ai_daily_quota = _int(
            form, "ai_daily_quota", group.ai_daily_quota or 0, 0, 1000000
        )
    if want("sample_daily_quota"):
        group.sample_daily_quota = _int(
            form, "sample_daily_quota", group.sample_daily_quota or 0, 0, 1000000
        )
    if want("verify_timeout"):
        group.verify_timeout = _int(form, "verify_timeout", group.verify_timeout or 300, 60, 86400)
    if want("points_per_message"):
        group.points_per_message = _int(
            form, "points_per_message", group.points_per_message or 0, 0, 100000
        )
    if want("points_cooldown"):
        group.points_cooldown = _int(
            form, "points_cooldown", group.points_cooldown or 0, 0, 86400
        )
    if want("daily_bonus"):
        group.daily_bonus = _int(form, "daily_bonus", group.daily_bonus or 0, 0, 100000)
    if want("log_channel_id"):
        group.log_channel_id = _optional_int(form, "log_channel_id")
    if want("report_chat_id"):
        group.report_chat_id = _optional_int(form, "report_chat_id")
    group.updated_at = utcnow()
    await repo.add_log(session, group.id, admin_id, "web_settings", None, f"Web 面板更新设置（{scope}）")
    tab = SCOPE_TABS.get(scope, "settings")
    return _redirect(f"/groups/{group.id}", msg="群设置已保存", tab=tab)


@router.post("/groups/{group_id}/admins/add")
async def add_group_admin(
    group_id: int,
    request: Request,
    admin_id: int = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    group = await _group_or_404(session, group_id)
    await _require_group_access(session, request, group)
    _require_feature(request, "admins")
    form = await request.form()
    user_id = _optional_int(form, "user_id")
    if not user_id:
        return _redirect(f"/groups/{group.id}", err="用户 ID 无效", tab="admins")
    role = str(form.get("role") or "assistant")
    if role not in {"owner", "admin", "assistant"}:
        role = "assistant"
    allowed = {"ban", "mute", "warn", "delete", "info", "settings"}
    permissions = sorted(set(form.getlist("permissions")) & allowed) or ["ban", "mute", "warn", "delete", "info"]
    existing = await repo.get_admin(session, group.id, user_id)
    if existing:
        existing.role = role
        existing.permissions = json.dumps(permissions)
    else:
        session.add(
            Admin(
                group_id=group.id,
                user_id=user_id,
                role=role,
                permissions=json.dumps(permissions),
                added_by=admin_id,
            )
        )
    await repo.add_log(session, group.id, admin_id, "web_admin_add", user_id, role)
    return _redirect(f"/groups/{group.id}", msg="协助管理员已保存", tab="admins")


@router.post("/groups/{group_id}/admins/{entry_id}/delete")
async def delete_group_admin(
    group_id: int,
    entry_id: int,
    request: Request,
    admin_id: int = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    group = await _group_or_404(session, group_id)
    await _require_group_access(session, request, group)
    _require_feature(request, "admins")
    entry = await session.get(Admin, entry_id)
    if entry and entry.group_id == group_id:
        await session.delete(entry)
        await repo.add_log(session, group_id, admin_id, "web_admin_del", entry.user_id, "")
    return _redirect(f"/groups/{group_id}", msg="已移除协助管理员", tab="admins")


@router.post("/groups/{group_id}/warnings/{warning_id}/review")
async def review_warning(
    group_id: int,
    warning_id: int,
    request: Request,
    admin_id: int = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    """Let an administrator judge an AI warning and learn from false positives."""
    group = await _group_or_404(session, group_id)
    await _require_group_access(session, request, group)
    _require_feature(request, "ai")
    warning = await session.get(Warning, warning_id)
    if warning is None or warning.group_id != group.id:
        raise HTTPException(status_code=404, detail="警告记录不存在")
    form = await request.form()
    decision = _text(form, "decision", 16)
    note = _text(form, "note", 500)
    filters = {
        "tab": "warnings",
        "q": _text(form, "q", 200),
        "act": _text(form, "act", 16),
        "src": _text(form, "src", 16),
        "review": _text(form, "review", 16),
    }
    if decision not in {"false_positive", "confirmed"}:
        return _redirect(f"/groups/{group.id}", err="未知审核操作", **filters)

    source = warning.source or (
        "ai" if (warning.reason or "").startswith("AI 判定为广告") else "manual"
    )
    if decision == "false_positive":
        if source != "ai":
            return _redirect(
                f"/groups/{group.id}",
                err="只有 AI 判定记录可以标记为误杀并写入学习数据",
                **filters,
            )
        content = (warning.content or "").strip()
        if not content:
            # Older records did not store the message. Recover it from the
            # keyword-hit audit row written at the same moment.
            hits = (
                await session.scalars(
                    select(KeywordHit)
                    .where(
                        KeywordHit.group_id == group.id,
                        KeywordHit.user_id == warning.user_id,
                        KeywordHit.source == "ai",
                    )
                    .order_by(KeywordHit.id.desc())
                    .limit(20)
                )
            ).all()
            for hit in hits:
                if (
                    hit.created_at
                    and warning.created_at
                    and abs((hit.created_at - warning.created_at).total_seconds()) <= 8
                ):
                    content = (hit.content or "").strip()
                    break
        if not content:
            return _redirect(
                f"/groups/{group.id}",
                err="该记录没有保存原始消息，无法写入 AI 学习数据",
                **filters,
            )
        warning.source = "ai"
        warning.content = content[:4000]
        await ai_service.learning_store.put(
            "ad",
            group.id,
            content,
            "正常消息（管理员判定为 AI 误杀）",
            meta={
                "provider": "admin-review",
                "is_ad": False,
                "reason": note or "管理员判定为 AI 误杀",
                "reviewed_by": admin_id,
            },
        )
        warning.review_status = "false_positive"
        warning.review_note = note or "管理员判定为 AI 误杀"
        warning.reviewed_by = admin_id
        warning.reviewed_at = utcnow()
        warning.active = False
        await repo.add_log(
            session,
            group.id,
            admin_id,
            "ai_false_positive",
            warning.user_id,
            f"{warning.code} {warning.review_note}".strip(),
        )
        return _redirect(
            f"/groups/{group.id}",
            msg=f"已将 {warning.code or warning.id} 标记为误杀并写入 AI 学习数据",
            **filters,
        )

    warning.source = source
    warning.review_status = "confirmed"
    warning.review_note = note
    warning.reviewed_by = admin_id
    warning.reviewed_at = utcnow()
    warning.active = True
    if source == "ai":
        content = (warning.content or "").strip()
        if content:
            await ai_service.learning_store.put(
                "ad",
                group.id,
                content,
                note or "管理员确认违规",
                meta={
                    "provider": "admin-review",
                    "is_ad": True,
                    "reason": note or "管理员确认违规",
                    "reviewed_by": admin_id,
                },
            )
            warning.content = content[:4000]
    await repo.add_log(
        session,
        group.id,
        admin_id,
        "ai_confirmed",
        warning.user_id,
        f"{warning.code} {note}".strip(),
    )
    return _redirect(
        f"/groups/{group.id}",
        msg=f"已确认 {warning.code or warning.id} 为违规",
        **filters,
    )


@router.post("/groups/{group_id}/members/sync")
async def sync_members(
    group_id: int,
    request: Request,
    admin_id: int = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    """Refresh the status of members the bot already knows about."""
    group = await _group_or_404(session, group_id)
    await _require_group_access(session, request, group)
    updated = 0
    failed = 0
    admins_added = 0

    # Admins are the one member category the Bot API can enumerate.  Adding
    # them makes the panel useful right after the bot is promoted.
    try:
        admins = await _telegram_call(
            "getChatAdministrators", {"chat_id": group.id}, timeout=10
        )
    except ValueError:
        admins = []
    if isinstance(admins, list):
        for entry in admins:
            user = entry.get("user") if isinstance(entry, dict) else None
            if not isinstance(user, dict) or user.get("is_bot"):
                continue
            uid = int(user.get("id") or 0)
            if not uid:
                continue
            existing = await repo.get_member(session, group.id, uid)
            member = await repo.ensure_member(
                session,
                group.id,
                uid,
                username=str(user.get("username") or ""),
                first_name=str(user.get("first_name") or ""),
            )
            if existing is None:
                admins_added += 1
            member.is_member = True
            member.is_verified = True
            member.is_banned = False

    rows = (
        await session.scalars(
            select(Member)
            .where(Member.group_id == group.id)
            .order_by(Member.last_seen.desc())
            .limit(200)
        )
    ).all()
    for member in rows:
        try:
            data = await _telegram_call(
                "getChatMember",
                {"chat_id": group.id, "user_id": member.user_id},
                timeout=8,
            )
        except ValueError:
            failed += 1
            continue
        status = str(data.get("status") or "") if isinstance(data, dict) else ""
        in_chat = status in {"creator", "administrator", "member"} or (
            status == "restricted" and bool(data.get("is_member"))
        )
        member.is_member = in_chat
        member.is_banned = status == "kicked"
        if not in_chat:
            member.is_verified = False
            member.is_muted = False
            member.muted_until = None
        updated += 1

    try:
        count = await _telegram_call(
            "getChatMemberCount", {"chat_id": group.id}, timeout=8
        )
        if isinstance(count, int):
            group.member_count = count
    except ValueError:
        pass

    await repo.add_log(
        session,
        group.id,
        admin_id,
        "web_members_sync",
        None,
        f"同步 {updated} 人，失败 {failed} 人，补录管理员 {admins_added} 人",
    )
    note = f"已同步 {updated} 名成员状态"
    if failed:
        note += f"，{failed} 人查询失败"
    return _redirect(f"/groups/{group.id}", msg=note, tab="members")


@router.post("/groups/{group_id}/members/{user_id}/action")
async def member_action(
    group_id: int,
    user_id: int,
    request: Request,
    admin_id: int = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    """Apply a Telegram permission change to a member from the panel."""
    group = await _group_or_404(session, group_id)
    await _require_group_access(session, request, group)
    form = await request.form()
    action = _text(form, "action", 16)
    tab = _text(form, "tab", 20, "members")
    if tab not in {"members", "warnings", "moderation"}:
        tab = "members"
    if user_id in set(get_config().bot.admin_ids) or user_id == _bot_user_id():
        return _redirect(
            f"/groups/{group.id}",
            err="不能对机器人管理员或机器人自身执行此操作",
            tab=tab,
        )

    member = await repo.get_member(session, group.id, user_id)
    if member is None:
        member = await repo.ensure_member(session, group.id, user_id)
    target = user_label(member.user_id, member.first_name, member.username)
    reason = _text(form, "reason", 255)
    detail = reason or f"网页管理面板：{target}"

    try:
        if action == "mute":
            minutes = _int(form, "minutes", group.mute_minutes or 60, 1, 100000)
            await _telegram_action(
                "restrictChatMember",
                {
                    "chat_id": group.id,
                    "user_id": user_id,
                    "permissions": _MUTE_PERMISSIONS,
                    "until_date": int(time.time()) + minutes * 60,
                    "use_independent_chat_permissions": True,
                },
            )
            member.is_muted = True
            member.muted_until = utcnow() + timedelta(minutes=minutes)
            message = f"已禁言 {target}（{minutes} 分钟）"
        elif action == "unmute":
            await _telegram_action(
                "restrictChatMember",
                {
                    "chat_id": group.id,
                    "user_id": user_id,
                    "permissions": _UNMUTE_PERMISSIONS,
                    "until_date": 0,
                    "use_independent_chat_permissions": True,
                },
            )
            member.is_muted = False
            member.muted_until = None
            member.mute_on_rejoin = False
            message = f"已解除 {target} 的禁言限制"
        elif action == "ban":
            await _telegram_action(
                "banChatMember",
                {
                    "chat_id": group.id,
                    "user_id": user_id,
                    "revoke_messages": False,
                },
            )
            member.is_banned = True
            member.is_member = False
            member.is_muted = False
            member.muted_until = None
            member.mute_on_rejoin = True
            message = f"已封禁 {target}"
        elif action == "unban":
            await _telegram_action(
                "unbanChatMember",
                {
                    "chat_id": group.id,
                    "user_id": user_id,
                    "only_if_banned": True,
                },
            )
            member.is_banned = False
            message = f"已解除 {target} 的封禁"
        elif action == "kick":
            await _telegram_action(
                "banChatMember",
                {
                    "chat_id": group.id,
                    "user_id": user_id,
                    "revoke_messages": False,
                },
            )
            await _telegram_action(
                "unbanChatMember",
                {
                    "chat_id": group.id,
                    "user_id": user_id,
                    "only_if_banned": True,
                },
            )
            member.is_member = False
            member.is_verified = False
            member.is_muted = False
            member.muted_until = None
            member.mute_on_rejoin = True
            message = f"已踢出 {target}"
        elif action == "clearwarns":
            await session.execute(
                delete(Warning).where(
                    Warning.group_id == group.id, Warning.user_id == user_id
                )
            )
            message = f"已清除 {target} 的全部警告"
        else:
            return _redirect(f"/groups/{group.id}", err="未知操作", tab=tab)
    except ValueError as exc:
        return _redirect(f"/groups/{group.id}", err=f"操作失败：{exc}", tab=tab)

    await repo.add_log(
        session,
        group.id,
        admin_id,
        f"web_member_{action}",
        user_id,
        detail,
    )
    return _redirect(f"/groups/{group.id}", msg=message, tab=tab)


@router.post("/groups/{group_id}/keywords/add")
async def add_keyword(
    group_id: int,
    request: Request,
    admin_id: int = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    group = await _group_or_404(session, group_id)
    await _require_group_access(session, request, group)
    form = await request.form()
    pattern = _text(form, "pattern", 255)
    if not pattern:
        return _redirect(f"/groups/{group.id}", err="关键词不能为空", tab="keywords")
    is_regex = _bool(form, "is_regex")
    if is_regex:
        try:
            re.compile(pattern)
        except re.error as exc:
            return _redirect(f"/groups/{group.id}", err=f"正则表达式无效：{exc}", tab="keywords")
    action = _text(form, "action", 16, "delete")
    if action not in {"delete", "warn", "mute", "kick", "ban", "reply"}:
        action = "delete"
    reply_text = _text(form, "reply_text", 4000)
    if action == "reply" and not reply_text:
        return _redirect(
            f"/groups/{group.id}", err="选择“关键词回复”时必须填写回复内容", tab="keywords"
        )
    session.add(
        Keyword(
            group_id=group.id,
            pattern=pattern,
            is_regex=is_regex,
            action=action,
            reply_text=reply_text if action == "reply" else "",
            note=_text(form, "note", 255),
            created_by=admin_id,
        )
    )
    await repo.add_log(
        session,
        group.id,
        admin_id,
        "web_keyword_add",
        None,
        f"{pattern} -> {action}",
    )
    return _redirect(f"/groups/{group.id}", msg="关键词已添加", tab="keywords")


@router.post("/groups/{group_id}/keywords/{entry_id}/delete")
async def delete_keyword(
    group_id: int,
    entry_id: int,
    request: Request,
    admin_id: int = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    group = await _group_or_404(session, group_id)
    await _require_group_access(session, request, group)
    entry = await session.get(Keyword, entry_id)
    if entry and entry.group_id == group_id:
        await session.delete(entry)
        await repo.add_log(session, group_id, admin_id, "web_keyword_del", None, entry.pattern)
    return _redirect(f"/groups/{group_id}", msg="关键词已删除", tab="keywords")


@router.post("/groups/{group_id}/whitelist/add")
async def add_whitelist(
    group_id: int,
    request: Request,
    admin_id: int = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    group = await _group_or_404(session, group_id)
    await _require_group_access(session, request, group)
    form = await request.form()
    user_id = _optional_int(form, "user_id")
    if not user_id:
        return _redirect(f"/groups/{group.id}", err="用户 ID 无效", tab="whitelist")
    exists = await session.scalar(
        select(Whitelist.id).where(
            Whitelist.group_id == group.id, Whitelist.user_id == user_id
        )
    )
    if exists:
        return _redirect(f"/groups/{group.id}", err="该用户已在白名单中", tab="whitelist")
    session.add(
        Whitelist(
            group_id=group.id,
            user_id=user_id,
            note=_text(form, "note", 255),
            added_by=admin_id,
        )
    )
    await repo.ensure_member(session, group.id, user_id)
    await repo.add_log(session, group.id, admin_id, "web_whitelist_add", user_id, "")
    return _redirect(f"/groups/{group.id}", msg="已加入白名单", tab="whitelist")


@router.post("/groups/{group_id}/whitelist/{entry_id}/delete")
async def delete_whitelist(
    group_id: int,
    entry_id: int,
    request: Request,
    admin_id: int = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    group = await _group_or_404(session, group_id)
    await _require_group_access(session, request, group)
    entry = await session.get(Whitelist, entry_id)
    if entry and entry.group_id == group_id:
        await session.delete(entry)
        await repo.add_log(session, group_id, admin_id, "web_whitelist_del", entry.user_id, "")
    return _redirect(f"/groups/{group_id}", msg="已移出白名单", tab="whitelist")


@router.post("/groups/{group_id}/channels/add")
async def add_channel(
    group_id: int,
    request: Request,
    admin_id: int = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    group = await _group_or_404(session, group_id)
    await _require_group_access(session, request, group)
    form = await request.form()
    channel_id = _optional_int(form, "channel_id")
    if not channel_id:
        return _redirect(f"/groups/{group.id}", err="频道 ID 无效", tab="channels")
    username = _text(form, "username", 255).lstrip("@")
    existing = await session.scalar(
        select(RequiredChannel).where(
            RequiredChannel.group_id == group.id,
            RequiredChannel.channel_id == channel_id,
        )
    )
    if existing:
        existing.title = _text(form, "title", 255, existing.title or "")
        existing.username = username
        existing.invite_link = _text(form, "invite_link", 512, existing.invite_link or "")
    else:
        session.add(
            RequiredChannel(
                group_id=group.id,
                channel_id=channel_id,
                title=_text(form, "title", 255),
                username=username,
                invite_link=_text(form, "invite_link", 512),
            )
        )
    await repo.add_log(session, group.id, admin_id, "web_channel_add", channel_id, "")
    return _redirect(f"/groups/{group.id}", msg="必关频道已保存", tab="channels")


@router.post("/groups/{group_id}/channels/{entry_id}/delete")
async def delete_channel(
    group_id: int,
    entry_id: int,
    request: Request,
    admin_id: int = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    group = await _group_or_404(session, group_id)
    await _require_group_access(session, request, group)
    entry = await session.get(RequiredChannel, entry_id)
    if entry and entry.group_id == group_id:
        await session.delete(entry)
        await repo.add_log(session, group_id, admin_id, "web_channel_del", entry.channel_id, "")
    return _redirect(f"/groups/{group_id}", msg="必关频道已删除", tab="channels")


@router.post("/groups/{group_id}/crosslinks/add")
async def add_crosslink(
    group_id: int,
    request: Request,
    admin_id: int = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    group = await _group_or_404(session, group_id)
    await _require_group_access(session, request, group)
    _require_feature(request, "linkage")
    form = await request.form()
    other_id = _optional_int(form, "other_group_id")
    if not other_id or other_id == group.id:
        return _redirect(f"/groups/{group.id}", err="请输入其他群组 ID", tab="links")
    allowed = await _accessible_group_ids(session, request)
    if allowed is not None and int(other_id) not in allowed:
        return _redirect(f"/groups/{group.id}", err="只能联动自己可访问的群组", tab="links")
    other = await session.get(Group, other_id)
    if other is None:
        return _redirect(f"/groups/{group.id}", err="目标群组尚未被机器人记录", tab="links")
    exists = await session.scalar(
        select(CrossLink.id).where(
            or_(
                (CrossLink.group_id == group.id) & (CrossLink.linked_group_id == other_id),
                (CrossLink.group_id == other_id) & (CrossLink.linked_group_id == group.id),
            )
        )
    )
    if not exists:
        session.add(CrossLink(group_id=group.id, linked_group_id=other_id, created_by=admin_id))
    await repo.add_log(session, group.id, admin_id, "web_crosslink_add", other_id, "")
    return _redirect(f"/groups/{group.id}", msg="联动群已添加", tab="links")


@router.post("/groups/{group_id}/crosslinks/{entry_id}/delete")
async def delete_crosslink(
    group_id: int,
    entry_id: int,
    request: Request,
    admin_id: int = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    group = await _group_or_404(session, group_id)
    await _require_group_access(session, request, group)
    _require_feature(request, "linkage")
    entry = await session.get(CrossLink, entry_id)
    if entry and group_id in {entry.group_id, entry.linked_group_id}:
        await session.delete(entry)
        await repo.add_log(session, group_id, admin_id, "web_crosslink_del", None, "")
    return _redirect(f"/groups/{group_id}", msg="联动已删除", tab="links")


@router.post("/groups/{group_id}/reportlinks/add")
async def add_reportlink(
    group_id: int,
    request: Request,
    admin_id: int = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    group = await _group_or_404(session, group_id)
    await _require_group_access(session, request, group)
    _require_feature(request, "reports")
    form = await request.form()
    other_id = _optional_int(form, "subscriber_id")
    if not other_id or other_id == group.id:
        return _redirect(f"/groups/{group.id}", err="请输入订阅群 ID", tab="links")
    allowed = await _accessible_group_ids(session, request)
    if allowed is not None and int(other_id) not in allowed:
        return _redirect(f"/groups/{group.id}", err="只能通报到自己可访问的群组", tab="links")
    other = await session.get(Group, other_id)
    if other is None:
        return _redirect(f"/groups/{group.id}", err="订阅群尚未被机器人记录", tab="links")
    exists = await session.scalar(
        select(ReportLink.id).where(
            ReportLink.group_id == group.id, ReportLink.subscriber_id == other_id
        )
    )
    if not exists:
        session.add(ReportLink(group_id=group.id, subscriber_id=other_id, created_by=admin_id))
    other.report_receive = True
    await repo.add_log(session, group.id, admin_id, "web_reportlink_add", other_id, "")
    return _redirect(f"/groups/{group.id}", msg="通报订阅者已添加", tab="links")


@router.post("/groups/{group_id}/reportlinks/{entry_id}/delete")
async def delete_reportlink(
    group_id: int,
    entry_id: int,
    request: Request,
    admin_id: int = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    group = await _group_or_404(session, group_id)
    await _require_group_access(session, request, group)
    _require_feature(request, "reports")
    entry = await session.get(ReportLink, entry_id)
    if entry and entry.group_id == group_id:
        await session.delete(entry)
        await repo.add_log(session, group_id, admin_id, "web_reportlink_del", entry.subscriber_id, "")
    return _redirect(f"/groups/{group_id}", msg="通报订阅已删除", tab="links")


@router.post("/groups/{group_id}/shop/add")
async def add_shop_item(
    group_id: int,
    request: Request,
    admin_id: int = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    group = await _group_or_404(session, group_id)
    await _require_group_access(session, request, group)
    _require_feature(request, "games")
    form = await request.form()
    name = _text(form, "name", 128)
    if not name:
        return _redirect(f"/groups/{group.id}", err="商品名称不能为空", tab="games")
    session.add(
        ShopItem(
            group_id=group.id,
            name=name,
            description=_text(form, "description", 2000),
            cost=_int(form, "cost", 10, 0, 1000000),
            stock=_int(form, "stock", -1, -1, 1000000),
            reward_type=_text(form, "reward_type", 16, "custom"),
            reward_text=_text(form, "reward_text", 4000),
            enabled=True,
        )
    )
    await repo.add_log(session, group.id, admin_id, "web_shop_add", None, name)
    return _redirect(f"/groups/{group.id}", msg="商品已上架", tab="games")


@router.post("/groups/{group_id}/shop/{entry_id}/delete")
async def delete_shop_item(
    group_id: int,
    entry_id: int,
    request: Request,
    admin_id: int = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    group = await _group_or_404(session, group_id)
    await _require_group_access(session, request, group)
    _require_feature(request, "games")
    entry = await session.get(ShopItem, entry_id)
    if entry and entry.group_id == group_id:
        await session.delete(entry)
        await repo.add_log(session, group_id, admin_id, "web_shop_del", None, entry.name)
    return _redirect(f"/groups/{group_id}", msg="商品已删除", tab="games")


@router.post("/groups/{group_id}/points")
async def adjust_group_points(
    group_id: int,
    request: Request,
    admin_id: int = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    group = await _group_or_404(session, group_id)
    await _require_group_access(session, request, group)
    _require_feature(request, "points")
    form = await request.form()
    user_id = _optional_int(form, "user_id")
    try:
        delta = int(str(form.get("delta") or "0"))
    except ValueError:
        delta = 0
    if not user_id or not delta:
        return _redirect(f"/groups/{group.id}", err="请填写有效的用户 ID 与积分变化", tab="members")
    result = await repo.adjust_points(
        session,
        group.id,
        user_id,
        delta,
        _text(form, "reason", 200) or "管理员调整",
    )
    if result is None:
        return _redirect(f"/groups/{group.id}", err="积分不足，不能扣成负数", tab="members")
    _member, balance = result
    await repo.add_log(
        session, group.id, admin_id, "web_points", user_id, f"{delta:+d} -> {balance}"
    )
    return _redirect(f"/groups/{group.id}", msg=f"积分已调整，当前余额 {balance}", tab="members")


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


@router.get("/ai/add", response_class=HTMLResponse)
async def ai_add_page(request: Request) -> HTMLResponse:
    _require_feature(request, "ai_providers")
    return _render(
        request,
        "ai_add.html",
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
    today_requests, today_samples, today_hits = (
        await session.execute(today_stmt)
    ).one()
    request_limit = ent.ai_daily_quota
    sample_limit = ent.sample_daily_quota
    if gids is not None:
        bound_groups = (
            await session.scalars(select(Group).where(Group.id.in_(gids)))
        ).all() if gids else []
        def capped(limit: int | None, configured: int | None) -> int:
            if limit is None:
                return int(configured or 0)
            configured_value = int(configured or 0)
            return int(limit) if configured_value <= 0 else min(int(limit), configured_value)

        request_limit = sum(
            capped(ent.ai_daily_quota, group.ai_daily_quota) for group in bound_groups
        )
        sample_limit = sum(
            capped(ent.sample_daily_quota, group.sample_daily_quota) for group in bound_groups
        )
    ai_summary = {
        "day": today,
        "requests": int(today_requests or 0),
        "samples": int(today_samples or 0),
        "hits": int(today_hits or 0),
        "request_limit": request_limit,
        "sample_limit": sample_limit,
    }
    group_ids = {item.group_id for item in usage} | {item.group_id for item in events}
    groups = (
        await session.scalars(select(Group).where(Group.id.in_(group_ids)))
    ).all() if group_ids else []
    owner_ids = {p.owner_user_id for p in providers if p.owner_user_id}
    owners = (
        await session.scalars(select(ConsoleUser).where(ConsoleUser.id.in_(owner_ids)))
    ).all() if owner_ids else []
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
        base_url = base_url or provider.base_url
        api_key = api_key or provider.api_key
    try:
        models = await ai_service.fetch_models(base_url, api_key)
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
        base_url = ai_service.validated_base_url(raw_base_url)
    except ValueError as exc:
        return _redirect("/ai/add", err=str(exc))
    session.add(
        AIProvider(
            name=name,
            label=_text(form, "label", 128) or name,
            base_url=base_url,
            api_key=_text(form, "api_key", 512),
            model=model,
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
    provider.label = _text(form, "label", 128, provider.label or provider.name)
    base_url = _text(form, "base_url", 512, provider.base_url or "")
    model = _text(form, "model", 128, provider.model or "")
    if not base_url or not model:
        return _redirect("/ai", err="接口地址和模型不能为空")
    try:
        provider.base_url = ai_service.validated_base_url(base_url)
    except ValueError as exc:
        return _redirect("/ai", err=str(exc))
    new_key = _text(form, "api_key", 512)
    if new_key:
        provider.api_key = new_key
    provider.model = model
    provider.priority = _int(form, "priority", provider.priority or 100, 0, 100000)
    provider.enabled = _bool(form, "enabled")
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
    result = await ai_service.test_provider(provider)
    if result.ok:
        return _redirect("/ai", msg=f"{provider.name} 连通正常（{result.latency_ms}ms）：{result.text[:80]}")
    return _redirect("/ai", err=f"{provider.name} 测试失败：{result.error[:200]}")


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
    if not ent.is_superadmin and (
        ent.user is None or entry.owner_user_id != ent.user.id
    ):
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
    groups = (
        await session.scalars(select(Group).where(Group.id.in_(gids)))
    ).all() if gids else []
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
async def stats_page(request: Request, session: AsyncSession = Depends(get_session)) -> HTMLResponse:
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
        await session.scalars(select(Group).where(Group.id.in_(top_ids)))
    ).all() if top_ids else []
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
    sources = [{"source": source or "未分类", "count": int(count)} for source, count in source_rows.all()]
    total_hits = int(
        await session.scalar(
            select(func.count(KeywordHit.id)).where(*keyword_cond)
        )
        or 0
    )
    ai_rows = (
        await session.scalars(
            select(AIUsage).where(*ai_cond).order_by(AIUsage.day.desc())
        )
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
