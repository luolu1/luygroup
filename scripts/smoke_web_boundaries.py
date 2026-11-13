"""Synthetic HTTP regressions: .venv/bin/python scripts/smoke_web_boundaries.py."""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from collections.abc import Mapping
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Final, TypedDict
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

ROOT: Final = Path(__file__).resolve().parents[1]
TRUSTED: Final = "https://trusted.invalid/v1"
UNTRUSTED: Final = "https://untrusted.invalid/v1"
PREVIEW: Final = "http://127.0.0.1:11434/v1"


class ModelRequest(TypedDict, total=False):
    provider_id: int
    base_url: str
    api_key: str
    kind: str


def seed_provider(connection: sqlite3.Connection, provider_id: int, owner: int | None) -> None:
    name = f"synthetic-{provider_id}"
    with connection:
        connection.execute(
            "INSERT INTO ai_providers "
            "(id, name, label, owner_user_id, base_url, api_key, model, vision_model, "
            "priority, enabled, builtin) VALUES (?, ?, ?, ?, ?, ?, 'original', 'vision', 10, 1, 0)",
            (provider_id, name, name, owner, TRUSTED, f"synthetic-key-{provider_id}"),
        )
        connection.execute(
            "INSERT INTO ai_events (id, group_id, provider) VALUES (?, -990001, ?)",
            (provider_id, name),
        )


