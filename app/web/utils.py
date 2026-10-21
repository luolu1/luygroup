"""Small presentation helpers shared by the web templates."""
from __future__ import annotations

from datetime import datetime
from html import escape


def fmt_dt(value: datetime | None, fallback: str = "—") -> str:
    if not value:
        return fallback
    try:
        return value.strftime("%Y-%m-%d %H:%M")
    except AttributeError:
        return str(value)


def fmt_date(value: datetime | None, fallback: str = "—") -> str:
    if not value:
        return fallback
    try:
        return value.strftime("%Y-%m-%d")
    except AttributeError:
        return str(value)


def short(value: str | None, limit: int = 80) -> str:
    text = (value or "").strip().replace("\n", " ")
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "…"


def yes_no(value: bool | None) -> str:
    return "是" if value else "否"


def n(value: int | None) -> int:
    return int(value or 0)


def user_label(user_id: int | None, name: str | None = None, username: str | None = None) -> str:
    if name:
        return f"{name} (@{username})" if username else name
    if username:
        return f"@{username}"
    return f"ID {user_id}" if user_id else "未知"


__all__ = ["fmt_dt", "fmt_date", "short", "yes_no", "n", "user_label", "escape"]
