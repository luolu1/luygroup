"""Offline timeout, membership-update and fresh CAPTCHA lifecycle regression."""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Literal, assert_never
from unittest.mock import patch

import anyio
from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.methods import (
    AnswerCallbackQuery, BanChatMember, DeleteMessage, GetChatMember,
    GetChatMemberCount, GetMe, RestrictChatMember, SendMessage, SendPhoto,
    TelegramMethod, UnbanChatMember,
)
from aiogram.methods.base import TelegramType
from aiogram.types import (
    CallbackQuery, Chat, ChatMemberBanned, ChatMemberLeft, ChatMemberMember, ChatMemberOwner,
    ChatMemberUpdated, Message, User,
)
from sqlalchemy import select

from smoke_scheduler import RUNTIME
from app.bot import captcha, helpers
from app.bot.handlers import join
from app.db import dispose_db, init_db, session_scope
from app.models import ConsoleGroupAccess, ConsoleUser, Group, Member, Verification, utcnow
from app.services import scheduler


class OfflineVerificationSession(AiohttpSession):
    """Record real API methods and return typed responses without network access."""

    def __init__(self, user: User) -> None:
        super().__init__()
        self.user = user
        self.calls: list[str] = []
        self.restrictions: list[bool | None] = []
        self.deleted: list[tuple[int | str, int]] = []
        self.status: Literal["member", "left", "kicked"] = "member"
        self.message_id = 100

    async def make_request(
        self, bot: Bot, method: TelegramMethod[TelegramType], timeout: int | None = None,
    ) -> TelegramType:
        self.calls.append(method.__api_method__)
        result: Message | ChatMemberMember | ChatMemberLeft | ChatMemberBanned | bool | int
        match method:
            case GetChatMember():
                match self.status:
                    case "member":
                        result = ChatMemberMember(user=self.user)
                    case "left":
                        result = ChatMemberLeft(user=self.user)
                    case "kicked":
                        result = ChatMemberBanned(user=self.user, until_date=0)
                    case unreachable:
                        assert_never(unreachable)
            case GetChatMemberCount():
                result = 10
            case BanChatMember():
                assert method.revoke_messages is False
                self.status = "kicked"
                result = True
            case UnbanChatMember():
                assert method.only_if_banned is True
                self.status = "left"
                result = True
            case RestrictChatMember():
                self.restrictions.append(method.permissions.can_send_messages)
                result = True
            case SendPhoto() | SendMessage():
                assert isinstance(method.chat_id, int)
                self.message_id += 1
                result = Message(
                    message_id=self.message_id, date=datetime.now(UTC),
                    chat=Chat(id=method.chat_id, type="private" if method.chat_id > 0 else "supergroup"),
                )
            case DeleteMessage():
                self.deleted.append((method.chat_id, method.message_id))
                result = True
            case AnswerCallbackQuery():
                result = True
            case _:
                raise AssertionError(f"Offline transport refuses {method.__api_method__}")
        body = json.dumps(result) if isinstance(result, (bool, int)) else result.model_dump_json()
        response = self.check_response(bot, method, 200, '{"ok":true,"result":' + body + '}')
        assert response.result is not None
        return response.result


class OwnershipSession(OfflineVerificationSession):
    """Answer getMe and report selected users as the group owner."""

    def __init__(self, user: User, admin_ids: set[int]) -> None:
        super().__init__(user)
        self.admin_ids = admin_ids
        self.bot_user: User | None = None

    async def make_request(
        self, bot: Bot, method: TelegramMethod[TelegramType], timeout: int | None = None,
    ) -> TelegramType:
        body: str | None = None
        match method:
            case GetMe():
                assert self.bot_user is not None
                body = self.bot_user.model_dump_json()
            case GetChatMember() if method.user_id in self.admin_ids:
                body = ChatMemberOwner(
                    user=User(id=method.user_id, is_bot=False, first_name="Owner"),
                    is_anonymous=False,
                ).model_dump_json()
        if body is None:
            return await super().make_request(bot, method, timeout)
        self.calls.append(method.__api_method__)
        response = self.check_response(bot, method, 200, '{"ok":true,"result":' + body + '}')
        assert response.result is not None
        return response.result


async def inviter_bound_as_owner(group_id: int, *, inviter_is_admin: bool) -> bool:
    inviter = User(id=abs(group_id) + 7, is_bot=False, first_name="Inviter")
    transport = OwnershipSession(inviter, {inviter.id} if inviter_is_admin else set())
    async with Bot("123456:OFFLINE_OWNERSHIP", session=transport) as bot:
        me = User(id=bot.id, is_bot=True, first_name="Offline bot", username="offlinebot")
        transport.bot_user = me
        update = ChatMemberUpdated.model_validate({
            "chat": Chat(id=group_id, type="supergroup", title="Ownership"),
            "from_user": inviter, "date": datetime.now(UTC),
            "old_chat_member": {"status": "left", "user": me, "until_date": 0},
            "new_chat_member": {"status": "member", "user": me, "until_date": 0},
        })
        await join.on_my_chat_member(update, bot)
    async with session_scope() as session:
        owner = await session.scalar(select(ConsoleGroupAccess).where(
            ConsoleGroupAccess.group_id == group_id, ConsoleGroupAccess.role == "owner",
        ))
    return owner is not None


async def pending(group_id: int) -> Verification:
    async with session_scope() as session:
        rows = (await session.scalars(select(Verification).where(
            Verification.group_id == group_id, Verification.status == "pending",
        ))).all()
        assert len(rows) == 1, f"expected one fresh challenge, got {len(rows)}"
        return rows[0]


