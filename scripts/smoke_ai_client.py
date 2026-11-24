from __future__ import annotations

import asyncio
import atexit
import json
import os
import sys
import tempfile
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx

ROOT = Path(__file__).resolve().parents[1]
CLEANUP = ExitStack()
_ = atexit.register(CLEANUP.close)
TMP = Path(CLEANUP.enter_context(tempfile.TemporaryDirectory(
    prefix="groupguard-ai-client-smoke-", dir="/tmp/opencode"
)))
TMP_CONFIG = TMP / "config.json"
config = {
    "bot": {"token": "123456789:AI_SMOKE_SYNTHETIC_TOKEN", "admin_ids": [891000002],
            "username": "ai_smoke_bot", "proxy": ""},
    "web": {"secret_key": "ai-client-smoke-secret", "base_url": "http://testserver"},
    "database": {"url": f"sqlite+aiosqlite:///{TMP / 'ai.db'}"},
    "ai": {"learning_dir": str(TMP / "learning"), "providers": []},
    "payments": {"enabled": False},
}
TMP_CONFIG.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
_ = CLEANUP.enter_context(patch.dict(os.environ, {"GROUPGUARD_CONFIG": str(TMP_CONFIG)}, clear=True))
sys.path.insert(0, str(ROOT))

from app.db import dispose_db, init_db, session_scope  # noqa: E402
from app.models import (
    AIProvider,
    AIUsage,
    ConsoleGroupAccess,
    ConsoleUser,
    Group,
    Subscription,
    SubscriptionPlan,
    utcnow,
)  # noqa: E402
from app.services import ai  # noqa: E402
from app.services import ai_http  # noqa: E402
from app.services import quota  # noqa: E402

from sqlalchemy import select  # noqa: E402


def use_http_client(factory) -> None:
    ai_http._clients.clear()
    ai.httpx.AsyncClient = factory


class FakeClient:
    def __init__(self, *, responses: list[object], **_kwargs) -> None:
        self.responses = responses
        self.posts: list[str] = []
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args) -> None:
        self.closed = True

    @property
    def is_closed(self) -> bool:
        return self.closed

    async def aclose(self) -> None:
        self.closed = True

    async def post(self, url, **_kwargs):
        self.posts.append(url)
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


