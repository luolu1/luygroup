"""OpenAI-compatible AI client with priority/fallback providers."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_config
from ..models import AIProvider
from .ai_learning import learning_store

log = logging.getLogger(__name__)

MODERATION_SYSTEM = """你是 Telegram 群组的内容安全审核员。判断用户消息是否为广告、引流或违规推广。
只输出一个 JSON 对象，不要输出其它内容，格式：
{"is_ad": true/false, "category": "广告类型或none", "reason": "简短中文理由", "confidence": 0.0-1.0}
判定为广告的情况包括：商品/服务推广、拉人加微信/QQ/群/频道、博彩、色情、诈骗、刷单兼职、币圈空投、贷款发票等黑产、恶意引流链接。
正常聊天、提问、技术讨论、新闻分享、官方链接、熟人玩笑不要判为广告。拿不准时 is_ad 输出 false。"""

ASSISTANT_SYSTEM = """你是群组里的 AI 管理助理，名字叫「群卫」。你的回答要简洁、友好、务实，优先使用中文。
你可以帮助管理员解释群规、整理信息、回答技术问题、起草公告，也可以陪群友正常聊天。
不要编造群内规定；不确定时请说明不知道。不要输出任何敏感个人信息。"""

CHAT_SYSTEM = """你是 Telegram 群里的一位友好、幽默但不油腻的聊天伙伴。回复要短（通常 1-3 句），
使用中文，可以适当使用表情，但不要刷屏、不要发广告、不要讨论政治敏感话题。"""

# Preconfigured supplier templates. The panel uses these to fill the base URL
# and to offer common model names before fetching the full model list from the
# supplier's OpenAI-compatible /models endpoint.
PROVIDER_PRESETS: list[dict[str, Any]] = [
    {
        "id": "openai",
        "label": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "models": ["gpt-4o-mini", "gpt-4o", "gpt-4.1-mini", "gpt-4.1"],
        "hint": "官方 OpenAI，需要海外网络与 API Key。",
    },
    {
        "id": "deepseek",
        "label": "DeepSeek",
        "base_url": "https://api.deepseek.com/v1",
        "models": ["deepseek-chat", "deepseek-reasoner"],
        "hint": "中文能力强，价格较低，适合广告检测与助理。",
    },
    {
        "id": "openrouter",
        "label": "OpenRouter",
        "base_url": "https://openrouter.ai/api/v1",
        "models": [],
        "hint": "聚合多家模型，可在同一 Key 下切换供应商模型。",
    },
    {
        "id": "siliconflow",
        "label": "硅基流动 SiliconFlow",
        "base_url": "https://api.siliconflow.cn/v1",
        "models": [],
        "hint": "国内可直接访问，聚合多种开源模型。",
    },
    {
        "id": "moonshot",
        "label": "Moonshot / Kimi",
        "base_url": "https://api.moonshot.cn/v1",
        "models": ["moonshot-v1-8k", "moonshot-v1-32k", "moonshot-v1-128k"],
        "hint": "长上下文，中文对话表现稳定。",
    },
    {
        "id": "zhipu",
        "label": "智谱 GLM",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "models": ["glm-4-flash", "glm-4-plus", "glm-4-air"],
        "hint": "智谱开放平台，兼容 OpenAI 格式。",
    },
    {
        "id": "groq",
        "label": "Groq",
        "base_url": "https://api.groq.com/openai/v1",
        "models": [],
        "hint": "推理速度快，适合低延迟广告检测。",
    },
    {
        "id": "together",
        "label": "Together AI",
        "base_url": "https://api.together.xyz/v1",
        "models": [],
        "hint": "开源模型托管，模型列表可在面板中拉取。",
    },
    {
        "id": "mistral",
        "label": "Mistral AI",
        "base_url": "https://api.mistral.ai/v1",
        "models": ["mistral-small-latest", "mistral-large-latest"],
        "hint": "欧洲供应商，兼容 OpenAI 接口。",
    },
    {
        "id": "ollama",
        "label": "本地 Ollama",
        "base_url": "http://127.0.0.1:11434/v1",
        "models": [],
        "hint": "本机模型不消耗 API 费用，适合内网部署。",
    },
    {
        "id": "custom",
        "label": "自定义 / 其他兼容接口",
        "base_url": "",
        "models": [],
        "hint": "任何兼容 OpenAI /chat/completions 与 /models 的服务。",
    },
]


@dataclass
class AIResult:
    ok: bool
    text: str = ""
    provider: str = ""
    latency_ms: int = 0
    error: str = ""
    attempts: int = 0
    cached: bool = False


def normalize_base_url(base_url: str) -> str:
    """Accept a bare host and turn it into a usable HTTP(S) base URL.

    Users often paste ``api.example.com/v1`` without a scheme. Previously that
    reached httpx unchanged and failed with ``UnsupportedProtocol``. Keep the
    UI forgiving while still rejecting non-HTTP protocols later.
    """
    base = (base_url or "").strip()
    if not base:
        return ""
    if base.startswith("//"):
        base = "https:" + base
    if "://" not in base:
        host = (urlparse("//" + base).hostname or "").lower()
        private = (
            host in {"localhost", "127.0.0.1", "0.0.0.0", "::1"}
            or host.startswith("192.168.")
            or host.startswith("10.")
            or any(host.startswith(f"172.{index}.") for index in range(16, 32))
        )
        base = ("http://" if private else "https://") + base
    return base.rstrip("/")


def validated_base_url(base_url: str) -> str:
    """Return a normalized http(s) URL or raise a Chinese ValueError."""
    base = normalize_base_url(base_url)
    if not base:
        raise ValueError("请先填写接口地址")
    parsed = urlparse(base)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("接口地址格式不正确，请以 http:// 或 https:// 开头")
    return base


def _endpoint(base_url: str) -> str:
    base = normalize_base_url(base_url)
    if not base:
        return ""
    if base.endswith("/chat/completions"):
        return base
    return base + "/chat/completions"


def _models_endpoint(base_url: str) -> str:
    base = normalize_base_url(base_url)
    if not base:
        return ""
    if base.endswith("/chat/completions"):
        base = base[: -len("/chat/completions")]
    if base.endswith("/models"):
        return base
    return base + "/models"


def provider_preset(base_url: str) -> dict[str, Any] | None:
    """Return the preset matching ``base_url`` (ignoring normalisation)."""
    base = normalize_base_url(base_url)
    for preset in PROVIDER_PRESETS:
        if preset["base_url"] and normalize_base_url(preset["base_url"]) == base:
            return preset
    return None


def provider_preset_label(base_url: str) -> str:
    base = normalize_base_url(base_url)
    preset = provider_preset(base)
    if preset is not None:
        return str(preset["label"])
    if not base:
        return "未配置"
    host = urlparse(base).hostname or base
    return f"自定义 · {host}"


async def fetch_models(
    base_url: str,
    api_key: str = "",
    *,
    timeout: float | None = None,
) -> list[str]:
    """Fetch the supplier's model list through the OpenAI-compatible /models API."""
    endpoint = _models_endpoint(validated_base_url(base_url))
    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    request_timeout = timeout or get_config().ai.timeout
    last_exc: Exception | None = None
    response: httpx.Response | None = None
    for attempt in range(3):
        try:
            async with httpx.AsyncClient(timeout=request_timeout) as client:
                response = await client.get(endpoint, headers=headers)
            break
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout) as exc:
            last_exc = exc
            if attempt < 2:
                await asyncio.sleep(0.6 * (attempt + 1))
                continue
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            last_exc = exc
            break
    if response is None:
        exc = last_exc or RuntimeError("未知网络错误")
        host = urlparse(endpoint).hostname or endpoint
        raw = f"{type(exc).__name__}: {exc}"
        if any(
            marker in str(exc)
            for marker in (
                "Name or service not known",
                "nodename nor servname",
                "Temporary failure in name resolution",
            )
        ):
            message = (
                f"无法解析供应商域名 {host}（{endpoint}）。"
                "服务器已自动重试 3 次，仍无法完成域名解析。"
                "请稍后再试，或检查接口地址中的域名是否拼写正确。"
                f"原始错误：{raw}"
            )
        else:
            message = f"无法连接供应商 {endpoint}：{raw}"
        log.warning("AI 模型列表获取失败：%s", message)
        raise ValueError(message) from exc
    if response.status_code >= 400:
        raise ValueError(f"供应商返回 HTTP {response.status_code}：{response.text[:240]}")
    try:
        payload = response.json()
    except ValueError as exc:
        raise ValueError("供应商返回了无效 JSON") from exc
    raw = payload.get("data") if isinstance(payload, dict) else None
    if raw is None and isinstance(payload, dict):
        raw = payload.get("models")
    if not isinstance(raw, list):
        raise ValueError("供应商没有返回模型列表")
    models: set[str] = set()
    for item in raw:
        if isinstance(item, str):
            value = item
        elif isinstance(item, dict):
            value = item.get("id") or item.get("name") or item.get("model") or ""
        else:
            value = ""
        value = str(value).strip()
        if value:
            models.add(value)
    if not models:
        raise ValueError("供应商返回的模型列表为空")
    # Hide obvious non-chat models while keeping the list usable.
    blocked = ("embedding", "whisper", "tts", "dall-e", "moderation", "rerank")
    cleaned = sorted(
        model for model in models if not any(token in model.lower() for token in blocked)
    )
    return cleaned[:500] or sorted(models)[:500]


