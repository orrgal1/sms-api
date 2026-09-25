"""Runtime configuration for the macOS SMS forwarder."""

from __future__ import annotations

from dataclasses import dataclass
import logging
import math
import os
import re
from collections.abc import Mapping


_DEFAULT_MESSAGES_DB = "~/Library/Messages/chat.db"
_DEFAULT_STATE_DB = "./sms_forwarder.sqlite3"
_DEFAULT_POLL_SECONDS = 5.0
_DEFAULT_BATCH_SIZE = 100
_DEFAULT_GAPI_BIN = "gapi"
_DEFAULT_GAPI_TIMEOUT_SECONDS = 120.0
_DEFAULT_LOG_LEVEL = "INFO"
_EMAIL_ADDRESS = re.compile(
    r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+"
    r"(?:\.[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+)*"
    r"@(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+"
    r"[A-Za-z]{2,63}\Z",
    re.ASCII,
)


@dataclass(frozen=True, slots=True)
class Settings:
    """Validated, immutable runtime settings."""

    FORWARD_TO_EMAIL: str
    SMS_MESSAGES_DB: str
    SMS_STATE_DB: str
    SMS_POLL_SECONDS: float
    SMS_BATCH_SIZE: int
    GAPI_BIN: str
    GAPI_TIMEOUT_SECONDS: float
    LOG_LEVEL: str


def _nonempty(env: Mapping[str, str], name: str, default: str) -> str:
    value = env.get(name, default)
    if not value.strip():
        raise ValueError(f"{name} must be nonempty")
    return value

def validate_email_recipient(value: str) -> str:
    """Return a safe single recipient or raise an error that omits its value."""

    if (
        not isinstance(value, str)
        or len(value) > 254
        or len(value.partition("@")[0]) > 64
        or _EMAIL_ADDRESS.fullmatch(value) is None
    ):
        raise ValueError("recipient must be a single valid email address")
    return value


def _positive_number(env: Mapping[str, str], name: str, default: float) -> float:
    raw = env.get(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a finite positive number") from exc
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be a finite positive number")
    return value


def _positive_integer(env: Mapping[str, str], name: str, default: int) -> int:
    raw = env.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a positive integer") from exc
    if str(raw).strip() != str(value):
        raise ValueError(f"{name} must be a positive integer")
    if value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    """Load and validate settings from *env*, defaulting to ``os.environ``."""

    source = os.environ if env is None else env
    recipient = source.get("FORWARD_TO_EMAIL")
    if recipient is None or not recipient.strip():
        raise ValueError("FORWARD_TO_EMAIL is required")
    try:
        recipient = validate_email_recipient(recipient)
    except ValueError:
        raise ValueError(
            "FORWARD_TO_EMAIL must be a single valid email address"
        ) from None

    level = _nonempty(source, "LOG_LEVEL", _DEFAULT_LOG_LEVEL).upper()
    valid_levels = {name for name in logging.getLevelNamesMapping() if name.isalpha()}
    if level not in valid_levels:
        raise ValueError(f"LOG_LEVEL must be a standard logging level name")

    return Settings(
        FORWARD_TO_EMAIL=recipient,
        SMS_MESSAGES_DB=_nonempty(source, "SMS_MESSAGES_DB", _DEFAULT_MESSAGES_DB),
        SMS_STATE_DB=_nonempty(source, "SMS_STATE_DB", _DEFAULT_STATE_DB),
        SMS_POLL_SECONDS=_positive_number(source, "SMS_POLL_SECONDS", _DEFAULT_POLL_SECONDS),
        SMS_BATCH_SIZE=_positive_integer(source, "SMS_BATCH_SIZE", _DEFAULT_BATCH_SIZE),
        GAPI_BIN=_nonempty(source, "GAPI_BIN", _DEFAULT_GAPI_BIN),
        GAPI_TIMEOUT_SECONDS=_positive_number(
            source, "GAPI_TIMEOUT_SECONDS", _DEFAULT_GAPI_TIMEOUT_SECONDS
        ),
        LOG_LEVEL=level,
    )
