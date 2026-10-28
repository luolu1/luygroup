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


def console_login_kb(url: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="🌐 访问控制台", url=url))
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


def lottery_setup_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="✅ 确认创建", callback_data="lotsetup:create"),
        InlineKeyboardButton(text="✖️ 取消", callback_data="lotsetup:cancel"),
    )
    builder.row(InlineKeyboardButton(text="⬅️ 返回修改", callback_data="lotsetup:back:contact"))
    return builder.as_markup()


def lottery_condition_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="无门槛", callback_data="lotsetup:condition:none"),
        InlineKeyboardButton(text="关键词", callback_data="lotsetup:condition:keyword"),
    )
    builder.row(
        InlineKeyboardButton(text="积分门槛", callback_data="lotsetup:condition:points"),
        InlineKeyboardButton(
            text="发言数门槛", callback_data="lotsetup:condition:message_count"
        ),
    )
    builder.row(InlineKeyboardButton(text="⬅️ 返回", callback_data="lotsetup:back:prize"))
    return builder.as_markup()


def lottery_draw_condition_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="按时间开奖", callback_data="lotsetup:draw:time"),
        InlineKeyboardButton(
            text="按参与人数开奖", callback_data="lotsetup:draw:participant"
        ),
    )
    builder.row(InlineKeyboardButton(text="⬅️ 返回", callback_data="lotsetup:back:quantity"))
    return builder.as_markup()


def lottery_back_kb(target_step: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="⬅️ 返回上一步", callback_data=f"lotsetup:back:{target_step}")
    )
    return builder.as_markup()


def lottery_management_kb(lottery_id: int, can_change: bool) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="查看详情", callback_data=f"lotmanage:view:{lottery_id}"),
    )
    if can_change:
        builder.row(
            InlineKeyboardButton(text="取消抽奖", callback_data=f"lotmanage:cancel:{lottery_id}"),
            InlineKeyboardButton(text="编辑抽奖", callback_data=f"lotmanage:edit:{lottery_id}"),
        )
    else:
        builder.row(
            InlineKeyboardButton(text="删除记录", callback_data=f"lotmanage:delete:{lottery_id}")
        )
    return builder.as_markup()


def lottery_history_list_kb(
    rows: list[tuple[int, str, str, int]]
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for lottery_id, title, status, entries in rows:
        label = {"open": "进行中", "drawn": "已开奖", "cancelled": "已取消"}.get(
            status, status
        )
        builder.row(
            InlineKeyboardButton(
                text=f"#{lottery_id} · {title} · {label} · {entries}人",
                callback_data=f"lotmanage:select:{lottery_id}",
            )
        )
    return builder.as_markup()


def lottery_edit_fields_kb(lottery_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for field, label in (
        ("title", "标题"),
        ("prize", "奖品"),
        ("condition", "参与条件"),
        ("quantity", "中奖人数"),
        ("draw", "开奖条件"),
        ("contact", "领奖联系人"),
    ):
        builder.row(
            InlineKeyboardButton(
                text=f"编辑{label}",
                callback_data=f"lotmanage:field:{lottery_id}:{field}",
            )
        )
    builder.row(
        InlineKeyboardButton(
            text="完整编辑", callback_data=f"lotmanage:full_edit:{lottery_id}"
        )
    )
    builder.row(InlineKeyboardButton(text="返回", callback_data="lotmanage:refresh"))
    return builder.as_markup()


def lottery_history_pagination_kb(page: int, has_previous: bool, has_next: bool) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    row = []
    if has_previous:
        row.append(InlineKeyboardButton(text="上一页", callback_data=f"lotmanage:page:{page - 1}"))
    if has_next:
        row.append(InlineKeyboardButton(text="下一页", callback_data=f"lotmanage:page:{page + 1}"))
    if row:
        builder.row(*row)
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
