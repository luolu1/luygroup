"""Run unchanged smoke entrypoints with offline safeguards (not a security sandbox).

Usage: .venv/bin/python -B scripts/run_offline_smokes.py --list
       .venv/bin/python -B scripts/run_offline_smokes.py --only smoke_ai_client --output-dir /tmp/smokes
Each run retains *.log, per-script *.json and summary.json; temporary state is removed.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import signal
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from functools import partial
from pathlib import Path
from typing import Final, Literal, TypedDict

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.offline_support import ROOT, GuardViolation, check_production_path, child_environment

DEFAULT_SMOKES: Final = (
    "smoke_web",
    "smoke_scheduler",
    "smoke_scheduler_idempotency",
    "smoke_scheduler_queries",
    "smoke_games_race",
    "smoke_auction_reminders",
    "smoke_auction_dispatcher",
    "smoke_commands",
    "smoke_bot_recovery",
    "smoke_economy_claims",
    "smoke_subscription_purchase",
    "smoke_permissions",
    "smoke_captcha",
    "smoke_rejoin_mute",
    "smoke_payments",
    "smoke_ai_client",
    "smoke_entitlement_queries",
    "smoke_db_migrations",
    "smoke_web_boundaries",
    "smoke_settlement_delivery",
    "smoke_redpacket_publication",
    "smoke_keyword_entitlements",
)


class SmokeResult(TypedDict):
    script: str
    status: Literal["passed", "failed", "timeout", "error"]
    returncode: int | None
    elapsed_seconds: float
    log: str
    storage: str


def run_smoke(script: Path, output_directory: Path, timeout: float) -> SmokeResult:
    """Run one fresh interpreter, retaining evidence after owned state is cleaned."""
    started = time.monotonic()
    artifact_directory = check_production_path(output_directory)
    log_path = artifact_directory / f"{script.stem}.log"
    status: Literal["passed", "failed", "timeout", "error"] = "error"
    returncode: int | None = None
    storage_path = ""
    with log_path.open("xb") as output:
        try:
            with tempfile.TemporaryDirectory(
                prefix=f"{script.stem}-", dir=artifact_directory
            ) as directory:
                storage = Path(directory)
                storage_path = str(storage)
                environment = child_environment(storage)
                command = [
                    sys.executable,
                    "-B",
                    str(ROOT / "scripts/offline_support.py"),
                    str(script),
                ]
                with subprocess.Popen(
                    command,
                    cwd=storage,
                    env=environment,
                    stdin=subprocess.DEVNULL,
                    stdout=output,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                ) as process:
                    try:
                        returncode = process.wait(timeout=timeout)
                        status = "passed" if returncode == 0 else "failed"
                    except subprocess.TimeoutExpired:
                        status = "timeout"
                        _ = output.write(f"\nTIMEOUT after {timeout} seconds\n".encode())
                    finally:
                        with suppress(ProcessLookupError):
                            os.killpg(process.pid, signal.SIGKILL)
                        returncode = process.wait(timeout=5)
        except OSError as error:
            status = "error"
            _ = output.write(f"\nRUNNER_ERROR {type(error).__name__}: {error}\n".encode())
    result = SmokeResult(
        script=str(script),
        status=status,
        returncode=returncode,
        elapsed_seconds=round(time.monotonic() - started, 3),
        log=str(log_path),
        storage=storage_path,
    )
    _ = (artifact_directory / f"{script.stem}.json").write_text(
        json.dumps(result, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        f"{status.upper()} {script.stem} ({result['elapsed_seconds']}s) log={log_path}", flush=True
    )
    return result


class Arguments(argparse.Namespace):
    list_smokes: bool = False
    only: list[str] | None = None
    jobs: int = 1
    timeout: float = 180
    output_dir: Path = Path("/tmp/opencode/offline-smokes")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument(
        "--list",
        dest="list_smokes",
        action="store_true",
        help="List without running or writing artifacts",
    )
    _ = parser.add_argument(
        "--only",
        nargs="+",
        action="extend",
        choices=DEFAULT_SMOKES,
        help="Select exact entrypoint stems; repeatable",
    )
    _ = parser.add_argument(
        "--jobs",
        type=int,
        choices=range(1, 5),
        default=1,
        help="Concurrent children (1-4, default: 1)",
    )
    _ = parser.add_argument(
        "--timeout",
        type=float,
        default=180,
        help="Per-script seconds (0 < timeout <= 900, default: 180)",
    )
    _ = parser.add_argument(
        "--output-dir",
        type=Path,
        default=Arguments.output_dir,
        help="Artifact parent; a unique run directory is retained",
    )
    options = parser.parse_args(namespace=Arguments())
    if not math.isfinite(options.timeout) or not 0 < options.timeout <= 900:
        parser.error("--timeout must be finite and greater than 0, at most 900 seconds")
    selected = tuple(
        name for name in DEFAULT_SMOKES if options.only is None or name in options.only
    )
    if options.list_smokes:
        print("\n".join(f"scripts/{name}.py" for name in selected))
        return 0
    try:
        output_directory = check_production_path(options.output_dir)
    except GuardViolation as error:
        parser.error(str(error))
    output_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    run_directory = Path(tempfile.mkdtemp(prefix="run-", dir=output_directory))
    print(f"ARTIFACTS {run_directory}", flush=True)
    worker = partial(run_smoke, output_directory=run_directory, timeout=options.timeout)
    with ThreadPoolExecutor(max_workers=options.jobs) as pool:
        results = list(pool.map(worker, (ROOT / "scripts" / f"{name}.py" for name in selected)))
    _ = (run_directory / "summary.json").write_text(
        json.dumps(
            {
                "python": sys.version,
                "executable": sys.executable,
                "jobs": options.jobs,
                "timeout_seconds": options.timeout,
                "results": results,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return int(any(result["status"] != "passed" for result in results))


if __name__ == "__main__":
    raise SystemExit(main())
