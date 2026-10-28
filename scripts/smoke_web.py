"""Web 面板冒烟测试。

使用临时配置与临时 SQLite 数据库，不会修改真实的 config.json 或 data/。
运行：

    .venv/bin/python scripts/smoke_web.py
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

from sqlalchemy import create_engine

ROOT = Path(__file__).resolve().parents[1]
TMP = Path(tempfile.mkdtemp(prefix="groupguard-smoke-"))
DB = TMP / "smoke.db"
TMP_CONFIG = TMP / "config.json"

config_path = ROOT / "config.json"
if not config_path.exists():
    print("未找到 config.json，请先复制 config.example.json 并填写。")
    sys.exit(2)

cfg = json.loads(config_path.read_text(encoding="utf-8"))
admin_ids = [int(item) for item in cfg.get("bot", {}).get("admin_ids", [])]
if not admin_ids:
    print("config.json 的 bot.admin_ids 为空，无法执行登录测试。")
    sys.exit(2)
ADMIN_ID = admin_ids[0]

cfg["database"]["url"] = f"sqlite+aiosqlite:///{DB}"
cfg.setdefault("web", {})["base_url"] = "http://testserver"
cfg.setdefault("ai", {})["learning_dir"] = str(TMP / "learning")
TMP_CONFIG.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
os.environ["GROUPGUARD_CONFIG"] = str(TMP_CONFIG)
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402
from app.web import pages as web_pages  # noqa: E402
from app.web.main import app  # noqa: E402
from app.web.auth import hash_console_token  # noqa: E402


async def fake_bot_membership(chat_id: int) -> dict:
    return {
        "status": "administrator",
        "can_restrict_members": True,
        "can_delete_messages": True,
        "can_invite_users": True,
    }


web_pages._fetch_bot_membership = fake_bot_membership

failures: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    if cond:
        print(f"PASS {name}")
    else:
        failures.append(name)
        print(f"FAIL {name} {extra}")


def connect() -> sqlite3.Connection:
    return sqlite3.connect(DB)


try:
    with TestClient(app) as client:
        r = client.get("/healthz")
        check("healthz", r.status_code == 200 and r.json() == {"ok": True}, r.text)

        token = "smoke-console-token"
        con = connect()
        con.execute(
            "UPDATE console_users SET display_name=?, token_hash=?, "
            "role=?, token_value=NULL, token_expires_at=datetime('now', '+15 minutes'), "
            "is_active=1 WHERE telegram_id=?",
            ("Smoke Admin", hash_console_token(token), "superadmin", ADMIN_ID),
        )
        con.execute(
            "INSERT OR REPLACE INTO console_users "
            "(telegram_id, display_name, username, token_hash, token_expires_at, role, is_active, balance_cents) "
            "VALUES (?, ?, ?, ?, datetime('now', '+15 minutes'), 'user', 1, 800)",
            (ADMIN_ID + 10000, "Balance User", "balance_user", hash_console_token("balance-user-token")),
        )
        con.commit()
        con.close()

        legacy_db = TMP / "legacy-console.db"
        legacy_engine = create_engine(f"sqlite:///{legacy_db}")
        with legacy_engine.begin() as connection:
            from app.models import Base
            from app.db import _upgrade_sqlite_schema

            Base.metadata.create_all(connection)
            connection.exec_driver_sql(
                "ALTER TABLE console_users ADD COLUMN console_path VARCHAR(32) NOT NULL DEFAULT 'legacy'"
            )
            connection.exec_driver_sql(
                "INSERT INTO console_users (telegram_id, display_name, token_hash, role, is_active, balance_cents) "
                "VALUES (123, 'Legacy', 'legacy-hash', 'user', 1, 0)"
            )
            _upgrade_sqlite_schema(connection)
            columns = {
                row[1]
                for row in connection.exec_driver_sql("PRAGMA table_info(console_users)")
            }
            legacy_user = connection.exec_driver_sql(
                "SELECT telegram_id, display_name FROM console_users WHERE telegram_id=123"
            ).one()
        legacy_engine.dispose()
        check(
            "legacy console path migration preserves account",
            "console_path" not in columns and legacy_user == (123, "Legacy"),
            f"{columns} {legacy_user}",
        )

        r = client.get("/login")
        check("login page", r.status_code == 200 and "luygroup 控制台" in r.text, r.text[:200])

        anonymous = TestClient(app)
        r = anonymous.get("/groups", follow_redirects=False)
        check(
            "anonymous redirect",
            r.status_code == 303 and r.headers["location"].startswith("/login?next="),
            str(r.headers),
        )

        r = client.post(
            "/login",
            data={"token": "wrong"},
            follow_redirects=False,
        )
        check("bad token", r.status_code == 200 and "无效" in r.text, r.text[:200])

        r = client.post(
            "/login",
            data={"token": token},
            headers={"Origin": "http://evil.example"},
            follow_redirects=False,
        )
        check(
            "cross-origin login rejected",
            r.status_code == 403 and "CSRF" in r.text,
            f"{r.status_code} {r.text[:200]}",
        )
        r = client.get("/", follow_redirects=False)
        check(
            "cross-origin login did not create session",
            r.status_code == 303 and r.headers["location"].startswith("/login"),
            str(r.headers),
        )

        r = client.get(f"/console?token={token}", follow_redirects=False)
        check("direct console token login", r.status_code == 303 and r.headers["location"] == "/", str(r.headers))
        check(
            "random path login route removed",
            client.get("/console/some-random-path?token=" + token, follow_redirects=False).status_code == 404,
        )

        r = client.get(f"/console?token={token}", follow_redirects=False)
        check("login code one-time", r.status_code == 200 and "无效、已过期或已被使用" in r.text, r.text[:200])

        con = connect()
        balance_row = con.execute(
            "SELECT id, balance_cents FROM console_users WHERE telegram_id=?", (ADMIN_ID + 10000,)
        ).fetchone()
        con.close()
        admin_db_id, initial_balance = balance_row
        r = client.post(
            f"/users/{admin_db_id}/balance",
            data={"balance": "12.34"},
            follow_redirects=False,
        )
        con = connect()
        balance = con.execute(
            "SELECT balance_cents FROM console_users WHERE telegram_id=?", (ADMIN_ID + 10000,)
        ).fetchone()[0]
        con.close()
        check("admin can update account balance", r.status_code == 303 and balance == 1234, f"{r.status_code} {balance}")
        r = client.post(
            f"/users/{admin_db_id}/balance",
            data={"balance": "12.345"},
            follow_redirects=False,
        )
        con = connect()
        unchanged = con.execute(
            "SELECT balance_cents FROM console_users WHERE telegram_id=?", (ADMIN_ID + 10000,)
        ).fetchone()[0]
        con.close()
        check(
            "invalid balance keeps stored value",
            r.status_code == 303 and unchanged == 1234 and "err=" in r.headers.get("location", ""),
            f"{r.status_code} {unchanged} {r.headers.get('location')}",
        )

        r = client.get("/")
        check(
            "logout form in header",
            'action="/logout"' in r.text and 'method="post"' in r.text and "余额 ¥0.00" in r.text,
            r.text[:300],
        )

        for path in ["/", "/groups", "/users", "/ai", "/ai/add", "/blacklist", "/reports", "/stats"]:
            r = client.get(path)
            check(f"GET {path}", r.status_code == 200, f"{r.status_code} {r.text[:300]}")

        r = client.get("/users?q=Balance")
        check(
            "users search and edit modal",
            r.status_code == 200
            and "Balance User" in r.text
            and "data-modal-open=" in r.text
            and "重置登录码" in r.text
            and "绑定群组数量" in r.text
            and "<th>角色</th>" not in r.text
            and "<th>订阅</th><th>绑定群组数量</th><th>状态</th><th>操作</th>" in r.text
            and "当前结果绑定" in r.text
            and "保存余额" in r.text
            and "控制台路径" not in r.text,
            f"{r.status_code} {r.text[:300]}",
        )

        r = client.get("/users?q=no-such-console-user")
        check(
            "users search empty",
            r.status_code == 200 and "没有匹配的用户" in r.text,
            f"{r.status_code} {r.text[:300]}",
        )

        client.post("/logout", follow_redirects=False)
        con = connect()
        con.execute(
            "UPDATE console_users SET balance_cents=800 WHERE telegram_id=?",
            (ADMIN_ID + 10000,),
        )
        con.commit()
        con.close()
        r = client.get("/console?token=balance-user-token", follow_redirects=False)
        check("balance user can open account page", r.status_code == 303 and r.headers.get("location") == "/")
        con = connect()
        con.execute(
            "INSERT INTO groups (id, title, chat_type, is_active) VALUES (?, ?, ?, 1)",
            (-100990001, "Web Lottery Group", "supergroup"),
        )
        balance_user_id = con.execute(
            "SELECT id FROM console_users WHERE telegram_id=?", (ADMIN_ID + 10000,)
        ).fetchone()[0]
        con.execute(
            "INSERT INTO lotteries (group_id, creator_id, title, prize, winners_count, "
            "participation_type, draw_type, status, ends_at, hidden_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now', '+1 hour'), NULL)",
            (-100990001, balance_user_id, "Web 抽奖记录", "测试奖品", 1, "none", "time", "drawn"),
        )
        lottery_id = con.execute("SELECT last_insert_rowid()").fetchone()[0]
        con.execute(
            "INSERT INTO lottery_entries (lottery_id, user_id) VALUES (?, ?)",
            (lottery_id, ADMIN_ID + 10001),
        )
        con.execute(
            "INSERT INTO lottery_winners (lottery_id, user_id) VALUES (?, ?)",
            (lottery_id, ADMIN_ID + 10001),
        )
        con.commit()
        con.close()
        r = client.get("/account")
        check(
            "unsubscribed account shows one open action and balance choice",
            r.status_code == 200
            and "¥8.00 CNY" in r.text
            and "开通一个月 · ¥8.00" in r.text
            and "续费一个月 · ¥8.00" not in r.text
            and "余额抵扣" in r.text
            and "在线支付续费 · ¥8.00" not in r.text
            and 'trigger.addEventListener("click"' in r.text
            and "账单记录" in r.text,
            r.text[:500],
        )
        check(
            "account shows my lottery records",
            r.status_code == 200
            and "我的抽奖" in r.text
            and "Web 抽奖记录" in r.text
            and "Web Lottery Group" in r.text
            and "已开奖" in r.text
            and "报名人数" in r.text
            and str(ADMIN_ID + 10001) in r.text,
            r.text[:800],
        )
        r = client.post("/account/subscribe", follow_redirects=False)
        con = connect()
        account_balance = con.execute(
            "SELECT balance_cents FROM console_users WHERE telegram_id=?", (ADMIN_ID + 10000,)
        ).fetchone()[0]
        subscription = con.execute(
            "SELECT status, expires_at FROM subscriptions WHERE user_id=(SELECT id FROM console_users WHERE telegram_id=?)",
            (ADMIN_ID + 10000,),
        ).fetchone()
        con.close()
        check(
            "account purchase charges eight CNY and activates subscription",
            r.status_code == 303 and account_balance == 0 and subscription and subscription[0] == "active",
            f"{r.status_code} {account_balance} {subscription}",
        )
        r = client.get("/account")
        check(
            "active subscription shows one renewal action",
            "续费一个月 · ¥8.00" in r.text and "开通一个月 · ¥8.00" not in r.text,
            r.text[:500],
        )
        client.post("/logout", follow_redirects=False)
        con = connect()
        con.execute(
            "UPDATE console_users SET balance_cents=799 WHERE telegram_id=?",
            (ADMIN_ID + 10000,),
        )
        con.execute(
            "UPDATE console_users SET token_hash=?, token_expires_at=datetime('now', '+15 minutes') WHERE telegram_id=?",
            (hash_console_token("balance-user-low-token"), ADMIN_ID + 10000),
        )
        con.commit()
        con.close()
        client.get("/console?token=balance-user-low-token", follow_redirects=False)
        r = client.post("/account/subscribe", follow_redirects=False)
        con = connect()
        unchanged = con.execute(
            "SELECT balance_cents FROM console_users WHERE telegram_id=?", (ADMIN_ID + 10000,)
        ).fetchone()[0]
        con.close()
        check(
            "account purchase rejects insufficient funds",
            r.status_code == 303 and unchanged == 799 and "err=" in r.headers.get("location", ""),
            f"{r.status_code} {unchanged} {r.headers.get('location')}",
        )
        client.post("/logout", follow_redirects=False)
        admin_token = "smoke-admin-after-purchase"
        con = connect()
        con.execute(
            "UPDATE console_users SET token_hash=?, token_expires_at=datetime('now', '+15 minutes') WHERE telegram_id=?",
            (hash_console_token(admin_token), ADMIN_ID),
        )
        con.commit()
        con.close()
        client.get(f"/console?token={admin_token}", follow_redirects=False)

        con = connect()
        con.execute(
            "INSERT INTO groups (id, title, username, chat_type, is_active, member_count, message_count, "
            "anti_ad_enabled, anti_spam_enabled, ai_assistant_enabled, report_enabled, points_enabled, "
            "welcome_enabled, rules_text, warn_limit, warn_action, mute_minutes, flood_limit, flood_window, "
            "flood_action, max_message_length, ai_daily_quota, sample_daily_quota, verify_timeout, "
            "points_per_message, points_cooldown, daily_bonus, points_name, crossban_mode) VALUES "
            "(-1001234567890, '测试群', 'smokegroup', 'supergroup', 1, 12, 345, 1, 1, 1, 1, 1, 1, '旧群规', "
            "3, 'mute', 60, 8, 10, 'mute', 0, 200, 500, 300, 1, 60, 5, '积分', 'shared')"
        )
        con.execute(
            "INSERT INTO members (group_id, user_id, username, first_name, points, message_count, is_verified, is_muted) "
            "VALUES (-1001234567890, 111, 'tester', '测试员', 42, 7, 1, 1)"
        )
        con.execute(
            "INSERT INTO members (group_id, user_id, username, first_name, is_member, is_verified, is_muted) "
            "VALUES (-1001234567890, 222, 'leftuser', '已离开', 0, 0, 0)"
        )
        con.execute(
            "INSERT INTO members (group_id, user_id, username, first_name, is_member, is_verified, is_muted) "
            "VALUES (-1001234567890, 333, 'staleuser', '待校正', 1, 0, 0)"
        )
        con.execute(
            "INSERT INTO warnings (group_id, user_id, admin_id, code, reason, content, source, action, active, created_at) "
            "VALUES (-1001234567890, 111, ?, '202610190000', '冒烟警告', '', 'manual', 'mute', 1, '2026-10-19 00:00:00.000000')",
            (ADMIN_ID,),
        )
        ai_warning = con.execute(
            "INSERT INTO warnings (group_id, user_id, admin_id, code, reason, content, source, action, review_status, active, created_at) "
            "VALUES (-1001234567890, 111, ?, '202610190001', 'AI 判定为广告：冒烟测试', "
            "'冒烟测试广告原文', 'ai', 'warn', 'pending', 1, '2026-10-19 00:01:00.000000')",
            (ADMIN_ID,),
        )
        ai_warning_id = ai_warning.lastrowid
        con.execute(
            "INSERT INTO ai_providers (name, label, base_url, api_key, model, priority, enabled, builtin) "
            "VALUES ('smoke-ai', '冒烟接口', 'https://api.deepseek.com/v1', 'sk-smoke', 'deepseek-chat', 1, 1, 0)"
        )
        con.execute(
            "INSERT INTO groups (id, title, username, chat_type, is_active) "
            "VALUES (987654321, '私聊用户不应显示', 'private_chat_user', 'private', 1)"
        )
        con.commit()
        con.close()

        tabs = [
            "settings",
            "moderation",
            "ai",
            "join",
            "admins",
            "members",
            "warnings",
            "keywords",
            "whitelist",
            "channels",
            "links",
            "games",
            "logs",
            "reports",
        ]
        for tab in tabs:
            r = client.get(f"/groups/-1001234567890?tab={tab}")
            check(f"group tab {tab}", r.status_code == 200, f"{r.status_code} {r.text[:400]}")

        r = client.get("/ai")
        check(
            "ai provider list",
            r.status_code == 200
            and "smoke-ai" in r.text
            and "AI 学习数据" in r.text
            and "清空学习数据" not in r.text
            and 'action="/ai/learning/dir"' in r.text
            and '<span class="k">存储目录</span>' in r.text
            and str(TMP / "learning") in r.text
            and '<span class="fact-label">接口</span>' in r.text,
            r.text[:600],
        )

        provider_id = connect().execute(
            "SELECT id FROM ai_providers WHERE name='smoke-ai'"
        ).fetchone()[0]
        check(
            "ai provider card header actions",
            all(
                marker in r.text
                for marker in (
                    'class="provider-tools"',
                    ">测试连通</button>",
                    ">删除</button>",
                    'data-edit-toggle="provider-edit-',
                    'class="provider-switch"',
                    ">优先级 1</span>",
                )
            )
            and "编辑 / 切换供应商与模型" not in r.text
            and "smoke-ai · DeepSeek" not in r.text,
            r.text[:600],
        )
        check(
            "ai provider quick model dropdown",
            r.text.count('class="quick-control"') >= 2
            and 'name="base_url" data-auto-submit' in r.text
            and 'class="quick-control quick-model" data-model-form' in r.text
            and 'data-fallback=' in r.text
            and 'class="quick-model-trigger"' in r.text
            and 'data-model-trigger' in r.text
            and 'data-model-menu' in r.text
            and 'data-model-search' in r.text
            and 'data-model-refresh' in r.text
            and 'data-model-value' in r.text
            and 'class="quick-model-input"' not in r.text
            and "<datalist" not in r.text
            and 'name="model" data-auto-submit' not in r.text
            and 'class="provider-edit-panel"' in r.text,
            r.text[:600],
        )

        r = client.post(
            f"/ai/providers/{provider_id}/update",
            data={"base_url": "https://api.moonshot.cn/v1", "enabled": "on"},
            follow_redirects=False,
        )
        con = connect()
        row = con.execute(
            "SELECT base_url, model, enabled FROM ai_providers WHERE id=?",
            (provider_id,),
        ).fetchone()
        con.close()
        check(
            "ai quick supplier change keeps other fields",
            r.status_code == 303
            and row == ("https://api.moonshot.cn/v1", "deepseek-chat", 1),
            f"{r.status_code} {row}",
        )

        r = client.post(
            f"/ai/providers/{provider_id}/update",
            data={"model": "deepseek-reasoner", "enabled": "on"},
            follow_redirects=False,
        )
        con = connect()
        row = con.execute(
            "SELECT base_url, model, enabled FROM ai_providers WHERE id=?",
            (provider_id,),
        ).fetchone()
        con.close()
        check(
            "ai quick model change keeps other fields",
            r.status_code == 303
            and row == ("https://api.moonshot.cn/v1", "deepseek-reasoner", 1),
            f"{r.status_code} {row}",
        )

        # Restore the fixture provider for the remaining AI checks.
        client.post(
            f"/ai/providers/{provider_id}/update",
            data={
                "base_url": "https://api.deepseek.com/v1",
                "model": "deepseek-chat",
                "enabled": "on",
            },
            follow_redirects=False,
        )

        r = client.get("/ai/add")
        check(
            "ai add page",
            r.status_code == 200
            and 'action="/ai/providers/add"' in r.text
            and "获取供应商模型" in r.text,
            r.text[:500],
        )

        ai_js = (ROOT / "app/web/static/ai.js").read_text(encoding="utf-8")
        check(
            "ai js targets base url input",
            'querySelector("input[data-base-url]")' in ai_js,
            ai_js[:300],
        )
        check(
            "ai add page cache-busts script",
            "/static/ai.js?v=" in r.text,
            r.text[:500],
        )
        check(
            "ai js edit toggle and auto submit",
            'querySelectorAll("[data-edit-toggle]")' in ai_js
            and 'closest("[data-auto-submit]")' in ai_js
            and "form.submit()" in ai_js,
            ai_js[:300],
        )
        check(
            "ai js quick model fetch and custom input",
            'querySelectorAll("form[data-model-form]")' in ai_js
            and 'querySelector("[data-model-trigger]")' in ai_js
            and 'querySelector("[data-model-menu]")' in ai_js
            and 'querySelector("[data-model-search]")' in ai_js
            and 'querySelector("[data-model-list]")' in ai_js
            and 'fetch("/ai/models"' in ai_js
            and "provider_id" in ai_js
            and "form.dataset.fallback" in ai_js
            and 'event.key !== "Enter"' in ai_js
            and 'event.key !== "Escape"' in ai_js
            and "applyModel" in ai_js
            and "form.submit()" in ai_js
            and 'classList.add("loading")' in ai_js
            and "aria-busy" in ai_js
            and "dataset.skipChange" not in ai_js
            and 'querySelector("[data-model-input]")' not in ai_js,
            ai_js[:300],
        )

        r = client.get("/groups/-1001234567890?tab=members")
        check(
            "left member hidden by default",
            r.status_code == 200 and "leftuser" not in r.text and "同步成员状态" in r.text,
            r.text[:400],
        )
        r = client.get("/groups/-1001234567890?tab=members&show_left=1")
        check(
            "left member visible on demand",
            r.status_code == 200 and "leftuser" in r.text and "已离开" in r.text,
            r.text[:400],
        )

        r = client.get("/groups/-1001234567890?tab=warnings")
        check(
            "warning table disposition columns",
            r.status_code == 200
            and "记录 ID" in r.text
            and "处置时间" in r.text
            and "202610190000" in r.text
            and "202610190001" in r.text
            and "冒烟警告" in r.text
            and "AI 审核" in r.text
            and "过期时间" not in r.text
            and "账号操作" not in r.text,
            r.text[:800],
        )

        r = client.get("/groups/-1001234567890?tab=warnings&q=202610190001")
        check(
            "warning filter by id",
            "202610190001" in r.text and "202610190000" not in r.text,
            r.text[:500],
        )

        r = client.get("/groups/-1001234567890?tab=warnings&src=ai")
        check(
            "warning filter by source",
            "202610190001" in r.text and "202610190000" not in r.text,
            r.text[:500],
        )

        r = client.get("/groups/-1001234567890?tab=warnings&review=pending")
        check(
            "warning filter pending review",
            "202610190001" in r.text and "待审核" in r.text,
            r.text[:500],
        )

        r = client.post(
            f"/groups/-1001234567890/warnings/{ai_warning_id}/review",
            data={"decision": "false_positive", "note": "冒烟误杀", "tab": "warnings"},
            follow_redirects=False,
        )
        check(
            "ai false positive review",
            r.status_code == 303 and "msg=" in r.headers["location"],
            str(r.headers),
        )
        con = connect()
        reviewed = con.execute(
            "SELECT review_status, active, review_note, content FROM warnings WHERE id=?",
            (ai_warning_id,),
        ).fetchone()
        con.close()
        check(
            "ai false positive db",
            reviewed == ("false_positive", 0, "冒烟误杀", "冒烟测试广告原文"),
            str(reviewed),
        )
        learning_file = TMP / "learning" / "ad_detection" / "group_-1001234567890.jsonl"
        learned_text = learning_file.read_text(encoding="utf-8") if learning_file.exists() else ""
        check(
            "ai false positive learned",
            '"is_ad": false' in learned_text and "冒烟测试广告原文" in learned_text,
            learned_text[:500],
        )
        r = client.get("/groups/-1001234567890?tab=warnings&review=false_positive")
        check(
            "warning review filter",
            "202610190001" in r.text and "误杀已学习" in r.text,
            r.text[:500],
        )

        r = client.post(
            f"/groups/-1001234567890/warnings/{ai_warning_id}/review",
            data={"decision": "confirmed", "note": "冒烟确认违规", "tab": "warnings"},
            follow_redirects=False,
        )
        check(
            "ai confirmed review",
            r.status_code == 303 and "msg=" in r.headers["location"],
            str(r.headers),
        )
        con = connect()
        confirmed_row = con.execute(
            "SELECT review_status, active, review_note FROM warnings WHERE id=?",
            (ai_warning_id,),
        ).fetchone()
        con.close()
        check(
            "ai confirmed db",
            confirmed_row == ("confirmed", 1, "冒烟确认违规"),
            str(confirmed_row),
        )
        learned_text = learning_file.read_text(encoding="utf-8") if learning_file.exists() else ""
        check(
            "ai confirmed learned",
            '"is_ad": true' in learned_text and "冒烟确认违规" in learned_text,
            learned_text[:500],
        )
        r = client.get("/groups/-1001234567890?tab=warnings&review=confirmed")
        check(
            "warning confirmed filter",
            "202610190001" in r.text and "已确认违规" in r.text,
            r.text[:500],
        )

        r = client.get("/groups/-1001234567890?tab=moderation")
        check("keyword hits card removed", "最近关键词命中" not in r.text, r.text[:500])

        new_learning = TMP / "learning2"
        r = client.post(
            "/ai/learning/dir",
            data={"directory": str(new_learning)},
            follow_redirects=False,
        )
        check(
            "ai learning dir set",
            r.status_code == 303 and "msg=" in r.headers["location"],
            str(r.headers),
        )
        saved_cfg = json.loads(TMP_CONFIG.read_text(encoding="utf-8"))
        check(
            "ai learning dir persisted",
            saved_cfg.get("ai", {}).get("learning_dir") == str(new_learning.resolve()),
            str(saved_cfg.get("ai")),
        )
        moved = new_learning / "ad_detection" / "group_-1001234567890.jsonl"
        check(
            "ai learning dir migrates files",
            moved.exists() and not learning_file.exists(),
            f"{moved.exists()} {learning_file.exists()}",
        )

        r = client.get("/groups/-1001234567890?tab=members")
        check(
            "member clear warnings action",
            r.status_code == 200 and "清警告" in r.text,
            r.text[:500],
        )

        from app.services.ai import normalize_base_url

        check(
            "ai base url normalization",
            normalize_base_url("vyceai.com/v1") == "https://vyceai.com/v1",
            normalize_base_url("vyceai.com/v1"),
        )

        r = client.post(
            "/ai/providers/add",
            data={
                "name": "smoke-noscheme",
                "label": "无协议接口",
                "base_url": "vyceai.com/v1",
                "api_key": "sk-smoke",
                "model": "deepseek-v4.1",
                "priority": "5",
                "enabled": "on",
            },
            follow_redirects=False,
        )
        check("ai provider add without scheme", r.status_code == 303, r.text[:300])
        con = connect()
        saved_base = con.execute(
            "SELECT base_url FROM ai_providers WHERE name='smoke-noscheme'"
        ).fetchone()
        con.close()
        check(
            "ai provider normalized in db",
            saved_base == ("https://vyceai.com/v1",),
            str(saved_base),
        )

        model_calls: list[tuple[str, str]] = []

        async def fake_fetch_models(base_url: str, api_key: str) -> list[str]:
            model_calls.append((base_url, api_key))
            return ["deepseek-chat", "deepseek-reasoner"]

        web_pages.ai_service.fetch_models = fake_fetch_models
        r = client.post(
            "/ai/models",
            json={"base_url": "https://api.deepseek.com/v1", "api_key": "sk-smoke"},
        )
        check(
            "ai models fetch",
            r.status_code == 200 and r.json().get("models") == ["deepseek-chat", "deepseek-reasoner"],
            r.text[:300],
        )
        r = client.post("/ai/models", json={"provider_id": provider_id})
        check(
            "ai models fetch by provider",
            r.status_code == 200
            and r.json().get("models") == ["deepseek-chat", "deepseek-reasoner"]
            and model_calls[-1] == ("https://api.deepseek.com/v1", "sk-smoke"),
            f"{r.status_code} {r.text[:300]} {model_calls[-1:]!r}",
        )

        calls: list[tuple[str, dict]] = []

        async def fake_telegram_action(method: str, payload: dict, *, timeout: float = 15) -> dict:
            calls.append((method, payload))
            return {}

        web_pages._telegram_action = fake_telegram_action
        r = client.post(
            "/groups/-1001234567890/members/111/action",
            data={"action": "unmute", "tab": "members"},
            follow_redirects=False,
        )
        check(
            "member unmute",
            r.status_code == 303 and calls and calls[-1][0] == "restrictChatMember",
            f"{r.status_code} {r.headers.get('location')} {calls[-1:] }",
        )
        con = connect()
        muted = con.execute(
            "SELECT is_muted FROM members WHERE group_id=-1001234567890 AND user_id=111"
        ).fetchone()[0]
        con.close()
        check("member unmute db", muted == 0, str(muted))

        r = client.post(
            "/groups/-1001234567890/members/111/action",
            data={"action": "ban", "tab": "warnings"},
            follow_redirects=False,
        )
        check(
            "member ban",
            r.status_code == 303 and calls and calls[-1][0] == "banChatMember",
            f"{r.status_code} {r.headers.get('location')} {calls[-1:] }",
        )
        con = connect()
        banned = con.execute(
            "SELECT is_banned FROM members WHERE group_id=-1001234567890 AND user_id=111"
        ).fetchone()[0]
        con.close()
        check("member ban db", banned == 1, str(banned))

        original_call = web_pages._telegram_call

        async def fake_telegram_call(method: str, params: dict, *, timeout: float = 15):
            if method == "getChatAdministrators":
                return [
                    {
                        "user": {
                            "id": 444,
                            "is_bot": False,
                            "first_name": "群管理员",
                            "username": "groupadmin",
                        }
                    }
                ]
            if method == "getChatMemberCount":
                return 61
            if method == "getChatMember":
                uid = int(params.get("user_id") or 0)
                if uid in {222, 333}:
                    return {"status": "left"}
                if uid == 111:
                    return {"status": "kicked"}
                return {"status": "administrator" if uid == 444 else "member"}
            return {}

        web_pages._telegram_call = fake_telegram_call
        try:
            r = client.post(
                "/groups/-1001234567890/members/sync",
                follow_redirects=False,
            )
            check(
                "member sync action",
                r.status_code == 303 and "msg=" in r.headers["location"],
                f"{r.status_code} {r.headers.get('location')}",
            )
        finally:
            web_pages._telegram_call = original_call
        con = connect()
        sync_rows = {
            row[0]: (row[1], row[2])
            for row in con.execute(
                "SELECT user_id, is_member, is_banned FROM members "
                "WHERE group_id=-1001234567890 AND user_id IN (222, 333, 444)"
            ).fetchall()
        }
        group_count = con.execute(
            "SELECT member_count FROM groups WHERE id=-1001234567890"
        ).fetchone()[0]
        con.close()
        check(
            "member sync corrects left member",
            sync_rows.get(333) == (0, 0),
            str(sync_rows.get(333)),
        )
        check(
            "member sync backfills admins",
            sync_rows.get(444) == (1, 0),
            str(sync_rows.get(444)),
        )
        check("member sync updates group count", group_count == 61, str(group_count))

        before = len(calls)
        r = client.post(
            f"/groups/-1001234567890/members/{ADMIN_ID}/action",
            data={"action": "ban", "tab": "members"},
            follow_redirects=False,
        )
        check(
            "global admin protected",
            r.status_code == 303 and "err=" in r.headers["location"] and len(calls) == before,
            str(r.headers),
        )

        r = client.post(
            "/groups/-1001234567890/keywords/add",
            data={
                "pattern": "营业时间",
                "action": "reply",
                "reply_text": "每天 9:00-22:00",
            },
            follow_redirects=False,
        )
        check("keyword reply add", r.status_code == 303, r.text[:200])
        r = client.get("/groups/-1001234567890?tab=keywords")
        check(
            "keyword reply visible",
            "每天 9:00-22:00" in r.text and "关键词回复" in r.text,
            r.text[:600],
        )

        r = client.post(
            "/groups/-1001234567890/settings",
            data={
                "_scope": "ai",
                "ai_ad_enabled": "on",
                "ai_daily_quota": "77",
                "sample_daily_quota": "88",
            },
            follow_redirects=False,
        )
        check("save ai scope", r.status_code == 303, r.text[:200])
        con = connect()
        row = con.execute(
            "SELECT ai_ad_enabled, ai_daily_quota, sample_daily_quota, rules_text "
            "FROM groups WHERE id=-1001234567890"
        ).fetchone()
        con.close()
        check("ai scope applied", row[0] == 1 and row[1] == 77 and row[2] == 88, str(row))
        check("ai scope keeps rules", row[3] == "旧群规", str(row))

        r = client.get("/groups/-1001234567890?tab=settings")
        check(
            "group settings default cleanup switches are on",
            r.status_code == 200
            and 'name="delete_command_messages"' in r.text
            and 'name="auto_delete_messages"' in r.text
            and 'name="welcome_enabled"' in r.text,
            r.text[:800],
        )
        r = client.post(
            "/groups/-1001234567890/settings",
            data={"_scope": "general", "is_active": "on"},
            follow_redirects=False,
        )
        con = connect()
        settings = con.execute(
            "SELECT delete_command_messages, auto_delete_messages, welcome_enabled "
            "FROM groups WHERE id=-1001234567890"
        ).fetchone()
        con.close()
        check(
            "group settings can turn cleanup switches off",
            r.status_code == 303 and settings == (0, 0, 0),
            f"{r.status_code} {settings}",
        )

        r = client.post(
            "/groups/-1001234567890/whitelist/add",
            data={"user_id": "222", "note": "smoke"},
            follow_redirects=False,
        )
        check("whitelist add", r.status_code == 303, r.text[:200])

        r = client.post(
            "/blacklist/add",
            data={"user_id": "333", "reason": "smoke"},
            follow_redirects=False,
        )
        check("blacklist add", r.status_code == 303, r.text[:200])
        r = client.get("/blacklist")
        check("blacklist page has entry", "333" in r.text, r.text[:300])

        r = client.post(
            "/groups/-1001234567890/keywords/add",
            data={"pattern": "加微信", "action": "warn", "note": "smoke"},
            follow_redirects=False,
        )
        check("keyword add", r.status_code == 303, r.text[:200])
        r = client.get("/groups/-1001234567890?tab=keywords")
        check("keyword visible", "加微信" in r.text, r.text[:300])

        r = client.post(
            "/groups/-1001234567890/keywords/add",
            data={"pattern": "([", "is_regex": "on", "action": "warn"},
            follow_redirects=False,
        )
        check(
            "invalid regex rejected",
            r.status_code == 303 and "err=" in r.headers["location"],
            str(r.headers),
        )

        r = client.post(
            "/groups/-1001234567890/channels/add",
            data={"channel_id": "-100999", "title": "公告", "username": "notice"},
            follow_redirects=False,
        )
        check("channel add", r.status_code == 303, r.text[:200])

        r = client.post(
            "/groups/-1001234567890/admins/add",
            data={"user_id": "444", "role": "assistant", "permissions": ["ban", "warn"]},
            follow_redirects=False,
        )
        check("admin add", r.status_code == 303, r.text[:200])
        r = client.get("/groups/-1001234567890?tab=admins")
        check("admin visible", "444" in r.text, r.text[:300])

        con = connect()
        con.execute(
            "INSERT INTO groups (id, title, chat_type, is_active) "
            "VALUES (-100999, '联动群', 'supergroup', 1)"
        )
        con.commit()
        con.close()
        r = client.post(
            "/groups/-1001234567890/crosslinks/add",
            data={"other_group_id": "-100999"},
            follow_redirects=False,
        )
        check("crosslink add", r.status_code == 303, r.text[:200])
        r = client.post(
            "/groups/-1001234567890/reportlinks/add",
            data={"subscriber_id": "-100999"},
            follow_redirects=False,
        )
        check("reportlink add", r.status_code == 303, r.text[:200])
        r = client.get("/groups/-1001234567890?tab=links")
        check("links visible", "联动群" in r.text, r.text[:400])

        r = client.post(
            "/groups/-1001234567890/shop/add",
            data={
                "name": "自定义头衔",
                "cost": "50",
                "stock": "3",
                "reward_type": "custom",
                "reward_text": "联系管理员",
            },
            follow_redirects=False,
        )
        check("shop add", r.status_code == 303, r.text[:200])
        r = client.get("/groups/-1001234567890?tab=games")
        check("shop visible", "自定义头衔" in r.text, r.text[:300])

        r = client.post(
            "/groups/-1001234567890/points",
            data={"user_id": "111", "delta": "-10", "reason": "smoke"},
            follow_redirects=False,
        )
        check("points adjust", r.status_code == 303, r.text[:200])
        con = connect()
        pts = con.execute(
            "SELECT points FROM members WHERE group_id=-1001234567890 AND user_id=111"
        ).fetchone()[0]
        con.close()
        check("points updated", pts == 32, str(pts))

        r = client.post(
            "/groups/-1001234567890/settings",
            data={"_scope": "moderation", "anti_ad_enabled": "on", "warn_limit": "5"},
            follow_redirects=False,
        )
        check("save moderation scope", r.status_code == 303, r.text[:200])
        con = connect()
        row = con.execute(
            "SELECT warn_limit, ai_daily_quota, is_active FROM groups WHERE id=-1001234567890"
        ).fetchone()
        con.close()
        check("moderation scope isolated", row == (5, 77, 1), str(row))

        r = client.get("/groups?q=测试")
        check("groups search", r.status_code == 200 and "测试群" in r.text, r.text[:300])

        r = client.get("/groups")
        check("groups add form", 'action="/groups/add"' in r.text, r.text[:400])
        check("groups invite link", "startgroup=true" in r.text, r.text[:400])
        check("groups page excludes private chats", "私聊用户不应显示" not in r.text, r.text[:400])

        r = client.post("/groups/add", data={"chat": ""}, follow_redirects=False)
        check(
            "groups add empty rejected",
            r.status_code == 303 and "err=" in r.headers["location"],
            str(r.headers),
        )

        async def fake_fetch_group(ref: str) -> dict:
            return {
                "id": -100777,
                "type": "supergroup",
                "title": "手动添加群",
                "username": "manual_group",
            }

        web_pages._fetch_telegram_group = fake_fetch_group

        async def fake_left_membership(chat_id: int) -> dict:
            return {"status": "left"}

        web_pages._fetch_bot_membership = fake_left_membership
        r = client.post("/groups/add", data={"chat": "@manual_group"}, follow_redirects=False)
        check(
            "groups add rejects absent bot",
            r.status_code == 303 and "err=" in r.headers["location"],
            str(r.headers),
        )
        web_pages._fetch_bot_membership = fake_bot_membership

        r = client.post("/groups/add", data={"chat": "@manual_group"}, follow_redirects=False)
        check(
            "groups add ok",
            r.status_code == 303 and "msg=" in r.headers["location"],
            str(r.headers),
        )
        con = connect()
        added_owner = con.execute(
            "SELECT telegram_id FROM console_users cu JOIN console_group_access ga ON ga.user_id=cu.id "
            "WHERE ga.group_id=-100777 AND ga.role='owner'"
        ).fetchone()
        con.close()
        check(
            "added group is owned by current console user",
            added_owner is not None and int(added_owner[0]) == ADMIN_ID,
            repr(added_owner),
        )
        r = client.get("/groups?q=手动添加")
        check(
            "added group visible with owner",
            "手动添加群" in r.text and "Smoke Admin" in r.text,
            r.text[:400],
        )

        r = client.get("/logout", follow_redirects=False)
        check(
            "logout GET rejected",
            r.status_code == 405,
            f"{r.status_code} {r.text[:200]}",
        )

        r = client.post(
            "/groups/-1001234567890/settings",
            data={"title": "csrf"},
            headers={"Referer": "http://evil.example/x"},
            follow_redirects=False,
        )
        check(
            "cross-origin referer rejected",
            r.status_code == 403 and "CSRF" in r.text,
            f"{r.status_code} {r.text[:200]}",
        )

        r = client.post("/logout", follow_redirects=False)
        check(
            "logout",
            r.status_code == 303 and r.headers["location"] == "/login",
            str(r.headers),
        )

        r = client.get("/", follow_redirects=False)
        check(
            "logout cleared session",
            r.status_code == 303 and r.headers["location"].startswith("/login"),
            str(r.headers),
        )
finally:
    shutil.rmtree(TMP, ignore_errors=True)

print("\nFAILURES:", failures if failures else "none")
sys.exit(1 if failures else 0)
