"""SQLAlchemy models for GroupGuard."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import declarative_base

Base = declarative_base()


def utcnow() -> datetime:
    """Naive UTC timestamps, which are what SQLite stores reliably."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def day_key(now: datetime | None = None) -> str:
    return (now or utcnow()).strftime("%Y-%m-%d")


class JsonMixin:
    @staticmethod
    def loads(raw: str | None, default: Any) -> Any:
        if not raw:
            return default
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            return default


class Group(Base):
    __tablename__ = "groups"

    id = Column(BigInteger, primary_key=True, autoincrement=False)  # Telegram chat id
    title = Column(String(255), default="")
    username = Column(String(255), default="")
    chat_type = Column(String(32), default="supergroup")
    is_active = Column(Boolean, default=True, index=True)
    owner_id = Column(BigInteger, nullable=True)
    member_count = Column(Integer, default=0)
    message_count = Column(Integer, default=0)
    created_at = Column(DateTime, default=utcnow)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)

    # ---- moderation ----
    anti_ad_enabled = Column(Boolean, default=True)
    anti_spam_enabled = Column(Boolean, default=True)
    block_forwarded = Column(Boolean, default=False)
    max_message_length = Column(Integer, default=0)  # 0 = off
    allowed_domains = Column(Text, default="")  # comma separated
    ad_keywords = Column(Text, default="")  # newline/comma separated extra keywords

    # ---- warnings / flood ----
    warn_limit = Column(Integer, default=3)
    warn_action = Column(String(16), default="mute")  # mute / kick / ban
    mute_minutes = Column(Integer, default=60)
    flood_limit = Column(Integer, default=8)
    flood_window = Column(Integer, default=10)
    flood_action = Column(String(16), default="mute")

    # ---- AI ----
    ai_ad_enabled = Column(Boolean, default=False)
    ai_assistant_enabled = Column(Boolean, default=True)
    ai_chat_enabled = Column(Boolean, default=False)
    ai_daily_quota = Column(Integer, default=200)
    sample_daily_quota = Column(Integer, default=500)

    # ---- join verification ----
    verify_enabled = Column(Boolean, default=False)
    verify_timeout = Column(Integer, default=300)
    require_username = Column(Boolean, default=False)
    bio_block_keywords = Column(Text, default="")
    invite_redemption = Column(Boolean, default=False)

    # ---- welcome / rules ----
    welcome_enabled = Column(Boolean, default=True)
    welcome_text = Column(
        Text,
        default="欢迎 {name} 加入 {group}！\n请先阅读群规：/rules",
    )
    rules_text = Column(Text, default="")
    log_channel_id = Column(BigInteger, nullable=True)
    delete_command_messages = Column(Boolean, default=False)

    # ---- linkage / report ----
    crossban_enabled = Column(Boolean, default=False)
    crossban_mode = Column(String(16), default="shared")  # shared / own
    report_enabled = Column(Boolean, default=True)
    report_receive = Column(Boolean, default=False)
    report_chat_id = Column(BigInteger, nullable=True)

    # ---- points ----
    points_enabled = Column(Boolean, default=True)
    points_per_message = Column(Integer, default=1)
    points_cooldown = Column(Integer, default=60)
    daily_bonus = Column(Integer, default=5)
    points_name = Column(String(32), default="积分")

    __table_args__ = (Index("ix_groups_active_id", "is_active", "id"),)


class ConsoleUser(Base):
    """A web-console identity, independent from Telegram chat/user IDs."""

    __tablename__ = "console_users"

    id = Column(Integer, primary_key=True)
    telegram_id = Column(BigInteger, nullable=True, unique=True, index=True)
    username = Column(String(128), default="")
    display_name = Column(String(255), default="")
    token_hash = Column(String(64), unique=True, index=True, nullable=False)
    # The bot must be able to show the same temporary token again within its
    # validity window. This value is only exposed to the owning admin in a
    # private chat; token authentication still uses token_hash.
    token_value = Column(String(128), nullable=True)
    token_expires_at = Column(DateTime, nullable=True, index=True)
    console_path = Column(String(32), unique=True, index=True, nullable=False)
    role = Column(String(24), default="user")  # superadmin / subscriber / user
    is_active = Column(Boolean, default=True, index=True)
    created_at = Column(DateTime, default=utcnow)
    last_login_at = Column(DateTime, nullable=True)


