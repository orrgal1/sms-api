"""Interactive setup for the macOS Messages email forwarder."""

from __future__ import annotations

from collections.abc import Callable, Mapping
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from typing import Protocol

from config import load_settings
from gmail_sender import GmailSender
from messages_reader import MessagesReader
from service import APP_ROOT, ENV_KEYS, install_service, read_dotenv


DEFAULT_VALUES = {
    "SMS_MESSAGES_DB": "~/Library/Messages/chat.db",
    "SMS_STATE_DB": "./sms_forwarder.sqlite3",
    "SMS_POLL_SECONDS": "5",
    "SMS_BATCH_SIZE": "100",
    "GAPI_BIN": "gapi",
    "GAPI_TIMEOUT_SECONDS": "120",
    "LOG_LEVEL": "INFO",
}
GAPI_CHECK_TIMEOUT_SECONDS = 30.0
GAPI_CHECK_QUERY = "newer_than:1d"


class SetupError(RuntimeError):
    """A setup prerequisite failed with a user-actionable explanation."""


class _MessagesReader(Protocol):
    def current_max_rowid(self) -> int: ...


class _CompletedProcess(Protocol):
    returncode: int


Input = Callable[[str], str]
Output = Callable[[str], None]
Runner = Callable[..., _CompletedProcess]


def _masked_address(address: str) -> str:
    """Return a recognizable but non-sensitive rendering of an address."""

    local, separator, domain = address.partition("@")
    if not separator:
        return "<configured>"
    visible = local[:1] if local else ""
    return f"{visible}***@{domain}"


def _resolve_executable(value: str, which: Callable[[str], str | None]) -> str:
    expanded = os.path.expanduser(value.strip())
    resolved = which(expanded)
    return str(Path(resolved).resolve()) if resolved else expanded


def collect_settings(
    existing: Mapping[str, str],
    *,
    input_fn: Input = input,
    which: Callable[[str], str | None] = shutil.which,
) -> dict[str, str]:
    """Prompt for required choices and return a complete environment mapping."""

    current_recipient = existing.get("FORWARD_TO_EMAIL", "").strip()
    if current_recipient:
        prompt = (
            f"Forwarding email [{_masked_address(current_recipient)}; "
            "press Enter to keep]: "
        )
    else:
        prompt = "Forwarding email: "
    recipient = input_fn(prompt).strip() or current_recipient
    if not recipient:
        raise SetupError("A forwarding email address is required.")

    configured_gapi = existing.get("GAPI_BIN", DEFAULT_VALUES["GAPI_BIN"])
    default_gapi = _resolve_executable(configured_gapi, which)
    selected_gapi = input_fn(f"gapi executable [{default_gapi}]: ").strip()
    gapi_bin = _resolve_executable(selected_gapi or default_gapi, which)
    if not gapi_bin:
        raise SetupError("A gapi executable is required.")

    values = dict(DEFAULT_VALUES)
    values.update({key: value for key, value in existing.items() if key in ENV_KEYS})
    values["FORWARD_TO_EMAIL"] = recipient
    values["GAPI_BIN"] = gapi_bin
    for key, value in values.items():
        if "\n" in value or "\r" in value:
            raise SetupError(f"{key} cannot contain a newline.")
    return values


def validate_settings(
    values: Mapping[str, str],
    *,
    sender_factory: Callable[..., object] = GmailSender,
) -> None:
    """Apply the runtime's configuration and recipient validation without sending."""

    try:
        load_settings(values)
        sender_factory(
            values["FORWARD_TO_EMAIL"],
            gapi_bin=values["GAPI_BIN"],
            timeout_seconds=float(values["GAPI_TIMEOUT_SECONDS"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise SetupError(f"Invalid setup value: {exc}") from exc


def verify_gapi_auth(
    gapi_bin: str,
    *,
    timeout_seconds: float = GAPI_CHECK_TIMEOUT_SECONDS,
    runner: Runner = subprocess.run,
) -> None:
    """Confirm read-only Gmail access without retaining command output."""

    argv = [gapi_bin, "gmail", "search", GAPI_CHECK_QUERY, "--max", "1"]
    try:
        result = runner(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout_seconds,
            check=False,
            shell=False,
        )
    except FileNotFoundError as exc:
        raise SetupError(
            "gapi was not found. Install gapi or enter the path to its executable."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise SetupError(
            "The gapi Gmail access check timed out. Confirm gapi is authenticated, "
            "then run setup again."
        ) from exc
    except OSError as exc:
        raise SetupError(
            "gapi could not be started. Check that the configured file is executable."
        ) from exc
    if result.returncode != 0:
        raise SetupError(
            "gapi could not read Gmail. Authenticate gapi for this macOS user, "
            "then run setup again."
        )


def verify_messages_access(
    messages_db: str,
    *,
    reader_factory: Callable[[str], _MessagesReader] = MessagesReader,
) -> None:
    """Confirm the configured Apple Messages database can be queried read-only."""

    try:
        reader_factory(messages_db).current_max_rowid()
    except Exception as exc:
        raise SetupError(
            "Apple Messages could not be read. Grant Full Disk Access to the "
            "Python 3.11 executable running setup, then run setup again."
        ) from exc


def render_env(values: Mapping[str, str]) -> str:
    """Render every supported setting in stable service-compatible order."""

    missing = [key for key in ENV_KEYS if key not in values]
    if missing:
        raise SetupError(f"Missing setup value: {missing[0]}")
    return "".join(f"{key}={values[key]}\n" for key in ENV_KEYS)


def write_env_atomically(contents: str, path: Path) -> None:
    """Replace *path* atomically with a private mode-0600 file."""

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            os.fchmod(temporary.fileno(), 0o600)
            temporary.write(contents)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass


def run_setup(
    *,
    env_path: Path | None = None,
    input_fn: Input = input,
    output_fn: Output = print,
    which: Callable[[str], str | None] = shutil.which,
    runner: Runner = subprocess.run,
    sender_factory: Callable[..., object] = GmailSender,
    reader_factory: Callable[[str], _MessagesReader] = MessagesReader,
    installer: Callable[[], None] = install_service,
) -> None:
    """Validate prerequisites, save configuration, and install the service."""

    destination = APP_ROOT / ".env" if env_path is None else Path(env_path)
    existing = read_dotenv(destination)
    values = collect_settings(existing, input_fn=input_fn, which=which)
    validate_settings(values, sender_factory=sender_factory)

    output_fn("Checking authenticated read-only Gmail access...")
    verify_gapi_auth(values["GAPI_BIN"], runner=runner)
    output_fn("Checking read-only Apple Messages access...")
    verify_messages_access(values["SMS_MESSAGES_DB"], reader_factory=reader_factory)

    write_env_atomically(render_env(values), destination)
    installer()
    output_fn("Setup complete. The forwarding service is installed and running.")


def main() -> int:
    try:
        run_setup()
    except SetupError as exc:
        print(f"Setup failed: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nSetup cancelled.", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
