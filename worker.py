"""Polling and serialized delivery for the SMS forwarder."""

from __future__ import annotations

import asyncio
import json
import math
import logging
from typing import Any


logger = logging.getLogger(__name__)


class SmsForwarder:
    """Move incoming Messages events through durable state to Gmail."""

    def __init__(
        self,
        reader: Any,
        store: Any,
        sender: Any,
        poll_seconds: float = 5,
        batch_size: int = 100,
    ) -> None:
        if not math.isfinite(poll_seconds) or poll_seconds <= 0:
            raise ValueError("poll_seconds must be finite and positive")
        if not isinstance(batch_size, int) or isinstance(batch_size, bool) or batch_size <= 0:
            raise ValueError("batch_size must be a positive integer")

        self._reader = reader
        self._store = store
        self._sender = sender
        self._poll_seconds = poll_seconds
        self._batch_size = batch_size

    @staticmethod
    def _payload(message: Any) -> tuple[str, str]:
        sender = message.sender or ""
        service = message.service or "unknown"
        subject = f"[MESSAGE:{service}] {sender.strip() or 'unknown'}"
        event = {
            "type": "message.received",
            "id": message.guid,
            "service": message.service,
            "sender": sender,
            "received_at": message.received_at,
            "text": message.text,
            "attachments": [
                {
                    "guid": attachment.guid,
                    "filename": attachment.filename,
                    "transfer_name": attachment.transfer_name,
                    "mime_type": attachment.mime_type,
                    "uti": attachment.uti,
                    "total_bytes": attachment.total_bytes,
                }
                for attachment in message.attachments
            ],
        }
        body = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
        return subject, body

    async def _drain_pending(self) -> tuple[int, bool]:
        """Send pending rows oldest-first, stopping at the first failure."""

        delivered = 0
        while True:
            rows = self._store.pending(self._batch_size)
            if not rows:
                return delivered, True

            for row in rows:
                try:
                    provider_id = await self._sender.send(row.subject, row.body)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning(
                        "SMS delivery failed (%s); the pending queue will be retried",
                        type(exc).__name__,
                    )
                    return delivered, False

                self._store.mark_sent(row.guid, provider_id)
                delivered += 1

    async def poll_once(self) -> int:
        """Run one ordered retry/read/stage/deliver cycle."""

        if not self._store.initialized():
            self._store.initialize_cursor(self._reader.current_max_rowid())
            logger.info("Initialized SMS cursor at the current source position")
            return 0

        delivered, drained = await self._drain_pending()
        if not drained:
            return delivered

        messages = self._reader.read_after(self._store.cursor(), self._batch_size)
        if not messages:
            return delivered

        staged = []
        for message in messages:
            subject, body = self._payload(message)
            staged.append((message, subject, body))
        self._store.stage_batch(staged)

        newly_delivered, _ = await self._drain_pending()
        return delivered + newly_delivered

    async def run_forever(self) -> None:
        """Poll until the task is cancelled, keeping transient failures retryable."""

        logger.info("Messages forwarder started")
        try:
            while True:
                try:
                    delivered = await self.poll_once()
                    if delivered:
                        logger.info("Forwarded %d message(s)", delivered)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.error("Messages poll failed (%s)", type(exc).__name__)
                await asyncio.sleep(self._poll_seconds)
        finally:
            logger.info("Messages forwarder stopped")
