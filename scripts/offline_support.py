"""Reusable offline smoke safeguards, not a security sandbox.

Python audit hooks cover ordinary file/SQLite opens and socket operations, not
hostile native code, hard-link aliases, inherited descriptors or syscall races.
The owned sitecustomize also guards the suite's Python restart workers.
"""

from __future__ import annotations

import atexit
import json
import os
import runpy
import sys
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final
from unittest.mock import patch
from urllib.parse import unquote, urlsplit

ROOT: Final = Path(__file__).resolve().parents[1]
TEMP_ENV: Final = (
    "TMPDIR",
    "TMP",
    "TEMP",
    "SQLITE_TMPDIR",
    "HOME",
    "XDG_CACHE_HOME",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "XDG_RUNTIME_DIR",
)
NETWORK_EVENTS: Final = frozenset(
    {
        "socket.getaddrinfo",
        "socket.gethostbyname",
        "socket.gethostbyaddr",
        "socket.getnameinfo",
        "socket.connect",
        "socket.bind",
        "socket.sendto",
        "socket.sendmsg",
    }
)
type JsonValue = str | int | bool | list[JsonValue] | dict[str, JsonValue] | None


class GuardViolation(PermissionError):
    """A rejected operation; event and target are diagnostic metadata only."""

    def __init__(self, event: str, target: str) -> None:
        self.event: str = event
        self.target: str = target
        super().__init__(f"offline guard rejected {event}: {target}")


def check_production_path(path: Path, root: Path = ROOT) -> Path:
    """Reject lexical and resolved production paths, including symlink aliases."""
    candidates = (Path(os.path.abspath(path)), path.resolve())
    for protected in (root / "config.json", root / "data"):
        for boundary in (protected, protected.resolve()):
            if any(
                candidate == boundary or candidate.is_relative_to(boundary)
                for candidate in candidates
            ):
                raise GuardViolation("production_path", str(path))
    return candidates[1]


def child_environment(storage: Path) -> Mapping[str, str]:
    """Create synthetic config and a clean environment inside an owned directory."""
    owned_storage = check_production_path(storage)
    bootstrap = owned_storage / "bootstrap"
    bootstrap.mkdir()
    temporary = owned_storage / "tmp"
    temporary.mkdir()
    config: dict[str, JsonValue] = {
        "bot": {
            "token": "123456789:OFFLINE_SMOKE_SYNTHETIC_TOKEN",
            "admin_ids": [],
            "username": "offline_smoke_bot",
            "proxy": "",
        },
        "web": {
            "host": "127.0.0.1",
            "port": 0,
            "secret_key": "offline-synthetic-secret",
            "base_url": "https://example.test",
        },
        "database": {"url": f"sqlite+aiosqlite:///{owned_storage / 'smoke.db'}"},
        "ai": {"learning_dir": str(owned_storage / "learning"), "providers": []},
        "payments": {"enabled": False},
    }
    config_path = owned_storage / "config.json"
    _ = config_path.write_text(json.dumps(config), encoding="utf-8")
    _ = (bootstrap / "sitecustomize.py").write_text(
        "import os\ntry:\n    from scripts.offline_support import activate\n    activate()\n"
        + "except BaseException:\n    import traceback\n    traceback.print_exc()\n    os._exit(3)\n",
        encoding="utf-8",
    )
    return {
        **dict.fromkeys(TEMP_ENV, str(temporary)),
        "PATH": os.defpath,
        "LANG": "C.UTF-8",
        "TZ": "UTC",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUNBUFFERED": "1",
        "PYTHONNOUSERSITE": "1",
        "PYTHONPATH": os.pathsep.join(map(str, (bootstrap, ROOT / "scripts", ROOT))),
        "GROUPGUARD_CONFIG": str(config_path),
        "GROUPGUARD_OFFLINE_STORAGE": str(owned_storage),
    }


