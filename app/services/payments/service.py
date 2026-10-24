from __future__ import annotations

import re
import secrets
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from io import BytesIO
from pathlib import Path
from typing import Mapping

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from PIL import Image, UnidentifiedImageError

from ...config import EpayConfig, PaymentConfig, get_config
from ...models import AccountLedgerEntry, ConsoleUser, PaymentChannel, PaymentOrder, Subscription, SubscriptionPlan, utcnow
from ..subscriptions import MONTHLY_PRICE_CENTS, add_calendar_month
from .base import PaymentCallback, PaymentCreation, PaymentProvider
from .epay import EpayError, EpayProvider


def payment_config() -> PaymentConfig:
    return get_config().payments


def get_provider(
    config: PaymentConfig | None = None, channel: PaymentChannel | None = None
) -> PaymentProvider:
    if channel is not None:
        if channel.channel_type != "epay":
            raise EpayError(f"不支持的支付渠道：{channel.channel_type}")
        return EpayProvider(
            EpayConfig(
                base_url=channel.gateway,
                pid=channel.merchant_id,
                key=channel.merchant_key,
                type=channel.pay_type,
                method="",
            ),
            version=channel.version,
            notify_url=channel.notify_url,
            return_url=channel.return_url,
        )
    cfg = config or payment_config()
    if cfg.provider != "epay":
        raise EpayError(f"不支持的支付渠道：{cfg.provider}")
    return EpayProvider(cfg.epay)


def provider_ready(config: PaymentConfig | None = None) -> tuple[bool, str]:
    cfg = config or payment_config()
    if cfg.provider != "epay":
        return False, f"不支持的支付渠道：{cfg.provider}"
    try:
        EpayProvider(cfg.epay)._require_config()
    except EpayError as exc:
        return False, str(exc)
    return True, ""


async def select_channel(session: AsyncSession) -> PaymentChannel | None:
    return await session.scalar(
        select(PaymentChannel)
        .where(PaymentChannel.enabled.is_(True), PaymentChannel.channel_type == "epay")
        .order_by(PaymentChannel.sort_weight.asc(), PaymentChannel.id.asc())
        .limit(1)
    )


async def channels_configured(session: AsyncSession) -> bool:
    return await session.scalar(select(PaymentChannel.id).limit(1)) is not None


def validate_channel(channel: PaymentChannel) -> None:
    get_provider(channel=channel)._require_config()
    if channel.version.upper() not in {"V1", "V2"}:
        raise EpayError("支付渠道版本无效")


def channel_price(channel: PaymentChannel, amount_cents: int) -> tuple[int, int, str]:
    fee = int(
        (Decimal(amount_cents) * Decimal(channel.fee_percent) / Decimal(10000)).quantize(
            Decimal("1"), rounding=ROUND_HALF_UP
        )
    ) + int(channel.fee_fixed_cents or 0)
    gross_cents = int(amount_cents) + fee
    currency = (channel.currency or "CNY").upper()
    rate = (channel.rate_base or "").strip()
    if currency != "CNY" and rate:
        rate_value = Decimal(rate)
        if not rate_value.is_finite() or rate_value <= 0:
            raise EpayError("渠道汇率基数无效")
        charge_cents = int(
            (Decimal(gross_cents) / rate_value).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        )
    else:
        currency = "CNY"
        charge_cents = gross_cents
    return fee, charge_cents, currency


def new_order_no() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    return f"R{stamp}{secrets.token_hex(4)}"


def callback_urls() -> tuple[str, str]:
    base = get_config().web.base_url.rstrip("/")
    return f"{base}/payments/epay/notify", f"{base}/account/recharge/return"


async def checkout_enabled(session: AsyncSession) -> bool:
    channel = await select_channel(session)
    if channel is not None:
        try:
            validate_channel(channel)
            return True
        except EpayError:
            return False
    if await channels_configured(session):
        return False
    cfg = payment_config()
    return bool(cfg.enabled and provider_ready(cfg)[0])


async def provider_for_order(
    session: AsyncSession, order: PaymentOrder
) -> PaymentProvider:
    channel = await session.get(PaymentChannel, order.channel_id) if order.channel_id else None
    if channel is not None:
        validate_channel(channel)
        return get_provider(channel=channel)
    return get_provider()


