"""Immutable data models for the SMS forwarder."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Attachment:
    guid: str | None
    filename: str | None
    transfer_name: str | None
    mime_type: str | None
    uti: str | None
    total_bytes: int | None


@dataclass(frozen=True)
class SmsMessage:
    rowid: int
    guid: str
    service: str | None
    sender: str | None
    received_at: str
    text: str | None
    attachments: tuple[Attachment, ...]

@dataclass(frozen=True)
class PendingDelivery:
    guid: str
    rowid: int
    subject: str
    body: str