async def run_tests() -> None:
    await init_db()
    async with session_scope() as session:
        session.add_all(
            [
                AIProvider(
                    name="first",
                    base_url="https://first.example/v1",
                    model="model-a",
                    enabled=True,
                    priority=1,
                ),
                AIProvider(
                    name="second",
                    base_url="https://second.example/v1",
                    model="model-b",
                    vision_model="vision-b",
                    enabled=True,
                    priority=2,
                ),
            ]
        )

    clients: list[FakeClient] = []
    response_error = SimpleNamespace(
        status_code=503,
        text="unavailable",
    )
    response_ok = SimpleNamespace(
        status_code=200,
        json=lambda: {"choices": [{"message": {"content": "ok"}}]},
    )

    queued_responses = [response_error, response_ok]

    def client_factory(**kwargs):
        client = FakeClient(responses=queued_responses, **kwargs)
        clients.append(client)
        return client

    original_client = ai.httpx.AsyncClient
    use_http_client(client_factory)
    try:
        async with session_scope() as session:
            result = await ai.chat(session, [{"role": "user", "content": "hello"}])
        open_after_first = not clients[0].closed
        queued_responses.append(response_ok)
        async with session_scope() as session:
            again = await ai.chat(session, [{"role": "user", "content": "again"}])
        await ai_http.close_client()
    finally:
        use_http_client(original_client)

    assert result.ok and result.provider == "second" and result.attempts == 2, result
    assert again.ok, again
    assert len(clients) == 1, len(clients)
    assert clients[0].posts == [
        "https://first.example/v1/chat/completions",
        "https://second.example/v1/chat/completions",
        "https://first.example/v1/chat/completions",
    ]
    assert open_after_first
    assert clients[0].closed
    print("PASS provider fallback and later calls reuse one pooled HTTP client")

    exhausted_clients: list[FakeClient] = []

    def exhausted_client_factory(**kwargs):
        client = FakeClient(responses=[response_error, response_error], **kwargs)
        exhausted_clients.append(client)
        return client

    use_http_client(exhausted_client_factory)
    try:
        async with session_scope() as session:
            failed = await ai.chat(session, [{"role": "user", "content": "hello"}])
    finally:
        use_http_client(original_client)
    assert not failed.ok and failed.attempts == 2
    assert len(exhausted_clients) == 1 and not exhausted_clients[0].closed
    print("PASS exhausted provider fallback keeps the pooled client open")

    vision_clients: list[FakeClient] = []

    def vision_client_factory(**kwargs):
        client = FakeClient(responses=[response_ok], **kwargs)
        vision_clients.append(client)
        return client

    use_http_client(vision_client_factory)
    try:
        async with session_scope() as session:
            image_result = await ai.check_ad(
                session,
                "图片广告",
                "测试群",
                -1001,
                image_data_url="data:image/jpeg;base64,AAAA",
            )
    finally:
        use_http_client(original_client)
    assert image_result[2].ok and image_result[2].provider == "second"
    assert vision_clients[0].posts == ["https://second.example/v1/chat/completions"]
    print("PASS vision check uses configured image model")

    original_get = ai.httpx.AsyncClient

    class FakeResponse:
        def __init__(self):
            self.status_code = 200
            self.text = ""

        def json(self):
            return {
                "data": [
                    {"id": "deepseek-chat"},
                    {"id": "qwen2.5-vl-32b-instruct"},
                    {"id": "gpt-4o-mini"},
                    {"id": "embedding-v1"},
                ]
            }

    class FakeGetClient:
        is_closed = False

        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

        async def get(self, *args, **kwargs):
            return FakeResponse()

    use_http_client(FakeGetClient)
    try:
        vision_models = await ai.fetch_models("https://api.deepseek.com/v1", kind="vision")
    finally:
        use_http_client(original_get)
    assert vision_models == ["gpt-4o-mini", "qwen2.5-vl-32b-instruct"], vision_models
    print("PASS vision model filtering returns image-capable models")

    truncated_response = SimpleNamespace(
        status_code=200,
        json=lambda: {"choices": [{"finish_reason": "length", "message": {"content": ""}}]},
    )
    truncation_client = FakeClient(responses=[truncated_response, response_ok])
    use_http_client(lambda **kwargs: truncation_client)
    try:
        async with session_scope() as session:
            rescued = await ai.chat(session, [{"role": "user", "content": "hello"}])
    finally:
        use_http_client(original_client)
    assert rescued.ok and rescued.attempts == 2, rescued
    assert len(truncation_client.posts) == 2, truncation_client.posts
    print("PASS truncated reasoning output is retried with a larger budget")

    exhausted_truncation = FakeClient(responses=[truncated_response] * 4)
    use_http_client(lambda **kwargs: exhausted_truncation)
    try:
        async with session_scope() as session:
            still_failed = await ai.chat(session, [{"role": "user", "content": "hello"}])
    finally:
        use_http_client(original_client)
    assert not still_failed.ok and still_failed.provider == "second", still_failed
    assert "长度限制截断" in still_failed.error, still_failed.error
    print("PASS exhausted truncation keeps the attempted provider name")

    async with session_scope() as session:
        second = await session.scalar(select(AIProvider).where(AIProvider.name == "second"))
        second_id = int(second.id)
        ordered = await ai.enabled_providers(session, None, provider_id=second_id)
    assert [p.name for p in ordered] == ["second"], [p.name for p in ordered]
    print("PASS a pinned provider is isolated from fallback providers")

    pinned_client = FakeClient(responses=[response_ok])
    use_http_client(lambda **kwargs: pinned_client)
    try:
        async with session_scope() as session:
            pinned = await ai.chat(
                session, [{"role": "user", "content": "hello"}], provider_id=second_id
            )
    finally:
        use_http_client(original_client)
    assert pinned.ok and pinned.provider == "second", pinned
    assert pinned_client.posts == ["https://second.example/v1/chat/completions"], (
        pinned_client.posts
    )
    print("PASS pinned provider is called first at request time")

    async with session_scope() as session:
        owner = ConsoleUser(
            telegram_id=990001,
            token_hash="quota-owner-token",
            role="subscriber",
            ai_text_daily_quota=2,
            ai_image_daily_quota=3,
        )
        session.add(owner)
        await session.flush()
        plan = SubscriptionPlan(
            name="quota-plan",
            label="Quota Plan",
            features='["ai"]',
            group_limit=5,
            ai_daily_quota=2,
            sample_daily_quota=3,
            enabled=True,
        )
        session.add(plan)
        await session.flush()
        session.add(
            Subscription(
                user_id=owner.id,
                plan_id=plan.id,
                status="active",
                starts_at=utcnow(),
            )
        )
        group_a = Group(id=-100990001, title="Quota A", chat_type="supergroup")
        group_b = Group(id=-100990002, title="Quota B", chat_type="supergroup")
        session.add_all([group_a, group_b])
        session.add_all(
            [
                ConsoleGroupAccess(user_id=owner.id, group_id=group_a.id, role="owner"),
                ConsoleGroupAccess(user_id=owner.id, group_id=group_b.id, role="owner"),
                AIUsage(
                    group_id=group_a.id,
                    day=utcnow().strftime("%Y-%m-%d"),
                    text_requests=2,
                ),
            ]
        )
        await session.flush()
        system = await session.scalar(select(AIProvider).where(AIProvider.owner_user_id.is_(None)))
        system = system or AIProvider(
            name="quota-system",
            base_url="https://quota-system.example/v1",
            model="text",
            vision_model="vision",
            enabled=True,
        )
        if system.id is None:
            session.add(system)
            await session.flush()
        group_b.ai_provider_id = system.id
        text_allowed = await quota.allow_request(
            session, group_b, provider_id=system.id, image=False
        )
        image_allowed = await quota.allow_request(
            session, group_b, provider_id=system.id, image=True
        )
        custom = AIProvider(
            name="quota-custom",
            owner_user_id=owner.id,
            base_url="https://quota-custom.example/v1",
            model="custom",
            enabled=True,
        )
        session.add(custom)
        await session.flush()
        custom_allowed = await quota.allow_request(
            session, group_b, provider_id=custom.id, image=False
        )
    assert not text_allowed and image_allowed and custom_allowed
    print("PASS user-wide text quota, separate image quota, custom provider exemption")

    timeout_error = httpx.ReadTimeout("upstream timeout")
    retry_client = FakeClient(responses=[timeout_error] * 4 + [response_ok])
    original_fetch = ai.fetch_models

    async def fake_models(*_args, **_kwargs):
        return ["model-a", "model-alt"]

    use_http_client(lambda **kwargs: retry_client)
    ai.fetch_models = fake_models
    try:
        async with session_scope() as session:
            retried = await ai.chat(session, [{"role": "user", "content": "hello"}])
    finally:
        use_http_client(original_client)
        ai.fetch_models = original_fetch
    assert retried.ok and retried.model == "model-alt", retried
    assert len(retry_client.posts) == 5, retry_client.posts
    print("PASS timeout retries a model three times then switches models")