class SubscriptionPlan(Base):
    __tablename__ = "subscription_plans"

    id = Column(Integer, primary_key=True)
    name = Column(String(64), unique=True)
    label = Column(String(128), default="")
    features = Column(Text, default="[]")
    group_limit = Column(Integer, default=1)
    ai_daily_quota = Column(Integer, default=0)
    sample_daily_quota = Column(Integer, default=0)
    enabled = Column(Boolean, default=True)


class Subscription(Base):
    __tablename__ = "subscriptions"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, index=True)
    plan_id = Column(Integer, index=True)
    status = Column(String(16), default="active", index=True)
    starts_at = Column(DateTime, default=utcnow)
    expires_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=utcnow)


class ConsoleGroupAccess(Base):
    __tablename__ = "console_group_access"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, index=True)
    group_id = Column(BigInteger, index=True)
    role = Column(String(16), default="owner")  # owner / manager / viewer
    created_at = Column(DateTime, default=utcnow)

    __table_args__ = (UniqueConstraint("user_id", "group_id", name="uq_console_user_group"),)


class Admin(Base):
    __tablename__ = "admins"

    id = Column(Integer, primary_key=True)
    group_id = Column(BigInteger, index=True)
    user_id = Column(BigInteger, index=True)
    role = Column(String(16), default="assistant")  # owner / admin / assistant
    permissions = Column(Text, default='["ban","mute","warn","delete","info"]')
    added_by = Column(BigInteger, nullable=True)
    created_at = Column(DateTime, default=utcnow)

    __table_args__ = (UniqueConstraint("group_id", "user_id", name="uq_admin_group_user"),)

    @property
    def permission_set(self) -> set[str]:
        raw = (self.permissions or "").strip()
        if not raw:
            return set()
        parsed = JsonMixin.loads(raw, None)
        if isinstance(parsed, list):
            return {str(item).strip() for item in parsed if str(item).strip()}
        # Backward compatibility: older /assistant writes stored "ban,mute,...".
        return {item.strip() for item in raw.split(",") if item.strip()}


class Warning(Base):
    __tablename__ = "warnings"

    id = Column(Integer, primary_key=True)
    group_id = Column(BigInteger, index=True)
    user_id = Column(BigInteger, index=True)
    admin_id = Column(BigInteger, nullable=True)
    # Human-readable record ID, e.g. 202610191023 (UTC minute + optional suffix).
    code = Column(String(32), index=True, default="")
    reason = Column(String(512), default="")
    # Original message text, stored so an AI false positive can be learned.
    content = Column(Text, default="")
    source = Column(String(16), default="manual")  # manual / keyword / builtin / ai / flood / other rules
    action = Column(String(16), default="warn")  # warn / mute / kick / ban
    review_status = Column(String(16), default="")  # pending / false_positive / confirmed
    review_note = Column(String(512), default="")
    reviewed_by = Column(BigInteger, nullable=True)
    reviewed_at = Column(DateTime, nullable=True)
    active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=utcnow)
    expires_at = Column(DateTime, nullable=True)

    __table_args__ = (Index("ix_warnings_group_user_active", "group_id", "user_id", "active"),)


class Member(Base):
    __tablename__ = "members"

    id = Column(Integer, primary_key=True)
    group_id = Column(BigInteger, index=True)
    user_id = Column(BigInteger, index=True)
    username = Column(String(255), default="")
    first_name = Column(String(255), default="")
    is_member = Column(Boolean, default=True)
    is_verified = Column(Boolean, default=False)
    is_banned = Column(Boolean, default=False)
    is_muted = Column(Boolean, default=False)
    muted_until = Column(DateTime, nullable=True)
    # Set when a moderator bans/kicks the member. The next join is muted and
    # stays muted until an administrator explicitly unmutes them.
    mute_on_rejoin = Column(Boolean, default=False)
    message_count = Column(Integer, default=0)
    points = Column(Integer, default=0)
    last_points_at = Column(DateTime, nullable=True)
    joined_at = Column(DateTime, default=utcnow)
    last_seen = Column(DateTime, default=utcnow)

    __table_args__ = (UniqueConstraint("group_id", "user_id", name="uq_member_group_user"),)


