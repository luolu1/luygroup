"""Regress offline safeguards without production I/O, even if a guard regresses.

Run: .venv/bin/python -B scripts/smoke_offline_guards.py [--output-dir DIRECTORY]
Independent tripwires stop actual protected opens/network if the tested hook fails.
"""

from __future__ import annotations

import argparse
import os
import socket
import sqlite3
import sys
import tempfile
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, redirect_stderr
from functools import partial
from pathlib import Path
from typing import Final
from unittest.mock import patch

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.offline_support import ROOT, GuardViolation, check_production_path, install_guards
from scripts.run_offline_smokes import run_smoke

PROBE: Final = """import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

storage = Path(os.environ["GROUPGUARD_OFFLINE_STORAGE"])
assert os.environ["GROUPGUARD_OFFLINE_PID"] == str(os.getpid())
assert "app" not in sys.modules
assert sys.dont_write_bytecode
assert "OPENAI_API_KEY" not in os.environ
for name in ("TMPDIR", "TMP", "TEMP", "SQLITE_TMPDIR", "HOME", "XDG_CACHE_HOME"):
    assert Path(os.environ[name]).is_relative_to(storage), name
from app.config import get_config
config = get_config()
assert config.config_path.is_relative_to(storage)
assert Path(config.resolve_database_url().split("///", 1)[1]).is_relative_to(storage)
assert Path(config.ai.learning_dir).is_relative_to(storage)
assert config.ai.providers == [] and not config.payments.enabled
assert config.bot.proxy == "" and config.payments.epay.key == ""
with tempfile.TemporaryDirectory(prefix="explicit-", dir="/tmp/opencode") as directory:
    owned = Path(directory)
    assert owned.is_relative_to(storage)
    (owned / "allowed.txt").write_text("owned", encoding="utf-8")
    assert (owned / "allowed.txt").read_text(encoding="utf-8") == "owned"
if "--worker" not in sys.argv:
    child = subprocess.run([sys.executable, "-B", __file__, "--worker"],
                           capture_output=True, text=True, timeout=10, check=True)
    assert json.loads(child.stdout) == {"guarded": True}
print(json.dumps({"guarded": True}))
"""


def rejects_when_guarded[Result](event: str, action: Callable[[], Result]) -> None:
    try:
        _ = action()
    except GuardViolation as error:
        assert error.event == event, error
    else:
        raise AssertionError(f"Guard did not reject {event}")


def runner_isolated_when_environment_is_inherited(artifacts: Path, fixtures: Path) -> None:
    scripts = tuple(fixtures / f"isolated_{index}.py" for index in range(2))
    for script in scripts:
        _ = script.write_text(PROBE, encoding="utf-8")
    inherited = {
        "GROUPGUARD_CONFIG": str(ROOT / "config.json"),
        "OPENAI_API_KEY": "never-inherit",
        "TMPDIR": str(ROOT / "data"),
        "TEMP": str(ROOT / "data"),
        "TMP": str(ROOT / "data"),
    }
    with patch.dict(os.environ, inherited), ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(partial(run_smoke, output_directory=artifacts, timeout=20), scripts)
        )
    assert all(result["status"] == "passed" for result in results), results
    assert len({result["storage"] for result in results}) == 2
    assert all(not Path(result["storage"]).exists() for result in results)
    print("PASS concurrent children: synthetic config, temp ownership, guarded restart, cleanup")


def runner_records_when_guard_is_caught_or_child_stalls(artifacts: Path, fixtures: Path) -> None:
    cases = (
        (
            "caught",
            f"import sys\ntry:\n    sys.audit('open', {str(ROOT / 'config.json')!r}, 'r', 0)\nexcept PermissionError:\n    print('caught')\n",
            "failed",
            3,
            10,
        ),
        ("failed", "raise SystemExit(7)\n", "failed", 7, 10),
        ("stalled", "from threading import Event\nEvent().wait()\n", "timeout", -9, 0.3),
    )
    for name, source, status, returncode, timeout in cases:
        script = fixtures / f"{name}.py"
        _ = script.write_text(source, encoding="utf-8")
        result = run_smoke(script, artifacts, timeout)
        assert (result["status"], result["returncode"]) == (status, returncode), result
        assert not Path(result["storage"]).exists()
        assert Path(result["log"]).stat().st_size > 0
    print("PASS caught violations, nonzero exit, timeout evidence and state cleanup")


