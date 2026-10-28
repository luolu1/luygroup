from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TMP = Path(tempfile.mkdtemp(prefix="luygroup-payments-"))
DB = TMP / "payments.db"
TMP_CONFIG = TMP / "config.json"
config = json.loads((ROOT / "config.example.json").read_text(encoding="utf-8"))
config["bot"]["token"] = "123456:smoke-token"
config["web"]["secret_key"] = "smoke-secret-key"
config["web"]["base_url"] = "http://127.0.0.1:8090"
config["database"]["url"] = f"sqlite+aiosqlite:///{DB}"
config["payments"]["enabled"] = True
config["payments"]["epay"].update(
    {"base_url": "https://pay.example.test", "pid": "10001", "key": "smoke-key"}
)
TMP_CONFIG.write_text(json.dumps(config), encoding="utf-8")
os.environ["GROUPGUARD_CONFIG"] = str(TMP_CONFIG)
sys.path.insert(0, str(ROOT))

from sqlalchemy import select, update  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.config import get_config  # noqa: E402
from app.db import dispose_db, get_sessionmaker, init_db  # noqa: E402
from app.models import (
    ConsoleUser,
    PaymentChannel,
    PaymentOrder,
    Subscription,
    utcnow,
)  # noqa: E402
from app.services.payments.base import PaymentCallback, PaymentCreation  # noqa: E402
from app.services.payments.epay import EpayProvider, sign_fields  # noqa: E402
from app.web.auth import hash_console_token  # noqa: E402
from app.services.payments.service import (  # noqa: E402
    channel_price,
    create_recharge_order,
    create_subscription_order,
    expire_pending_orders,
    handle_notification,
    settle_payment,
    start_checkout,
)
from app.services.subscriptions import purchase_month  # noqa: E402
from app.web.main import create_app  # noqa: E402
from app.web import pages as web_pages  # noqa: E402


def check(name: str, condition: bool, detail: str = "") -> None:
    print(f"{'PASS' if condition else 'FAIL'} {name}{': ' + detail if detail else ''}")
    if not condition:
        raise AssertionError(name)


