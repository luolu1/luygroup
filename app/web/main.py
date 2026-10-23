"""FastAPI application that serves the GroupGuard control panel."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote, urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.sessions import SessionMiddleware

from ..config import get_config
from ..db import dispose_db, init_db, session_scope
from ..entitlements import EMPTY_ENTITLEMENTS, build_for_user
from ..models import ConsoleUser
from . import pages
from .auth import LoginRequired, current_admin_id

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"


class ConsoleContextMiddleware(BaseHTTPMiddleware):
    """Load the current console identity and its effective entitlements.

    The session role cookie is only an authentication marker.  Permissions are
    always rebuilt from the database so role changes and expired subscriptions
    take effect immediately.
    """

    async def dispatch(self, request: Request, call_next):  # type: ignore[no-untyped-def]
        request.state.entitlements = EMPTY_ENTITLEMENTS
        request.state.console_user = None
        path = request.url.path
        if not path.startswith("/static") and path != "/healthz":
            user_id = current_admin_id(request)
            if user_id is not None:
                try:
                    async with session_scope() as session:
                        user = await session.get(ConsoleUser, int(user_id))
                        if user is not None and user.is_active:
                            request.state.console_user = user
                            request.state.entitlements = await build_for_user(
                                session, user
                            )
                            # Keep the session marker in sync for legacy checks.
                            request.session["console_role"] = user.role
                except Exception:  # never let a context lookup break the panel
                    log.exception("加载控制台权限失败")
        return await call_next(request)


UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


class CSRFOriginMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):  # type: ignore[no-untyped-def]
        path = request.url.path
        if request.method in UNSAFE_METHODS and not path.startswith("/static") and path != "/healthz":
            origin = request.headers.get("origin")
            referer = request.headers.get("referer")
            if origin == "null":
                return PlainTextResponse("CSRF check failed", status_code=403)
            candidate = origin or referer
            if candidate:
                netloc = urlsplit(candidate).netloc.lower()
                allowed = {
                    (request.headers.get("host") or "").lower(),
                    urlsplit(get_config().web.base_url).netloc.lower(),
                }
                if netloc not in allowed:
                    return PlainTextResponse("CSRF check failed", status_code=403)
            # Missing Origin/Referer means a non-browser client; real cross-site
            # browser POSTs carry Origin and are already blocked by SameSite=Lax.
        return await call_next(request)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    await init_db()
    cfg = get_config()
    log.info("GroupGuard panel ready on %s", cfg.web.base_url)
    yield
    await dispose_db()


def create_app() -> FastAPI:
    cfg = get_config()
    app = FastAPI(
        title="GroupGuard 控制台",
        version="1.0.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    # Added first so SessionMiddleware wraps this middleware and
    # ``request.session`` is available while loading entitlements.
    app.add_middleware(ConsoleContextMiddleware)
    app.add_middleware(
        SessionMiddleware,
        secret_key=cfg.web.secret_key,
        session_cookie="groupguard_session",
        max_age=cfg.web.session_max_age,
        same_site="lax",
        https_only=cfg.web.base_url.lower().startswith("https://"),
    )

    if STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.exception_handler(LoginRequired)
    async def _login_required(request: Request, _exc: LoginRequired) -> RedirectResponse:
        target = request.url.path
        if request.url.query:
            target = f"{target}?{request.url.query}"
        return RedirectResponse(f"/login?next={quote(target, safe='/')}", status_code=303)

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, bool]:
        return {"ok": True}

    app.include_router(pages.public_router)
    app.include_router(pages.router)
    app.add_middleware(CSRFOriginMiddleware)
    return app


app = create_app()
