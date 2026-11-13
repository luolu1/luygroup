from __future__ import annotations

import asyncio
import atexit
import json
import os
import sys
import tempfile
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import event

ROOT = Path(__file__).resolve().parents[1]
CLEANUP = ExitStack()
_ = atexit.register(CLEANUP.close)
TMP = Path(CLEANUP.enter_context(tempfile.TemporaryDirectory(
    prefix="groupguard-entitlement-smoke-", dir="/tmp/opencode"
)))
TMP_CONFIG = TMP / "config.json"
config = {
    "bot": {"token": "123456789:ENTITLEMENT_SMOKE_SYNTHETIC_TOKEN", "admin_ids": [891000004],
            "username": "entitlement_smoke_bot", "proxy": ""},
    "web": {"secret_key": "entitlement-smoke-secret", "base_url": "http://testserver"},
    "database": {"url": f"sqlite+aiosqlite:///{TMP / 'entitlements.db'}"},
    "ai": {"learning_dir": str(TMP / "learning"), "providers": []},
    "payments": {"enabled": False},
}
TMP_CONFIG.write_text(json.dumps(config), encoding="utf-8")
_ = CLEANUP.enter_context(patch.dict(os.environ, {"GROUPGUARD_CONFIG": str(TMP_CONFIG)}, clear=True))
sys.path.insert(0, str(ROOT))

from app.db import dispose_db, get_engine, init_db, session_scope  # noqa: E402
from app.entitlements import (  # noqa: E402
    GroupAccessCache,
    group_feature_allowed,
    group_owner_user_id,
)
from app.models import ConsoleGroupAccess, ConsoleUser  # noqa: E402
from app.bot.handlers import ai_chat  # noqa: E402
from app.services import ai, quota, repo  # noqa: E402


async def run_tests() -> None:
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
        session.add(ConsoleGroupAccess(user_id=user.id, group_id=-100821001, role="owner"))

    statement_count = 0

    def count_statement(*_args) -> None:
        nonlocal statement_count
        statement_count += 1

    event.listen(get_engine().sync_engine, "before_cursor_execute", count_statement)
    _ = CLEANUP.callback(event.remove, get_engine().sync_engine, "before_cursor_execute", count_statement)
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

    bound_group_id = -100821011
    unbound_group_id = -100821012
    async with session_scope() as session:
        bound_user = ConsoleUser(
            telegram_id=821011,
            display_name="Subscriber",
            token_hash="entitlement-subscriber-token",
            role="subscriber",
            is_active=True,
        )
        session.add(bound_user)
        await session.flush()
        session.add(
            ConsoleGroupAccess(user_id=bound_user.id, group_id=bound_group_id, role="owner")
        )
        await repo.ensure_group(session, bound_group_id, title="Bound AI")
        await repo.ensure_group(session, unbound_group_id, title="Platform AI")

    statement_count = 0
    async with session_scope() as session:
        bound_group = await repo.ensure_group(session, bound_group_id)
        assert await group_feature_allowed(session, bound_group.id, "ai")
        assert await quota.allow_request(session, bound_group)
        assert await group_owner_user_id(session, bound_group.id)
    uncached_count = statement_count
    assert uncached_count == 8, uncached_count
    print(
        "PASS uncached entitlement path performs repeated access lookups "
        f"({uncached_count} statements)"
    )

    class FakeMessage:
        def __init__(self, chat_id: int) -> None:
            self.text = "/ask smoke question"
            self.chat = SimpleNamespace(
                id=chat_id,
                type="supergroup",
                title="Smoke group",
                username="",
            )
            self.from_user = SimpleNamespace(id=821011)
            self.replies: list[str] = []

        async def reply(self, text: str, **_kwargs) -> None:
            self.replies.append(text)

    async def fake_assistant_reply(*_args, **_kwargs):
        return SimpleNamespace(
            ok=True,
            text="smoke answer",
            provider="smoke",
            system_attempts=0,
            latency_ms=1,
            error="",
        )

    original_assistant_reply = ai.assistant_reply
    ai.assistant_reply = fake_assistant_reply
    try:
        for group_id, label in (
            (bound_group_id, "bound"),
            (unbound_group_id, "unbound"),
        ):
            statement_count = 0
            message = FakeMessage(group_id)
            await ai_chat.cmd_ask(message, SimpleNamespace())
            assert message.replies == ["smoke answer"], (label, message.replies)
            expected_count = 6 if label == "bound" else 5
            assert statement_count == expected_count, (label, statement_count)
            print(
                f"PASS /ask {label} group reuses access and replies ({statement_count} statements)"
            )
    finally:
        ai.assistant_reply = original_assistant_reply

async def main() -> None:
    try:
        await run_tests()
    finally:
        await dispose_db()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    finally:
        atexit.unregister(CLEANUP.close)
        CLEANUP.close()