async def main() -> None:
    get_config.cache_clear()
    await init_db()
    async with get_sessionmaker()() as session:
        user = ConsoleUser(
            telegram_id=900001,
            display_name="Payment smoke",
            token_hash=hash_console_token("payment-smoke-token"),
            token_expires_at=utcnow() + __import__("datetime").timedelta(minutes=15),
            role="user",
            balance_cents=250,
        )
        session.add(user)
        await session.commit()
        user_id = user.id

    provider = EpayProvider(get_config().payments.epay)
    params = {
        "pid": "10001",
        "trade_no": "epay-001",
        "out_trade_no": "smoke-order",
        "type": "alipay",
        "name": "recharge",
        "money": "12.34",
        "trade_status": "TRADE_SUCCESS",
    }
    params["sign"] = sign_fields(params, "smoke-key")
    callback = provider.verify_callback(params)
    check("Epay MD5 callback signature", callback.amount == Decimal("12.34"))
    bad_params = {**params, "money": "99.00"}
    try:
        provider.verify_callback(bad_params)
        signature_rejected = False
    except ValueError:
        signature_rejected = True
    check("tampered callback rejected", signature_rejected)

    channel = PaymentChannel(
        name="Epay smoke",
        channel_type="epay",
        pay_type="alipay",
        interact_type="qrcode",
        sort_weight=10,
        fee_percent=300,
        fee_fixed_cents=25,
        enabled=True,
        version="V1",
        gateway="https://pay.example.test",
        merchant_id="10001",
        merchant_key="smoke-key",
        currency="CNY",
    )
    fee, charge_cents, currency = channel_price(channel, 1000)
    check("channel fee calculates percentage and fixed charge", (fee, charge_cents, currency) == (55, 1055, "CNY"))
    v1 = EpayProvider(
        get_config().payments.epay,
        version="V1",
        notify_url="https://luygroup.example/notify",
        return_url="https://luygroup.example/return",
    )
    v1_result = await v1.create_payment(
        order_no="smoke-v1",
        amount=Decimal("10.55"),
        name="channel test",
        notify_url="https://default.example/notify",
        return_url="https://default.example/return",
        client_ip="127.0.0.1",
    )
    check(
        "Epay V1 builds submit.php checkout",
        "/submit.php?" in v1_result.pay_url
        and "notify_url=https%3A%2F%2Fluygroup.example%2Fnotify" in v1_result.pay_url
        and "&trade_no=" not in v1_result.pay_url,
    )

    async with get_sessionmaker()() as session:
        async with session.begin():
            order, error = await create_recharge_order(session, user_id, 1234)
            assert order is not None, error
            order_no = order.order_no
            creation, error = await start_checkout(provider, order, "127.0.0.1")
            assert creation is None and error
        order_row = await session.get(PaymentOrder, order.id)
        check("checkout failure remains queryable without credit", order_row.status == "pending")
        balance = await session.scalar(
            select(ConsoleUser.balance_cents).where(ConsoleUser.id == user_id)
        )
        check("checkout failure leaves balance unchanged", balance == 250)

    async with get_sessionmaker()() as session:
        async with session.begin():
            order, error = await create_recharge_order(session, user_id, 1234)
            assert order is not None, error
            order_no = order.order_no
    callback = PaymentCallback(
        order_no=order_no,
        trade_no="epay-paid-1",
        merchant_id="10001",
        amount=Decimal("12.34"),
        status="paid",
    )
    async with get_sessionmaker()() as session:
        async with session.begin():
            status, _ = await settle_payment(session, callback)
    check("first settlement pays order", status == "paid")
    async with get_sessionmaker()() as session:
        async with session.begin():
            status, _ = await settle_payment(session, callback)
    check("duplicate settlement is idempotent", status == "already_paid")
    async with get_sessionmaker()() as session:
        balance = await session.scalar(
            select(ConsoleUser.balance_cents).where(ConsoleUser.id == user_id)
        )
        check("balance credited exactly once", balance == 1484, str(balance))

    wrong = PaymentCallback(
        order_no=order_no,
        trade_no="epay-paid-2",
        merchant_id="10001",
        amount=Decimal("1.00"),
        status="paid",
    )
    async with get_sessionmaker()() as session:
        async with session.begin():
            status, _ = await settle_payment(session, wrong)
    check("amount mismatch rejected", status == "already_paid")

    async with get_sessionmaker()() as session:
        async with session.begin():
            order, error = await create_recharge_order(session, user_id, 500)
            assert order is not None, error
            http_order_no = order.order_no
    notify = {
        "pid": "10001",
        "trade_no": "epay-http-1",
        "out_trade_no": http_order_no,
        "type": "alipay",
        "name": "recharge",
        "money": "5.00",
        "trade_status": "TRADE_SUCCESS",
    }
    notify["sign"] = sign_fields(notify, "smoke-key")
    with TestClient(create_app()) as client:
        response = client.post("/payments/epay/notify", data=notify)
        check(
            "Epay notify acknowledges with success",
            response.status_code == 200 and response.text == "success",
            f"{response.status_code} {response.text[:60]}",
        )
        repeated = client.post("/payments/epay/notify", data=notify)
        check(
            "repeated Epay notify stays successful",
            repeated.status_code == 200 and repeated.text == "success",
            repeated.text[:60],
        )
        forged = dict(notify, money="500.00")
        forged_response = client.post("/payments/epay/notify", data=forged)
        check(
            "forged Epay notify rejected",
            forged_response.status_code == 400,
            str(forged_response.status_code),
        )
        cross_site = client.post(
            "/account/subscribe", headers={"origin": "https://evil.example"}
        )
        check("browser CSRF protection still enforced", cross_site.status_code == 403)
        original_provider = web_pages.payment_service.get_provider
        original_checkout = web_pages.payment_service.start_checkout
        web_pages.payment_service.get_provider = lambda: provider

        async def fake_checkout(_provider, _order, _client_ip, notify_url=None, return_url=None):
            return PaymentCreation(
                "epay-test", "https://pay.example.test/checkout", "alipay"
            ), ""

        web_pages.payment_service.start_checkout = fake_checkout
        try:
            client.get("/console?token=payment-smoke-token")
            async with get_sessionmaker()() as session:
                async with session.begin():
                    pending, error = await create_recharge_order(session, user_id, 700)
                    assert pending is not None, error
                    pending_order_no = pending.order_no
                    assert pending.expires_at is not None
                    assert timedelta(minutes=4, seconds=59) < pending.expires_at - utcnow() <= timedelta(minutes=5)
            pending_page = client.get("/account")
            check(
                "user sees own pending bill and continue-payment control",
                pending_order_no in pending_page.text
                and "继续付款" in pending_page.text
                and "5 分钟内有效" in pending_page.text,
            )
            before_balance = client.post(
                f"/account/orders/{pending_order_no}/pay", follow_redirects=False
            )
            check(
                "user can continue own pending payment",
                before_balance.status_code == 303
                and before_balance.headers.get("location") == "https://pay.example.test/checkout",
                str(before_balance.headers),
            )
            client.post("/logout", follow_redirects=False)
            other_token = "other-payment-user-token"
            async with get_sessionmaker()() as session:
                session.add(
                    ConsoleUser(
                        telegram_id=900003,
                        display_name="Other user",
                        token_hash=hash_console_token(other_token),
                        token_expires_at=utcnow() + timedelta(minutes=15),
                        role="user",
                        balance_cents=0,
                    )
                )
                await session.commit()
            client.get(f"/console?token={other_token}")
            foreign_pay = client.post(
                f"/account/orders/{pending_order_no}/pay", follow_redirects=False
            )
            check(
                "user cannot continue another account's order",
                foreign_pay.status_code == 303
                and foreign_pay.headers.get("location", "").startswith("/account?err=")
                and "pay.example.test" not in foreign_pay.headers.get("location", ""),
            )
            client.post("/logout", follow_redirects=False)
            async with get_sessionmaker()() as session:
                smoke_user = await session.scalar(
                    select(ConsoleUser).where(ConsoleUser.id == user_id)
                )
                smoke_user.token_hash = hash_console_token("payment-smoke-token-2")
                smoke_user.token_expires_at = utcnow() + timedelta(minutes=15)
                await session.commit()
            client.get("/console?token=payment-smoke-token-2")
            async with get_sessionmaker()() as session:
                async with session.begin():
                    expired, error = await create_recharge_order(session, user_id, 600)
                    assert expired is not None, error
                    expired.expires_at = utcnow() - timedelta(seconds=1)
                await expire_pending_orders(session, user_id)
                await session.commit()
            expired_page = client.get("/account")
            check(
                "expired pending bill has no continue-payment action",
                f"/account/orders/{expired.order_no}/pay" not in expired_page.text,
            )
            account = client.get("/account")
            check(
                "account shows recharge and direct subscription payment",
                account.status_code == 200
                and 'action="/account/recharge"' in account.text
                and 'action="/account/subscribe/pay"' in account.text,
                f"{account.status_code} {account.url} {account.text[:180]}",
            )
            bills = client.get("/account")
            check(
                "account page displays payment and balance records",
                "支付订单" in bills.text and "余额流水" in bills.text,
            )
            external = client.post(
                "/account/subscribe/pay", follow_redirects=False
            )
            check("subscription checkout route created", external.status_code in {303, 307})
        finally:
            web_pages.payment_service.get_provider = original_provider
            web_pages.payment_service.start_checkout = original_checkout
        ordinary = client.get("/payments", follow_redirects=False)
        check("ordinary users cannot access channel admin", ordinary.status_code == 403)
        async def seed_superadmin_token() -> None:
            async with get_sessionmaker()() as session:
                session.add(
                    ConsoleUser(
                        telegram_id=900002,
                        display_name="Payment admin",
                        token_hash=hash_console_token("payment-admin-token"),
                        token_expires_at=utcnow() + __import__("datetime").timedelta(minutes=15),
                        role="superadmin",
                        balance_cents=0,
                    )
                )
                await session.commit()

        await seed_superadmin_token()
        client.get("/console?token=payment-admin-token")
        channels_page = client.get("/payments")
        check("superadmin channel list renders", channels_page.status_code == 200 and "支付渠道" in channels_page.text)
        all_orders = client.get("/payments/orders")
        check("superadmin all-orders page renders", all_orders.status_code == 200 and "所有用户订单" in all_orders.text)
        form_page = client.get("/payments/new")
        check(
            "channel form contains requested Epay settings",
            form_page.status_code == 200
            and "商户密钥" in form_page.text
            and "汇率基数" in form_page.text
            and 'name="notify_url"' in form_page.text,
        )
        created = client.post(
            "/payments/new",
            data={
                "name": "Smoke Alipay",
                "channel_type": "epay",
                "pay_type": "alipay",
                "interact_type": "qrcode",
                "sort_weight": "10",
                "fee_percent": "3.00",
                "fee_fixed": "0.25",
                "enabled": "on",
                "version": "V1",
                "gateway": "https://pay.example.test",
                "merchant_id": "10001",
                "merchant_key": "smoke-key",
                "currency": "CNY",
            },
            follow_redirects=False,
        )
        check("superadmin creates payment channel", created.status_code == 303)
        async with get_sessionmaker()() as session:
            saved_channel = await session.scalar(
                select(PaymentChannel).where(PaymentChannel.name == "Smoke Alipay")
            )
            check(
                "channel fee and version are persisted",
                saved_channel is not None
                and saved_channel.fee_percent == 300
                and saved_channel.fee_fixed_cents == 25
                and saved_channel.version == "V1",
            )
            channel_id = saved_channel.id
    async with get_sessionmaker()() as session:
        balance = await session.scalar(
            select(ConsoleUser.balance_cents).where(ConsoleUser.id == user_id)
        )
    check("notify credited exactly once", balance == 1984, str(balance))

    async with get_sessionmaker()() as session:
        async with session.begin():
            subscription_order, error = await create_subscription_order(session, user_id)
            assert subscription_order is not None, error
            subscription_order_no = subscription_order.order_no
            assert subscription_order.channel_id == channel_id
            assert subscription_order.charge_cents == 849
    async with get_sessionmaker()() as session:
        async with session.begin():
            status, _ = await handle_notification(session, None, {
                "pid": "10001",
                "trade_no": "epay-subscription-1",
                "out_trade_no": subscription_order_no,
                "type": "alipay",
                "name": "subscription",
                "money": "8.49",
                "trade_status": "TRADE_SUCCESS",
                "sign": sign_fields(
                    {
                        "pid": "10001",
                        "trade_no": "epay-subscription-1",
                        "out_trade_no": subscription_order_no,
                        "type": "alipay",
                        "name": "subscription",
                        "money": "8.49",
                        "trade_status": "TRADE_SUCCESS",
                    },
                    "smoke-key",
                ),
            })
    async with get_sessionmaker()() as session:
        subscription = await session.scalar(
            select(Subscription).where(
                Subscription.user_id == user_id, Subscription.status == "active"
            )
        )
        check("direct subscription payment activates plan", subscription is not None)
        settled_order = await session.scalar(
            select(PaymentOrder).where(PaymentOrder.order_no == subscription_order_no)
        )
        balance_after_online = await session.scalar(
            select(ConsoleUser.balance_cents).where(ConsoleUser.id == user_id)
        )
        check(
            "online subscription records paid order without changing balance",
            settled_order is not None
            and settled_order.status == "paid"
            and balance_after_online == 1984,
        )
    async with get_sessionmaker()() as session:
        async with session.begin():
            await session.execute(
                update(ConsoleUser)
                .where(ConsoleUser.id == user_id)
                .values(balance_cents=1000)
            )
    async with get_sessionmaker()() as session:
        async with session.begin():
            balance_purchase, _, _ = await purchase_month(session, user_id)
    check("balance subscription purchase creates bill", balance_purchase == "ok")
    async with get_sessionmaker()() as session:
        admin = await session.scalar(
            select(ConsoleUser).where(ConsoleUser.telegram_id == 900002)
        )
        admin.token_hash = hash_console_token("payment-admin-token-3")
        admin.token_expires_at = utcnow() + __import__("datetime").timedelta(minutes=15)
        await session.commit()
    with TestClient(create_app()) as client:
        client.get("/console?token=payment-admin-token-3")
        all_orders = client.get("/payments/orders")
        check(
            "admin all-orders page includes balance ledger",
            "余额订阅" in all_orders.text,
            all_orders.text[-1200:],
        )
    async with get_sessionmaker()() as session:
        async with session.begin():
            expired_order, error = await create_recharge_order(session, user_id, 400)
            assert expired_order is not None, error
            expired_no = expired_order.order_no
            expired_order.expires_at = utcnow() - timedelta(seconds=1)
            params = {
                "pid": "10001",
                "trade_no": "epay-expired",
                "out_trade_no": expired_no,
                "type": "alipay",
                "name": "expired",
                "money": "4.00",
                "trade_status": "TRADE_SUCCESS",
            }
            params["sign"] = sign_fields(params, "smoke-key")
            status, _ = await handle_notification(session, provider, params)
            check("expired callback does not settle order", status == "order_expired")
        expired_order = await session.scalar(
            select(PaymentOrder).where(PaymentOrder.order_no == expired_no)
        )
        check("expired callback persists expired order status", expired_order.status == "expired")
    async with get_sessionmaker()() as session:
        async with session.begin():
            get_order, error = await create_recharge_order(session, user_id, 300)
            assert get_order is not None, error
            get_order_no = get_order.order_no
    get_notify = {
        "pid": "10001",
        "trade_no": "epay-get-1",
        "out_trade_no": get_order_no,
        "type": "alipay",
        "name": "recharge",
        "money": "3.34",
        "trade_status": "TRADE_SUCCESS",
    }
    get_notify["sign"] = sign_fields(get_notify, "smoke-key")
    async with get_sessionmaker()() as session:
        admin = await session.scalar(
            select(ConsoleUser).where(ConsoleUser.telegram_id == 900002)
        )
        admin.token_hash = hash_console_token("payment-admin-token-2")
        admin.token_expires_at = utcnow() + __import__("datetime").timedelta(minutes=15)
        await session.commit()
    with TestClient(create_app()) as client:
        response = client.get("/payments/epay/notify", params=get_notify)
        check("Epay GET notify acknowledged", response.status_code == 200 and response.text == "success")
        client.get("/console?token=payment-admin-token-2")
        toggled = client.post(f"/payments/{channel_id}/toggle", follow_redirects=False)
        check("superadmin toggles payment channel", toggled.status_code == 303)
        client.get("/payments")
        async with get_sessionmaker()() as session:
            async with session.begin():
                disabled_channel = await session.get(PaymentChannel, channel_id)
                check("channel disabled state persists", not disabled_channel.enabled)
    await dispose_db()


if __name__ == "__main__":
    asyncio.run(main())
