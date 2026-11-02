from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
TMP = Path(tempfile.mkdtemp(prefix="groupguard-ai-client-smoke-"))
TMP_CONFIG = TMP / "config.json"
config = json.loads((ROOT / "config.example.json").read_text(encoding="utf-8"))
config["bot"]["token"] = "123456:smoke-token"
config["web"]["secret_key"] = "ai-client-smoke-secret"
config["database"]["url"] = f"sqlite+aiosqlite:///{TMP / 'ai.db'}"
config.setdefault("ai", {})["learning_dir"] = str(TMP / "learning")
TMP_CONFIG.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
os.environ["GROUPGUARD_CONFIG"] = str(TMP_CONFIG)
sys.path.insert(0, str(ROOT))

from app.db import dispose_db, init_db, session_scope  # noqa: E402
from app.models import AIProvider  # noqa: E402
from app.services import ai  # noqa: E402


class FakeClient:
    def __init__(self, *, responses: list[object], **_kwargs) -> None:
        self.responses = responses
        self.posts: list[str] = []
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args) -> None:
        self.closed = True

    async def post(self, url, **_kwargs):
        self.posts.append(url)
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


async def main() -> None:
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
    ai.httpx.AsyncClient = client_factory
    try:
        async with session_scope() as session:
            result = await ai.chat(session, [{"role": "user", "content": "hello"}])
    finally:
        ai.httpx.AsyncClient = original_client

    assert result.ok and result.provider == "second" and result.attempts == 2, result
    assert len(clients) == 1
    assert clients[0].posts == [
        "https://first.example/v1/chat/completions",
        "https://second.example/v1/chat/completions",
    ]
    assert clients[0].closed
    print("PASS provider fallback reuses and closes one HTTP client")

    exhausted_clients: list[FakeClient] = []

    def exhausted_client_factory(**kwargs):
        client = FakeClient(
            responses=[response_error, response_error], **kwargs
        )
        exhausted_clients.append(client)
        return client

    ai.httpx.AsyncClient = exhausted_client_factory
    try:
        async with session_scope() as session:
            failed = await ai.chat(session, [{"role": "user", "content": "hello"}])
    finally:
        ai.httpx.AsyncClient = original_client
    assert not failed.ok and failed.attempts == 2
    assert len(exhausted_clients) == 1 and exhausted_clients[0].closed
    print("PASS exhausted provider fallback closes its HTTP client")
    await dispose_db()


if __name__ == "__main__":
    asyncio.run(main())
