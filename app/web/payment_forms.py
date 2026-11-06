from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlsplit

from fastapi import UploadFile

from ..models import PaymentChannel
from ..services.payments import service as payment_service
from .form_values import bool_value, int_value, text_value


def channel_fields(form: Any, *, existing: PaymentChannel | None = None) -> dict[str, object]:
    name = text_value(form, "name", 64)
    gateway = text_value(form, "gateway", 255).rstrip("/")
    merchant_id = text_value(form, "merchant_id", 64)
    merchant_key = text_value(form, "merchant_key", 255)
    if not name or not gateway or not merchant_id:
        raise ValueError("渠道名称、支付网关和商户 ID 必填")
    parsed_gateway = urlsplit(gateway)
    if parsed_gateway.scheme not in {"http", "https"} or not parsed_gateway.netloc:
        raise ValueError("支付网关必须是完整的 http:// 或 https:// 地址")
    if parsed_gateway.username or parsed_gateway.password:
        raise ValueError("支付网关地址不能包含用户名或密码")
    if not merchant_key and existing is None:
        raise ValueError("商户密钥必填")
    version = text_value(form, "version", 8, "V1").upper()
    if version not in {"V1", "V2"}:
        raise ValueError("仅支持 V1 或 V2")
    channel_type = text_value(form, "channel_type", 32, "epay")
    if channel_type != "epay":
        raise ValueError("当前仅支持 Epay 易支付渠道")
    pay_type = text_value(form, "pay_type", 32, "alipay")
    if pay_type not in {"alipay", "wxpay", "qqpay", "bank", "usdt"}:
        raise ValueError("不支持的支付方式")
    interact_type = text_value(form, "interact_type", 16, "qrcode")
    if interact_type not in {"qrcode", "redirect"}:
        raise ValueError("不支持的交互方式")
    currency = text_value(form, "currency", 8, "CNY").upper() or "CNY"
    rate_base = text_value(form, "rate_base", 16)
    rate = Decimal(rate_base) if rate_base else None
    if rate is not None and (not rate.is_finite() or rate <= 0):
        raise ValueError("汇率基数必须是大于 0 的数字")
    try:
        fee_percent = Decimal(text_value(form, "fee_percent", 16, "0") or "0")
        fee_fixed = Decimal(text_value(form, "fee_fixed", 16, "0") or "0")
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
        icon = payment_service.save_icon(name, upload.content_type or "", payload)
    if text_value(form, "remove_icon", 8) == "on":
        icon = ""
    return {
        "name": name,
        "icon": icon,
        "channel_type": channel_type,
        "pay_type": pay_type,
        "interact_type": interact_type,
        "sort_weight": int_value(form, "sort_weight", 10, 0, 100000),
        "fee_percent": int((fee_percent * 100).quantize(Decimal("1"))),
        "fee_fixed_cents": int((fee_fixed * 100).quantize(Decimal("1"))),
        "enabled": bool_value(form, "enabled"),
        "version": version,
        "gateway": gateway,
        "merchant_id": merchant_id,
        "merchant_key": merchant_key or (existing.merchant_key if existing else ""),
        "notify_url": text_value(form, "notify_url", 255),
        "return_url": text_value(form, "return_url", 255),
        "currency": currency,
        "rate_base": rate_base,
    }
