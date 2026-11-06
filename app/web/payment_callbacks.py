from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_session
from ..services.payments import service as payment_service

public_router = APIRouter(tags=["payments"])
log = logging.getLogger(__name__)


@public_router.api_route("/payments/epay/notify", methods=["GET", "POST"])
async def epay_notify(request: Request, session: AsyncSession = Depends(get_session)) -> Response:
    if request.method == "POST":
        form = await request.form()
        form_items = list(form.multi_items())
    else:
        form_items = list(request.query_params.multi_items())
    if len({key for key, _value in form_items}) != len(form_items):
        return PlainTextResponse("fail", status_code=400)
    params = {str(k): str(v) for k, v in form_items}
    try:
        status, _order = await payment_service.handle_notification(session, None, params)
    except ValueError as exc:
        log.info("Rejected Epay notification: %s", exc)
        return PlainTextResponse("fail", status_code=400)
    except Exception:
        await session.rollback()
        log.exception("Epay notification processing failed")
        return PlainTextResponse("fail", status_code=400)
    if status in {"paid", "already_paid"}:
        return PlainTextResponse("success")
    return PlainTextResponse("fail", status_code=400)


@public_router.get("/payments/icons/{filename}")
async def payment_channel_icon(filename: str) -> FileResponse:
    if Path(filename).name != filename:
        raise HTTPException(status_code=404, detail="图标不存在")
    path = payment_service.icons_dir() / filename
    if not path.is_file():
        raise HTTPException(status_code=404, detail="图标不存在")
    return FileResponse(path, headers={"Cache-Control": "public, max-age=86400"})
