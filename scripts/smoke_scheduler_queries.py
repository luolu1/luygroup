from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

from sqlalchemy import event, func, select

ROOT = Path(__file__).resolve().parents[1]
TMP = Path(tempfile.mkdtemp(prefix="groupguard-scheduler-query-smoke-"))
TMP_CONFIG = TMP / "config.json"
config = json.loads((ROOT / "config.example.json").read_text(encoding="utf-8"))
config["bot"]["token"] = "123456:smoke-token"
config["web"]["secret_key"] = "scheduler-query-smoke-secret"
config["database"]["url"] = f"sqlite+aiosqlite:///{TMP / 'scheduler.db'}"
TMP_CONFIG.write_text(json.dumps(config), encoding="utf-8")
os.environ["GROUPGUARD_CONFIG"] = str(TMP_CONFIG)
sys.path.insert(0, str(ROOT))

from app.db import dispose_db, get_engine, init_db, session_scope  # noqa: E402
from app.models import (  # noqa: E402
    AdminLog,
    Group,
    Member,
    RequiredChannel,
    Verification,
    utcnow,
)
from app.services.scheduler import cleanup_members, expire_verifications  # noqa: E402


class FakeBot:
    async def ban_chat_member(self, *_args, **_kwargs) -> None:
        return None

    async def unban_chat_member(self, *_args, **_kwargs) -> None:
        return None

    async def get_chat_member(self, *_args, **_kwargs):
        return type("ChatMember", (), {"status": "member"})()

    async def send_message(self, *_args, **_kwargs) -> None:
        return None

    async def delete_message(self, *_args, **_kwargs) -> None:
        return None


async def main() -> None:
    await init_db()
    statement_count = 0

    def count_statement(*_args) -> None:
        nonlocal statement_count
        statement_count += 1

    event.listen(get_engine().sync_engine, "before_cursor_execute", count_statement)
    group_ids = (-100811001, -100811002)
    user_ids = (811001, 811002, 811003)
    old = utcnow() - timedelta(minutes=10)
    async with session_scope() as session:
        session.add_all(
            [
                Group(id=group_ids[0], title="Active", is_active=True, verify_enabled=True),
                Group(id=group_ids[1], title="Inactive", is_active=False),
                RequiredChannel(group_id=group_ids[0], channel_id=-100811101),
                Member(group_id=group_ids[0], user_id=user_ids[0], is_verified=True),
                Verification(
                    group_id=group_ids[0], user_id=user_ids[0], token="query-a",
                    status="pending", created_at=old, message_id=1,
                ),
                Verification(
                    group_id=group_ids[0], user_id=user_ids[1], token="query-b",
                    status="pending", created_at=old, message_id=2,
                ),
                Verification(
                    group_id=group_ids[1], user_id=user_ids[2], token="query-c",
                    status="pending", created_at=old, message_id=3,
                ),
                Member(
                    group_id=group_ids[0], user_id=811010, is_muted=True,
                    muted_until=old,
                ),
                Member(
                    group_id=group_ids[0], user_id=811011, is_muted=True,
                    muted_until=old,
                ),
            ]
        )

    statement_count = 0
    await expire_verifications(FakeBot())
    expire_statements = statement_count
    async with session_scope() as session:
        statuses = {
            (int(row.group_id), int(row.user_id)): row.status
            for row in (
                await session.scalars(
                    select(Verification).where(Verification.token.in_(("query-a", "query-b", "query-c")))
                )
            ).all()
        }
        verified = await session.scalar(
            select(Member.is_verified).where(
                Member.group_id == group_ids[0], Member.user_id == user_ids[0]
            )
        )
        log_count = int(
            await session.scalar(
                select(func.count(AdminLog.id)).where(
                    AdminLog.action == "verify_timeout",
                    AdminLog.group_id == group_ids[0],
                )
            )
            or 0
        )
    assert statuses[(group_ids[0], user_ids[0])] == "timeout"
    assert statuses[(group_ids[0], user_ids[1])] == "timeout"
    assert statuses[(group_ids[1], user_ids[2])] == "expired"
    assert verified is False
    assert log_count == 2
    assert expire_statements <= 14, expire_statements
    print(f"PASS verification expiry batches reads and preserves state ({expire_statements} statements)")

    statement_count = 0
    await cleanup_members()
    cleanup_statements = statement_count
    async with session_scope() as session:
        muted = int(
            await session.scalar(
                select(func.count(Member.id)).where(
                    Member.user_id.in_((811010, 811011)), Member.is_muted.is_(True)
                )
            )
            or 0
        )
    assert muted == 0
    assert cleanup_statements <= 3, cleanup_statements
    print(f"PASS expired mute cleanup is set-based ({cleanup_statements} statements)")
    event.remove(get_engine().sync_engine, "before_cursor_execute", count_statement)
    await dispose_db()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    finally:
        import shutil

        shutil.rmtree(TMP, ignore_errors=True)
