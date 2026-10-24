"""Application configuration loaded from config.json.

The file path can be overridden with the GROUPGUARD_CONFIG environment variable.
Relative SQLite paths are resolved against the directory containing config.json.
"""
from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, field_validator


class BotConfig(BaseModel):
    token: str
    admin_ids: list[int] = Field(default_factory=list)
    username: str = ""
    proxy: str = ""

    @field_validator("admin_ids", mode="before")
    @classmethod
    def _ints(cls, value: Any) -> list[int]:
        return [int(v) for v in (value or [])]


class WebConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8090
    secret_key: str = "change-me"
    base_url: str = "http://127.0.0.1:8090"
    session_max_age: int = 86400


class DatabaseConfig(BaseModel):
    url: str = "sqlite+aiosqlite:///./data/groupguard.db"


class DefaultsConfig(BaseModel):
    warn_limit: int = 3
    warn_action: str = "mute"
    mute_minutes: int = 60
    flood_limit: int = 8
    flood_window: int = 10
    flood_action: str = "mute"
    ai_daily_quota: int = 200
    sample_daily_quota: int = 500
    points_per_message: int = 1
    points_cooldown: int = 60
    daily_bonus: int = 5


class AIProviderSeed(BaseModel):
    name: str
    label: str = ""
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    priority: int = 100
    enabled: bool = False
    builtin: bool = True


class AIConfig(BaseModel):
    timeout: int = 25
    max_context_messages: int = 8
    # Optional root directory for permanent AI learning JSONL files. The
    # directory stores ad_detection/, assistant/ and chat/ subdirectories.
    # Relative paths are resolved against the directory containing config.json.
    learning_dir: str = ""
    providers: list[AIProviderSeed] = Field(default_factory=list)


class EpayConfig(BaseModel):
    base_url: str = ""
    pid: str = ""
    key: str = ""
    type: str = ""
    method: str = ""
    timeout: int = 15


class PaymentConfig(BaseModel):
    enabled: bool = False
    provider: str = "epay"
    min_amount_cents: int = 100
    max_amount_cents: int = 100000
    epay: EpayConfig = Field(default_factory=EpayConfig)


class AppConfig(BaseModel):
    bot: BotConfig
    web: WebConfig = Field(default_factory=WebConfig)
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    defaults: DefaultsConfig = Field(default_factory=DefaultsConfig)
    ai: AIConfig = Field(default_factory=AIConfig)
    payments: PaymentConfig = Field(default_factory=PaymentConfig)
    config_path: Path = Path("config.json")

    @property
    def project_dir(self) -> Path:
        return self.config_path.parent

    def resolve_database_url(self) -> str:
        url = self.database.url
        prefix = "sqlite+aiosqlite:///"
        if not url.startswith(prefix):
            return url
        raw = url[len(prefix):]
        if raw == ":memory:" or raw.startswith("/"):
            return url
        path = (self.project_dir / raw).resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        return prefix + str(path)


def _config_path() -> Path:
    raw = os.environ.get("GROUPGUARD_CONFIG", "config.json")
    path = Path(raw).expanduser()
    if not path.is_absolute():
        candidate = Path.cwd() / path
        if not candidate.exists():
            # Allow starting the bot from any directory: fall back to the
            # project root (the parent of the ``app`` package).
            project_root = Path(__file__).resolve().parent.parent / path
            if project_root.exists():
                candidate = project_root
        path = candidate
    return path.resolve()


@lru_cache(maxsize=1)
def get_config() -> AppConfig:
    path = _config_path()
    if not path.exists():
        raise SystemExit(
            f"配置文件不存在: {path}\n请先复制 config.example.json 为 config.json 并填写。"
        )
    payload = json.loads(path.read_text(encoding="utf-8"))
    config = AppConfig(**payload)
    config.config_path = path
    return config


def resolve_learning_dir(raw: str) -> Path:
    """Validate and create an AI learning directory.

    Relative paths are resolved against the project directory. The directory
    must be writable so a typo cannot silently discard future learning data.
    """
    text = (raw or "").strip()
    if not text:
        raise ValueError("请填写学习数据目录")
    cfg = get_config()
    path = Path(text).expanduser()
    if not path.is_absolute():
        path = cfg.project_dir / path
    path = path.resolve()
    if path.exists() and not path.is_dir():
        raise ValueError("该路径已存在，但不是目录")
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".groupguard-write-test"
        probe.write_text("", encoding="utf-8")
        probe.unlink(missing_ok=True)
    except OSError as exc:
        raise ValueError(f"目录不可写：{exc}") from exc
    return path


def save_learning_dir(raw: str) -> Path:
    """Persist a validated learning directory to config.json atomically."""
    path = resolve_learning_dir(raw)
    cfg = get_config()
    payload = json.loads(cfg.config_path.read_text(encoding="utf-8"))
    ai = payload.setdefault("ai", {})
    if not isinstance(ai, dict):
        ai = {}
        payload["ai"] = ai
    ai["learning_dir"] = str(path)
    tmp = cfg.config_path.with_name(cfg.config_path.name + ".tmp")
    try:
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        tmp.chmod(0o600)
        os.replace(tmp, cfg.config_path)
    except OSError as exc:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise ValueError(f"保存配置失败：{exc}") from exc
    get_config.cache_clear()
    return path


def is_global_admin(user_id: int | None) -> bool:
    if user_id is None:
        return False
    return int(user_id) in get_config().bot.admin_ids
