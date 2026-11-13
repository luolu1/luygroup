"""Run the public installer only in disposable checkouts with shell side effects stubbed."""

from __future__ import annotations

from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile


STUBS = r"""
set -Eeuo pipefail
record() { printf '%s\n' "$*" >> "$SANDBOX/calls"; }
safe() { case "$1" in "$SANDBOX"/*) ;; *) exit 91 ;; esac; }
id() { if [[ "$1" == -u ]]; then printf '0\n'; else return 1; fi; }
useradd() { record useradd "$@"; }
chown() { record chown "$@"; }
systemctl() { record systemctl "$@"; }
docker() { record docker "$@"; }
python3() { record python3 "$@"; return 92; }
dirname() { /usr/bin/dirname "$@"; }
mkdir() { safe "${@: -1}"; record mkdir "$@"; /bin/mkdir "$@"; }
cp() { safe "$1"; safe "$2"; record cp "$@"; /bin/cp "$@"; }
mktemp() { record mktemp "$@"; /bin/mktemp -d "$SANDBOX/render.XXXXXX"; }
rm() { safe "${@: -1}"; record rm "$@"; /bin/rm "$@"; }
install() {
    [[ "$1" == -m && "$2" == 0644 && "$#" == 4 ]] || exit 93
    safe "$3"
    case "$4" in
        /etc/systemd/system/groupguard-web.service) target=groupguard-web.service ;;
        /etc/systemd/system/groupguard-bot.service) target=groupguard-bot.service ;;
        *) exit 94 ;;
    esac
    record install "$@"
    /bin/cp -- "$3" "$SANDBOX/units/$target"
}
"""


def fixture(root: Path, name: str) -> Path:
    checkout = root / name
    (checkout / "deploy").mkdir(parents=True)
    source = Path(__file__).resolve().parents[1] / "deploy"
    for filename in (
        "install.sh",
        "groupguard.service",
        "groupguard-web.service",
        "groupguard-bot.service",
    ):
        _ = shutil.copyfile(source / filename, checkout / "deploy" / filename)
    (checkout / ".venv/bin").mkdir(parents=True)
    interpreter = checkout / ".venv/bin/python"
    _ = interpreter.write_text("#!/bin/sh\nexit 95\n", encoding="utf-8")
    interpreter.chmod(0o755)
    _ = (checkout / "config.example.json").write_text("{}\n", encoding="utf-8")
    _ = (checkout / "config.json").write_text('{"sandbox": true}\n', encoding="utf-8")
    (checkout / "data").mkdir()
    _ = (checkout / "data/sentinel").write_bytes(b"sandbox data\x00")
    return checkout


def invoke(checkout: Path, mode: str) -> subprocess.CompletedProcess[str]:
    """Use a closed PATH and BASH_ENV functions, never host deployment commands."""
    root = checkout.parent
    return subprocess.run(
        ["/bin/bash", str(checkout / "deploy/install.sh"), mode],
        cwd=root,
        env={
            "PATH": str(root / "empty-bin"),
            "BASH_ENV": str(root / "stubs.sh"),
            "SANDBOX": str(root),
            "LC_ALL": "C",
        },
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )


def check_units(checkout: Path) -> None:
    for role in ("web", "bot"):
        filename = f"groupguard-{role}.service"
        lines = (checkout.parent / "units" / filename).read_text(encoding="utf-8").splitlines()
        template = (checkout / "deploy" / filename).read_text(encoding="utf-8").splitlines()
        directory = next(
            line.removeprefix("WorkingDirectory=")
            for line in lines
            if line.startswith("WorkingDirectory=")
        )
        assert directory == str(checkout), (directory, str(checkout))
        environment = next(
            line.removeprefix("Environment=") for line in lines if "GROUPGUARD_CONFIG=" in line
        )
        assert shlex.split(environment) == [f"GROUPGUARD_CONFIG={checkout}/config.json"]
        command = next(
            line.removeprefix("ExecStart=") for line in lines if line.startswith("ExecStart=")
        )
        assert shlex.split(command) == [f"{checkout}/.venv/bin/python", f"run_{role}.py"]
        changed = ("WorkingDirectory=", "Environment=GROUPGUARD_CONFIG=", "ExecStart=")
        assert [
            line
            for line in lines
            if not line.startswith(("WorkingDirectory=", "ExecStart="))
            and "GROUPGUARD_CONFIG=" not in line
        ] == [line for line in template if not line.startswith(changed)]


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="groupguard-install-") as temporary:
        root = Path(temporary)
        (root / "units").mkdir()
        (root / "empty-bin").mkdir()
        _ = (root / "stubs.sh").write_text(STUBS, encoding="utf-8")
        checkout = fixture(root, "checkout with spaces")
        original = {path: path.read_bytes() for path in (checkout / "deploy").iterdir()}

        result = invoke(checkout, "systemd")

        assert result.returncode == 0, (result.stdout, result.stderr)
        check_units(checkout)
        assert (checkout / "config.json").read_text() == '{"sandbox": true}\n'
        assert (checkout / "data/sentinel").read_bytes() == b"sandbox data\x00"
        assert all(path.read_bytes() == content for path, content in original.items())
        calls = (root / "calls").read_text().splitlines()
        assert "systemctl daemon-reload" in calls
        assert "systemctl enable --now groupguard-web.service groupguard-bot.service" in calls
        assert (
            f"useradd --system --home-dir {checkout} --shell /usr/sbin/nologin groupguard" in calls
        )
        assert f"chown -R groupguard:groupguard {checkout}/data" in calls

        (checkout / "config.json").unlink()
        result = invoke(checkout, "systemd")
        assert result.returncode == 0, (result.stdout, result.stderr)
        assert (checkout / "config.json").read_text() == "{}\n"
        assert (checkout / "data/sentinel").read_bytes() == b"sandbox data\x00"

        for edge in (
            "%n",
            "$HOME",
            "back\\slash",
            'double"quote',
            "single'quote",
            "tab\t",
            "line\nbreak",
            "trailing\n",
            "trailing ",
        ):
            rejected = fixture(root, f"edge {edge}")
            before = (root / "calls").read_bytes()

            result = invoke(rejected, "systemd")

            assert result.returncode != 0, repr(edge)
            assert (root / "calls").read_bytes() == before, repr(edge)

        docker_checkout = fixture(root, "docker %n $HOME back\\slash")
        for mode in ("docker-compose", "docker"):
            _ = (root / "calls").write_text("")

            result = invoke(docker_checkout, mode)

            assert result.returncode == 0, (result.stdout, result.stderr)
            calls = (root / "calls").read_text().splitlines()
            assert all(
                not line.startswith(("systemctl ", "install ", "useradd ", "chown "))
                for line in calls
            )
            assert any(line.startswith("docker ") for line in calls)
        print(
            "install paths: sandbox split units, preservation, rejected characters, Docker modes OK"
        )


if __name__ == "__main__":
    main()
