"""Hermetic legacy console migration regression.

Run: .venv/bin/python -B scripts/smoke_db_migrations.py
Only an in-memory database and a temporary synthetic config are used.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from sqlalchemy import create_engine, inspect, select
from sqlalchemy.orm import Session


def check_provider_attribution() -> None:
    from app.db import _upgrade_sqlite_schema
    from app.models import Base

    cases = (
        ("occupied", (("alpha", "beta"), ("beta", "beta")), ("alpha", "beta"), 0),
        ("shared", (("alpha", "target"), ("beta", "target")), ("alpha", "beta"), 0),
        ("chain", (("alpha", "beta"), ("beta", "target")), ("alpha", "target"), 0),
        ("cycle", (("alpha", "beta"), ("beta", "alpha")), ("alpha", "beta"), 0),
        ("safe", (("alpha", "target"), ("beta", "other")), ("target", "other"), 0),
        ("empty", (("alpha", ""), ("beta", "beta")), ("alpha", "beta"), 0),
        ("v1-applied", (("alpha", "target"), ("beta", "beta")), ("alpha", "beta"), 1),
        ("v2-applied", (("alpha", "target"), ("beta", "beta")), ("alpha", "beta"), 2),
        ("v3-applied", (("alpha", "target"), ("beta", "beta")), ("alpha", "beta"), 3),
    )
    for case, providers, expected_names, applied in cases:
        for reverse in (False, True):
            ordered_providers = providers[::-1] if reverse else providers
            ordered_names = expected_names[::-1] if reverse else expected_names
            engine = create_engine("sqlite:///:memory:")
            try:
                with engine.begin() as connection:
                    Base.metadata.create_all(connection)
                    for identity, (name, label) in enumerate(ordered_providers, start=1):
                        _ = connection.exec_driver_sql(
                            "INSERT INTO ai_providers (id, name, label) VALUES (?, ?, ?)",
                            (identity, name, label),
                        )
                        _ = connection.exec_driver_sql(
                            "INSERT INTO ai_events (id, provider, prompt) VALUES (?, ?, ?)",
                            (identity, name, f"source-{identity}"),
                        )
                    _ = connection.exec_driver_sql(
                        "INSERT INTO ai_events (id, provider, prompt) VALUES (99, 'unknown', 'orphan')"
                    )
                    _ = connection.exec_driver_sql(
                        "CREATE TABLE groupguard_migrations (name TEXT PRIMARY KEY NOT NULL)"
                    )
                    for version in range(1, applied + 1):
                        _ = connection.exec_driver_sql(
                            "INSERT INTO groupguard_migrations (name) VALUES (?)",
                            (f"ai_provider_names_match_labels_v{version}",),
                        )

                for upgrade in (1, 2):
                    with engine.begin() as connection:
                        _upgrade_sqlite_schema(connection)
                    with engine.connect() as connection:
                        events = connection.exec_driver_sql(
                            "SELECT id, provider, prompt FROM ai_events ORDER BY id"
                        ).fetchall()
                        expected_events = [
                            (identity, name, f"source-{identity}")
                            for identity, name in enumerate(ordered_names, start=1)
                        ] + [(99, "unknown", "orphan")]
                        assert events == expected_events, (case, reverse, upgrade, events)
                        names = (
                            connection.exec_driver_sql("SELECT name FROM ai_providers ORDER BY id")
                            .scalars()
                            .all()
                        )
                        assert names == list(ordered_names), (case, reverse, upgrade, names)
                        attribution = connection.exec_driver_sql(
                            "SELECT event.id, provider.id FROM ai_events event "
                            "LEFT JOIN ai_providers provider ON provider.name = event.provider "
                            "ORDER BY event.id"
                        ).fetchall()
                        assert attribution == [(1, 1), (2, 2), (99, None)], attribution
                    print(f"PASS providers {case} reverse={reverse} upgrade={upgrade}: {events!r}")
            finally:
                engine.dispose()


def check_legacy_provider_gains_api_mode() -> None:
    from app.db import _upgrade_sqlite_schema
    from app.models import Base

    engine = create_engine("sqlite:///:memory:")
    try:
        with engine.begin() as connection:
            Base.metadata.create_all(connection)
            _ = connection.exec_driver_sql("ALTER TABLE ai_providers DROP COLUMN api_mode")
            _ = connection.exec_driver_sql(
                "ALTER TABLE ai_providers DROP COLUMN vision_api_mode"
            )
            _ = connection.exec_driver_sql(
                "INSERT INTO ai_providers (id, name, label, base_url, model) "
                "VALUES (1, 'legacy', 'legacy', 'https://legacy.example/v1', 'm')"
            )

        with engine.begin() as connection:
            _upgrade_sqlite_schema(connection)

        with engine.connect() as connection:
            row = connection.exec_driver_sql(
                "SELECT api_mode, vision_api_mode FROM ai_providers WHERE id = 1"
            ).fetchone()
            assert row == ("chat", "chat"), row
        print("PASS legacy provider table gains api_mode and vision_api_mode defaulting to chat")
    finally:
        engine.dispose()


def main() -> None:
    """Preserve legacy account data and schema across two committed upgrades."""
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    with TemporaryDirectory(prefix="groupguard-db-migrations-") as temporary:
        config_path = Path(temporary) / "config.json"
        _ = config_path.write_text(
            json.dumps(
                {
                    "bot": {"token": "000000:synthetic-migration-test", "admin_ids": []},
                    "database": {"url": "sqlite+aiosqlite:///:memory:"},
                    "web": {"secret_key": "synthetic-migration-secret"},
                    "ai": {"providers": [], "learning_dir": str(Path(temporary) / "learning")},
                }
            ),
            encoding="utf-8",
        )
        with patch.dict(os.environ, {"GROUPGUARD_CONFIG": str(config_path)}):
            from app.db import _upgrade_sqlite_schema
            from app.models import Base, ConsoleUser

            engine = create_engine("sqlite:///:memory:")
            try:
                with engine.begin() as connection:
                    Base.metadata.create_all(connection)
                    _ = connection.exec_driver_sql(
                        """ALTER TABLE console_users ADD COLUMN
                        console_path VARCHAR(32) NOT NULL DEFAULT 'legacy'"""
                    )
                    _ = connection.exec_driver_sql(
                        """INSERT INTO console_users
                        (id, telegram_id, display_name, token_hash, role, is_active,
                         balance_cents, ai_text_daily_quota, ai_image_daily_quota)
                        VALUES
                        (901, 1901, 'Quota account', 'quota-hash', 'user', 1, 1234, 71, 13),
                        (902, 1902, 'Nullable account', 'nullable-hash', 'user', 1, 4321, NULL, NULL)
                        """
                    )

                for upgrade in (1, 2):
                    with engine.begin() as connection:
                        _upgrade_sqlite_schema(connection)

                    with Session(engine) as session:
                        users = session.scalars(select(ConsoleUser).order_by(ConsoleUser.id)).all()
                        accounts = [
                            (
                                user.id,
                                user.telegram_id,
                                user.balance_cents,
                                user.ai_text_daily_quota,
                                user.ai_image_daily_quota,
                            )
                            for user in users
                        ]
                        assert accounts == [
                            (901, 1901, 1234, 71, 13),
                            (902, 1902, 4321, None, None),
                        ], f"upgrade {upgrade} changed account data: {accounts!r}"
                    print(f"PASS upgrade {upgrade}: full ConsoleUser ORM reads {accounts!r}")

                    schema = inspect(engine)
                    columns = {
                        column["name"]: (str(column["type"]), column["nullable"])
                        for column in schema.get_columns("console_users")
                    }
                    assert "console_path" not in columns, columns
                    assert columns["ai_text_daily_quota"] == ("INTEGER", True), columns
                    assert columns["ai_image_daily_quota"] == ("INTEGER", True), columns
                    indices = {
                        index["name"]: (index["unique"], tuple(index["column_names"]))
                        for index in schema.get_indexes("console_users")
                    }
                    required_indices = {
                        "ix_console_users_telegram_id": (True, ("telegram_id",)),
                        "ix_console_users_token_hash": (True, ("token_hash",)),
                        "ix_console_users_token_expires_at": (False, ("token_expires_at",)),
                        "ix_console_users_role": (False, ("role",)),
                        "ix_console_users_is_active": (False, ("is_active",)),
                    }
                    for name, definition in required_indices.items():
                        assert indices.get(name) == definition, (name, indices)
                    print(
                        f"PASS upgrade {upgrade}: legacy path removed; nullable quotas and indices intact"
                    )
            finally:
                engine.dispose()
            check_provider_attribution()
            check_legacy_provider_gains_api_mode()


if __name__ == "__main__":
    main()