def exercise(client: TestClient, connection: sqlite3.Connection) -> None:
    from app.web.auth import hash_console_token

    failures: list[str] = []

    def check(name: str, passed: bool, observed: str = "") -> None:
        print(f"{'PASS' if passed else 'FAIL'} {name} {observed}")
        if not passed:
            failures.append(name)

    def post(path: str, data: Mapping[str, str]) -> httpx.Response:
        return client.post(path, data=data, follow_redirects=False)

    def login(user_id: int) -> None:
        token = f"synthetic-login-{user_id}"
        with connection:
            connection.execute(
                "UPDATE console_users SET token_hash=?, "
                "token_expires_at=datetime('now', '+1 hour') WHERE id=?",
                (hash_console_token(token), user_id),
            )
        response = post("/login", {"token": token})
        assert response.status_code == 303, response.status_code

    login(102)
    calls: list[tuple[str, str]] = []
    original_client = httpx.AsyncClient

    def capture(request: httpx.Request) -> httpx.Response:
        calls.append((str(request.url), request.headers.get("Authorization", "")))
        return httpx.Response(200, json={"data": [{"id": "gpt-4o-mini"}]})

    def isolated_client(*, timeout: float) -> httpx.AsyncClient:
        return original_client(
            transport=httpx.MockTransport(capture), timeout=timeout, trust_env=False
        )

    shared = (TRUSTED, "synthetic-key-301")
    owned = (TRUSTED, "synthetic-key-302")
    explicit = (PREVIEW, "explicit-key")
    model_cases: tuple[tuple[str, ModelRequest, tuple[str, str]], ...] = (
        ("shared_listing", {"provider_id": 301}, shared),
        ("shared_override", {"provider_id": 301, "base_url": UNTRUSTED}, shared),
        ("shared_vision", {"provider_id": 301, "base_url": PREVIEW, "kind": "vision"}, shared),
        ("shared_key", {"provider_id": 301, "base_url": PREVIEW, "api_key": explicit[1]}, shared),
        ("own_listing", {"provider_id": 302}, owned),
        (
            "own_new_key",
            {"provider_id": 302, "base_url": UNTRUSTED, "api_key": explicit[1]},
            (UNTRUSTED, explicit[1]),
        ),
        (
            "new_provider_key",
            {"base_url": UNTRUSTED, "api_key": explicit[1]},
            (UNTRUSTED, explicit[1]),
        ),
    )
    internal_cases: tuple[tuple[str, ModelRequest], ...] = (
        ("subscriber_preview_rejected", {"provider_id": 302, "base_url": "127.0.0.1:11434/v1"}),
        ("subscriber_new_internal_rejected", {"base_url": PREVIEW, "api_key": explicit[1]}),
        ("subscriber_private_rejected", {"base_url": "http://10.0.0.5/v1"}),
        ("subscriber_localhost_rejected", {"base_url": "http://localhost:8080/v1"}),
    )
    with patch.object(httpx, "AsyncClient", isolated_client):
        for name, payload, (endpoint, key) in model_cases:
            calls.clear()
            response = client.post("/ai/models", json=payload)
            check(
                name,
                response.status_code == 200
                and response.json() == {"ok": True, "models": ["gpt-4o-mini"]}
                and calls == [(f"{endpoint}/models", f"Bearer {key}")],
                repr(calls),
            )
        for name, payload in internal_cases:
            calls.clear()
            response = client.post("/ai/models", json=payload)
            check(name, response.status_code == 400 and not calls, repr((response.status_code, calls)))
        calls.clear()
        response = client.post("/ai/models", json={"provider_id": 303, "base_url": PREVIEW})
        check("foreign_private_provider", response.status_code == 404 and not calls)
    preview_state = connection.execute(
        "SELECT base_url, api_key FROM ai_providers WHERE id=302"
    ).fetchone()
    check("previews_are_unsaved", preview_state == owned)

    state_sql = (
        "SELECT p.name, p.label, p.base_url, p.api_key, p.model, p.vision_model, "
        "p.priority, p.enabled, e.provider FROM ai_providers p "
        "JOIN ai_events e ON e.id=p.id WHERE p.id=?"
    )
    invalid_fields = (
        {"base_url": "file:///invalid"},
        {"base_url": ""},
        {"model": ""},
        {"name": ""},
        {"name": "synthetic-301"},
    )
    for provider_id, invalid in enumerate(invalid_fields, start=310):
        seed_provider(connection, provider_id, 102)
        before = connection.execute(state_sql, (provider_id,)).fetchone()
        fields = {
            "name": f"renamed-{provider_id}",
            "api_key": "replacement",
            "model": "changed",
            "vision_model": "new-vision",
            "priority": "77",
            **invalid,
        }
        response = post(f"/ai/providers/{provider_id}/update", fields)
        after = connection.execute(state_sql, (provider_id,)).fetchone()
        check(
            f"invalid_edit_{provider_id}",
            response.status_code == 303
            and "err=" in response.headers.get("location", "")
            and after == before,
            repr(after),
        )


    for provider_id, addr in enumerate([
        ("http://127.0.0.1:11434/v1", "subscriber cannot use 127.0.0.1"),
        ("http://10.1.2.3/v1", "subscriber cannot use 10.x private"),
        ("http://192.168.1.1/v1", "subscriber cannot use 192.168.x private"),
        ("http://172.16.0.1/v1", "subscriber cannot use 172.16.x private"),
        ("http://[::1]/v1", "subscriber cannot use IPv6 loopback"),
    ], start=320):
        seed_provider(connection, provider_id, 102)
        response = post(f"/ai/providers/{provider_id}/update", {
            "name": f"blocked-{provider_id}",
            "base_url": addr[0],
            "model": "test",
        })
        check(
            addr[1],
            response.status_code == 303 and "err=" in response.headers.get("location", ""),
            repr(response.headers.get("location")),
        )

    renamed = "renamed-owned"
    response = post("/ai/providers/302/update", {"name": renamed, "enabled": "on"})
    expected = (renamed, renamed, TRUSTED, owned[1], "original", "vision", 10, 1, renamed)
    check(
        "partial_rename_updates_events",
        response.status_code == 303
        and connection.execute(state_sql, (302,)).fetchone() == expected,
    )
    fields = {
        "base_url": UNTRUSTED,
        "model": "changed",
        "api_key": "new-key",
        "vision_model": "",
        "priority": "7",
        "enabled": "on",
    }
    response = post("/ai/providers/302/update", fields)
    expected = (renamed, renamed, UNTRUSTED, "new-key", "changed", "", 7, 1, renamed)
    check(
        "valid_edit_preserves_name",
        response.status_code == 303
        and connection.execute(state_sql, (302,)).fetchone() == expected,
    )

    login(101)
    response = post("/users/103/subscription", {"plan_id": "501", "days": "1"})
    role = connection.execute("SELECT role FROM console_users WHERE id=103").fetchone()
    check("timed_activation_keeps_role", response.status_code == 303 and role == ("user",))
    login(103)
    for path, expected_status in (("/reports", 200), ("/ai", 403)):
        response = client.get(path)
        check(f"active_limited_{path}", response.status_code == expected_status)
    with connection:
        connection.execute(
            "UPDATE subscriptions SET expires_at=datetime('now', '-1 minute') WHERE user_id=103"
        )
    for path in ("/reports", "/ai"):
        response = client.get(path)
        check(f"expired_limited_{path}", response.status_code == 403, str(response.status_code))

    login(101)
    assert post("/users/104/role", {"role": "subscriber"}).status_code == 303
    assert post("/users/104/subscription", {"plan_id": "501", "days": "1"}).status_code == 303
    with connection:
        connection.execute(
            "UPDATE subscriptions SET expires_at=datetime('now', '-1 minute') WHERE user_id=104"
        )
    login(104)
    response = client.get("/ai")
    role = connection.execute("SELECT role FROM console_users WHERE id=104").fetchone()
    check(
        "explicit_subscriber_survives_expiry",
        response.status_code == 200 and role == ("subscriber",),
    )
    login(101)
    assert post("/users/104/subscription", {"action": "cancel"}).status_code == 303
    login(104)
    check("explicit_cancellation_revokes_access", client.get("/ai").status_code == 403)
    print(f"FAILURES: {failures or 'none'}")
    assert not failures, failures