class Whitelist(Base):
    __tablename__ = "whitelist"

    id = Column(Integer, primary_key=True)
    group_id = Column(BigInteger, index=True)
    user_id = Column(BigInteger, index=True)
    note = Column(String(255), default="")
    added_by = Column(BigInteger, nullable=True)
    created_at = Column(DateTime, default=utcnow)

    __table_args__ = (UniqueConstraint("group_id", "user_id", name="uq_whitelist_group_user"),)


class GlobalBlacklist(Base):
    __tablename__ = "global_blacklist"

    id = Column(Integer, primary_key=True)
    user_id = Column(BigInteger, unique=True, index=True)
    # NULL means a platform-wide entry managed by the superadmin.  A non-NULL
    # owner scopes the entry to that console user's bound groups so one
    # subscriber cannot blacklist users in everybody else's groups.
    owner_user_id = Column(Integer, index=True)
    reason = Column(String(512), default="")
    added_by = Column(BigInteger, nullable=True)
    created_at = Column(DateTime, default=utcnow)


class Keyword(Base):
    __tablename__ = "keywords"

    id = Column(Integer, primary_key=True)
    group_id = Column(BigInteger, index=True)
    pattern = Column(String(255))
    is_regex = Column(Boolean, default=False)
    action = Column(String(16), default="delete")  # delete / warn / mute / ban / kick / reply
    reply_text = Column(Text, default="")  # action=reply 时发送的自动回复
    note = Column(String(255), default="")
    enabled = Column(Boolean, default=True)
    created_by = Column(BigInteger, nullable=True)
    created_at = Column(DateTime, default=utcnow)


class KeywordHit(Base):
    __tablename__ = "keyword_hits"

    id = Column(Integer, primary_key=True)
    group_id = Column(BigInteger, index=True)
    user_id = Column(BigInteger, index=True)
    keyword = Column(String(255), default="")
    content = Column(Text, default="")
    action = Column(String(16), default="")
    source = Column(String(16), default="keyword")  # keyword / builtin / ai / flood
    created_at = Column(DateTime, default=utcnow)


class AdminLog(Base):
    __tablename__ = "admin_logs"

    id = Column(Integer, primary_key=True)
    group_id = Column(BigInteger, index=True)
    admin_id = Column(BigInteger, nullable=True, index=True)
    action = Column(String(32), index=True)
    target_id = Column(BigInteger, nullable=True)
    detail = Column(Text, default="")
    created_at = Column(DateTime, default=utcnow)


class AIProvider(Base):
    __tablename__ = "ai_providers"

    id = Column(Integer, primary_key=True)
    # NULL means a platform/built-in provider visible to every console user.
    # A non-NULL value isolates a user's own API key/provider configuration.
    owner_user_id = Column(Integer, nullable=True, index=True)
    name = Column(String(64), unique=True)
    label = Column(String(128), default="")
    base_url = Column(String(512), default="")
    api_key = Column(String(512), default="")
    model = Column(String(128), default="")
    priority = Column(Integer, default=100)
    enabled = Column(Boolean, default=True)
    builtin = Column(Boolean, default=False)
    created_at = Column(DateTime, default=utcnow)


class AIUsage(Base):
    __tablename__ = "ai_usage"

    id = Column(Integer, primary_key=True)
    group_id = Column(BigInteger, index=True)
    day = Column(String(10), index=True)
    requests = Column(Integer, default=0)
    samples = Column(Integer, default=0)
    hits = Column(Integer, default=0)

    __table_args__ = (UniqueConstraint("group_id", "day", name="uq_ai_usage_group_day"),)


class AIEvent(Base):
    __tablename__ = "ai_events"

    id = Column(Integer, primary_key=True)
    group_id = Column(BigInteger, index=True)
    user_id = Column(BigInteger, nullable=True)
    kind = Column(String(16), default="moderation")  # moderation / assistant / chat
    provider = Column(String(64), default="")
    prompt = Column(Text, default="")
    result = Column(Text, default="")
    hit = Column(Boolean, default=False)
    latency_ms = Column(Integer, default=0)
    created_at = Column(DateTime, default=utcnow, index=True)


