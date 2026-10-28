"""Role and subscription entitlements shared by the bot and web console.

The console has three practical levels:

* ``superadmin`` — platform owner, sees every group and every feature.
* ``subscriber`` / active subscription — basic management plus AI, linkage,
  reports, points/games, statistics and other advanced features.
* ``user`` — basic group moderation, local anti-ad rules, logs and settings.

The feature keys are deliberately small and stable.  Routes and templates use
``Entitlements.can(key)`` instead of comparing role strings, so a subscription
upgrade immediately unlocks the same feature everywhere.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from collections.abc import Iterable
from typing import Final

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import (
    ConsoleGroupAccess,
    ConsoleUser,
    Subscription,
    SubscriptionPlan,
    utcnow,
)

# ---------------------------------------------------------------------------
# Feature catalogue
# ---------------------------------------------------------------------------
BASIC_FEATURES: frozenset[str] = frozenset(
    {
        "settings",  # 常规群组配置、规则文案
        "moderation",  # 本地反广告、刷屏、警告策略
        "members",  # 成员信息查询、成员统计
        "warnings",  # 警告记录查询
        "keywords",  # 关键词与命中查询
        "whitelist",  # 白名单 / 免检名单
        "channels",  # 必关频道
        "logs",  # 管理日志查询
        "commands",  # 常用命令面板
        "join",  # 基础进群验证与验证记录
    }
)

ADVANCED_FEATURES: frozenset[str] = frozenset(
    {
        "ai",  # AI 广告检测 / 助理 / 闲聊
        "ai_providers",  # 自定义 AI 接口
        "ai_quota",  # 每群每日 AI / 样本额度
        "linkage",  # 跨群封禁联动
        "global_blacklist",  # 全局黑名单
        "reports",  # 举报与通报
        "verify_advanced",  # 用户名必填、邀请核销、简介关键词
        "points",  # 积分体系、商城、红包
        "welcome",  # 验证通过后的新人欢迎语
        "games",  # 抽奖 / 竞拍
        "admins",  # 协助管理员
        "stats_global",  # 全量数据统计与 AI 命中分析
        "multi_group",  # 按套餐绑定多个群组
    }
)

ALL_FEATURES: frozenset[str] = BASIC_FEATURES | ADVANCED_FEATURES

FEATURE_LABELS: dict[str, str] = {
    "settings": "常规群组配置与规则文案",
    "moderation": "本地反广告、刷屏与警告策略",
    "members": "成员信息与成员统计查询",
    "warnings": "警告记录查询",
    "keywords": "关键词与命中查询",
    "whitelist": "白名单 / 免检名单",
    "channels": "必关频道设置",
    "logs": "管理日志查询",
    "commands": "常用命令面板",
    "join": "基础进群验证",
    "ai": "AI 检测、管理助理与闲聊",
    "ai_providers": "自定义 AI 接口配置",
    "ai_quota": "AI 与样本每日额度",
    "linkage": "跨群封禁联动",
    "global_blacklist": "全局黑名单",
    "reports": "举报与通报",
    "verify_advanced": "进群验证高级选项",
    "points": "积分体系、商城与红包",
    "welcome": "新人欢迎语",
    "games": "抽奖 / 竞拍",
    "admins": "协助管理员",
    "stats_global": "全量数据统计",
    "multi_group": "多群绑定",
}

ROLE_LABELS: dict[str, str] = {
    "superadmin": "超级管理员",
    "subscriber": "订阅用户",
    "user": "普通用户",
}

TAB_FEATURES: dict[str, str] = {
    "settings": "settings",
    "moderation": "moderation",
    "ai": "ai",
    "join": "join",
    "admins": "admins",
    "members": "members",
    "warnings": "warnings",
    "keywords": "keywords",
    "whitelist": "whitelist",
    "channels": "channels",
    "links": "linkage",
    "games": "games",
    "logs": "logs",
    "reports": "reports",
}

# Fields that belong to a feature even though they are submitted by a broader
# form scope.  This prevents a basic user from changing advanced settings by
# posting the general/join form directly.
FIELD_FEATURES: dict[str, str] = {
    "welcome_enabled": "welcome",
    "welcome_text": "welcome",
    "report_enabled": "reports",
    "report_receive": "reports",
    "report_chat_id": "reports",
    "crossban_enabled": "linkage",
    "crossban_mode": "linkage",
    "points_enabled": "points",
    "points_name": "points",
    "points_per_message": "points",
    "points_cooldown": "points",
    "daily_bonus": "points",
    "require_username": "verify_advanced",
    "invite_redemption": "verify_advanced",
    "bio_block_keywords": "verify_advanced",
    "ai_ad_enabled": "ai",
    "ai_assistant_enabled": "ai",
    "ai_chat_enabled": "ai",
    "ai_daily_quota": "ai_quota",
    "sample_daily_quota": "ai_quota",
}


@dataclass(frozen=True)
class Entitlements:
    """Effective permissions for one console user."""

    user: ConsoleUser | None = None
    features: frozenset[str] = field(default_factory=frozenset)
    advanced: bool = False
    plan: SubscriptionPlan | None = None
    subscription: Subscription | None = None
    group_limit: int | None = 1
    ai_daily_quota: int | None = 0
    sample_daily_quota: int | None = 0

    @property
    def is_authenticated(self) -> bool:
        return self.user is not None

    @property
    def is_superadmin(self) -> bool:
        return bool(self.user and self.user.role == "superadmin")

    @property
    def is_subscriber(self) -> bool:
        return bool(self.user and self.user.role == "subscriber")

    @property
    def role_label(self) -> str:
        if self.user is None:
            return "未登录"
        return ROLE_LABELS.get(self.user.role, self.user.role or "普通用户")

    @property
    def display_name(self) -> str:
        if self.user is None:
            return ""
        return self.user.display_name or self.user.username or f"用户 {self.user.telegram_id or self.user.id}"

    def can(self, feature: str) -> bool:
        return feature in self.features

    def require(self, feature: str) -> bool:
        return self.can(feature)

    def missing_features(self, features: Iterable[str]) -> list[str]:
        return [item for item in features if item not in self.features]


EMPTY_ENTITLEMENTS = Entitlements(features=frozenset(), group_limit=0)


@dataclass(frozen=True, slots=True)
class GroupAccess:
    owner_id: int | None
    entitlements: Entitlements | None

    def can(self, feature: str) -> bool:
        return self.entitlements.can(feature) if self.entitlements is not None else True


UNBOUND_GROUP_ACCESS: Final = GroupAccess(owner_id=None, entitlements=None)


class GroupAccessCache:
    def __init__(self, session: AsyncSession, group_id: int) -> None:
        self._session = session
        self._group_id = int(group_id)
        self._access: GroupAccess | None = None

    async def resolve(self) -> GroupAccess:
        if self._access is None:
            self._access = await resolve_group_access(self._session, self._group_id)
        return self._access

    async def can(self, feature: str) -> bool:
        access = await self.resolve()
        return access.can(feature)


async def active_subscription(
    session: AsyncSession, user_id: int
) -> Subscription | None:
    now = utcnow()
    return await session.scalar(
        select(Subscription)
        .where(
            Subscription.user_id == int(user_id),
            Subscription.status == "active",
            Subscription.starts_at <= now,
            or_(Subscription.expires_at.is_(None), Subscription.expires_at > now),
        )
        .order_by(Subscription.id.desc())
        .limit(1)
    )


async def build_for_user(
    session: AsyncSession, user: ConsoleUser | None
) -> Entitlements:
    if user is None or not user.is_active:
        return EMPTY_ENTITLEMENTS

    if user.role == "superadmin":
        return Entitlements(
            user=user,
            features=ALL_FEATURES,
            advanced=True,
            group_limit=None,  # unlimited
            ai_daily_quota=None,
            sample_daily_quota=None,
        )

    now = utcnow()
    subscription, plan = (
        await session.execute(
            select(Subscription, SubscriptionPlan)
            .outerjoin(SubscriptionPlan, SubscriptionPlan.id == Subscription.plan_id)
            .where(
                Subscription.user_id == int(user.id),
                Subscription.status == "active",
                Subscription.starts_at <= now,
                or_(Subscription.expires_at.is_(None), Subscription.expires_at > now),
            )
            .order_by(Subscription.id.desc())
            .limit(1)
        )
    ).first() or (None, None)

    advanced = user.role == "subscriber" or subscription is not None
    if not advanced:
        return Entitlements(
            user=user,
            features=BASIC_FEATURES,
            advanced=False,
            group_limit=2,
            ai_daily_quota=0,
            sample_daily_quota=0,
        )

    group_limit = 5
    ai_quota = int(plan.ai_daily_quota) if plan and plan.ai_daily_quota else 200
    sample_quota = int(plan.sample_daily_quota) if plan and plan.sample_daily_quota else 500
    return Entitlements(
        user=user,
        features=ALL_FEATURES,
        advanced=True,
        plan=plan,
        subscription=subscription,
        group_limit=max(1, group_limit),
        ai_daily_quota=max(0, ai_quota),
        sample_daily_quota=max(0, sample_quota),
    )


async def resolve_group_access(
    session: AsyncSession, group_id: int
) -> GroupAccess:
    row = (
        await session.execute(
            select(ConsoleGroupAccess.user_id, ConsoleUser)
            .outerjoin(ConsoleUser, ConsoleUser.id == ConsoleGroupAccess.user_id)
            .where(ConsoleGroupAccess.group_id == int(group_id))
            .order_by(
                (ConsoleGroupAccess.role == "owner").desc(),
                ConsoleGroupAccess.id.asc(),
            )
            .limit(1)
        )
    ).first()
    if row is None:
        return UNBOUND_GROUP_ACCESS
    owner_id, owner = row
    entitlements = (
        await build_for_user(session, owner)
        if owner is not None and owner.is_active
        else EMPTY_ENTITLEMENTS
    )
    return GroupAccess(owner_id=int(owner_id), entitlements=entitlements)


async def group_owner_user_id(
    session: AsyncSession,
    group_id: int,
    *,
    access: GroupAccess | None = None,
) -> int | None:
    """Return the console user that owns/bound a Telegram group, if any."""
    resolved = access or await resolve_group_access(session, group_id)
    return resolved.owner_id


async def group_owner(
    session: AsyncSession,
    group_id: int,
    *,
    access: GroupAccess | None = None,
) -> ConsoleUser | None:
    owner_id = await group_owner_user_id(session, group_id, access=access)
    if owner_id is None:
        return None
    user = await session.get(ConsoleUser, owner_id)
    if user is None or not user.is_active:
        return None
    return user


async def group_feature_allowed(
    session: AsyncSession,
    group_id: int,
    feature: str,
    *,
    access: GroupAccess | None = None,
) -> bool:
    """Check a feature for the group's owning console user.

    Groups that have no console binding are platform-managed and keep working,
    which preserves behaviour for groups added directly by the bot owner.
    """
    resolved = access or await resolve_group_access(session, group_id)
    return resolved.can(feature)


async def user_can_for_group(
    session: AsyncSession, user: ConsoleUser | None, group_id: int, feature: str
) -> bool:
    """Check a feature for a specific console user acting on a group."""
    entitlements = await build_for_user(session, user)
    if not entitlements.can(feature):
        return False
    if entitlements.is_superadmin:
        return True
    access = await session.scalar(
        select(ConsoleGroupAccess.id).where(
            ConsoleGroupAccess.user_id == user.id,
            ConsoleGroupAccess.group_id == int(group_id),
        )
    )
    return access is not None
