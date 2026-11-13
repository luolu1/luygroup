from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import assert_never
from unittest.mock import patch

from aiogram.exceptions import (
    TelegramAPIError, TelegramBadRequest, TelegramNetworkError, TelegramServerError,
)
from aiogram.methods import DeleteMessage, PinChatMessage, SendMessage
from aiogram.types import Message
from sqlalchemy import update

from auction_reminder_claim_tests import run_restart_worker
from auction_reminder_fixtures import (
    NOW,
    WorkerReceipt,
    auction_at,
    captured_bot,
    captured_logs,
    financial_rows,
    isolated_db,
    persist_auction,
    reminder_rows,
)

from app.db import session_scope
from app.bot import helpers
from app.models import Group
from app.services import scheduler


async def ownership_committed_when_telegram_method_dispatched() -> None:
    async with isolated_db(), captured_bot() as (bot, transport):
        await persist_auction(auction_at(NOW + timedelta(seconds=119)))
        before = await financial_rows()

        async def observe_committed_claim(method: SendMessage | PinChatMessage) -> None:
            rows = await reminder_rows()
            assert len(rows) == 1
            match method:
                case SendMessage():
                    assert rows[0].status == "pending" and rows[0].message_id is None
                case PinChatMessage():
                    assert (rows[0].status, rows[0].message_id) == ("sent", method.message_id)
                case unexpected:
                    assert_never(unexpected)
            async with session_scope() as session:
                await session.execute(update(Group).values(title=Group.title, updated_at=NOW))

        transport.before_request = observe_committed_claim

        await scheduler._send_auction_reminders(bot, NOW)

        assert len(transport.delivered) == 1 and transport.pins == []
        assert await financial_rows() == before
        print("PASS separate DB reader/writer sees pending before send; no lock over Telegram or intermediate pin")


async def retry_when_api_rejection_is_confirmed_and_window_eligible() -> None:
    async with isolated_db(), captured_bot() as (bot, transport), captured_logs() as logs:
        await persist_auction(auction_at(NOW + timedelta(seconds=119)))
        before = await financial_rows()

        async def reject_first_send(method: SendMessage | PinChatMessage) -> None:
            match method:
                case SendMessage() if len(transport.sends) == 1:
                    raise TelegramBadRequest(method=method, message="offline confirmed rejection")
                case SendMessage() | PinChatMessage():
                    return
                case unexpected:
                    assert_never(unexpected)

        transport.before_request = reject_first_send
        await scheduler._send_auction_reminders(bot, NOW)
        failed = await reminder_rows()
        assert len(failed) == 1
        assert (failed[0].status, failed[0].message_id) == ("failed", None)
        assert failed[0].last_error and len(transport.delivered) == len(transport.pins) == 0

        await scheduler._send_auction_reminders(bot, NOW + timedelta(seconds=1))
        await scheduler._send_auction_reminders(bot, NOW + timedelta(seconds=2))

        rows = await reminder_rows()
        assert len(rows) == 1 and rows[0].id == failed[0].id
        assert (rows[0].status, rows[0].message_id, rows[0].last_error) == ("sent", 9002, "")
        assert rows[0].claimed_at == rows[0].sent_at == NOW + timedelta(seconds=1)
        assert len(transport.sends) == 2 and len(transport.delivered) == 1 and transport.pins == []
        assert any(record.levelno >= logging.WARNING for record in logs.records)
        assert await financial_rows() == before
        print("PASS confirmed rejection: failed claim retries once while eligible, same claim ID, delivered=1")


async def rejection_not_retried_when_deadline_expired() -> None:
    async with isolated_db(), captured_bot() as (bot, transport):
        await persist_auction(auction_at(NOW + timedelta(seconds=1)))

        async def reject_send(method: SendMessage | PinChatMessage) -> None:
            assert isinstance(method, SendMessage)
            raise TelegramBadRequest(method=method, message="offline confirmed rejection")

        transport.before_request = reject_send
        await scheduler._send_auction_reminders(bot, NOW)

        await scheduler._send_auction_reminders(bot, NOW + timedelta(seconds=1))

        assert [(row.window, row.status) for row in await reminder_rows()] == [(3, "failed")]
        assert len(transport.sends) == 1 and transport.pins == []
        print("PASS failed send never retries after expiry")


