"""Bot 断线恢复生命周期回归测试。

验证 Telegram 暂时不可达时 Bot 不会退出，恢复后继续轮询：

    .venv/bin/python scripts/smoke_bot_recovery.py
"""
from __future__ import annotations

import asyncio
import os
import signal
import sys
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.bot import main as bot_main  # noqa: E402

failures: list[str] = []
_REAL_SLEEP = asyncio.sleep


def check(name: str, cond: bool, extra: str = "") -> None:
    if cond:
        print(f"PASS {name}")
    else:
        failures.append(name)
        print(f"FAIL {name} {extra}")


class FakeSession:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class FakeBot:
    def __init__(self, number: int, fail_startup: bool) -> None:
        self.number = number
        self.fail_startup = fail_startup
        self.session = FakeSession()

    async def delete_my_commands(self) -> None:
        if self.fail_startup:
            raise OSError("temporary startup outage")

    async def set_my_commands(self, *_args, **_kwargs) -> None:
        return

    async def me(self) -> SimpleNamespace:
        return SimpleNamespace(username=f"smoke{self.number}", id=self.number)

    async def delete_webhook(self, *, drop_pending_updates: bool) -> None:
        assert drop_pending_updates is False


class Harness:
    def __init__(self, startup_failures: int, polling_failures: int) -> None:
        self.startup_failures = startup_failures
        self.polling_failures = polling_failures
        self.bots: list[FakeBot] = []
        self.sleeps: list[float] = []
        self.polled_bots: list[int] = []
        self.stale_bot_polls = 0
        self.dispatcher_count = 0
        self.scheduler_count = 0
        self.scheduler_cancelled = 0
        self.init_count = 0
        self.dispose_count = 0
        self.scheduler_ready = asyncio.Event()
        self.active_ref: list = [None]
        self.cancel_after_sleeps: int | None = None

    def build_bot(self) -> FakeBot:
        bot = FakeBot(len(self.bots) + 1, self.startup_failures > 0)
        if self.startup_failures > 0:
            self.startup_failures -= 1
        self.bots.append(bot)
        return bot

    def build_dispatcher(self) -> "FakeDispatcher":
        self.dispatcher_count += 1
        return FakeDispatcher(self)

    async def init_db(self) -> None:
        self.init_count += 1

    async def dispose_db(self) -> None:
        self.dispose_count += 1

    async def run_scheduler(self, proxy) -> None:
        self.scheduler_count += 1
        self.active_ref[0] = proxy
        self.scheduler_ready.set()
        try:
            await asyncio.Event().wait()
        finally:
            self.scheduler_cancelled += 1

    async def sleep(self, delay: float) -> None:
        self.sleeps.append(delay)
        await self.scheduler_ready.wait()
        await _REAL_SLEEP(0)
        if (
            self.cancel_after_sleeps is not None
            and len(self.sleeps) >= self.cancel_after_sleeps
        ):
            raise asyncio.CancelledError

    def should_fail_polling(self) -> bool:
        if self.polling_failures <= 0:
            return False
        self.polling_failures -= 1
        return True

    def apply_patches(self, stack: ExitStack) -> None:
        stack.enter_context(patch.object(bot_main, "init_db", self.init_db))
        stack.enter_context(patch.object(bot_main, "dispose_db", self.dispose_db))
        stack.enter_context(patch.object(bot_main, "run_scheduler", self.run_scheduler))
        stack.enter_context(patch.object(bot_main, "build_bot", self.build_bot))
        stack.enter_context(
            patch.object(bot_main, "build_dispatcher", self.build_dispatcher)
        )
        stack.enter_context(patch.object(bot_main.asyncio, "sleep", self.sleep))

    def lifecycle(self) -> tuple[int, int, int, int]:
        return (
            self.scheduler_count,
            self.scheduler_cancelled,
            self.init_count,
            self.dispose_count,
        )