async def test_responses_mode() -> None:
    responses_payload = {
        "id": "resp_test",
        "object": "response",
        "status": "completed",
        "output": [
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "你好，我是群卫"}],
            }
        ],
    }
    response_ok = SimpleNamespace(status_code=200, json=lambda: responses_payload)

    async with session_scope() as session:
        provider = AIProvider(
            name="responses-provider",
            base_url="https://responses.example/v1",
            model="gpt-5",
            api_mode="responses",
            vision_model="gpt-5-vision",
            vision_api_mode="chat",
            enabled=True,
            priority=0,
        )
        session.add(provider)
        await session.flush()
        provider_id = int(provider.id)

    client = FakeClient(responses=[response_ok])
    captured: list[dict] = []
    original_post = client.post

    async def capturing_post(url, **kwargs):
        captured.append({"url": url, "json": kwargs.get("json")})
        return await original_post(url, **kwargs)

    client.post = capturing_post
    original_client = ai.httpx.AsyncClient
    use_http_client(lambda **kwargs: client)
    try:
        async with session_scope() as session:
            result = await ai.chat(
                session,
                [
                    {"role": "system", "content": "系统提示"},
                    {"role": "user", "content": "你好"},
                ],
                provider_id=provider_id,
            )
    finally:
        use_http_client(original_client)

    assert result.ok and result.text == "你好，我是群卫", result
    assert captured[0]["url"] == "https://responses.example/v1/responses", captured[0]["url"]
    sent = captured[0]["json"]
    assert sent["instructions"] == "系统提示", sent
    assert sent["input"] == [{"role": "user", "content": "你好"}], sent
    assert sent["max_output_tokens"] == 600, sent
    assert "messages" not in sent, sent
    print("PASS responses mode maps system prompt to instructions and reads output_text")

    vision_payload = {
        "status": "completed",
        "output": [
            {
                "type": "message",
                "content": [{"type": "output_text", "text": "这是一张广告图"}],
            }
        ],
    }
    vision_response = SimpleNamespace(status_code=200, json=lambda: vision_payload)
    vision_client = FakeClient(responses=[vision_response])
    vision_captured: list[dict] = []
    original_vision_post = vision_client.post

    async def capturing_vision_post(url, **kwargs):
        vision_captured.append({"url": url, "json": kwargs.get("json")})
        return await original_vision_post(url, **kwargs)

    vision_client.post = capturing_vision_post
    async with session_scope() as session:
        provider = await session.get(AIProvider, provider_id)
        provider.vision_model = "gpt-5-vision"
        provider.vision_api_mode = "responses"
    use_http_client(lambda **kwargs: vision_client)
    try:
        async with session_scope() as session:
            _is_ad, _reason, vision_result = await ai.check_ad(
                session,
                "图片广告",
                "测试群",
                -100991001,
                image_data_url="data:image/jpeg;base64,AAAA",
                provider_id=provider_id,
            )
    finally:
        use_http_client(original_client)

    assert vision_result.ok, vision_result
    sent = vision_captured[0]["json"]
    content = sent["input"][0]["content"]
    assert isinstance(content, list), content
    assert content[0]["type"] == "input_text", content
    assert any(part["type"] == "input_image" for part in content), content
    assert "data:image/jpeg;base64,AAAA" in str(content), content
    assert "messages" not in sent, sent
    print("PASS responses mode converts image parts to input_image")

    test_client = FakeClient(responses=[response_ok])
    async with session_scope() as session:
        provider = await session.get(AIProvider, provider_id)
    use_http_client(lambda **kwargs: test_client)
    try:
        test_result = await ai.test_provider(provider)
    finally:
        use_http_client(original_client)
    assert test_result.ok, test_result
    assert test_client.posts == ["https://responses.example/v1/responses"], test_client.posts
    print("PASS provider test uses the responses endpoint when api_mode is responses")

    async with session_scope() as session:
        provider = await session.get(AIProvider, provider_id)
        provider.api_mode = "chat"
    chat_client = FakeClient(
        responses=[SimpleNamespace(status_code=200, json=lambda: {"choices": [{"message": {"content": "ok"}}]})]
    )
    use_http_client(lambda **kwargs: chat_client)
    try:
        async with session_scope() as session:
            provider = await session.get(AIProvider, provider_id)
            chat_result = await ai.chat(
                session, [{"role": "user", "content": "hi"}], provider_id=provider_id
            )
    finally:
        use_http_client(original_client)
    assert chat_result.ok, chat_result
    assert chat_client.posts == ["https://responses.example/v1/chat/completions"], chat_client.posts
    print("PASS switching back to chat mode uses /chat/completions again")