class Verification(Base):
    __tablename__ = "verifications"

    id = Column(Integer, primary_key=True)
    group_id = Column(BigInteger, index=True)
    user_id = Column(BigInteger, index=True)
    token = Column(String(64), unique=True, index=True)
    status = Column(String(16), default="pending")  # pending / passed / timeout / kicked
    message_id = Column(BigInteger, nullable=True)
    created_at = Column(DateTime, default=utcnow)
    passed_at = Column(DateTime, nullable=True)
    failed_attempts = Column(Integer, default=0)

    __table_args__ = (Index("ix_verification_group_user_status", "group_id", "user_id", "status"),)


class RequiredChannel(Base):
    __tablename__ = "required_channels"

    id = Column(Integer, primary_key=True)
    group_id = Column(BigInteger, index=True)
    channel_id = Column(BigInteger)
    title = Column(String(255), default="")
    username = Column(String(255), default="")
    invite_link = Column(String(512), default="")
    created_at = Column(DateTime, default=utcnow)

    __table_args__ = (UniqueConstraint("group_id", "channel_id", name="uq_channel_group_channel"),)


class InviteCode(Base):
    __tablename__ = "invite_codes"

    id = Column(Integer, primary_key=True)
    group_id = Column(BigInteger, index=True)
    code = Column(String(32), unique=True, index=True)
    link = Column(String(512), default="")
    created_by = Column(BigInteger, nullable=True)
    uses = Column(Integer, default=0)
    max_uses = Column(Integer, default=0)  # 0 = unlimited
    expires_at = Column(DateTime, nullable=True)
    active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=utcnow)


class CrossLink(Base):
    __tablename__ = "cross_links"

    id = Column(Integer, primary_key=True)
    group_id = Column(BigInteger, index=True)
    linked_group_id = Column(BigInteger, index=True)
    created_by = Column(BigInteger, nullable=True)
    created_at = Column(DateTime, default=utcnow)

    __table_args__ = (UniqueConstraint("group_id", "linked_group_id", name="uq_cross_link"),)


class ReportLink(Base):
    __tablename__ = "report_links"

    id = Column(Integer, primary_key=True)
    group_id = Column(BigInteger, index=True)  # source group
    subscriber_id = Column(BigInteger, index=True)  # receives reports from group_id
    created_by = Column(BigInteger, nullable=True)
    created_at = Column(DateTime, default=utcnow)

    __table_args__ = (UniqueConstraint("group_id", "subscriber_id", name="uq_report_link"),)


class Report(Base):
    __tablename__ = "reports"

    id = Column(Integer, primary_key=True)
    group_id = Column(BigInteger, index=True)
    reporter_id = Column(BigInteger)
    target_user_id = Column(BigInteger, nullable=True)
    reason = Column(String(512), default="")
    excerpt = Column(Text, default="")
    message_id = Column(BigInteger, nullable=True)
    created_at = Column(DateTime, default=utcnow)


class PointsTransaction(Base):
    __tablename__ = "points_transactions"

    id = Column(Integer, primary_key=True)
    group_id = Column(BigInteger, index=True)
    user_id = Column(BigInteger, index=True)
    amount = Column(Integer)
    balance_after = Column(Integer, default=0)
    reason = Column(String(255), default="")
    created_at = Column(DateTime, default=utcnow)


class ShopItem(Base):
    __tablename__ = "shop_items"

    id = Column(Integer, primary_key=True)
    group_id = Column(BigInteger, nullable=True, index=True)  # null = global
    name = Column(String(128))
    description = Column(Text, default="")
    cost = Column(Integer, default=10)
    stock = Column(Integer, default=-1)  # -1 = unlimited
    reward_type = Column(String(16), default="custom")  # custom / role
    reward_text = Column(Text, default="")
    enabled = Column(Boolean, default=True)
    created_at = Column(DateTime, default=utcnow)


class ShopOrder(Base):
    __tablename__ = "shop_orders"

    id = Column(Integer, primary_key=True)
    group_id = Column(BigInteger, index=True)
    user_id = Column(BigInteger, index=True)
    item_id = Column(Integer, index=True)
    cost = Column(Integer, default=0)
    status = Column(String(16), default="completed")
    detail = Column(Text, default="")
    created_at = Column(DateTime, default=utcnow)