async def checkout_urls_for_order(
    session: AsyncSession, order: PaymentOrder
) -> tuple[str, str]:
    channel = await session.get(PaymentChannel, order.channel_id) if order.channel_id else None
    return channel_urls(channel) if channel is not None else callback_urls()


def channel_urls(channel: PaymentChannel) -> tuple[str, str]:
    default_notify, default_return = callback_urls()
    return channel.notify_url or default_notify, channel.return_url or default_return


def icons_dir() -> Path:
    path = get_config().project_dir / "data" / "payment_icons"
    path.mkdir(parents=True, exist_ok=True)
    return path


ICON_TYPES = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
}
ICON_MAX_BYTES = 512 * 1024


def save_icon(name: str, content_type: str, payload: bytes) -> str:
    suffix = ICON_TYPES.get((content_type or "").split(";")[0].strip().lower())
    if suffix is None:
        raise EpayError("图标仅支持 PNG、JPG、WEBP、GIF 或 SVG")
    if not payload:
        raise EpayError("图标文件为空")
    if len(payload) > ICON_MAX_BYTES:
        raise EpayError("图标不能超过 512KB")
    try:
        with Image.open(BytesIO(payload)) as image:
            if image.format not in {"PNG", "JPEG", "WEBP", "GIF"}:
                raise EpayError("图标格式无效")
            image.verify()
    except (UnidentifiedImageError, OSError) as exc:
        raise EpayError("无法读取图标图片") from exc
    safe = re.sub(r"[^a-z0-9]+", "-", (name or "icon").lower()).strip("-")[:32] or "icon"
    filename = f"{safe}-{secrets.token_hex(6)}{suffix}"
    (icons_dir() / filename).write_bytes(payload)
    return filename


async def list_channels(session: AsyncSession) -> list[PaymentChannel]:
    rows = await session.scalars(
        select(PaymentChannel).order_by(
            PaymentChannel.sort_weight.asc(), PaymentChannel.id.asc()
        )
    )
    return list(rows.all())


async def create_channel(session: AsyncSession, fields: dict[str, object]) -> PaymentChannel:
    channel = PaymentChannel(**fields)
    session.add(channel)
    await session.flush()
    return channel


async def update_channel(
    session: AsyncSession, channel_id: int, fields: dict[str, object]
) -> PaymentChannel | None:
    channel = await session.get(PaymentChannel, int(channel_id))
    if channel is None:
        return None
    for name, value in fields.items():
        setattr(channel, name, value)
    return channel


async def delete_channel(session: AsyncSession, channel_id: int) -> bool:
    pending = await session.scalar(
        select(PaymentOrder.id)
        .where(PaymentOrder.channel_id == int(channel_id), PaymentOrder.status == "pending")
        .limit(1)
    )
    if pending is not None:
        raise EpayError("此渠道仍有关联的待支付订单，不能删除；可先停用渠道")
    channel = await session.get(PaymentChannel, int(channel_id))
    if channel is None:
        return False
    await session.delete(channel)
    return True


async def create_recharge_order(
    session: AsyncSession, user_id: int, amount_cents: int
) -> tuple[PaymentOrder | None, str]:
    cfg = payment_config()
    channel = await select_channel(session)
    if channel is None:
        if await channels_configured(session):
            return None, "没有可用的已启用支付渠道"
        if not cfg.enabled:
            return None, "支付功能尚未启用"
        ready, error = provider_ready(cfg)
        if not ready:
            return None, error
    if amount_cents < cfg.min_amount_cents:
        return None, f"充值金额不能低于 ¥{cfg.min_amount_cents / 100:.2f}"
    if amount_cents > cfg.max_amount_cents:
        return None, f"充值金额不能高于 ¥{cfg.max_amount_cents / 100:.2f}"
    fee_cents, charge_cents, charge_currency = (
        channel_price(channel, int(amount_cents)) if channel else (0, int(amount_cents), "CNY")
    )
    order = PaymentOrder(
        order_no=new_order_no(),
        user_id=int(user_id),
        provider=channel.channel_type if channel else cfg.provider,
        purpose="recharge",
        amount_cents=int(amount_cents),
        currency="CNY",
        status="pending",
        pay_type=channel.pay_type if channel else cfg.epay.type,
        channel_id=channel.id if channel else None,
        fee_cents=fee_cents,
        charge_cents=charge_cents,
        charge_currency=charge_currency,
        expires_at=utcnow() + timedelta(minutes=5),
    )
    session.add(order)
    await session.flush()
    return order, ""


