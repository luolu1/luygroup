from __future__ import annotations

from decimal import InvalidOperation
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_config
from ..db import get_session
from ..models import AccountLedgerEntry, ConsoleUser, PaymentChannel, PaymentOrder
from ..services.payments import service as payment_service
from . import pages
from .auth import require_admin
from .payment_forms import channel_fields

router = APIRouter(tags=["payment-admin"], dependencies=[Depends(require_admin)])


@router.get("/payments", response_class=HTMLResponse)
async def payment_channels_page(
    request: Request,
    channel_type: str = "",
    pay_type: str = "",
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    pages._require_superadmin(request)
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
    return pages._render(
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
    pages._require_superadmin(request)
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
        user_match = ConsoleUser.display_name.ilike(f"%{q}%") | ConsoleUser.username.ilike(f"%{q}%")
        payment_stmt = payment_stmt.where(PaymentOrder.order_no.ilike(f"%{q}%") | user_match)
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
    ledger_rows = (await session.execute(ledger_stmt.limit(1000))).all() if include_ledger else []
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
    return pages._render(
        request,
        "payment_orders.html",
        orders=orders[:1000],
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
    pages._require_superadmin(request)
    channel = await session.get(PaymentChannel, channel_id) if channel_id else None
    if channel_id and channel is None:
        raise HTTPException(status_code=404, detail="支付渠道不存在")
    cfg = get_config()
    return pages._render(
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
    pages._require_superadmin(request)
    try:
        fields = channel_fields(await request.form())
    except (ValueError, InvalidOperation) as exc:
        return pages._redirect("/payments/new", err=str(exc))
    await payment_service.create_channel(session, fields)
    return pages._redirect("/payments", msg="支付渠道已新增")


@router.post("/payments/{channel_id}/update")
async def payment_channel_update(
    channel_id: int,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    pages._require_superadmin(request)
    channel = await session.get(PaymentChannel, channel_id)
    if channel is None:
        raise HTTPException(status_code=404, detail="支付渠道不存在")
    try:
        fields = channel_fields(await request.form(), existing=channel)
    except (ValueError, InvalidOperation) as exc:
        return pages._redirect(f"/payments/{channel_id}/edit", err=str(exc))
    await payment_service.update_channel(session, channel_id, fields)
    return pages._redirect("/payments", msg="支付渠道已更新")


@router.post("/payments/{channel_id}/toggle")
async def payment_channel_toggle(
    channel_id: int,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    pages._require_superadmin(request)
    channel = await session.get(PaymentChannel, channel_id)
    if channel is None:
        raise HTTPException(status_code=404, detail="支付渠道不存在")
    channel.enabled = not channel.enabled
    return pages._redirect("/payments", msg="渠道状态已更新")


@router.post("/payments/{channel_id}/delete")
async def payment_channel_delete(
    channel_id: int,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    pages._require_superadmin(request)
    try:
        deleted = await payment_service.delete_channel(session, channel_id)
    except ValueError as exc:
        return pages._redirect("/payments", err=str(exc))
    if not deleted:
        raise HTTPException(status_code=404, detail="支付渠道不存在")
    return pages._redirect("/payments", msg="支付渠道已删除")