class RedPacket(Base):
    __tablename__ = "red_packets"

    id = Column(Integer, primary_key=True)
    group_id = Column(BigInteger, index=True)
    sender_id = Column(BigInteger)
    total = Column(Integer, default=0)
    count = Column(Integer, default=1)
    remaining_amount = Column(Integer, default=0)
    remaining_count = Column(Integer, default=0)
    note = Column(String(255), default="")
    status = Column(String(16), default="open")  # open / finished / expired
    message_id = Column(BigInteger, nullable=True)
    created_at = Column(DateTime, default=utcnow)
    expires_at = Column(DateTime, nullable=True)


class RedPacketClaim(Base):
    __tablename__ = "red_packet_claims"

    id = Column(Integer, primary_key=True)
    red_packet_id = Column(Integer, index=True)
    user_id = Column(BigInteger, index=True)
    amount = Column(Integer, default=0)
    created_at = Column(DateTime, default=utcnow)

    __table_args__ = (UniqueConstraint("red_packet_id", "user_id", name="uq_redpacket_claim"),)


class Lottery(Base):
    __tablename__ = "lotteries"

    id = Column(Integer, primary_key=True)
    group_id = Column(BigInteger, index=True)
    creator_id = Column(BigInteger)
    title = Column(String(255))
    prize = Column(String(512), default="")
    entry_fee = Column(Integer, default=0)
    winners_count = Column(Integer, default=1)
    status = Column(String(16), default="open")  # open / drawn / cancelled
    message_id = Column(BigInteger, nullable=True)
    ends_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=utcnow)


class LotteryEntry(Base):
    __tablename__ = "lottery_entries"

    id = Column(Integer, primary_key=True)
    lottery_id = Column(Integer, index=True)
    user_id = Column(BigInteger, index=True)
    created_at = Column(DateTime, default=utcnow)

    __table_args__ = (UniqueConstraint("lottery_id", "user_id", name="uq_lottery_entry"),)


class Auction(Base):
    __tablename__ = "auctions"

    id = Column(Integer, primary_key=True)
    group_id = Column(BigInteger, index=True)
    creator_id = Column(BigInteger)
    item = Column(String(255))
    start_price = Column(Integer, default=0)
    step = Column(Integer, default=1)
    current_price = Column(Integer, default=0)
    current_bidder = Column(BigInteger, nullable=True)
    status = Column(String(16), default="open")  # open / finished / cancelled
    message_id = Column(BigInteger, nullable=True)
    ends_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=utcnow)


class AuctionBid(Base):
    __tablename__ = "auction_bids"

    id = Column(Integer, primary_key=True)
    auction_id = Column(Integer, index=True)
    user_id = Column(BigInteger, index=True)
    amount = Column(Integer)
    created_at = Column(DateTime, default=utcnow)


class GroupDailyStat(Base):
    __tablename__ = "group_daily_stats"

    id = Column(Integer, primary_key=True)
    group_id = Column(BigInteger, index=True)
    day = Column(String(10), index=True)
    messages = Column(Integer, default=0)
    joins = Column(Integer, default=0)
    leaves = Column(Integer, default=0)
    warns = Column(Integer, default=0)
    bans = Column(Integer, default=0)
    deletes = Column(Integer, default=0)
    ai_requests = Column(Integer, default=0)
    ai_hits = Column(Integer, default=0)

    __table_args__ = (UniqueConstraint("group_id", "day", name="uq_daily_stat_group_day"),)


__all__ = [
    "Base",
    "utcnow",
    "day_key",
    "Group",
    "Admin",
    "Warning",
    "Member",
    "Whitelist",
    "GlobalBlacklist",
    "Keyword",
    "KeywordHit",
    "AdminLog",
    "AIProvider",
    "AIUsage",
    "AIEvent",
    "Verification",
    "RequiredChannel",
    "InviteCode",
    "CrossLink",
    "ReportLink",
    "Report",
    "PointsTransaction",
    "ShopItem",
    "ShopOrder",
    "RedPacket",
    "RedPacketClaim",
    "Lottery",
    "LotteryEntry",
    "Auction",
    "AuctionBid",
    "GroupDailyStat",
]