@dataclass(frozen=True, slots=True)
class OfflineGuard:
    """Accumulate rejected events so a swallowed exception still fails the smoke."""

    root: Path
    storage: Path
    violations: list[str] = field(default_factory=list, init=False)

    def audit[Value](
        self, event: str, arguments: tuple[Value | list[str] | tuple[str, ...], ...]
    ) -> None:
        """Parse the relevant subset of CPython's open-ended audit event stream."""
        try:
            if event in NETWORK_EVENTS or event in {
                "os.system",
                "os.exec",
                "os.fork",
                "os.forkpty",
            }:
                raise GuardViolation(event, "network and unguarded process operations are disabled")
            if event in {"open", "sqlite3.connect"}:
                match arguments[0]:
                    case int() as descriptor:
                        path = Path(f"/proc/self/fd/{descriptor}")
                    case str() | bytes() | Path() as raw:
                        text = os.fsdecode(raw)
                        if event == "sqlite3.connect":
                            text = (
                                unquote(urlsplit(text).path) if text.startswith("file:") else text
                            )
                            if text in {"", ":memory:"}:
                                return
                        path = Path(text)
                    case _:
                        raise GuardViolation(event, "unsupported path representation")
                resolved = check_production_path(path, self.root)
                if event == "open" and self.root.is_relative_to(resolved):
                    raise GuardViolation("production_directory", str(path))
                if event == "sqlite3.connect" and not resolved.is_relative_to(self.storage):
                    raise GuardViolation(event, str(path))
            if event in {"subprocess.Popen", "os.posix_spawn"}:
                match arguments[1]:
                    case [str() as executable, "-B", str() as script, *_] | [
                        str() as executable,
                        str() as script,
                        *_,
                    ]:
                        if script.startswith("-") or not script.endswith(".py"):
                            raise GuardViolation(event, "worker must execute a Python script")
                    case _:
                        raise GuardViolation(
                            event, "only inherited Python script workers are allowed"
                        )
                paths = (self.storage / "bootstrap", ROOT / "scripts", ROOT)
                expected_path = os.pathsep.join(map(str, paths))
                inherited = (
                    arguments[3] is None
                    if event == "subprocess.Popen"
                    else arguments[2] is None or arguments[2] == os.environ
                )
                if (
                    arguments[0] != sys.executable
                    or not inherited
                    or executable != sys.executable
                    or os.environ.get("PYTHONPATH") != expected_path
                ):
                    raise GuardViolation(event, "worker must inherit the guarded Python startup")
                config = check_production_path(Path(os.environ["GROUPGUARD_CONFIG"]), self.root)
                if not config.is_relative_to(self.storage):
                    raise GuardViolation(event, "worker config must remain in owned storage")
        except GuardViolation as error:
            self.violations.append(str(error))
            print(str(error), file=sys.stderr, flush=True)
            raise

    def finish(self) -> None:
        """Fail at process exit even when application code caught a guard exception."""
        if self.violations:
            _ = sys.stdout.flush()
            _ = sys.stderr.flush()
            os._exit(3)


def install_guards(root: Path, storage: Path) -> OfflineGuard:
    """Install irreversible, process-local guards; use a fresh child for each smoke."""
    guard = OfflineGuard(root.resolve(), storage.resolve())
    sys.addaudithook(guard.audit)
    return guard


def activate() -> None:
    """Run from owned sitecustomize before the entrypoint or application imports."""
    storage = check_production_path(Path(os.environ["GROUPGUARD_OFFLINE_STORAGE"]))
    guard = install_guards(ROOT, storage)
    if any(name == "app" or name.startswith("app.") for name in sys.modules):
        raise GuardViolation("bootstrap", "application was already imported")
    config = check_production_path(Path(os.environ["GROUPGUARD_CONFIG"]))
    if not config.is_relative_to(storage):
        raise GuardViolation("bootstrap", "config must be synthetic and owned")
    sys.dont_write_bytecode = True
    tempfile.tempdir = str(storage / "tmp")
    original_mkdtemp = tempfile.mkdtemp

    def owned_mkdtemp(
        suffix: str | bytes | None = None,
        prefix: str | bytes | None = None,
        dir: str | bytes | os.PathLike[str] | os.PathLike[bytes] | None = None,
    ) -> str | bytes:
        destination = Path(os.fsdecode(dir)).resolve() if dir is not None else storage / "tmp"
        if not destination.is_relative_to(storage):
            destination = storage / "tmp"
        if isinstance(suffix, bytes) or isinstance(prefix, bytes) or isinstance(dir, bytes):
            return original_mkdtemp(
                os.fsencode(suffix or ""), os.fsencode(prefix or "tmp"), os.fsencode(destination)
            )
        return original_mkdtemp(
            os.fsdecode(suffix or ""), os.fsdecode(prefix or "tmp"), str(destination)
        )

    _ = patch.object(tempfile, "mkdtemp", owned_mkdtemp).start()
    _ = atexit.register(guard.finish)
    os.environ["GROUPGUARD_OFFLINE_PID"] = str(os.getpid())
    print(
        f"OFFLINE_READY pid={os.getpid()} storage={storage} config={config} network=denied",
        file=sys.stderr,
        flush=True,
    )


if __name__ == "__main__":
    if os.environ.get("GROUPGUARD_OFFLINE_PID") != str(os.getpid()):
        raise SystemExit("Use scripts/run_offline_smokes.py to supply the guarded environment")
    sys.argv = sys.argv[1:]
    _ = runpy.run_path(sys.argv[0], run_name="__main__")
