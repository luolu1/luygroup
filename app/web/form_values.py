from __future__ import annotations

from typing import Any


def text_value(form: Any, key: str, limit: int = 4000, default: str = "") -> str:
    value = form.get(key)
    if value is None:
        return default
    return str(value).strip()[:limit]


def int_value(
    form: Any,
    key: str,
    default: int,
    low: int = 0,
    high: int = 2_000_000_000,
) -> int:
    raw = form.get(key)
    try:
        value = int(str(raw))
    except (TypeError, ValueError):
        return default
    return max(low, min(high, value))


def bool_value(form: Any, key: str) -> bool:
    return form.get(key) is not None