async def enabled_providers(
    session: AsyncSession, owner_user_id: int | None = None
) -> list[AIProvider]:
    """Return providers available to a console user.

    Built-in/platform providers (``owner_user_id IS NULL``) are shared.  A
    user's own providers are only added when their id is supplied, which keeps
    API keys and custom endpoints isolated between console users.
    """
    condition = AIProvider.owner_user_id.is_(None)
    if owner_user_id is not None:
        condition = or_(condition, AIProvider.owner_user_id == int(owner_user_id))
    result = await session.scalars(
        select(AIProvider)
        .where(AIProvider.enabled.is_(True), condition)
        .order_by(AIProvider.priority.asc(), AIProvider.id.asc())
    )
    return [p for p in result.all() if p.base_url and p.model]


async def chat(
    session: AsyncSession,
    messages: list[dict[str, str]],
    *,
    temperature: float = 0.3,
    max_tokens: int = 600,
    json_mode: bool = False,
    owner_user_id: int | None = None,
) -> AIResult:
    providers = await enabled_providers(session, owner_user_id)
    if not providers:
        return AIResult(ok=False, error="没有启用可用的 AI 接口")

    timeout = get_config().ai.timeout
    last_error = ""
    attempts = 0

    for provider in providers:
        attempts += 1
        endpoint = _endpoint(provider.base_url)
        headers = {"Content-Type": "application/json"}
        if provider.api_key:
            headers["Authorization"] = f"Bearer {provider.api_key}"
        payload: dict[str, Any] = {
            "model": provider.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        started = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(endpoint, headers=headers, json=payload)
            latency = int((time.monotonic() - started) * 1000)
            if response.status_code >= 400:
                last_error = f"{provider.name}: HTTP {response.status_code} {response.text[:200]}"
                continue
            data = response.json()
            text = (
                data.get("choices", [{}])[0]
                .get("message", {})
                .get("content", "")
            )
            if isinstance(text, list):  # some providers return content parts
                text = "".join(part.get("text", "") for part in text if isinstance(part, dict))
            if not text:
                last_error = f"{provider.name}: 空响应"
                continue
            return AIResult(
                ok=True,
                text=text.strip(),
                provider=provider.name,
                latency_ms=latency,
                attempts=attempts,
            )
        except Exception as exc:  # network/JSON errors fall through to backup
            last_error = f"{provider.name}: {type(exc).__name__}: {exc}"
            continue

    return AIResult(ok=False, error=last_error or "AI 调用失败", attempts=attempts)


def _strip_fences(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I)
    text = re.sub(r"\s*```$", "", text)
    return text.strip()


def parse_moderation(text: str) -> tuple[bool, str, float]:
    cleaned = _strip_fences(text)
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start >= 0 and end > start:
        cleaned = cleaned[start : end + 1]
    try:
        data = json.loads(cleaned)
    except (TypeError, ValueError):
        lowered = cleaned.lower()
        is_ad = '"is_ad": true' in lowered or "'is_ad': true" in lowered
        return is_ad, cleaned[:200], 0.5
    is_ad = bool(data.get("is_ad") or data.get("ad") or data.get("is_advertisement"))
    reason = str(data.get("reason") or data.get("category") or "")
    try:
        confidence = float(data.get("confidence", 0.5))
    except (TypeError, ValueError):
        confidence = 0.5
    return is_ad, reason[:300], max(0.0, min(1.0, confidence))


async def check_ad(
    session: AsyncSession,
    text: str,
    group_title: str = "",
    group_id: int = 0,
    *,
    owner_user_id: int | None = None,
) -> tuple[bool, str, AIResult]:
    cached = learning_store.get("ad", group_id, text)
    if cached is not None:
        is_ad = bool(cached.get("is_ad"))
        reason = str(cached.get("reason") or "命中本地 AI 学习数据")
        return is_ad, reason, AIResult(
            ok=True,
            text=reason,
            provider="learning-cache",
            cached=True,
        )

    messages = [
        {"role": "system", "content": MODERATION_SYSTEM},
        {
            "role": "user",
            "content": f"群组：{group_title or '未命名'}\n待审核消息：\n{text[:3000]}",
        },
    ]
    result = await chat(
        session,
        messages,
        temperature=0.0,
        max_tokens=220,
        json_mode=True,
        owner_user_id=owner_user_id,
    )
    if not result.ok:
        # Some gateways reject response_format. Retry once without it.
        result = await chat(
            session,
            messages,
            temperature=0.0,
            max_tokens=220,
            owner_user_id=owner_user_id,
        )
    if not result.ok:
        return False, "", result
    is_ad, reason, _confidence = parse_moderation(result.text)
    await learning_store.put(
        "ad",
        group_id,
        text,
        reason or ("广告" if is_ad else "正常消息"),
        meta={
            "is_ad": is_ad,
            "reason": reason,
            "provider": result.provider,
        },
    )
    return is_ad, reason, result


async def assistant_reply(
    session: AsyncSession,
    question: str,
    *,
    group_title: str = "",
    rules: str = "",
    history: list[dict[str, str]] | None = None,
    group_id: int = 0,
    owner_user_id: int | None = None,
) -> AIResult:
    context_key = hashlib.sha1(
        f"{group_title}\n{rules[:1500]}".encode("utf-8")
    ).hexdigest()[:16]
    cached = learning_store.get(
        "assistant", group_id, question, extra=context_key
    ) if len(question.strip()) >= 4 else None
    if cached is not None and cached.get("answer"):
        return AIResult(
            ok=True,
            text=str(cached["answer"]),
            provider="learning-cache",
            cached=True,
        )

    context = f"当前群组：{group_title or '未命名'}\n"
    if rules:
        context += f"群规：{rules[:1500]}\n"
    messages: list[dict[str, str]] = [{"role": "system", "content": ASSISTANT_SYSTEM + "\n" + context}]
    for item in (history or [])[-get_config().ai.max_context_messages :]:
        role = item.get("role")
        content = item.get("content", "")
        if role in {"user", "assistant"} and content:
            messages.append({"role": role, "content": content[:1500]})
    messages.append({"role": "user", "content": question[:3000]})
    result = await chat(
        session,
        messages,
        temperature=0.5,
        max_tokens=700,
        owner_user_id=owner_user_id,
    )
    if result.ok:
        await learning_store.put(
            "assistant",
            group_id,
            question,
            result.text,
            extra=context_key,
            meta={"provider": result.provider},
        )
    return result


async def chat_reply(
    session: AsyncSession,
    text: str,
    *,
    group_title: str = "",
    history: list[dict[str, str]] | None = None,
    group_id: int = 0,
    owner_user_id: int | None = None,
) -> AIResult:
    cached = learning_store.get("chat", group_id, text) if len(text.strip()) >= 4 else None
    if cached is not None and cached.get("answer"):
        return AIResult(
            ok=True,
            text=str(cached["answer"]),
            provider="learning-cache",
            cached=True,
        )

    messages: list[dict[str, str]] = [
        {"role": "system", "content": CHAT_SYSTEM + f"\n当前群组：{group_title or '未命名'}"}
    ]
    for item in (history or [])[-get_config().ai.max_context_messages :]:
        role = item.get("role")
        content = item.get("content", "")
        if role in {"user", "assistant"} and content:
            messages.append({"role": role, "content": content[:1000]})
    messages.append({"role": "user", "content": text[:2000]})
    result = await chat(
        session,
        messages,
        temperature=0.7,
        max_tokens=300,
        owner_user_id=owner_user_id,
    )
    if result.ok:
        await learning_store.put(
            "chat",
            group_id,
            text,
            result.text,
            meta={"provider": result.provider},
        )
    return result


async def test_provider(provider: AIProvider) -> AIResult:
    """Test a provider without touching the DB."""
    endpoint = _endpoint(provider.base_url)
    if not endpoint or not provider.model:
        return AIResult(ok=False, error="请填写 Base URL 和模型名")
    headers = {"Content-Type": "application/json"}
    if provider.api_key:
        headers["Authorization"] = f"Bearer {provider.api_key}"
    payload = {
        "model": provider.model,
        "messages": [{"role": "user", "content": "回复 OK"}],
        "max_tokens": 10,
        "temperature": 0,
    }
    started = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=get_config().ai.timeout) as client:
            response = await client.post(endpoint, headers=headers, json=payload)
        latency = int((time.monotonic() - started) * 1000)
        if response.status_code >= 400:
            return AIResult(ok=False, error=f"HTTP {response.status_code}: {response.text[:300]}", latency_ms=latency)
        data = response.json()
        text = data.get("choices", [{}])[0].get("message", {}).get("content", "")
        return AIResult(ok=True, text=str(text)[:200], provider=provider.name, latency_ms=latency)
    except Exception as exc:
        return AIResult(ok=False, error=f"{type(exc).__name__}: {exc}")