async def lifecycle(group_id: int, *, username_only: bool, moderated: bool = False) -> None:
    user = User(id=abs(group_id), is_bot=False, first_name="Returning")
    chat = Chat(id=group_id, type="supergroup", title="Lifecycle")
    transport = OfflineVerificationSession(user)
    async with Bot("123456:OFFLINE_LIFECYCLE", session=transport) as bot:
        actor = User(id=bot.id, is_bot=True, first_name="Offline bot")
        async with session_scope() as session:
            session.add(Group(id=group_id, verify_enabled=not username_only,
                              require_username=username_only, welcome_enabled=False))
            owner = ConsoleUser(token_hash=str(group_id), role="superadmin")
            session.add(owner)
            await session.flush()
            session.add(ConsoleGroupAccess(user_id=owner.id, group_id=group_id, role="owner"))
            session.add(Member(group_id=group_id, user_id=user.id,
                               mute_on_rejoin=moderated, is_muted=moderated))

        async def membership(old: str, new: str) -> None:
            update = ChatMemberUpdated.model_validate({
                "chat": chat, "from_user": actor, "date": datetime.now(UTC),
                "old_chat_member": {"status": old, "user": user, "until_date": 0},
                "new_chat_member": {"status": new, "user": user, "until_date": 0},
            })
            await join.on_chat_member(update, bot)

        with patch.object(join.time, "monotonic", return_value=100.0):
            await membership("left", "member")
        initial = await pending(group_id)
        async with session_scope() as session:
            row = await session.get(Verification, initial.id)
            assert row is not None
            row.created_at = utcnow() - timedelta(minutes=10)
            row.failed_attempts = 2
        await scheduler.expire_verifications(bot)
        assert transport.calls.count("banChatMember") == 1, (
            f"timeout must remove member: {transport.calls}"
        )
        assert transport.calls.count("unbanChatMember") == 1
        assert (initial.message_chat_id, initial.message_id) in transport.deleted
        await membership("member", "kicked")
        await membership("kicked", "left")
        transport.status = "member"
        with patch.object(join.time, "monotonic", return_value=500.0):
            await membership("left", "member")
        fresh = await pending(group_id)
        assert fresh.token != initial.token and fresh.failed_attempts == 0
        assert fresh.created_at > initial.created_at
        before = list(transport.restrictions)
        answer = captcha.captcha_options(initial.token).index(captcha.captcha_code(initial.token))
        stale = CallbackQuery(id="stale", from_user=user, chat_instance="offline",
                              data=f"verify:{initial.token}:{answer}").as_(bot)
        await join.on_verify(stale, bot)
        assert transport.restrictions == before
        assert (await pending(group_id)).token == fresh.token
        async with session_scope() as session:
            row = await session.get(Verification, fresh.id)
            assert row is not None
            row.created_at = utcnow() - timedelta(minutes=10)
        await scheduler.expire_verifications(bot)
        assert transport.calls.count("banChatMember") == 2, (
            "second timeout skipped after bot kicked->left membership updates"
        )
        await membership("member", "kicked")
        await membership("kicked", "left")
        transport.status = "member"
        with patch.object(join.time, "monotonic", return_value=900.0):
            await membership("left", "member")
        final = await pending(group_id)
        answer = captcha.captcha_options(final.token).index(captcha.captcha_code(final.token))
        named_user = user.model_copy(update={"username": "returning"})
        callback = CallbackQuery(id="pass", from_user=named_user, chat_instance="offline",
                                 data=f"verify:{final.token}:{answer}").as_(bot)
        await join.on_verify(callback, bot)
        async with session_scope() as session:
            row = await session.get(Verification, final.id)
            member = await session.scalar(select(Member).where(Member.group_id == group_id))
            assert row is not None and row.status == "passed"
            assert member is not None and member.is_verified and member.is_member
            assert not member.is_banned and not member.verification_restricted
            assert bool(member.is_muted) == moderated
            assert bool(member.mute_on_rejoin) == moderated
        assert transport.restrictions[-1] is (not moderated)
        assert (final.message_chat_id, final.message_id) in transport.deleted


async def main() -> None:
    try:
        await init_db()
        failures: list[str] = []
        with patch.object(helpers, "NOTICE_TTL_SECONDS", 0):
            for group_id, name, username_only, moderated in (
                (-100901, "CAPTCHA timeout/rejoin", False, False),
                (-100902, "username-only timeout/rejoin", True, False),
                (-100903, "moderation mute preserved", False, True),
            ):
                try:
                    await lifecycle(group_id, username_only=username_only, moderated=moderated)
                except AssertionError as error:
                    failures.append(name)
                    print(f"FAIL {name}: {error}")
                else:
                    print(f"PASS {name}: two removals, fresh tokens/deadlines, stale callback rejected, pass permissions")
            for group_id, name, inviter_is_admin in (
                (-100911, "non-admin inviter is not bound as owner", False),
                (-100912, "admin inviter is bound as owner", True),
            ):
                bound = await inviter_bound_as_owner(group_id, inviter_is_admin=inviter_is_admin)
                if bound != inviter_is_admin:
                    failures.append(name)
                    print(f"FAIL {name}: owner binding present={bound}")
                else:
                    print(f"PASS {name}")
        assert not failures, failures
    finally:
        join._processed.clear()
        await dispose_db()


if __name__ == "__main__":
    with RUNTIME:
        anyio.run(main)
