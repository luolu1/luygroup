"""订阅余额购买、续费与并发扣款回归测试。

使用临时配置与 SQLite 数据库，不触碰真实账户余额：

    .venv/bin/python scripts/smoke_subscription_purchase.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

from sqlalchemy import select

ROOT = Path(__file__).resolve().parents[1]
TMP = Path(tempfile.mkdtemp(prefix="groupguard-subscription-"))
DB = TMP / "subscription.db"
TMP_CONFIG = TMP / "config.json"

config = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
config["database"]["url"] = f"sqlite+aiosqlite:///{DB}"
config.setdefault("ai", {})["learning_dir"] = str(TMP / "learning")
TMP_CONFIG.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
os.environ["GROUPGUARD_CONFIG"] = str(TMP_CONFIG)
sys.path.insert(0, str(ROOT))

from app.db import dispose_db, get_sessionmaker, init_db  # noqa: E402
from app.models import ConsoleUser, Subscription, SubscriptionPlan, utcnow  # noqa: E402
from app.services.subscriptions import add_calendar_month, purchase_month  # noqa: E402


async def create_user(telegram_id: int, balance: int) -> int:
    async with get_sessionmaker()() as session:
        async with session.begin():
            user = ConsoleUser(
                telegram_id=telegram_id,
                display_name=f"User {telegram_id}",
                token_hash=f"hash-{telegram_id}",
                role="user",
                is_active=True,
                balance_cents=balance,
            )
            session.add(user)
            await session.flush()
            return user.id


async def buy(user_id: int) -> tuple[str, datetime | None, int | None]:
    async with get_sessionmaker()() as session:
        async with session.begin():
            return await purchase_month(session, user_id)


async def snapshot(user_id: int) -> tuple[int, list[tuple[str, datetime | None, int]]]:
    async with get_sessionmaker()() as session:
        user = await session.get(ConsoleUser, user_id)
        rows = (
            await session.scalars(
                select(Subscription)
                .where(Subscription.user_id == user_id)
                .order_by(Subscription.id)
            )
        ).all()
        return int(user.balance_cents), [
            (row.status, row.expires_at, row.plan_id) for row in rows
        ]


def check(name: str, condition: bool, detail: str = "") -> None:
    prefix = "PASS" if condition else "FAIL"
    print(f"{prefix} {name}" + (f" · {detail}" if detail and not condition else ""))
    if not condition:
        raise AssertionError(name)


async def main() -> None:
    await init_db()
    async with get_sessionmaker()() as session:
        plan = await session.scalar(
            select(SubscriptionPlan).where(SubscriptionPlan.name == "pro")
        )
        assert plan is not None
        plan_id = plan.id

    poor = await create_user(8200001, 799)
    result = await buy(poor)
    poor_balance, poor_subs = await snapshot(poor)
    check("insufficient balance rejected", result[0] == "insufficient_balance")
    check("insufficient balance preserves funds and subscription", poor_balance == 799 and not poor_subs)

    first = await create_user(8200002, 1600)
    first_result = await buy(first)
    first_balance, first_subs = await snapshot(first)
    check("first purchase charges CNY 8", first_result[0] == "ok" and first_balance == 800)
    check("first purchase creates one calendar-month subscription", len(first_subs) == 1 and first_subs[0][0] == "active" and first_subs[0][2] == plan_id)

    renewal = await buy(first)
    renewed_balance, renewed_subs = await snapshot(first)
    check("renewal charges once and extends from current expiry", renewal[0] == "ok" and renewed_balance == 0 and len(renewed_subs) == 1 and renewal[1] is not None and first_subs[0][1] is not None and renewal[1] > first_subs[0][1])

    expired = await create_user(8200003, 800)
    async with get_sessionmaker()() as session:
        async with session.begin():
            session.add(
                Subscription(
                    user_id=expired,
                    plan_id=plan_id,
                    status="active",
                    starts_at=utcnow(),
                    expires_at=datetime(2020, 1, 1),
                )
            )
    before = utcnow()
    expired_result = await buy(expired)
    after_balance, expired_subs = await snapshot(expired)
    check("expired subscription starts a new month from purchase", expired_result[0] == "ok" and expired_result[1] is not None and before <= expired_result[1] and after_balance == 0)
    check("expired subscription row is reactivated rather than duplicated", len(expired_subs) == 1 and expired_subs[0][0] == "active")

    permanent_user = await create_user(8200005, 2400)
    async with get_sessionmaker()() as session:
        async with session.begin():
            session.add(
                Subscription(
                    user_id=permanent_user,
                    plan_id=plan_id,
                    status="active",
                    starts_at=utcnow(),
                    expires_at=None,
                )
            )
    permanent_result = await buy(permanent_user)
    permanent_balance, permanent_rows = await snapshot(permanent_user)
    check("permanent subscription is not charged again", permanent_result[0] == "permanent_subscription" and permanent_balance == 2400 and len(permanent_rows) == 1)

    concurrent = await create_user(8200004, 800)
    results = await asyncio.gather(buy(concurrent), buy(concurrent), return_exceptions=True)
    concurrent_balance, concurrent_subs = await snapshot(concurrent)
    successful = [result for result in results if isinstance(result, tuple) and result[0] == "ok"]
    check("concurrent purchase charges at most once with one month funded", len(successful) == 1 and concurrent_balance == 0 and len(concurrent_subs) == 1)
    check("calendar-month end clamps to destination month", add_calendar_month(datetime(2026, 1, 31)) == datetime(2026, 2, 28))
    print("FAILURES: none")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    finally:
        asyncio.run(dispose_db())