class FakeDispatcher:
    def __init__(self, harness: Harness) -> None:
        self.harness = harness

    def resolve_used_update_types(self) -> list[str]:
        return []

    async def start_polling(self, bot: FakeBot, **_kwargs) -> None:
        proxy = self.harness.active_ref[0]
        if proxy.session is not bot.session:
            self.harness.stale_bot_polls += 1
        self.harness.polled_bots.append(bot.number)
        if self.harness.should_fail_polling():
            raise OSError("temporary polling outage")


async def run_scenario(harness: Harness) -> BaseException | None:
    with ExitStack() as stack:
        harness.apply_patches(stack)
        try:
            await bot_main.run_bot()
        except BaseException as exc:  # noqa: BLE001 - the caller inspects it
            return exc
    return None


async def test_recovers_from_startup_and_polling_failures() -> None:
    harness = Harness(startup_failures=1, polling_failures=1)
    error = await run_scenario(harness)
    check("recovery completes without raising", error is None, repr(error))
    check(
        "each attempt builds a fresh bot",
        [bot.number for bot in harness.bots] == [1, 2, 3],
        str([bot.number for bot in harness.bots]),
    )
    check(
        "polling resumes on the retried bot",
        harness.polled_bots == [2, 3],
        str(harness.polled_bots),
    )
    check(
        "backoff restarts from the shortest delay after reconnecting",
        harness.sleeps == [1, 1],
        str(harness.sleeps),
    )
    check(
        "every attempted session is closed",
        all(bot.session.closed for bot in harness.bots),
        str([bot.session.closed for bot in harness.bots]),
    )
    check(
        "dispatcher is built once across reconnects",
        harness.dispatcher_count == 1,
        str(harness.dispatcher_count),
    )
    check(
        "scheduler never uses a previous attempt's bot",
        harness.stale_bot_polls == 0,
        str(harness.stale_bot_polls),
    )
    check(
        "one scheduler and one database lifecycle",
        harness.lifecycle() == (1, 1, 1, 1),
        str(harness.lifecycle()),
    )


async def test_keeps_retrying_with_capped_backoff() -> None:
    harness = Harness(startup_failures=99, polling_failures=0)
    harness.cancel_after_sleeps = 8
    error = await run_scenario(harness)
    check(
        "outage keeps the bot task retrying until cancelled",
        isinstance(error, asyncio.CancelledError),
        repr(error),
    )
    check(
        "retry delay doubles up to the cap",
        harness.sleeps == [1, 2, 4, 8, 16, 32, 60, 60],
        str(harness.sleeps),
    )
    check(
        "cancellation still closes every attempted session",
        all(bot.session.closed for bot in harness.bots),
        str([bot.session.closed for bot in harness.bots]),
    )
    check(
        "cancellation keeps a single scheduler and database lifecycle",
        harness.lifecycle() == (1, 1, 1, 1),
        str(harness.lifecycle()),
    )


async def test_sigterm_stops_the_bot_while_retrying() -> None:
    harness = Harness(startup_failures=99, polling_failures=0)
    retry_sleep = harness.sleep

    async def sleep_then_signal(delay: float) -> None:
        if not harness.sleeps:
            os.kill(os.getpid(), signal.SIGTERM)
        await retry_sleep(delay)

    harness.sleep = sleep_then_signal
    error = await run_scenario(harness)
    check(
        "sigterm during a retry wait stops the bot",
        isinstance(error, asyncio.CancelledError),
        repr(error),
    )
    check(
        "sigterm cleanup runs exactly once",
        harness.lifecycle() == (1, 1, 1, 1),
        str(harness.lifecycle()),
    )


async def main() -> None:
    await test_recovers_from_startup_and_polling_failures()
    await test_keeps_retrying_with_capped_backoff()
    await test_sigterm_stops_the_bot_while_retrying()
    if failures:
        print("FAILURES:", failures)
        raise SystemExit(1)
    print("FAILURES: none")


if __name__ == "__main__":
    asyncio.run(main())
