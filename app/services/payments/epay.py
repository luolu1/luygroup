from __future__ import annotations

import hashlib
import hmac
from decimal import Decimal, InvalidOperation
from collections.abc import Mapping
from urllib.parse import urlencode, urljoin, urlsplit

import httpx

from ...config import EpayConfig
from .base import PaymentCallback, PaymentCreation


class EpayError(ValueError):
    pass


def sign_fields(params: Mapping[str, object], key: str) -> str:
    pairs = []
    for name in sorted(params):
        value = params[name]
        if name in {"sign", "sign_type"} or value is None or str(value).strip() == "":
            continue
        if isinstance(value, (list, tuple, dict)):
            continue
        pairs.append(f"{name}={value}")
    return hashlib.md5(("&".join(pairs) + key).encode()).hexdigest()


class EpayProvider:
    name = "epay"

    def __init__(self, config: EpayConfig, *, version: str = "V2", notify_url: str = "", return_url: str = ""):
        self.config = config
        self.version = version.upper()
        self.base_url = config.base_url.rstrip("/")
        self.notify_url = notify_url
        self.return_url = return_url

    def _require_config(self) -> None:
        if not self.base_url or not self.config.pid or not self.config.key:
            raise EpayError("Epay 尚未配置")

    async def create_payment(
        self,
        *,
        order_no: str,
        amount: Decimal,
        name: str,
        notify_url: str,
        return_url: str,
        client_ip: str,
    ) -> PaymentCreation:
        self._require_config()
        payload = {
            "pid": self.config.pid,
            "type": self.config.type,
            "out_trade_no": order_no,
            "notify_url": self.notify_url or notify_url,
            "return_url": self.return_url or return_url,
            "name": name,
            "money": f"{amount:.2f}",
            "clientip": client_ip,
            "method": self.config.method,
        }
        payload["sign"] = sign_fields(payload, self.config.key)
        payload["sign_type"] = "MD5"
        if self.version == "V1":
            return PaymentCreation(
                trade_no="",
                pay_url=f"{self.base_url}/submit.php?{urlencode(payload)}",
                pay_type=self.config.type,
            )
        async with httpx.AsyncClient(timeout=self.config.timeout, follow_redirects=False) as client:
            response = await client.post(urljoin(self.base_url + "/", "mapi.php"), data=payload)
        response.raise_for_status()
        data = response.json()
        code = data.get("code")
        if code is not None and int(code) != 1:
            raise EpayError(str(data.get("msg") or "Epay 下单失败"))
        pay_url = str(data.get("payurl") or data.get("urlscheme") or "").strip()
        parsed = urlsplit(pay_url)
        trade_no = str(data.get("trade_no") or "").strip()
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or not trade_no:
            raise EpayError("Epay 未返回支付地址")
        return PaymentCreation(
            trade_no=trade_no,
            pay_url=pay_url,
            pay_type=str(data.get("type") or self.config.type),
        )

    def verify_callback(self, params: Mapping[str, str]) -> PaymentCallback:
        self._require_config()
        if params.get("pid") != self.config.pid:
            raise EpayError("Epay 商户号不匹配")
        if params.get("sign_type", "MD5").upper() != "MD5":
            raise EpayError("不支持的 Epay 签名类型")
        if not hmac.compare_digest(
            params.get("sign", "").lower(), sign_fields(params, self.config.key)
        ):
            raise EpayError("Epay 签名校验失败")
        if params.get("trade_status") != "TRADE_SUCCESS":
            raise EpayError("Epay 订单尚未支付成功")
        try:
            raw_amount = Decimal(params["money"])
            amount = raw_amount.quantize(Decimal("0.01"))
        except (KeyError, InvalidOperation):
            raise EpayError("Epay 金额无效") from None
        if amount != raw_amount:
            raise EpayError("Epay 金额小数位无效")
        order_no = str(params.get("out_trade_no") or "").strip()
        if not order_no:
            raise EpayError("Epay 订单号缺失")
        return PaymentCallback(
            order_no=order_no,
            trade_no=str(params.get("trade_no") or ""),
            merchant_id=str(params["pid"]),
            amount=amount,
            status="paid",
            currency=str(params.get("currency") or "CNY"),
        )
