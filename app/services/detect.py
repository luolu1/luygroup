"""Local (non-AI) advertising and keyword detection."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from urllib.parse import urlparse

from ..models import utcnow

# Strong advertising signals. Keep this list conservative: false positives annoy
# members, while the AI layer can catch the subtle cases.
AD_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(?:https?://|www\.)\S+", re.I), "包含外链"),
    (re.compile(r"(?:t\.me|telegram\.me|telegram\.dog)/[A-Za-z0-9_+/\-]+", re.I), "包含 Telegram 链接"),
    (re.compile(r"(联系|私聊|私信|加|找|咨询|关注)\s*@[A-Za-z][A-Za-z0-9_]{4,31}"), "诱导联系用户名"),
    (re.compile(r"(?<!\d)(?:\+?86[-\s]?)?1[3-9]\d{9}(?!\d)"), "包含手机号"),
    (re.compile(r"(加|＋|\+)\s*(微信|vx|v信|薇信|qq|扣扣)", re.I), "诱导添加联系方式"),
    (re.compile(r"(私聊|私信|戳我|找我|联系我).{0,12}(优惠|价格|购买|下单|代理|合作)", re.I), "诱导私聊交易"),
    (re.compile(r"(招|找|收).{0,6}(代理|下线|兼职|刷单|水军)", re.I), "招募广告"),
    (re.compile(r"(日赚|日入|躺赚|稳赚|包赚|月入过万|零成本|高额回报)", re.I), "收益诱导广告"),
    (re.compile(r"(博彩|菠菜|下注|投注|开户|返水|返利|博彩平台)", re.I), "博彩广告"),
    (re.compile(r"(空投|撸羊毛|币圈|USDT|泰达币|虚拟币|炒币|挖矿)", re.I), "币圈广告"),
    (re.compile(r"(成人视频|裸聊|约炮|色情|黄片|楼凤)", re.I), "色情广告"),
    (re.compile(r"(代开发票|办证|洗钱|黑产|接码|卡商|跑分)", re.I), "黑产广告"),
    (re.compile(r"(翻墙|机场节点|VPN|科学上网).{0,10}(购买|订阅|联系|优惠)", re.I), "翻墙广告"),
    (re.compile(r"(频道|群组).{0,8}(推广|引流|涨粉|互推)", re.I), "频道推广"),
    # 中文商品/账号交易类广告。要求“交易动词/促销词 + 商品”同时出现，
    # 避免把普通的技术讨论误判成广告。
    (
        re.compile(
            r"(出|售|卖|收|求购|转|转让).{0,6}"
            r"(VPS|VDS|独服|服务器|节点|机场|订阅|账号|会员|流量|IP|鸡|小鸡|母鸡|云盘|网盘)",
            re.I,
        ),
        "商品/账号交易广告",
    ),
    (
        re.compile(
            r"(低价|特价|优惠|折扣|促销|白菜价|清仓|甩卖|补货|现货).{0,10}"
            r"(VPS|VDS|独服|服务器|节点|机场|订阅|账号|会员|套餐|流量|IP)",
            re.I,
        ),
        "低价促销广告",
    ),
    (
        re.compile(
            r"(合租|拼车|上车|发车|车队|拼团).{0,10}"
            r"(VPS|VDS|独服|服务器|节点|机场|订阅|套餐|流量)",
            re.I,
        ),
        "合租/拼车广告",
    ),
    (
        re.compile(
            r"(批发|代理|分销|返佣|招代理|招下线|招商).{0,10}"
            r"(VPS|VDS|独服|服务器|节点|机场|订阅|账号|会员|套餐)",
            re.I,
        ),
        "批发/代理广告",
    ),
    (
        re.compile(
            r"(会员|账号|订阅|激活码|兑换码|礼品卡|Netflix|Spotify|YouTube|"
            r"ChatGPT|Capcut|Office|Adobe|Disney).{0,10}"
            r"(?:\d+(?:\.\d+)?\s*(?:元|块|[$€£¥])|[$€£¥]\s*\d+)"
            r".{0,16}(?:需要|联系|私聊|私信|下单|购买|出售|卖|出|优惠)",
            re.I,
        ),
        "会员/账号售卖广告",
    ),
    # 英文批发、代发、自动下单类广告。常见于跨群搬运的“货源”广告。
    (
        re.compile(
            r"\b(?:wholesale|reseller|affiliate|dropshipping?)\b.{0,40}"
            r"\b(?:supply|supplier|available|order|price|stock)\b",
            re.I,
        ),
        "批发/代发广告",
    ),
    (
        re.compile(r"\bbulk\s*orders?\b", re.I),
        "批量订单广告",
    ),
    (
        re.compile(
            r"\b(?:dm|pm|inbox)\s+(?:to|me|for)\b.{0,30}"
            r"\b(?:price|order|buy|purchase|negotiat\w*|details?)\b",
            re.I,
        ),
        "引导私聊交易",
    ),
    (
        re.compile(
            r"\b(?:full|100%)\s+warranty\b.{0,20}"
            r"(?:[$€£¥]\s*\d+|\d+(?:\.\d+)?\s*[$€£¥]|\d+\s*(?:usd|usdt|cny|rmb|元))",
            re.I,
        ),
        "带价格质保广告",
    ),
    (
        re.compile(
            r"\b(?:auto\s*order|order\s*now|buy\s*now)\b.{0,30}"
            r"@[A-Za-z][A-Za-z0-9_]{4,31}",
            re.I,
        ),
        "自动下单/购买引导",
    ),
    (
        re.compile(
            r"\b(?:telegram|tg)\s*(?:channel|group|bot)\b.{0,20}"
            r"\b(?:join|subscribe|follow)\b",
            re.I,
        ),
        "频道/群组推广",
    ),
]

MAX_REPEAT = 3
REPEAT_WINDOW_SECONDS = 90


@dataclass
class Detection:
    hit: bool = False
    source: str = ""       # builtin / keyword / length / forward / repeat / flood / ai
    reason: str = ""
    match: str = ""
    action: str = "delete"  # delete / warn / mute / kick / ban / reply
    reply_text: str = ""    # action=reply 时发送的自动回复


class RecentTracker:
    """In-memory duplicate/flood tracker (per process)."""

    def __init__(self) -> None:
        self._messages: dict[tuple[int, int], list[tuple[str, datetime]]] = {}
        self._last_cleanup = utcnow()

    def add(self, chat_id: int, user_id: int, text: str) -> tuple[int, int]:
        now = utcnow()
        key = (chat_id, user_id)
        items = self._messages.setdefault(key, [])
        items.append((text, now))
        cutoff = now - timedelta(seconds=60)
        items[:] = [(t, ts) for t, ts in items if ts >= cutoff]
        window_cutoff = now - timedelta(seconds=REPEAT_WINDOW_SECONDS)
        same = sum(1 for t, ts in items if t == text and ts >= window_cutoff)
        total = len(items)
        # Keep memory bounded.
        if len(self._messages) > 5000:
            self._cleanup(now)
        return same, total

    def _cleanup(self, now: datetime) -> None:
        cutoff = now - timedelta(seconds=120)
        for key in list(self._messages):
            self._messages[key] = [(t, ts) for t, ts in self._messages[key] if ts >= cutoff]
            if not self._messages[key]:
                self._messages.pop(key, None)


recent = RecentTracker()


def extract_domains(text: str) -> set[str]:
    domains: set[str] = set()
    for match in re.finditer(r"(?:https?://|www\.)([^\s/]+)", text, re.I):
        host = match.group(1).lower().strip(".")
        if ":" in host:
            host = host.split(":", 1)[0]
        domains.add(host)
        if host.startswith("www."):
            domains.add(host[4:])
    return domains


def domain_allowed(host: str, allowed_domains: list[str]) -> bool:
    return any(host == d or host.endswith("." + d) for d in allowed_domains)


def check_keywords(
    text: str,
    keywords: list[tuple[str, bool, str]] | list[tuple[str, bool, str, str]],
) -> Detection:
    """Match configured keyword rules. Independent of anti-ad being enabled."""
    for item in keywords:
        pattern, is_regex, action = item[0], item[1], item[2]
        reply_text = item[3] if len(item) > 3 else ""
        try:
            if is_regex:
                matched = re.search(pattern, text, re.I)
            else:
                matched = pattern.lower() in text.lower()
        except re.error:
            continue
        if matched:
            if action == "reply" and not reply_text:
                action = "delete"
            return Detection(
                hit=True,
                source="keyword",
                reason=f"命中关键词：{pattern}",
                match=pattern,
                action=action or "delete",
                reply_text=reply_text,
            )
    return Detection()


def check_local(
    *,
    text: str,
    group_id: int,
    user_id: int,
    keywords: list[tuple[str, bool, str]] | list[tuple[str, bool, str, str]] | None = None,
    allowed_domains: list[str] | None = None,
    max_length: int = 0,
    forwarded_from_channel: bool = False,
    block_forwarded: bool = False,
) -> Detection:
    if block_forwarded and forwarded_from_channel:
        return Detection(hit=True, source="forward", reason="禁止转发频道消息", action="delete")

    if max_length and len(text) > max_length:
        return Detection(hit=True, source="length", reason=f"消息过长（>{max_length}）", action="warn")

    # Configured keywords take precedence because their action is explicit.
    keyword_hit = check_keywords(text, keywords or [])
    if keyword_hit.hit:
        return keyword_hit

    domains = extract_domains(text)
    allowed = allowed_domains or []

    for pattern, reason in AD_PATTERNS:
        matched = pattern.search(text)
        if not matched:
            continue
        value = matched.group(0)
        if value.startswith(("http://", "https://", "www.")) or "://" in value:
            host = ""
            try:
                host = (urlparse(value if "://" in value else "https://" + value).hostname or "").lower()
            except ValueError:
                host = ""
            if host and domain_allowed(host, allowed):
                continue
        if any(domain_allowed(domain, allowed) for domain in domains):
            # Allow messages whose *only* link is on the allow list.
            if pattern.pattern.startswith("(?:https?://|www\\.") and value and all(
                domain_allowed(d, allowed) for d in domains
            ):
                continue
        return Detection(
            hit=True,
            source="builtin",
            reason=reason,
            match=value[:120],
            action="warn",
        )

    same, total = recent.add(group_id, user_id, text)
    if same >= MAX_REPEAT:
        return Detection(
            hit=True,
            source="repeat",
            reason="短时间内重复发送相同内容",
            action="warn",
        )

    return Detection()


def check_flood(
    *,
    message_times: list[datetime],
    limit: int,
    window_seconds: int,
    action: str,
) -> Detection:
    cutoff = utcnow() - timedelta(seconds=window_seconds)
    count = sum(1 for ts in message_times if ts >= cutoff)
    if limit > 0 and count > limit:
        return Detection(
            hit=True,
            source="flood",
            reason=f"刷屏（{window_seconds} 秒内 {count} 条）",
            action=action or "mute",
        )
    return Detection()
