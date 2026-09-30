"""Read message text and metadata from Apple's Messages database."""

from __future__ import annotations

from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3

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
    """Read eligible message rows without opening attachment files."""

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

    def list_messages(
        self,
        *,
        limit: int = 50,
        before: int | None = None,
        query: str | None = None,
        sender: str | None = None,
        service: str | None = None,
        direction: str = "incoming",
    ) -> tuple[list[dict], int | None]:
        """Return newest matching Messages rows and a cursor for the next page.

        Search runs after decoding attributedBody, since many macOS messages have
        no plain ``text`` column. Both the source database and attachments stay
        read-only; attachment file contents are never opened.
        """
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        if before is not None and before <= 0:
            raise ValueError("before must be positive")
        if direction not in {"incoming", "outgoing", "all"}:
            raise ValueError("invalid direction")

        needle = query.casefold() if query else None
        found: list[dict] = []
        cursor = before
        with closing(self._connect()) as db:
            columns = self._columns(db, "message")
            guid_expr = "m.guid" if "guid" in columns else "CAST(m.ROWID AS TEXT)"
            attributed_expr = "m.attributedBody" if "attributedBody" in columns else "NULL"
            system_clause = "COALESCE(m.is_system_message, 0) = 0" if "is_system_message" in columns else "1"
            service_clause = "COALESCE(m.is_service_message, 0) = 0" if "is_service_message" in columns else "1"
            while True:
                conditions = [system_clause, service_clause]
                params: list[object] = []
                if cursor is not None:
                    conditions.append("m.ROWID < ?")
                    params.append(cursor)
                if direction != "all":
                    conditions.append("m.is_from_me = ?")
                    params.append(0 if direction == "incoming" else 1)
                if sender:
                    conditions.append("h.id = ?")
                    params.append(sender)
                if service:
                    conditions.append("m.service = ?")
                    params.append(service)
                params.append(max(100, limit + 1))
                rows = db.execute(
                    f"SELECT m.ROWID, {guid_expr}, m.service, h.id, m.date, "
                    f"m.text, {attributed_expr}, m.is_from_me "
                    "FROM message AS m LEFT JOIN handle AS h ON h.ROWID = m.handle_id "
                    f"WHERE {' AND '.join(conditions)} ORDER BY m.ROWID DESC LIMIT ?",
                    params,
                ).fetchall()
                if not rows:
                    break
                for rowid, guid, msg_service, msg_sender, date_value, text, attributed, from_me in rows:
                    cursor = int(rowid)
                    message_text = text if isinstance(text, str) and text else _decode_typedstream_string(attributed)
                    if needle and needle not in (message_text or "").casefold():
                        continue
                    seconds, nanoseconds = divmod(int(date_value or 0), 1_000_000_000)
                    received = _APPLE_EPOCH + timedelta(seconds=seconds, microseconds=nanoseconds // 1_000)
                    found.append({
                        "id": str(guid) if guid is not None else str(rowid),
                        "rowid": int(rowid),
                        "service": msg_service,
                        "sender": msg_sender,
                        "received_at": received.isoformat(),
                        "direction": "outgoing" if from_me else "incoming",
                        "text": message_text,
                    })
                    if len(found) > limit:
                        return found[:limit], found[limit - 1]["rowid"]
                if len(rows) < max(100, limit + 1):
                    break
        return found, None