def run_cases(directory: Path) -> None:
    database = directory / "boundaries.db"
    config = directory / "synthetic.json"
    configuration = {
        "bot": {"token": "123456789:BOUNDARIES_SYNTHETIC_TOKEN", "admin_ids": [], "proxy": ""},
        "web": {"secret_key": "boundaries-synthetic-secret", "base_url": "http://testserver"},
        "database": {"url": f"sqlite+aiosqlite:///{database}"},
        "ai": {"providers": [], "learning_dir": str(directory / "learning")},
        "payments": {"enabled": False},
    }
    config.write_text(json.dumps(configuration), encoding="utf-8")
    with patch.dict(os.environ, {"GROUPGUARD_CONFIG": str(config)}, clear=True):
        sys.path.insert(0, str(ROOT))
        from app.config import get_config
        from app.web.main import app

        with TestClient(app) as client, closing(sqlite3.connect(database)) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            with connection:
                users = ((101, "superadmin"), (102, "subscriber"), (103, "user"), (104, "user"))
                for user_id, role in users:
                    connection.execute(
                        "INSERT INTO console_users "
                        "(id, telegram_id, display_name, token_hash, role, is_active, balance_cents) "
                        "VALUES (?, ?, ?, ?, ?, 1, 0)",
                        (user_id, user_id + 900000, f"Synthetic {user_id}", str(user_id), role),
                    )
                connection.execute(
                    "INSERT INTO groups (id, title, chat_type, is_active) "
                    "VALUES (-990001, 'Synthetic group', 'supergroup', 1)"
                )
                connection.execute(
                    "INSERT INTO subscription_plans "
                    "(id, name, label, features, group_limit, ai_daily_quota, sample_daily_quota, enabled) "
                    "VALUES (501, 'limited', 'Limited', '[\"reports\"]', 3, 4, 5, 1)"
                )
            for provider_id, owner in ((301, None), (302, 102), (303, 104)):
                seed_provider(connection, provider_id, owner)
            exercise(client, connection)
        get_config.cache_clear()


if __name__ == "__main__":
    with TemporaryDirectory(prefix="groupguard-web-boundaries-", dir="/tmp/opencode") as temporary:
        run_cases(Path(temporary))
