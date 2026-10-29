"""权限、群组隔离与 AI 接口归属回归测试。

使用临时配置与临时 SQLite 数据库，不修改真实 config.json / data/。

    .venv/bin/python scripts/smoke_permissions.py
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import asyncio
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TMP = Path(tempfile.mkdtemp(prefix="groupguard-perm-"))
DB = TMP / "permissions.db"
TMP_CONFIG = TMP / "config.json"

config_path = ROOT / "config.json"
if not config_path.exists():
    print("未找到 config.json，请先复制 config.example.json 并填写。")
    sys.exit(2)

cfg = json.loads(config_path.read_text(encoding="utf-8"))
admin_ids = [int(item) for item in cfg.get("bot", {}).get("admin_ids", [])]
if not admin_ids:
    print("config.json 的 bot.admin_ids 为空，无法初始化超级管理员。")
    sys.exit(2)

cfg["database"]["url"] = f"sqlite+aiosqlite:///{DB}"
cfg.setdefault("web", {})["base_url"] = "http://testserver"
cfg.setdefault("ai", {})["learning_dir"] = str(TMP / "learning")
TMP_CONFIG.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
os.environ["GROUPGUARD_CONFIG"] = str(TMP_CONFIG)
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402
from app.web import pages as web_pages  # noqa: E402
from app.web.auth import hash_console_token  # noqa: E402
from app.web.main import app  # noqa: E402
from app.db import session_scope  # noqa: E402
from app.entitlements import build_for_user  # noqa: E402
from app.models import ConsoleUser  # noqa: E402


async def fake_bot_membership(chat_id: int) -> dict:
    return {
        "status": "administrator",
        "can_restrict_members": True,
        "can_delete_messages": True,
        "can_invite_users": True,
    }


async def fake_group_ref(ref: str) -> dict:
    return {
        "id": -1009999999999,
        "title": "不应被添加的群",
        "username": "shouldnotadd",
        "type": "supergroup",
    }


web_pages._fetch_bot_membership = fake_bot_membership
web_pages._fetch_telegram_group = fake_group_ref
group_admin_status: dict[int, str] = {9001: "member"}


async def fake_user_membership(chat_id: int, user_id: int) -> dict:
    return {"status": group_admin_status.get(user_id, "left")}


web_pages._fetch_user_membership = fake_user_membership

failures: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    if cond:
        print(f"PASS {name}")
    else:
        failures.append(name)
        print(f"FAIL {name} {extra}")


def connect() -> sqlite3.Connection:
    return sqlite3.connect(DB)


def login(client: TestClient, token: str) -> bool:
    r = client.get(f"/console?token={token}", follow_redirects=False)
    return r.status_code == 303 and r.headers.get("location") == "/"


async def group_limit_for(user_id: int) -> int | None:
    async with session_scope() as session:
        user = await session.get(ConsoleUser, user_id)
        return (await build_for_user(session, user)).group_limit


async def plan_entitlement_for(user_id: int) -> tuple[int | None, frozenset[str]]:
    async with session_scope() as session:
        user = await session.get(ConsoleUser, user_id)
        entitlements = await build_for_user(session, user)
        return entitlements.group_limit, entitlements.features


try:
    with TestClient(app) as client:
        now = datetime.utcnow()
        future = (now + timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S.%f")
        tokens = {
            "basic": "basic-token",
            "sub": "sub-token",
            "other": "other-token",
        }
        con = connect()
        plan_id = con.execute("SELECT id FROM subscription_plans ORDER BY id LIMIT 1").fetchone()[0]
        for telegram_id, token, role, name in (
            (9001, tokens["basic"], "user", "Basic User"),
            (9002, tokens["sub"], "subscriber", "Sub User"),
            (9003, tokens["other"], "user", "Other User"),
        ):
            con.execute(
                "INSERT INTO console_users (telegram_id, display_name, token_hash, token_value, "
                "token_expires_at, role, is_active, balance_cents) VALUES (?,?,?,?,?,?,1,0)",
                (
                    telegram_id,
                    name,
                    hash_console_token(token),
                    token,
                    future,
                    role,
                ),
            )
        basic_id, sub_id, other_id = [
            row[0]
            for row in con.execute(
                "SELECT id FROM console_users WHERE telegram_id IN (9001,9002,9003) "
                "ORDER BY telegram_id"
            )
        ]
        con.execute(
            "INSERT INTO subscriptions (user_id, plan_id, status, starts_at, expires_at) "
            "VALUES (?,?, 'active', ?, ?)",
            (
                sub_id,
                plan_id,
                now.strftime("%Y-%m-%d %H:%M:%S.%f"),
                future,
            ),
        )
        con.execute(
            "INSERT INTO console_users (telegram_id, display_name, token_hash, role, is_active, balance_cents) "
            "VALUES (9004, 'Custom Plan User', ?, 'subscriber', 1, 0)",
            (hash_console_token("custom-plan-token"),),
        )
        custom_user_id = int(
            con.execute("SELECT id FROM console_users WHERE telegram_id=9004").fetchone()[0]
        )
        custom_plan_id = int(
            con.execute(
                "INSERT INTO subscription_plans (name, label, features, group_limit, "
                "ai_daily_quota, sample_daily_quota, enabled) VALUES (?, ?, ?, ?, ?, ?, 1)",
                ("smoke-limited", "Limited", '["games"]', 3, 17, 23),
            ).lastrowid
        )
        con.execute(
            "INSERT INTO subscriptions (user_id, plan_id, status, starts_at, expires_at) "
            "VALUES (?, ?, 'active', ?, ?)",
            (
                custom_user_id,
                custom_plan_id,
                now.strftime("%Y-%m-%d %H:%M:%S.%f"),
                future,
            ),
        )
        for gid, title, username in (
            (-1001111111111, "基础用户的群", "basicgroup"),
            (-1002222222222, "订阅用户的群", "subgroup"),
            (-1003333333333, "无权访问的群", "othergroup"),
        ):
            con.execute(
                "INSERT INTO groups (id, title, username, chat_type, is_active) "
                "VALUES (?,?,?, 'supergroup', 1)",
                (gid, title, username),
            )
        con.execute(
            "INSERT INTO console_group_access (user_id, group_id, role) VALUES (?,?, 'owner')",
            (basic_id, -1001111111111),
        )
        con.execute(
            "INSERT INTO console_group_access (user_id, group_id, role) VALUES (?,?, 'owner')",
            (sub_id, -1002222222222),
        )
        blacklist_ids: dict[str, int] = {}
        for key, uid, owner in (
            ("platform", 8000001, None),
            ("sub", 8000002, sub_id),
            ("other", 8000003, other_id),
        ):
            cur = con.execute(
                "INSERT INTO global_blacklist (user_id, owner_user_id, reason, added_by) "
                "VALUES (?,?,?,?)",
                (uid, owner, key, owner),
            )
            blacklist_ids[key] = int(cur.lastrowid)
        provider_ids: dict[str, int] = {}
        for name, owner in (
            ("system-provider", None),
            ("basic-provider", basic_id),
            ("sub-provider", sub_id),
            ("other-provider", other_id),
        ):
            cur = con.execute(
                "INSERT INTO ai_providers (owner_user_id, name, label, base_url, api_key, "
                "model, priority, enabled, builtin) VALUES (?,?,?,?,?,?,?,1,0)",
                (
                    owner,
                    name,
                    name,
                    "https://api.example.com/v1",
                    "sk-test",
                    "test-model",
                    100,
                ),
            )
            provider_ids[name] = int(cur.lastrowid)
        con.commit()
        con.close()

        # ----------------------------------------------------------- basic user
        check("basic login", login(client, tokens["basic"]))
        check("ordinary user group limit is two", asyncio.run(group_limit_for(basic_id)) == 2)
        r = client.get("/")
        check(
            "basic role label is not administrator",
            r.status_code == 200
            and "普通用户" in r.text
            and "超级管理员" not in r.text
            and "管理员 · " not in r.text,
            r.text[:500],
        )
        for path in ("/ai", "/ai/add", "/blacklist", "/reports", "/stats", "/users"):
            r = client.get(path)
            check(f"basic denied {path}", r.status_code == 403, f"{r.status_code} {r.text[:200]}")
        r = client.get("/groups")
        check(
            "basic sees only own group",
            r.status_code == 200
            and "基础用户的群" in r.text
            and "订阅用户的群" not in r.text
            and "无权访问的群" not in r.text,
            r.text[:500],
        )
        r = client.get("/groups/-1001111111111?tab=ai", follow_redirects=False)
        check(
            "basic advanced tab redirects",
            r.status_code == 303 and "tab=settings" in r.headers.get("location", ""),
            str(r.headers),
        )
        r = client.get("/groups/-1002222222222")
        check("basic cannot open another group", r.status_code == 404, str(r.status_code))
        r = client.post(
            "/groups/-1001111111111/settings",
            data={"_scope": "ai", "ai_ad_enabled": "on"},
            follow_redirects=False,
        )
        check("basic cannot save ai scope", r.status_code == 403, str(r.status_code))
        r = client.post(
            "/groups/-1001111111111/settings",
            data={
                "_scope": "general",
                "rules_text": "基础用户可改群规",
                "welcome_enabled": "on",
                "welcome_text": "不应保存的欢迎语",
                "crossban_enabled": "on",
                "points_enabled": "on",
            },
            follow_redirects=False,
        )
        check("basic can save general scope", r.status_code == 303, str(r.status_code))
        con = connect()
        row = con.execute(
            "SELECT rules_text, welcome_enabled, welcome_text, crossban_enabled, points_enabled "
            "FROM groups WHERE id=-1001111111111"
        ).fetchone()
        con.close()
        check(
            "basic cannot change advanced fields",
            row[0] == "基础用户可改群规"
            and not row[1]
            and not row[2]
            and not row[3]
            and not row[4],
            repr(row),
        )
        r = client.post(
            "/groups/add", data={"chat": "@shouldnotadd"}, follow_redirects=False
        )
        check(
            "non-admin cannot claim an unbound group",
            r.status_code == 303 and "err=" in r.headers.get("location", ""),
            str(r.headers),
        )
        con = connect()
        stray = con.execute("SELECT COUNT(*) FROM groups WHERE id=-1009999999999").fetchone()[0]
        con.close()
        check("non-admin group claim creates no group record", stray == 0, str(stray))
        group_admin_status[9001] = "administrator"
        r = client.post(
            "/groups/add", data={"chat": "@shouldnotadd"}, follow_redirects=False
        )
        check(
            "group admin can bind an unbound group",
            r.status_code == 303 and "msg=" in r.headers.get("location", ""),
            str(r.headers),
        )
        con = connect()
        stray = con.execute("SELECT COUNT(*) FROM groups WHERE id=-1009999999999").fetchone()[0]
        con.close()
        check("admin binding creates group record", stray == 1, str(stray))
        async def third_group(_ref: str) -> dict:
            return {
                "id": -1009999999998,
                "title": "第三个群",
                "username": "thirdgroup",
                "type": "supergroup",
            }

        web_pages._fetch_telegram_group = third_group
        r = client.post(
            "/groups/add", data={"chat": "@thirdgroup"}, follow_redirects=False
        )
        check(
            "basic third group is rejected",
            r.status_code == 303 and "err=" in r.headers.get("location", ""),
            str(r.headers),
        )
        con = connect()
        third = con.execute("SELECT COUNT(*) FROM groups WHERE id=-1009999999998").fetchone()[0]
        con.close()
        check("basic third group was not created", third == 0, str(third))
        client.get("/logout")

        # ------------------------------------------------------- subscriber user
        check("subscriber login", login(client, tokens["sub"]))
        check("subscriber group limit is five", asyncio.run(group_limit_for(sub_id)) == 5)
        custom_limit, custom_features = asyncio.run(plan_entitlement_for(custom_user_id))
        check(
            "custom plan applies feature list and group limit",
            custom_limit == 3
            and "games" in custom_features
            and "ai" not in custom_features
            and "settings" in custom_features,
            f"limit={custom_limit} features={sorted(custom_features)}",
        )
        r = client.get("/account")
        check("subscriber can access account and renewal page", r.status_code == 200 and "续费一个月" in r.text and "¥8.00" in r.text)
        for path in ("/ai", "/blacklist", "/reports", "/stats"):
            r = client.get(path)
            check(f"subscriber allowed {path}", r.status_code == 200, str(r.status_code))

        r = client.get("/ai")
        check(
            "subscriber AI page hides admin-only details",
            r.status_code == 200
            and "已学习条目" in r.text
            and "今日 AI 用量" in r.text
            and "分类目录" not in r.text
            and '<span class="k">分类</span>' not in r.text
            and '<span class="k">存储目录</span>' not in r.text
            and '<span class="fact-label">接口</span>' not in r.text
            and 'action="/ai/learning/dir"' not in r.text,
            r.text[:800],
        )
        con = connect()
        today = datetime.utcnow().strftime("%Y-%m-%d")
        con.execute(
            "INSERT INTO ai_usage (group_id, day, requests, samples, hits) VALUES (?, ?, ?, ?, ?)",
            (-1002222222222, today, 7, 3, 2),
        )
        con.commit()
        con.close()
        r = client.get("/ai")
        check(
            "subscriber AI summary scopes bound groups",
            r.status_code == 200 and "已用 7" in r.text and "总额 200" in r.text,
            r.text[:800],
        )

        r = client.get("/blacklist")
        check(
            "blacklist shows only platform and own entries",
            r.status_code == 200
            and "8000001" in r.text
            and "8000002" in r.text
            and "8000003" not in r.text,
            r.text[:1000],
        )
        r = client.post(
            "/blacklist/add",
            data={"user_id": "8000005", "reason": "sub-test"},
            follow_redirects=False,
        )
        check(
            "subscriber can add own blacklist entry",
            r.status_code == 303 and "msg=" in r.headers.get("location", ""),
            str(r.headers),
        )
        r = client.post(
            "/blacklist/add",
            data={"user_id": "8000003", "reason": "hijack"},
            follow_redirects=False,
        )
        check(
            "subscriber cannot overwrite another blacklist entry",
            r.status_code == 303 and "err=" in r.headers.get("location", ""),
            str(r.headers),
        )
        con = connect()
        added = con.execute(
            "SELECT owner_user_id FROM global_blacklist WHERE user_id=8000005"
        ).fetchone()
        other_entry = con.execute(
            "SELECT owner_user_id, reason FROM global_blacklist WHERE user_id=8000003"
        ).fetchone()
        con.close()
        check(
            "added blacklist entry belongs to subscriber",
            added is not None and int(added[0]) == sub_id,
            repr(added),
        )
        check(
            "other blacklist entry unchanged",
            other_entry is not None
            and int(other_entry[0]) == other_id
            and other_entry[1] == "other",
            repr(other_entry),
        )
        for key, label in (("platform", "platform"), ("other", "other user")):
            r = client.post(
                f"/blacklist/{blacklist_ids[key]}/delete", follow_redirects=False
            )
            con = connect()
            still_there = con.execute(
                "SELECT COUNT(*) FROM global_blacklist WHERE id=?",
                (blacklist_ids[key],),
            ).fetchone()[0]
            con.close()
            check(
                f"subscriber cannot delete {label} blacklist entry",
                r.status_code == 303
                and "err=" in r.headers.get("location", "")
                and still_there == 1,
                f"{r.status_code} {r.headers} {still_there}",
            )
        r = client.post(
            f"/blacklist/{blacklist_ids['sub']}/delete", follow_redirects=False
        )
        con = connect()
        own_gone = con.execute(
            "SELECT COUNT(*) FROM global_blacklist WHERE id=?",
            (blacklist_ids["sub"],),
        ).fetchone()[0]
        con.close()
        check(
            "subscriber can delete own blacklist entry",
            r.status_code == 303
            and "msg=" in r.headers.get("location", "")
            and own_gone == 0,
            f"{r.status_code} {r.headers} {own_gone}",
        )

        r = client.post(
            "/groups/-1002222222222/crosslinks/add",
            data={"other_group_id": "-1001111111111"},
            follow_redirects=False,
        )
        con = connect()
        cross_count = con.execute("SELECT COUNT(*) FROM cross_links").fetchone()[0]
        con.close()
        check(
            "crosslink target restricted to accessible groups",
            r.status_code == 303
            and "err=" in r.headers.get("location", "")
            and cross_count == 0,
            f"{r.status_code} {r.headers} {cross_count}",
        )
        r = client.post(
            "/groups/-1002222222222/reportlinks/add",
            data={"subscriber_id": "-1001111111111"},
            follow_redirects=False,
        )
        con = connect()
        report_count = con.execute("SELECT COUNT(*) FROM report_links").fetchone()[0]
        receive = con.execute(
            "SELECT report_receive FROM groups WHERE id=-1001111111111"
        ).fetchone()[0]
        con.close()
        check(
            "reportlink target restricted to accessible groups",
            r.status_code == 303
            and "err=" in r.headers.get("location", "")
            and report_count == 0
            and not receive,
            f"{r.status_code} {r.headers} {report_count} {receive}",
        )
        r = client.get("/users")
        check("subscriber denied /users", r.status_code == 403, str(r.status_code))
        r = client.get("/groups/-1002222222222?tab=ai")
        check("subscriber allowed ai tab", r.status_code == 200, str(r.status_code))
        r = client.get("/groups/-1001111111111")
        check("subscriber cannot open another group", r.status_code == 404, str(r.status_code))

        r = client.get("/ai")
        check(
            "ai providers isolated in list",
            r.status_code == 200
            and "system-provider" in r.text
            and "sub-provider" in r.text
            and "basic-provider" not in r.text
            and "other-provider" not in r.text,
            r.text[:1000],
        )
        r = client.post(
            f"/ai/providers/{provider_ids['system-provider']}/update",
            data={"base_url": "https://api.example.com/v1", "model": "changed"},
            follow_redirects=False,
        )
        check("subscriber cannot edit system provider", r.status_code == 403, str(r.status_code))
        r = client.post(
            f"/ai/providers/{provider_ids['basic-provider']}/delete",
            follow_redirects=False,
        )
        check("subscriber cannot delete other provider", r.status_code == 403, str(r.status_code))
        r = client.post(
            f"/ai/providers/{provider_ids['sub-provider']}/update",
            data={
                "base_url": "https://api.example.com/v1",
                "model": "my-new-model",
                "enabled": "on",
            },
            follow_redirects=False,
        )
        check("subscriber can edit own provider", r.status_code == 303, str(r.status_code))
        r = client.post(
            "/ai/providers/add",
            data={
                "name": "sub-added",
                "label": "订阅用户新增",
                "base_url": "https://api.example.com/v1",
                "api_key": "sk-new",
                "model": "test-model",
                "priority": "50",
                "enabled": "on",
            },
            follow_redirects=False,
        )
        check("subscriber can add provider", r.status_code == 303, str(r.status_code))
        con = connect()
        owner = con.execute(
            "SELECT owner_user_id FROM ai_providers WHERE name='sub-added'"
        ).fetchone()
        con.close()
        check("added provider belongs to subscriber", owner and int(owner[0]) == sub_id, repr(owner))
        client.get("/logout")

        # -------------------------------------------------- subscription by user
        check("other user login", login(client, tokens["other"]))
        r = client.get("/groups")
        check(
            "user without access sees no groups",
            r.status_code == 200
            and "基础用户的群" not in r.text
            and "订阅用户的群" not in r.text
            and "无权访问的群" not in r.text,
            r.text[:500],
        )
        r = client.get("/ai")
        check("plain user denied ai", r.status_code == 403, str(r.status_code))
        client.get("/logout")
except Exception as exc:  # noqa: BLE001
    failures.append(f"exception: {exc}")
    import traceback

    traceback.print_exc()

if failures:
    print(f"\nFAILURES: {len(failures)}")
    for item in failures:
        print(f" - {item}")
    sys.exit(1)
print("\nFAILURES: none")
