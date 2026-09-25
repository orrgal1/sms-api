"""Read incoming message metadata from Apple's Messages database."""

from __future__ import annotations

from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3


from models import Attachment, SmsMessage


_APPLE_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)
_TYPEDSTREAM_MARKER = b"NSString"
_TYPEDSTREAM_HEADER = b"\x01\x95\x84\x01\x2b"
_MAX_DECODED_TEXT = 1_048_576
_MAX_ARCHIVE_BYTES = 8_388_608


def _decode_typedstream_string(blob: bytes | bytearray | memoryview | None) -> str | None:
    """Decode the bounded NSString representation seen in attributedBody."""
    if not isinstance(blob, (bytes, bytearray, memoryview)):
        return None
    if len(blob) > _MAX_ARCHIVE_BYTES:
        return None
    try:
        data = bytes(blob)
    except (TypeError, ValueError):
        return None
    marker = data.find(_TYPEDSTREAM_MARKER)
    if marker < 0:
        return None
    pos = marker + len(_TYPEDSTREAM_MARKER)
    if not data.startswith(_TYPEDSTREAM_HEADER, pos):
        return None
    pos += len(_TYPEDSTREAM_HEADER)
    if pos >= len(data):
        return None

    length_tag = data[pos]
    pos += 1
    if length_tag <= 0x7F:
        length = length_tag
    elif length_tag == 0x81:
        if pos + 2 > len(data):
            return None
        length = int.from_bytes(data[pos : pos + 2], "little")
        pos += 2
    elif length_tag == 0x82:
        if pos + 4 > len(data):
            return None
        length = int.from_bytes(data[pos : pos + 4], "little")
        pos += 4
    else:
        return None

    if length <= 0 or length > _MAX_DECODED_TEXT or pos + length > len(data):
        return None
    try:
        return data[pos : pos + length].decode("utf-8")
    except UnicodeDecodeError:
        return None


class MessagesReader:
    """Read eligible incoming message rows without opening attachment files."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path).expanduser()

    def _connect(self) -> sqlite3.Connection:
        # Path.as_uri handles spaces and other path characters safely. URI mode
        # and query_only together prevent accidental writes to chat.db.
        uri = self._path.absolute().as_uri() + "?mode=ro"
        db = sqlite3.connect(uri, uri=True)
        db.execute("PRAGMA query_only=ON")
        return db

    @staticmethod
    def _columns(db: sqlite3.Connection, table: str) -> set[str]:
        return {str(row[1]) for row in db.execute(f"PRAGMA table_info({table})")}

    def current_max_rowid(self) -> int:
        with closing(self._connect()) as db:
            row = db.execute("SELECT COALESCE(MAX(ROWID), 0) FROM message").fetchone()
            return int(row[0] or 0)

    def read_after(self, rowid: int, limit: int) -> list[SmsMessage]:
        if limit <= 0:
            return []
        with closing(self._connect()) as db:
            columns = self._columns(db, "message")
            guid_expr = "m.guid" if "guid" in columns else "CAST(m.ROWID AS TEXT)"
            attributed_expr = "m.attributedBody" if "attributedBody" in columns else "NULL"
            system_expr = "COALESCE(m.is_system_message, 0) = 0" if "is_system_message" in columns else "1"
            service_expr = "COALESCE(m.is_service_message, 0) = 0" if "is_service_message" in columns else "1"
            rows = db.execute(
                f"""
                SELECT m.ROWID, {guid_expr}, m.service, h.id, m.date, m.text, {attributed_expr}
                  FROM message AS m
                  LEFT JOIN handle AS h ON h.ROWID = m.handle_id
                 WHERE m.ROWID > ?
                   AND m.is_from_me = 0
                   AND {system_expr}
                   AND {service_expr}
                 ORDER BY m.ROWID ASC
                 LIMIT ?
                """,
                (rowid, limit),
            ).fetchall()
            if not rows:
                return []

            row_ids = [int(item[0]) for item in rows]
            placeholders = ",".join("?" for _ in row_ids)
            attachment_columns = self._columns(db, "attachment")
            attachment_guid = "a.guid" if "guid" in attachment_columns else "NULL"
            filename = "a.filename" if "filename" in attachment_columns else "NULL"
            transfer_name = "a.transfer_name" if "transfer_name" in attachment_columns else "NULL"
            mime_type = "a.mime_type" if "mime_type" in attachment_columns else "NULL"
            uti = "a.uti" if "uti" in attachment_columns else "NULL"
            total_bytes = "a.total_bytes" if "total_bytes" in attachment_columns else "NULL"
            attachment_rows = db.execute(
                f"""
                SELECT j.message_id, a.ROWID, {attachment_guid}, {filename},
                       {transfer_name}, {mime_type}, {uti}, {total_bytes}
                  FROM message_attachment_join AS j
                  JOIN attachment AS a ON a.ROWID = j.attachment_id
                 WHERE j.message_id IN ({placeholders})
                 ORDER BY a.ROWID ASC
                """,
                row_ids,
            ).fetchall()

        attachments: dict[int, list[Attachment]] = {message_id: [] for message_id in row_ids}
        for message_id, _attachment_rowid, guid, filename, transfer_name, mime_type, uti, total_bytes in attachment_rows:
            attachments[int(message_id)].append(
                Attachment(
                    guid=str(guid) if guid is not None else None,
                    filename=str(filename) if filename is not None else None,
                    transfer_name=str(transfer_name) if transfer_name is not None else None,
                    mime_type=str(mime_type) if mime_type is not None else None,
                    uti=str(uti) if uti is not None else None,
                    total_bytes=int(total_bytes) if total_bytes is not None else None,
                )
            )

        result: list[SmsMessage] = []
        for message_rowid, guid, service, sender, date_value, text, attributed_body in rows:
            if isinstance(text, str) and text:
                message_text = text
            else:
                message_text = _decode_typedstream_string(attributed_body)
            date_nanoseconds = int(date_value or 0)
            seconds, nanoseconds = divmod(date_nanoseconds, 1_000_000_000)
            received = _APPLE_EPOCH + timedelta(
                seconds=seconds, microseconds=nanoseconds // 1_000
            )
            result.append(
                SmsMessage(
                    rowid=int(message_rowid),
                    guid=str(guid) if guid is not None else str(message_rowid),
                    service=str(service) if service is not None else None,
                    sender=str(sender) if sender is not None else None,
                    received_at=received.isoformat(),
                    text=message_text,
                    attachments=tuple(attachments[int(message_rowid)]),
                )
            )
        return result
