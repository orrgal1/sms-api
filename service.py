"""Manage the macOS Messages-to-email launchd service."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import shutil
import sys
import tempfile
from typing import NamedTuple

SERVICE_LABEL = "com.local.sms-forwarder"
APP_ROOT = Path(__file__).resolve().parent
MAIN_PATH = APP_ROOT / "main.py"
LOG_PATH = APP_ROOT / "forwarder.log"
LAUNCH_AGENTS_DIR = Path.home() / "Library" / "LaunchAgents"
SERVICE_PLIST_PATH = LAUNCH_AGENTS_DIR / f"{SERVICE_LABEL}.plist"
DOMAIN = f"gui/{os.getuid()}"
LAUNCHCTL = "/bin/launchctl"

ENV_KEYS = (
    "FORWARD_TO_EMAIL",
    "SMS_MESSAGES_DB",
    "SMS_STATE_DB",
    "SMS_POLL_SECONDS",
    "SMS_BATCH_SIZE",
    "GAPI_BIN",
    "GAPI_TIMEOUT_SECONDS",
    "LOG_LEVEL",
)


class LaunchctlError(RuntimeError):
    """A launchctl invocation failed."""

    def __init__(self, args: list[str], returncode: int, stdout: str, stderr: str):
        detail = stderr.strip() or stdout.strip() or f"exit code {returncode}"
        super().__init__(f"launchctl {' '.join(args)} failed: {detail}")
        self.args_passed = args
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class LaunchctlResult(NamedTuple):
    stdout: str
    stderr: str


def run_launchctl(args: list[str]) -> LaunchctlResult:
    """Run launchctl without invoking a shell."""
    try:
        result = subprocess.run(
            [LAUNCHCTL, *args],
            shell=False,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as error:
        raise RuntimeError(f"could not run launchctl: {error}") from error
    if result.returncode != 0:
        raise LaunchctlError(args, result.returncode, result.stdout, result.stderr)
    return LaunchctlResult(result.stdout, result.stderr)


def is_missing_service_error(error: BaseException) -> bool:
    if not isinstance(error, LaunchctlError):
        return False
    output = f"{error.stdout}\n{error.stderr}"
    return error.returncode == 113 or any(
        phrase in output.lower()
        for phrase in ("could not find service", "service not found", "no such process")
    )


def path_exists(path: Path) -> bool:
    return path.exists()


def is_service_loaded(label: str = SERVICE_LABEL) -> tuple[bool, str]:
    try:
        result = run_launchctl(["print", f"{DOMAIN}/{label}"])
    except LaunchctlError as error:
        if is_missing_service_error(error):
            return False, ""
        raise
    return True, result.stdout.strip()


def unload_service(label: str = SERVICE_LABEL, plist_path: Path | None = None) -> bool:
    if plist_path is None:
        plist_path = SERVICE_PLIST_PATH
    plist_path = Path(plist_path)
    target = [DOMAIN, str(plist_path)] if path_exists(plist_path) else [f"{DOMAIN}/{label}"]
    try:
        run_launchctl(["bootout", *target])
    except LaunchctlError as error:
        loaded, _ = is_service_loaded(label)
        if loaded:
            raise error
        return False

    loaded, _ = is_service_loaded(label)
    if loaded:
        raise RuntimeError(f"Service {label} is still loaded after launchctl bootout.")
    return True


def read_dotenv(path: Path | None = None) -> dict[str, str]:
    """Read the documented launchd settings from a simple .env file."""
    path = APP_ROOT / ".env" if path is None else Path(path)
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return values
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        if key not in ENV_KEYS:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    for key in ("SMS_MESSAGES_DB", "SMS_STATE_DB"):
        if key in values:
            values[key] = os.path.expanduser(values[key])
    if "GAPI_BIN" in values:
        configured = os.path.expanduser(values["GAPI_BIN"])
        resolved = shutil.which(configured)
        executable = resolved or configured
        values["GAPI_BIN"] = (
            str(Path(executable).resolve()) if os.path.isabs(executable) else executable
        )
    return values


def plist_environment() -> dict[str, str]:
    values = read_dotenv()
    if "GAPI_BIN" not in values:
        resolved = shutil.which("gapi")
        if resolved:
            values["GAPI_BIN"] = str(Path(resolved).resolve())
    return values


def environment_xml(values: dict[str, str]) -> str:
    rows = ["  <key>EnvironmentVariables</key>", "  <dict>"]
    for key in ENV_KEYS:
        if key in values:
            rows.extend(
                [
                    f"    <key>{xml_escape(key)}</key>",
                    f"    <string>{xml_escape(values[key])}</string>",
                ]
            )
    rows.append("  </dict>")
    return "\n".join(rows)


def xml_escape(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def create_plist() -> str:
    def string(value: Path | str) -> str:
        return f"    <string>{xml_escape(str(value))}</string>"

    return f'''<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
{string(SERVICE_LABEL)}
  <key>ProgramArguments</key>
  <array>
{string(sys.executable)}
{string(MAIN_PATH)}
  </array>
  <key>WorkingDirectory</key>
{string(APP_ROOT)}
  <key>StandardOutPath</key>
{string(LOG_PATH)}
  <key>StandardErrorPath</key>
{string(LOG_PATH)}
{environment_xml(plist_environment())}
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
</dict>
</plist>
'''


def remove_plist(plist_path: Path | None = None) -> None:
    if plist_path is None:
        plist_path = SERVICE_PLIST_PATH
    try:
        Path(plist_path).unlink()
    except FileNotFoundError:
        pass


def write_plist_atomically(contents: str, plist_path: Path | None = None) -> None:
    if plist_path is None:
        plist_path = SERVICE_PLIST_PATH
    plist_path = Path(plist_path)
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=plist_path.parent,
            prefix=f".{plist_path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write(contents)
            temporary.flush()
            os.fchmod(temporary.fileno(), 0o644)
        os.replace(temporary_path, plist_path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            remove_plist(temporary_path)

def install_service() -> None:
    if sys.version_info < (3, 11):
        raise RuntimeError("Python 3.11 or newer is required to install the service.")
    LAUNCH_AGENTS_DIR.mkdir(parents=True, exist_ok=True)
    unload_service(SERVICE_LABEL, SERVICE_PLIST_PATH)
    remove_plist(SERVICE_PLIST_PATH)
    write_plist_atomically(create_plist(), SERVICE_PLIST_PATH)
    run_launchctl(["bootstrap", DOMAIN, str(SERVICE_PLIST_PATH)])
    loaded, _ = is_service_loaded(SERVICE_LABEL)
    if not loaded:
        raise RuntimeError(f"Service {SERVICE_LABEL} was not loaded after installation.")
    print(f"Installed and started {SERVICE_LABEL}.")
    print(f"LaunchAgent: {SERVICE_PLIST_PATH}")
    print(f"Log: {LOG_PATH}")


def service_status() -> bool:
    loaded, details = is_service_loaded(SERVICE_LABEL)
    if not loaded:
        print(f"{SERVICE_LABEL} is not loaded.")
        print(f"Expected LaunchAgent: {SERVICE_PLIST_PATH}")
        return False
    print(f"{SERVICE_LABEL} is loaded.")
    if details:
        print(details)
    return True


def uninstall_service() -> None:
    unloaded = unload_service(SERVICE_LABEL, SERVICE_PLIST_PATH)
    remove_plist(SERVICE_PLIST_PATH)
    if unloaded:
        print(f"Stopped and uninstalled {SERVICE_LABEL}.")
    else:
        print(f"{SERVICE_LABEL} was not loaded; its LaunchAgent has been removed.")


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if len(arguments) != 1 or arguments[0] not in {"install", "status", "uninstall"}:
        print("Usage: python service.py install|status|uninstall", file=sys.stderr)
        return 1
    try:
        if arguments[0] == "install":
            install_service()
        elif arguments[0] == "status":
            return 0 if service_status() else 1
        else:
            uninstall_service()
    except Exception as error:
        print(str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
