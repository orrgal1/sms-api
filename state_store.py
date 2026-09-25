"""Durable SQLite state for SMS delivery."""

from __future__ import annotations

import os
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Sequence

from models import PendingDelivery, SmsMessage


class DeliveryStore:
    """Persist the source cursor and at-least-once delivery queue."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        raw_path = os.fspath(path)
        expanded = os.path.expandvars(os.path.expanduser(raw_path))
        if expanded != ":memory:":
            parent = os.path.dirname(os.path.abspath(expanded))
            if parent:
                os.makedirs(parent, exist_ok=True)

        self._lock = threading.RLock()
        self._connection = sqlite3.connect(expanded, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA synchronous=NORMAL")
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS delivery_cursor (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                source_rowid INTEGER NOT NULL
            )
            """
        )
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS pending_delivery (
                guid TEXT PRIMARY KEY,
                source_rowid INTEGER NOT NULL,
                subject TEXT NOT NULL,
                body TEXT NOT NULL,
                sent_provider_id TEXT,
                sent_at TEXT
            )
            """
        )
        self._connection.execute(
            "CREATE INDEX IF NOT EXISTS pending_delivery_unsent_order "
            "ON pending_delivery (source_rowid) WHERE sent_at IS NULL"
        )
        self._connection.commit()

    def initialized(self) -> bool:
        with self._lock:
            row = self._connection.execute(
                "SELECT 1 FROM delivery_cursor WHERE singleton = 1"
            ).fetchone()
            return row is not None

    def initialize_cursor(self, rowid: int) -> None:
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                self._connection.execute(
                    "INSERT OR IGNORE INTO delivery_cursor(singleton, source_rowid) "
                    "VALUES (1, ?)",
                    (rowid,),
                )
                self._connection.commit()
            except Exception:
                self._connection.rollback()
                raise

    def cursor(self) -> int:
        with self._lock:
            row = self._connection.execute(
                "SELECT source_rowid FROM delivery_cursor WHERE singleton = 1"
            ).fetchone()
            if row is None:
                raise RuntimeError("delivery cursor has not been initialized")
            return int(row[0])

    def stage_batch(
        self, batch: Sequence[tuple[SmsMessage, str, str]]
    ) -> None:
        if not batch:
            return
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                for message, subject, body in batch:
                    self._connection.execute(
                        """
                        INSERT INTO pending_delivery
                            (guid, source_rowid, subject, body)
                        VALUES (?, ?, ?, ?)
                        ON CONFLICT(guid) DO NOTHING
                        """,
                        (message.guid, message.rowid, subject, body),
                    )
                max_rowid = max(message.rowid for message, _, _ in batch)
                self._connection.execute(
                    """
                    INSERT INTO delivery_cursor(singleton, source_rowid)
                    VALUES (1, ?)
                    ON CONFLICT(singleton) DO UPDATE SET source_rowid =
                        MAX(source_rowid, excluded.source_rowid)
                    """,
                    (max_rowid,),
                )
                self._connection.commit()
            except Exception:
                self._connection.rollback()
                raise

    def pending(self, limit: int = 100) -> list[PendingDelivery]:
        if limit < 0:
            raise ValueError("limit must be non-negative")
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT guid, source_rowid, subject, body
                FROM pending_delivery
                WHERE sent_at IS NULL
                ORDER BY source_rowid ASC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [
                PendingDelivery(
                    guid=row["guid"],
                    rowid=int(row["source_rowid"]),
                    subject=row["subject"],
                    body=row["body"],
                )
                for row in rows
            ]

    def mark_sent(self, guid: str, provider_id: str) -> None:
        sent_at = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self._connection.execute(
                """
                UPDATE pending_delivery
                SET sent_provider_id = ?, sent_at = ?
                WHERE guid = ? AND sent_at IS NULL
                """,
                (provider_id, sent_at, guid),
            )
            self._connection.commit()

    def close(self) -> None:
        with self._lock:
            if self._connection is None:
                return
            connection = self._connection
            self._connection = None
            connection.close()
