"""Web panel authentication helpers."""
from __future__ import annotations

import secrets
import hashlib
import string

from fastapi import Request

from ..config import get_config


class LoginRequired(Exception):
    """Raised by protected routes when there is no valid admin session."""


def hash_console_token(token: str) -> str:
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()


def new_console_token() -> str:
    return secrets.token_urlsafe(32)


def new_console_path() -> str:
    alphabet = string.ascii_lowercase
    return "".join(secrets.choice(alphabet) for _ in range(12))


def current_admin_id(request: Request) -> int | None:
    try:
        raw = request.session.get("admin_id")
    except AssertionError:  # SessionMiddleware not installed
        return None
    if raw is None or request.session.get("console_auth") is not True:
        return None
    try:
        admin_id = int(raw)
    except (TypeError, ValueError):
        return None
    # The value is set only after a database-backed token validation.
    return admin_id


def require_admin(request: Request) -> int:
    admin_id = current_admin_id(request)
    if admin_id is None:
        raise LoginRequired()
    return admin_id


def verify_credentials(admin_id: int, password: str) -> bool:
    # Kept as a compatibility shim for integrations; web login no longer uses IDs.
    config = get_config()
    return secrets.compare_digest(password or "", config.web.password or "")


def safe_next(raw: str | None, default: str = "/") -> str:
    if not raw or not raw.startswith("/") or raw.startswith("//"):
        return default
    return raw
