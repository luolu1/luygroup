"""Offline Telegram transport preserving real aiogram request/response models."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Literal, assert_never

from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import (
    AnswerCallbackQuery,
    DeleteMessage,
    EditMessageReplyMarkup,
    EditMessageText,
    GetChatMember,
    GetMe,
    PinChatMessage,
    SendMessage,
    TelegramMethod,
)
from aiogram.methods.base import TelegramType
from aiogram.types import Chat, ChatMemberOwner, InlineKeyboardMarkup, Message, User

type AuctionRequest = (
    SendMessage | EditMessageText | EditMessageReplyMarkup | AnswerCallbackQuery
    | DeleteMessage | GetChatMember | GetMe | PinChatMessage
)


def callbacks(markup: InlineKeyboardMarkup | None) -> set[str]:
    return {
        button.callback_data
        for row in (markup.inline_keyboard if markup else [])
        for button in row
        if button.callback_data
    }


def bid_cards(requests: tuple[AuctionRequest, ...]) -> list[SendMessage]:
    return [
        request for request in requests
        if isinstance(request, SendMessage)
        and "<b>拍品最新出价</b>" in request.text
        and "<b>当前出价</b>" in request.text
    ]


class OfflineAuctionSession(AiohttpSession):
    """Record API methods, retain visible messages, and inject explicit API failures."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[AuctionRequest] = []
        self.messages: dict[tuple[int, int], Message] = {}
        self.sent_messages: list[tuple[SendMessage, Message]] = []
        self.next_message_id = 8000
        self.fail_next: set[str] = set()
        self.on_request: Callable[[AuctionRequest], Awaitable[None]] | None = None

    async def make_request(
        self, bot: Bot, method: TelegramMethod[TelegramType], timeout: int | None = None
    ) -> TelegramType:
        if not isinstance(method, (
            SendMessage, EditMessageText, EditMessageReplyMarkup, AnswerCallbackQuery,
            DeleteMessage, GetChatMember, GetMe, PinChatMessage,
        )):
            raise AssertionError(f"Offline transport refuses {method.__api_method__}")
        self.calls.append(method)
        if self.on_request is not None:
            await self.on_request(method)
        if method.__api_method__ in self.fail_next:
            self.fail_next.remove(method.__api_method__)
            raise TelegramBadRequest(method=method, message="synthetic auction API failure")
        result: Message | User | ChatMemberOwner | Literal[True]
        match method:
            case SendMessage():
                assert isinstance(method.chat_id, int)
                assert method.reply_markup is None or isinstance(method.reply_markup, InlineKeyboardMarkup)
                self.next_message_id += 1
                result = Message(
                    message_id=self.next_message_id, date=datetime.now(UTC),
                    chat=Chat(id=method.chat_id, type="private" if method.chat_id > 0 else "supergroup"),
                    from_user=User(id=bot.id, is_bot=True, first_name="Offline auction bot"),
                    text=method.text, reply_markup=method.reply_markup,
                )
                self.messages[(method.chat_id, result.message_id)] = result
            case EditMessageText():
                assert isinstance(method.chat_id, int) and method.message_id is not None
                assert method.business_connection_id is None
                original = self.messages[(method.chat_id, method.message_id)]
                result = original.model_copy(update={"text": method.text, "reply_markup": method.reply_markup})
                self.messages[(method.chat_id, method.message_id)] = result
            case EditMessageReplyMarkup():
                assert isinstance(method.chat_id, int) and method.message_id is not None
                original = self.messages[(method.chat_id, method.message_id)]
                result = original.model_copy(update={"reply_markup": method.reply_markup})
                self.messages[(method.chat_id, method.message_id)] = result
            case GetMe():
                result = User(id=bot.id, is_bot=True, first_name="Offline auction bot", username="auction_offline_bot")
            case GetChatMember():
                result = ChatMemberOwner(
                    user=User(id=method.user_id, is_bot=False, first_name="Synthetic admin"),
                    is_anonymous=False,
                )
            case DeleteMessage():
                assert isinstance(method.chat_id, int)
                self.messages.pop((method.chat_id, method.message_id), None)
                result = True
            case AnswerCallbackQuery() | PinChatMessage():
                result = True
            case unreachable:
                assert_never(unreachable)
        match result:
            case True:
                body = "true"
            case Message() | User() | ChatMemberOwner():
                body = result.model_dump_json(exclude_none=True)
            case unreachable:
                assert_never(unreachable)
        response = self.check_response(bot, method, 200, '{"ok":true,"result":' + body + "}")
        assert response.result is not None
        if isinstance(method, SendMessage):
            assert isinstance(response.result, Message)
            self.sent_messages.append((method, response.result))
            self.messages[(response.result.chat.id, response.result.message_id)] = response.result
        return response.result