def guards_reject_when_production_or_network_is_requested(storage: Path) -> None:
    alias = storage / "project-alias"
    alias.symlink_to(ROOT, target_is_directory=True)
    _ = install_guards(ROOT, storage)
    owned = storage / "owned.txt"
    _ = owned.write_text("synthetic", encoding="utf-8")
    assert owned.read_text(encoding="utf-8") == "synthetic"
    with closing(sqlite3.connect(storage / "owned.db")) as connection:
        assert connection.execute("SELECT 6 * 7").fetchone() == (42,)

    def tripwire[Value](event: str, arguments: tuple[Value, ...]) -> None:
        assert not (event.startswith("socket.") and event != "socket.__new__"), event
        assert event != "sqlite3.connect", "unguarded SQLite open"
        if event == "open" and isinstance(arguments[0], str | bytes | Path):
            target = Path(os.fsdecode(arguments[0])).resolve()
            assert target != (ROOT / "config.json").resolve(), "unguarded config open"
            assert not target.is_relative_to((ROOT / "data").resolve()), "unguarded data open"

    sys.addaudithook(tripwire)
    for path in (
        ROOT / "config.json",
        ROOT / "data",
        ROOT / "data/guard-probe.db",
        ROOT / "scripts/../config.json",
        alias / "config.json",
        alias / "data/guard-probe.db",
    ):
        rejects_when_guarded("production_path", path.read_bytes)
        rejects_when_guarded("production_path", partial(os.open, os.fsencode(path), os.O_RDONLY))
    rejects_when_guarded(
        "production_directory", partial(os.open, ROOT, os.O_RDONLY | os.O_DIRECTORY)
    )
    for path in (ROOT / "data/guard-probe.db", alias / "data/guard-probe.db"):
        rejects_when_guarded("production_path", partial(sqlite3.connect, path))
        rejects_when_guarded(
            "production_path", partial(sqlite3.connect, path.as_uri() + "?mode=ro", uri=True)
        )
    rejects_when_guarded("socket.getaddrinfo", partial(socket.getaddrinfo, "offline.invalid", 80))
    rejects_when_guarded("socket.gethostbyname", partial(socket.gethostbyname, "offline.invalid"))
    rejects_when_guarded("socket.gethostbyaddr", partial(socket.gethostbyaddr, "192.0.2.1"))
    rejects_when_guarded("socket.getnameinfo", partial(socket.getnameinfo, ("192.0.2.1", 80), 0))
    with socket.socket() as stream, socket.socket(type=socket.SOCK_DGRAM) as datagram:
        rejects_when_guarded("socket.connect", partial(stream.connect, ("192.0.2.1", 80)))
        rejects_when_guarded("socket.connect", partial(stream.connect_ex, ("127.0.0.1", 9)))
        rejects_when_guarded("socket.bind", partial(stream.bind, ("127.0.0.1", 0)))
        rejects_when_guarded("socket.sendto", partial(datagram.sendto, b"probe", ("127.0.0.1", 9)))
    with socket.socket(socket.AF_INET6) as stream:
        rejects_when_guarded("socket.connect", partial(stream.connect, ("::1", 9)))
    print("PASS production paths/aliases/SQLite, DNS, IPv4/IPv6/loopback, UDP; owned I/O allowed")


class Arguments(argparse.Namespace):
    output_dir: Path = Path("/tmp/opencode/offline-guard-regressions")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("--output-dir", type=Path, default=Arguments.output_dir)
    options = parser.parse_args(namespace=Arguments())
    output = check_production_path(options.output_dir)
    output.mkdir(mode=0o700, parents=True, exist_ok=True)
    artifacts = Path(tempfile.mkdtemp(prefix="run-", dir=output))
    print(f"ARTIFACTS {artifacts}", flush=True)
    with tempfile.TemporaryDirectory(prefix="fixtures-", dir=artifacts) as directory:
        fixtures = Path(directory)
        runner_isolated_when_environment_is_inherited(artifacts, fixtures)
        runner_records_when_guard_is_caught_or_child_stalls(artifacts, fixtures)
        with (artifacts / "guards.log").open("w", encoding="utf-8") as output_log:
            with redirect_stderr(output_log):
                guards_reject_when_production_or_network_is_requested(fixtures)
    print("PASS offline guard regressions")


if __name__ == "__main__":
    main()
