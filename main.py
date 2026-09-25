"""Executable entry point for the macOS Messages-to-email forwarder."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
import sys

from config import Settings, load_settings
from gmail_sender import GmailSender
from messages_reader import MessagesReader
from state_store import DeliveryStore
from worker import SmsForwarder


logger = logging.getLogger(__name__)


def _expanded(path: str) -> str:
    return str(Path(path).expanduser().resolve())


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=logging.getLevelNamesMapping()[level],
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


async def run(settings: Settings) -> None:
    """Build the runtime graph and run it until cancellation."""

    reader = MessagesReader(_expanded(settings.SMS_MESSAGES_DB))
    store = DeliveryStore(_expanded(settings.SMS_STATE_DB))
    sender = GmailSender(
        settings.FORWARD_TO_EMAIL,
        gapi_bin=settings.GAPI_BIN,
        timeout_seconds=settings.GAPI_TIMEOUT_SECONDS,
    )
    forwarder = SmsForwarder(
        reader,
        store,
        sender,
        poll_seconds=settings.SMS_POLL_SECONDS,
        batch_size=settings.SMS_BATCH_SIZE,
    )

    try:
        await forwarder.run_forever()
    finally:
        store.close()


def main() -> int:
    try:
        settings = load_settings()
    except ValueError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    configure_logging(settings.LOG_LEVEL)
    try:
        asyncio.run(run(settings))
    except KeyboardInterrupt:
        logger.info("Interrupted; shutting down")
        return 0
    except Exception as exc:
        logger.error("SMS forwarder stopped unexpectedly (%s)", type(exc).__name__)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
