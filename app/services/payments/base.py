from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol
from collections.abc import Mapping


@dataclass(frozen=True)
class PaymentCreation:
    trade_no: str
    pay_url: str
    pay_type: str


@dataclass(frozen=True)
class PaymentCallback:
    order_no: str
    trade_no: str
    merchant_id: str
    amount: Decimal
    status: str
    currency: str = "CNY"


class PaymentProvider(Protocol):
    name: str

    async def create_payment(
        self,
        *,
        order_no: str,
        amount: Decimal,
        name: str,
        notify_url: str,
        return_url: str,
        client_ip: str,
    ) -> PaymentCreation: ...

    def verify_callback(self, params: Mapping[str, str]) -> PaymentCallback: ...