async def create_subscription_order(
    session: AsyncSession, user_id: int
) -> tuple[PaymentOrder | None, str]:
    cfg = payment_config()
    channel = await select_channel(session)
    if channel is None:
        if await channels_configured(session):
            return None, "没有可用的已启用支付渠道"
        if not cfg.enabled:
            return None, "支付功能尚未启用"
        ready, error = provider_ready(cfg)
        if not ready:
            return None, error
    permanent = await session.scalar(
        select(Subscription.id)
        .where(
            Subscription.user_id == int(user_id),
            Subscription.status == "active",
            Subscription.expires_at.is_(None),
        )
        .limit(1)
    )
    if permanent is not None:
        return None, "当前账号已是长期订阅，无需重复购买"
    fee_cents, charge_cents, charge_currency = (
        channel_price(channel, MONTHLY_PRICE_CENTS)
        if channel
        else (0, MONTHLY_PRICE_CENTS, "CNY")
    )
    order = PaymentOrder(
        order_no=new_order_no(),
        user_id=int(user_id),
        provider=channel.channel_type if channel else cfg.provider,
        purpose="subscription",
        amount_cents=MONTHLY_PRICE_CENTS,
        currency="CNY",
        status="pending",
        pay_type=channel.pay_type if channel else cfg.epay.type,
        channel_id=channel.id if channel else None,
        fee_cents=fee_cents,
        charge_cents=charge_cents,
        charge_currency=charge_currency,
        expires_at=utcnow() + timedelta(minutes=5),
    )
    session.add(order)
    await session.flush()
    return order, ""


async def start_checkout(
    provider: PaymentProvider, order: PaymentOrder, client_ip: str,
    notify_url: str | None = None, return_url: str | None = None,
) -> tuple[PaymentCreation | None, str]:
    default_notify, default_return = callback_urls()
    try:
        creation = await provider.create_payment(
            order_no=order.order_no,
            amount=Decimal(order.charge_cents or order.amount_cents + order.fee_cents) / 100,
            name=(
                f"订阅一个月 ¥{order.amount_cents / 100:.2f}"
                if order.purpose == "subscription"
                else f"账户充值 ¥{order.amount_cents / 100:.2f}"
            ),
            notify_url=notify_url or default_notify,
            return_url=return_url or default_return,
            client_ip=client_ip,
        )
    except Exception as exc:
        order.last_error = str(exc)[:255]
        return None, str(exc)
    order.trade_no = creation.trade_no
    order.pay_url = creation.pay_url
    order.pay_type = creation.pay_type
    return creation, ""