async def delivery_retained_when_cleanup_rejected() -> None:
    async with isolated_db(), captured_bot() as (bot, transport), captured_logs() as logs:
        await persist_auction(auction_at(NOW + timedelta(seconds=119)))

        async def reject_cleanup(method: SendMessage | PinChatMessage | DeleteMessage) -> None:
            match method:
                case DeleteMessage():
                    row = (await reminder_rows())[0]
                    assert (row.status, row.message_id) == ("sent", method.message_id)
                    raise TelegramBadRequest(method=method, message="offline cannot delete")
                case SendMessage():
                    return
                case PinChatMessage():
                    raise AssertionError("Countdown must never be pinned")
                case unexpected:
                    assert_never(unexpected)

        transport.before_request = reject_cleanup

        with patch.object(helpers, "NOTICE_TTL_SECONDS", 0.05):
            await scheduler._send_auction_reminders(bot, NOW)
        await asyncio.gather(*tuple(helpers._background_tasks))
        await scheduler._send_auction_reminders(bot, NOW + timedelta(seconds=1))
        receipt = await run_restart_worker(NOW + timedelta(seconds=2))

        assert receipt == WorkerReceipt(sends=0, pins=0, claims=1), receipt
        assert len(transport.sends) == len(transport.delivered) == len(transport.deletes) == 1
        assert transport.pins == []
        rows = await reminder_rows()
        assert (rows[0].status, rows[0].message_id) == ("sent", 9001)
        assert any(record.levelno >= logging.WARNING for record in logs.records)
        print("PASS failed cleanup: sent/message ID durable, repeated tick/restart duplicate sends=0; pins=0")


async def ambiguous_delivery_retained_when_response_lost() -> None:
    for error_type in (TelegramNetworkError, TelegramServerError):
        async with isolated_db(), captured_bot() as (bot, transport), captured_logs() as logs:
            await persist_auction(auction_at(NOW + timedelta(seconds=119)))
            before = await financial_rows()

            async def lose_response(
                _message: Message, failure: type[TelegramAPIError] = error_type
            ) -> None:
                raise failure(method=transport.sends[-1], message="offline ambiguous delivery")

            transport.after_send = lose_response
            await scheduler._send_auction_reminders(bot, NOW)
            rows = await reminder_rows()
            assert len(rows) == 1 and (rows[0].status, rows[0].message_id) == ("unknown", None)
            assert rows[0].last_error
            transport.after_send = None

            await scheduler._send_auction_reminders(bot, NOW + timedelta(seconds=1))
            receipt = await run_restart_worker(NOW + timedelta(seconds=2))

            assert receipt == WorkerReceipt(sends=0, pins=0, claims=1), receipt
            assert len(transport.sends) == len(transport.delivered) == 1 and transport.pins == []
            assert any(record.levelno >= logging.ERROR for record in logs.records)
            assert await financial_rows() == before
            print(f"PASS {error_type.__name__}: unknown claim retained, possible delivery=1, resend=0 after restart")


async def pending_retained_when_dispatch_cancelled_after_delivery() -> None:
    async with isolated_db(), captured_bot() as (bot, transport), captured_logs() as logs:
        await persist_auction(auction_at(NOW + timedelta(seconds=119)))

        async def cancel_after_send(_message: Message) -> None:
            raise asyncio.CancelledError

        transport.after_send = cancel_after_send
        cancelled = False
        try:
            await scheduler._send_auction_reminders(bot, NOW)
        except asyncio.CancelledError:
            cancelled = True
        assert cancelled
        transport.after_send = None

        await scheduler._send_auction_reminders(bot, NOW + timedelta(seconds=1))
        receipt = await run_restart_worker(NOW + timedelta(seconds=2))

        rows = await reminder_rows()
        assert (rows[0].status, rows[0].message_id) == ("pending", None)
        assert receipt == WorkerReceipt(sends=0, pins=0, claims=1), receipt
        assert len(transport.sends) == len(transport.delivered) == 1 and transport.pins == []
        assert any(record.levelno >= logging.WARNING for record in logs.records)
        print("PASS cancellation after possible delivery: pending retained, resend=0 across tick/restart")


async def run_delivery_tests() -> None:
    await ownership_committed_when_telegram_method_dispatched()
    await retry_when_api_rejection_is_confirmed_and_window_eligible()
    await rejection_not_retried_when_deadline_expired()
    await delivery_retained_when_cleanup_rejected()
    await ambiguous_delivery_retained_when_response_lost()
    await pending_retained_when_dispatch_cancelled_after_delivery()
