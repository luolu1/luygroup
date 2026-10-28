from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

from sqlalchemy import event

ROOT = Path(__file__).resolve().parents[1]
TMP = Path(tempfile.mkdtemp(prefix="groupguard-entitlement-smoke-"))
TMP_CONFIG = TMP / "config.json"
config = json.loads((ROOT / "config.example.json").read_text(encoding="utf-8"))
config["bot"]["token"] = "123456:smoke-token"
config["web"]["secret_key"] = "entitlement-smoke-secret"
config["database"]["url"] = f"sqlite+aiosqlite:///{TMP / 'entitlements.db'}"
TMP_CONFIG.write_text(json.dumps(config), encoding="utf-8")
os.environ["GROUPGUARD_CONFIG"] = str(TMP_CONFIG)
sys.path.insert(0, str(ROOT))

from app.db import dispose_db, get_engine, init_db, session_scope  # noqa: E402
from app.entitlements import GroupAccessCache, group_feature_allowed  # noqa: E402
from app.models import ConsoleGroupAccess, ConsoleUser  # noqa: E402


async def main() -> None:
    await init_db()
    async with session_scope() as session:
        user = ConsoleUser(
            telegram_id=821001,
            display_name="Owner",
            token_hash="entitlement-owner-token",
            role="user",
            is_active=True,
        )
        session.add(user)
        await session.flush()
        session.add(
            ConsoleGroupAccess(user_id=user.id, group_id=-100821001, role="owner")
        )

    statement_count = 0

    def count_statement(*_args) -> None:
        nonlocal statement_count
        statement_count += 1

    event.listen(get_engine().sync_engine, "before_cursor_execute", count_statement)
    async with session_scope() as session:
        assert await group_feature_allowed(session, -100821001, "settings")
        baseline_count = statement_count
        statement_count = 0
        access = GroupAccessCache(session, -100821001)
        assert await access.can("settings")
        assert not await access.can("ai")
        assert not await access.can("games")
        cached_count = statement_count
    assert baseline_count <= 3, baseline_count
    assert cached_count <= 4, cached_count
    print(
        "PASS lazy group access preserves basic-user permissions "
        f"(single check={baseline_count}, 3 cached checks={cached_count} statements)"
    )

    statement_count = 0
    async with session_scope() as session:
        unbound = GroupAccessCache(session, -100821099)
        assert await unbound.can("ai")
        assert await unbound.can("games")
    unbound_count = statement_count
    assert unbound_count == 1, unbound_count
    print(f"PASS platform-managed group stays unrestricted ({unbound_count} statement)")

    event.remove(get_engine().sync_engine, "before_cursor_execute", count_statement)
    await dispose_db()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    finally:
        import shutil

        shutil.rmtree(TMP, ignore_errors=True)