async def settle_payment(
    session: AsyncSession, callback: PaymentCallback, provider_name: str = "epay"
) -> tuple[str, PaymentOrder | None]:
    if callback.status != "paid":
        return "payment_not_successful", None
    order = await session.scalar(
        select(PaymentOrder).where(PaymentOrder.order_no == callback.order_no)
    )
    if order is None:
        return "order_not_found", None
    if order.status == "paid":
        return "already_paid", order
    if order.status != "pending":
        return "order_not_payable", order
    now = utcnow()
    if order.expires_at is not None and order.expires_at <= now:
        order.status = "expired"
        order.updated_at = now
        return "order_expired", order
    if order.provider != provider_name:
        return "provider_mismatch", order
    if order.purpose not in {"recharge", "subscription"}:
        return "unsupported_purpose", order
    if order.trade_no and callback.trade_no and order.trade_no != callback.trade_no:
        return "trade_mismatch", order
    expected = Decimal(order.charge_cents or order.amount_cents + order.fee_cents) / 100
    if callback.amount != expected:
        return "amount_mismatch", order
    if callback.currency.upper() != order.charge_currency.upper():
        return "currency_mismatch", order
    if order.purpose == "subscription":
        plan = await session.scalar(
            select(SubscriptionPlan)
            .where(SubscriptionPlan.name == "pro", SubscriptionPlan.enabled.is_(True))
            .limit(1)
        )
        active = (
            await session.scalars(
                select(Subscription)
                .where(Subscription.user_id == order.user_id, Subscription.status == "active")
                .order_by(Subscription.id.desc())
            )
        ).all()
        if plan is None:
            return "plan_unavailable", order
        if any(row.expires_at is None for row in active):
            return "permanent_subscription", order
    claimed = await session.scalar(
        update(PaymentOrder)
        .where(PaymentOrder.id == order.id, PaymentOrder.status == "pending")
        .values(status="paid", paid_at=now, trade_no=callback.trade_no or order.trade_no)
        .returning(PaymentOrder.id)
    )
    if claimed is None:
        return "already_paid", order
    if order.purpose == "subscription":
        current = active[0] if active else None
        base = now
        if current is not None and current.expires_at is not None and current.expires_at > now:
            base = current.expires_at
        expires_at = add_calendar_month(base)
        for duplicate in active[1:]:
            duplicate.status = "expired"
        if current is None:
            session.add(
                Subscription(
                    user_id=order.user_id,
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
    else:
        balance = await session.scalar(
            update(ConsoleUser)
            .where(ConsoleUser.id == order.user_id, ConsoleUser.is_active.is_(True))
            .values(balance_cents=ConsoleUser.balance_cents + order.amount_cents)
            .returning(ConsoleUser.balance_cents)
        )
        if balance is None:
            raise RuntimeError("充值订单对应的账户不存在或已停用")
        session.add(
            AccountLedgerEntry(
                user_id=order.user_id,
                order_id=order.id,
                kind="recharge",
                amount_cents=order.amount_cents,
                balance_after_cents=int(balance),
                description="在线充值到账",
            )
        )
    if order.purpose == "subscription":
        user_balance = await session.scalar(
            select(ConsoleUser.balance_cents).where(ConsoleUser.id == order.user_id)
        )
        session.add(
            AccountLedgerEntry(
                user_id=order.user_id,
                order_id=order.id,
                kind="subscription_online",
                amount_cents=-order.amount_cents,
                balance_after_cents=int(user_balance or 0),
                description="在线购买/续费订阅一个月",
            )
        )
    order.status = "paid"
    order.paid_at = now
    if callback.trade_no:
        order.trade_no = callback.trade_no
    return "paid", order


async def handle_notification(
    session: AsyncSession, provider: PaymentProvider | None, params: Mapping[str, str]
) -> tuple[str, PaymentOrder | None]:
    order_no = str(params.get("out_trade_no") or "").strip()
    if not order_no:
        return "order_not_found", None
    order = await session.scalar(select(PaymentOrder).where(PaymentOrder.order_no == order_no))
    if order is None:
        return "order_not_found", None
    channel = await session.get(PaymentChannel, order.channel_id) if order.channel_id else None
    if channel is not None:
        validate_channel(channel)
        actual_provider = get_provider(channel=channel)
    elif provider is not None:
        actual_provider = provider
    else:
        actual_provider = get_provider()
    callback = actual_provider.verify_callback(params)
    callback = replace(callback, currency=order.charge_currency or "CNY")
    return await settle_payment(session, callback, order.provider)


async def list_orders(
    session: AsyncSession, user_id: int, limit: int = 1000
) -> list[PaymentOrder]:
    rows = await session.scalars(
        select(PaymentOrder)
        .where(PaymentOrder.user_id == int(user_id))
        .order_by(PaymentOrder.id.desc())
        .limit(limit)
    )
    return list(rows.all())


async def expire_pending_orders(
    session: AsyncSession, user_id: int | None = None
) -> int:
    now = utcnow()
    statement = (
        update(PaymentOrder)
        .where(
            PaymentOrder.status == "pending",
            PaymentOrder.expires_at.is_not(None),
            PaymentOrder.expires_at <= now,
        )
        .values(status="expired", updated_at=now)
    )
    if user_id is not None:
        statement = statement.where(PaymentOrder.user_id == int(user_id))
    result = await session.execute(statement)
    return int(result.rowcount or 0)