async def test_mixed_api_modes_per_model() -> None:
    async with session_scope() as session:
        provider = AIProvider(
            name="mixed-provider",
            base_url="https://mixed.example/v1",
            model="text-model",
            api_mode="responses",
            vision_model="vision-model",
            vision_api_mode="chat",
            enabled=True,
            priority=0,
        )
        session.add(provider)
        await session.flush()
        provider_id = int(provider.id)

    responses_payload = {
        "status": "completed",
        "output": [
            {"type": "message", "content": [{"type": "output_text", "text": "文本应答"}]}
        ],
    }
    chat_payload = {
        "choices": [{"message": {"content": "图像判定正常"}}],
    }
    client = FakeClient(
        responses=[
            SimpleNamespace(status_code=200, json=lambda: responses_payload),
            SimpleNamespace(status_code=200, json=lambda: chat_payload),
        ]
    )
    captured: list[dict] = []
    original_post = client.post

    async def capturing_post(url, **kwargs):
        captured.append({"url": url, "json": kwargs.get("json")})
        return await original_post(url, **kwargs)

    client.post = capturing_post
    original_client = ai.httpx.AsyncClient
    use_http_client(lambda **kwargs: client)
    try:
        async with session_scope() as session:
            text_result = await ai.chat(
                session, [{"role": "user", "content": "hi"}], provider_id=provider_id
            )
        async with session_scope() as session:
            await ai.check_ad(
                session,
                "图片广告",
                "测试群",
                -100991002,
                image_data_url="data:image/jpeg;base64,BBBB",
                provider_id=provider_id,
            )
    finally:
        use_http_client(original_client)

    assert text_result.ok and text_result.model == "text-model", text_result
    assert captured[0]["url"] == "https://mixed.example/v1/responses", captured[0]["url"]
    assert "messages" not in captured[0]["json"], captured[0]["json"]
    assert captured[1]["url"] == "https://mixed.example/v1/chat/completions", captured[1]["url"]
    assert "messages" in captured[1]["json"], captured[1]["json"]
    assert captured[1]["json"]["model"] == "vision-model", captured[1]["json"]
    print("PASS text and image models can use different api modes on one provider")

    async with session_scope() as session:
        provider = await session.get(AIProvider, provider_id)
        provider.api_mode = "chat"
        provider.vision_api_mode = "responses"
    reversed_client = FakeClient(
        responses=[
            SimpleNamespace(status_code=200, json=lambda: chat_payload),
            SimpleNamespace(status_code=200, json=lambda: responses_payload),
        ]
    )
    reversed_captured: list[dict] = []
    original_reversed_post = reversed_client.post

    async def capturing_reversed_post(url, **kwargs):
        reversed_captured.append({"url": url, "json": kwargs.get("json")})
        return await original_reversed_post(url, **kwargs)

    reversed_client.post = capturing_reversed_post
    use_http_client(lambda **kwargs: reversed_client)
    try:
        async with session_scope() as session:
            await ai.chat(session, [{"role": "user", "content": "hi"}], provider_id=provider_id)
        async with session_scope() as session:
            await ai.check_ad(
                session,
                "图片广告",
                "测试群",
                -100991003,
                image_data_url="data:image/jpeg;base64,CCCC",
                provider_id=provider_id,
            )
    finally:
        use_http_client(original_client)

    assert reversed_captured[0]["url"] == "https://mixed.example/v1/chat/completions"
    assert reversed_captured[1]["url"] == "https://mixed.example/v1/responses"
    assert "input" in reversed_captured[1]["json"], reversed_captured[1]["json"]
    print("PASS mixed modes invert independently for text and image models")

    async with session_scope() as session:
        provider = await session.get(AIProvider, provider_id)
    text_probe = FakeClient(
        responses=[SimpleNamespace(status_code=200, json=lambda: chat_payload)]
    )
    use_http_client(lambda **kwargs: text_probe)
    try:
        text_test = await ai.test_provider(provider)
    finally:
        use_http_client(original_client)
    assert text_test.ok and text_test.model == "text-model", text_test
    assert text_probe.posts == ["https://mixed.example/v1/chat/completions"], text_probe.posts

    vision_probe = FakeClient(
        responses=[SimpleNamespace(status_code=200, json=lambda: responses_payload)]
    )
    use_http_client(lambda **kwargs: vision_probe)
    try:
        vision_test = await ai.test_provider(provider, vision=True)
    finally:
        use_http_client(original_client)
    assert vision_test.ok and vision_test.model == "vision-model", vision_test
    assert vision_probe.posts == ["https://mixed.example/v1/responses"], vision_probe.posts
    print("PASS provider test probes text and image models on their own endpoints")


async def main() -> None:
    try:
        await run_tests()
        await test_responses_mode()
        await test_mixed_api_modes_per_model()
    finally:
        await dispose_db()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    finally:
        atexit.unregister(CLEANUP.close)
        CLEANUP.close()
