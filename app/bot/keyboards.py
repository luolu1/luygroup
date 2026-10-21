"""Inline keyboards used by the bot."""
from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder


def main_menu() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="👥 群组命令", callback_data="help:group"),
        InlineKeyboardButton(text="🔐 私聊命令", callback_data="help:private"),
    )
    builder.row(InlineKeyboardButton(text="✖️ 关闭", callback_data="panel:close"))
    return builder.as_markup()


def back_menu() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="↩️ 返回", callback_data="help:home"))
    return builder.as_markup()


def close_button() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="✖️ 关闭", callback_data="panel:close"))
    return builder.as_markup()


def verification_kb(
    token: str,
    channels: list[tuple[str, str]],
    options: list[str],
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for title, link in channels:
        if link:
            builder.row(InlineKeyboardButton(text=f"📢 {title}", url=link))
    for index in range(0, len(options), 2):
        buttons = [
            InlineKeyboardButton(
                text=f"🔤 {option}",
                callback_data=f"verify:{token}:{index + offset}",
            )
            for offset, option in enumerate(options[index : index + 2])
        ]
        builder.row(*buttons)
    builder.row(InlineKeyboardButton(text="🔄 换一张", callback_data=f"verifynew:{token}"))
    return builder.as_markup()


def redpacket_kb(packet_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="🧧 领取红包", callback_data=f"rp:{packet_id}"))
    return builder.as_markup()


def lottery_kb(lottery_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="🎟 参与抽奖", callback_data=f"lot:{lottery_id}"))
    return builder.as_markup()


def auction_kb(auction_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="💰 出价", callback_data=f"bid:{auction_id}"))
    return builder.as_markup()


def shop_kb(items: list[tuple[int, str, int]]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for item_id, name, cost in items[:20]:
        builder.row(InlineKeyboardButton(text=f"🛒 {name} · {cost}", callback_data=f"buy:{item_id}"))
    return builder.as_markup()


def confirm_kb(action: str, target_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="✅ 确认", callback_data=f"confirm:{action}:{target_id}"),
        InlineKeyboardButton(text="✖️ 取消", callback_data="panel:close"),
    )
    return builder.as_markup()
