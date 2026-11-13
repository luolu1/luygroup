from __future__ import annotations

import asyncio
import atexit
import json
import os
import sys
import tempfile
from contextlib import ExitStack
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import event, select
from sqlalchemy.engine import Connection
from sqlalchemy.engine.interfaces import DBAPICursor, ExecutionContext

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ExitStack()
TMP = Path(RUNTIME.enter_context(tempfile.TemporaryDirectory(prefix="keyword-entitlements-")))
CONFIG = TMP / "config.json"
_ = atexit.register(RUNTIME.close)
try:
    _ = CONFIG.write_text(
        json.dumps(
            {
                "bot": {"token": "123456789:KEYWORD_SMOKE_SYNTHETIC", "admin_ids": []},
                "web": {"secret_key": "keyword-smoke-secret", "base_url": "http://testserver"},
                "database": {"url": f"sqlite+aiosqlite:///{TMP / 'smoke.db'}"},
                "ai": {"learning_dir": str(TMP / "learning"), "providers": []},
                "payments": {"enabled": False},
            }
        ),
        encoding="utf-8",
    )
    RUNTIME.enter_context(patch.dict(os.environ, {"GROUPGUARD_CONFIG": str(CONFIG)}, clear=True))
    sys.path.insert(0, str(ROOT))
except BaseException:
    RUNTIME.close()
    raise

try:
    from app.config import get_config  # noqa: E402
    from app.db import dispose_db, get_engine, init_db, session_scope  # noqa: E402
    from app.models import (  # noqa: E402
        ConsoleGroupAccess,
        ConsoleUser,
        Lottery,
        LotteryEntry,
        LotteryQualification,
        Member,
        Subscription,
        utcnow,
    )
    from app.services import lottery_rules  # noqa: E402
except BaseException:
    RUNTIME.close()
    raise
_ = RUNTIME.callback(get_config.cache_clear)


async def _run() -> None:
    await init_db()
    now = utcnow()
    cases = (
        ("basic", "user", True, "active", None, False),
        ("inactive", "subscriber", False, "active", None, False),
        ("expired", "user", True, "active", now - timedelta(seconds=1), False),
        ("cancelled", "user", True, "cancelled", None, False),
        ("allowed", "subscriber", True, "active", None, True),
    )
    ids: dict[str, int] = {}
    async with session_scope() as session:
        for index, (name, role, active, sub_status, expires, _allowed) in enumerate(cases):
            group_id = -100880000 - index
            owner = ConsoleUser(
                telegram_id=880000 + index,
                display_name=name,
                token_hash=f"keyword-smoke-{name}",
                role=role,
                is_active=active,
            )
            session.add(owner)
            await session.flush()
            session.add(ConsoleGroupAccess(user_id=owner.id, group_id=group_id, role="owner"))
            if name != "basic":
                session.add(
                    Subscription(
                        user_id=owner.id,
                        plan_id=1,
                        status=sub_status,
                        starts_at=now - timedelta(days=2),
                        expires_at=expires,
                    )
                )
            session.add(
                Member(group_id=group_id, user_id=990000 + index, points=100, message_count=0)
            )
            for fee in (0, 7):
                lottery = Lottery(
                    id=1000 + index * 2 + fee // 7,
                    group_id=group_id,
                    creator_id=owner.id,
                    title=f"{name}-{fee}",
                    entry_fee=fee,
                    participation_type="keyword",
                    participation_value=f"enter{fee}",
                    status="open",
                    ends_at=now + timedelta(days=1),
                )
                session.add(lottery)
                await session.flush()
                ids[f"{name}-{fee}"] = 1000 + index * 2 + fee // 7
        for fee in (0, 7):
            unbound = Lottery(
                id=1100 + fee // 7,
                group_id=-100880099,
                creator_id=1,
                title=f"unbound-{fee}",
                entry_fee=fee,
                participation_type="keyword",
                participation_value=f"enter{fee}",
                status="open",
                ends_at=now + timedelta(days=1),
            )
            session.add(unbound)
            await session.flush()
            ids[f"unbound-{fee}"] = 1100 + fee // 7
        session.add(Member(group_id=-100880099, user_id=990099, points=100, message_count=0))

    statements: list[str] = []

    def capture(
        _conn: Connection,
        _cursor: DBAPICursor,
        statement: str,
        _parameters: object,
        _context: ExecutionContext,
        _executemany: bool,
    ) -> None:
        statements.append(statement)

    engine = get_engine().sync_engine
    event.listen(engine, "before_cursor_execute", capture)
    try:
        with patch("app.entitlements.utcnow", return_value=now), patch(
            "app.services.lottery_rules.utcnow", return_value=now
        ):
            for name, _role, _active, _status, _expires, allowed in cases:
                for fee in (0, 7):
                    statements.clear()
                    group_id = -100880000 - next(i for i, case in enumerate(cases) if case[0] == name)
                    user_id = 990000 + next(i for i, case in enumerate(cases) if case[0] == name)
                    async with session_scope() as session:
                        entered = await lottery_rules.record_keyword_matches(
                            session, group_id, user_id, f"enter{fee}"
                        )
                        expected = allowed
                        assert bool(entered) is expected, (name, fee, entered)
                        entry = await session.scalar(
                            select(LotteryEntry.id).where(
                                LotteryEntry.lottery_id == ids[f"{name}-{fee}"],
                                LotteryEntry.user_id == user_id,
                            )
                        )
                        qualification = await session.scalar(
                            select(LotteryQualification.id).where(
                                LotteryQualification.lottery_id == ids[f"{name}-{fee}"],
                                LotteryQualification.user_id == user_id,
                            )
                        )
                        points = await session.scalar(
                            select(Member.points).where(
                                Member.group_id == group_id, Member.user_id == user_id
                            )
                        )
                        assert (entry is not None) is expected
                        assert (qualification is not None) is expected
                        assert points == (93 if expected and fee else 100), (name, fee, expected, points)
        async with session_scope() as session:
            statements.clear()
            assert await lottery_rules.record_keyword_matches(session, -100880099, 990099, "other") == []
            assert len(statements) == 1, statements
            assert "lotteries" in statements[0].lower()
            assert not any("console_group_access" in sql.lower() for sql in statements)
            assert not any("subscriptions" in sql.lower() for sql in statements)
            print("PASS unmatched text uses exactly one candidate SELECT and no entitlement lookup")

            entered = await lottery_rules.record_keyword_matches(session, -100880099, 990099, "enter0")
            assert entered == ["unbound-0"]
            entered = await lottery_rules.record_keyword_matches(session, -100880099, 990099, "enter7")
            assert entered == ["unbound-7"]
            points = await session.scalar(
                select(Member.points).where(Member.group_id == -100880099, Member.user_id == 990099)
            )
            assert points == 93
            print("PASS platform-unbound group retains zero and positive-fee keyword entry")
    finally:
        event.remove(engine, "before_cursor_execute", capture)


async def main() -> None:
    try:
        await _run()
    finally:
        try:
            await dispose_db()
        finally:
            RUNTIME.close()


if __name__ == "__main__":
    asyncio.run(main())
