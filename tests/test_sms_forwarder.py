from __future__ import annotations

import json
import sqlite3
import stat
import tempfile
import unittest
from pathlib import Path

from config import load_settings
from gmail_sender import GmailSender
from messages_reader import MessagesReader
from service import read_dotenv
from state_store import DeliveryStore
from worker import SmsForwarder


class FakeAsyncSender:
    def __init__(self, failures: int = 0):
        self.failures = failures
        self.calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    async def send(self, *args: object, **kwargs: object) -> str:
        self.calls.append((args, kwargs))
        if self.failures:
            self.failures -= 1
            raise RuntimeError("temporary provider failure")
        return f"provider-{len(self.calls)}"

    @staticmethod
    def envelope(call: tuple[tuple[object, ...], dict[str, object]]) -> dict[str, object]:
        for value in (*call[0], *call[1].values()):
            if isinstance(value, str):
                try:
                    decoded = json.loads(value)
                except json.JSONDecodeError:
                    continue
                if isinstance(decoded, dict) and decoded.get("type") == "message.received":
                    return decoded
        raise AssertionError(f"sender call did not contain an envelope: {call!r}")


class SmsForwarderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        self.source_path = root / "chat.db"
        self.state_path = root / "state.db"
        self._create_messages_schema()

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def _create_messages_schema(self) -> None:
        with sqlite3.connect(self.source_path) as db:
            db.executescript(
                """
                CREATE TABLE handle (ROWID INTEGER PRIMARY KEY, id TEXT);
                CREATE TABLE message (
                    ROWID INTEGER PRIMARY KEY, guid TEXT, text TEXT, attributedBody BLOB,
                    date INTEGER, is_from_me INTEGER NOT NULL DEFAULT 0, service TEXT,
                    handle_id INTEGER, is_system_message INTEGER NOT NULL DEFAULT 0,
                    is_service_message INTEGER NOT NULL DEFAULT 0,
                    item_type INTEGER DEFAULT 0,
                    associated_message_type INTEGER DEFAULT 0,
                    cache_has_attachments INTEGER DEFAULT 0
                );
                CREATE TABLE attachment (
                    ROWID INTEGER PRIMARY KEY, guid TEXT, filename TEXT,
                    transfer_name TEXT, mime_type TEXT, uti TEXT, total_bytes INTEGER
                );
                CREATE TABLE message_attachment_join (
                    message_id INTEGER, attachment_id INTEGER
                );
                """
            )
            db.execute("INSERT INTO handle(ROWID, id) VALUES (1, 'sender@example.com')")
            db.execute("INSERT INTO handle(ROWID, id) VALUES (2, '+15551212')")
            db.execute(
                "INSERT INTO message(ROWID,guid,text,date,is_from_me,service,handle_id) "
                "VALUES (1,'old-guid','old',1000000000000000000,0,'SMS',1)"
            )
            # Rows already in history are also useful fixtures for first-poll behavior.
            db.execute(
                "INSERT INTO message(ROWID,guid,text,date,is_from_me,service,handle_id) "
                "VALUES (2,'imessage-guid','imessage',1000000000000000001,0,'iMessage',1)"
            )
            db.execute(
                "INSERT INTO message(ROWID,guid,text,date,is_from_me,service,handle_id) "
                "VALUES (3,'outbound-guid','outbound',1000000000000000002,1,'SMS',2)"
            )
            db.execute(
                "INSERT INTO message(ROWID,guid,text,date,is_from_me,service,handle_id) "
                "VALUES (4,'system-guid','system',1000000000000000003,0,'SMS',NULL)"
            )
            db.commit()

    def _insert_message(
        self, rowid: int, text: str, sender: str = "sender@example.com",
        attachment: bool = False, service: str | None = "SMS", is_from_me: int = 0,
        is_system_message: int = 0, is_service_message: int = 0,
    ) -> None:
        with sqlite3.connect(self.source_path) as db:
            handle_id = db.execute("SELECT ROWID FROM handle WHERE id=?", (sender,)).fetchone()
            if handle_id is None:
                db.execute("INSERT INTO handle(id) VALUES (?)", (sender,))
                handle_id = (db.execute("SELECT last_insert_rowid()").fetchone()[0],)
            db.execute(
                "INSERT INTO message(ROWID,guid,text,date,is_from_me,service,handle_id,"
                "is_system_message,is_service_message) VALUES (?,?,?,?,?,?,?,?,?)",
                (rowid, f"guid-{rowid}", text, 1000000000000000000 + rowid,
                 is_from_me, service, handle_id[0], is_system_message, is_service_message),
            )
            if attachment:
                db.execute(
                    "INSERT INTO attachment(ROWID,guid,filename,transfer_name,uti,mime_type,total_bytes) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (rowid, f"attachment-{rowid}", "photo.jpg", "photo.jpg",
                     "public.jpeg", "image/jpeg", 42),
                )
                db.execute(
                    "INSERT INTO message_attachment_join(message_id,attachment_id) VALUES (?,?)",
                    (rowid, rowid),
                )
            db.commit()

    def _forwarder(self, sender: FakeAsyncSender, batch_size: int = 50) -> tuple[SmsForwarder, MessagesReader, DeliveryStore]:
        reader = MessagesReader(str(self.source_path))
        store = DeliveryStore(str(self.state_path))
        store.initialize_cursor(reader.current_max_rowid())
        return SmsForwarder(reader, store, sender, batch_size=batch_size), reader, store

    async def test_first_uninitialized_poll_skips_history(self) -> None:
        sender = FakeAsyncSender()
        reader = MessagesReader(str(self.source_path))
        store = DeliveryStore(str(self.state_path))
        forwarder = SmsForwarder(reader, store, sender)
        try:
            await forwarder.poll_once()
            self.assertEqual(sender.calls, [])
            self.assertEqual(store.cursor(), reader.current_max_rowid())
        finally:
            store.close()

    async def test_new_inbound_messages_are_oldest_first_and_deterministic(self) -> None:
        sender = FakeAsyncSender()
        forwarder, _, store = self._forwarder(sender)
        try:
            await forwarder.poll_once()
            self._insert_message(10, "second")
            self._insert_message(9, "first")
            self._insert_message(11, "iMessage message", service="iMessage")
            self._insert_message(12, "unknown service", service=None)
            self._insert_message(13, "sent by me", is_from_me=1)
            self._insert_message(14, "system row", is_system_message=1)
            self._insert_message(15, "service row", is_service_message=1)
            await forwarder.poll_once()
            self.assertEqual(len(sender.calls), 4)
            first = {
                "type": "message.received",
                "id": "guid-9",
                "service": "SMS",
                "sender": "sender@example.com",
                "received_at": "2032-09-09T01:46:40+00:00",
                "text": "first",
                "attachments": [],
            }
            second = FakeAsyncSender.envelope(sender.calls[1])
            imessage = FakeAsyncSender.envelope(sender.calls[2])
            unknown = FakeAsyncSender.envelope(sender.calls[3])
            self.assertEqual(FakeAsyncSender.envelope(sender.calls[0]), first)
            self.assertEqual(
                [first["id"], second["id"], imessage["id"], unknown["id"]],
                ["guid-9", "guid-10", "guid-11", "guid-12"],
            )
            self.assertEqual(second["service"], "SMS")
            self.assertEqual(imessage["service"], "iMessage")
            self.assertIsNone(unknown["service"])
            self.assertEqual(
                sender.calls[0][0],
                (
                    "[MESSAGE:SMS] sender@example.com",
                    json.dumps(first, ensure_ascii=False, separators=(",", ":")),
                ),
            )
            self.assertEqual(sender.calls[2][0][0], "[MESSAGE:iMessage] sender@example.com")
            self.assertEqual(sender.calls[3][0][0], "[MESSAGE:unknown] sender@example.com")
        finally:
            store.close()

    async def test_attachment_contains_metadata_not_file_contents(self) -> None:
        sender = FakeAsyncSender()
        forwarder, _, store = self._forwarder(sender)
        try:
            await forwarder.poll_once()
            self._insert_message(10, "see photo", attachment=True)
            await forwarder.poll_once()
            envelope = FakeAsyncSender.envelope(sender.calls[-1])
            self.assertEqual(
                envelope["attachments"],
                [{
                    "guid": "attachment-10",
                    "filename": "photo.jpg",
                    "transfer_name": "photo.jpg",
                    "mime_type": "image/jpeg",
                    "uti": "public.jpeg",
                    "total_bytes": 42,
                }],
            )
        finally:
            store.close()

    async def test_failed_delivery_survives_reopen_and_retries(self) -> None:
        failing = FakeAsyncSender(failures=1)
        forwarder, _, store = self._forwarder(failing)
        await forwarder.poll_once()
        self._insert_message(10, "retry me")
        await forwarder.poll_once()
        self.assertEqual(len(store.pending()), 1)
        store.close()

        retrying = FakeAsyncSender()
        reopened = DeliveryStore(str(self.state_path))
        try:
            await SmsForwarder(MessagesReader(str(self.source_path)), reopened, retrying).poll_once()
            self.assertEqual(len(retrying.calls), 1)
            self.assertEqual(reopened.pending(), [])
        finally:
            reopened.close()

    async def test_sent_delivery_does_not_resend(self) -> None:
        sender = FakeAsyncSender()
        forwarder, _, store = self._forwarder(sender)
        try:
            await forwarder.poll_once()
            self._insert_message(10, "once")
            await forwarder.poll_once()
            await forwarder.poll_once()
            self.assertEqual(len(sender.calls), 1)
        finally:
            store.close()


class ConfigurationTests(unittest.TestCase):
    def test_destination_is_required(self) -> None:
        with self.assertRaisesRegex(ValueError, "^FORWARD_TO_EMAIL is required$"):
            load_settings({})

    def test_destination_is_loaded(self) -> None:
        settings = load_settings({"FORWARD_TO_EMAIL": "recipient@example.com"})
        self.assertEqual(settings.FORWARD_TO_EMAIL, "recipient@example.com")

    def test_invalid_destination_error_does_not_expose_value(self) -> None:
        invalid = "recipient@example.com\nBcc: attacker@example.com"
        with self.assertRaises(ValueError) as caught:
            load_settings({"FORWARD_TO_EMAIL": invalid})
        self.assertEqual(
            str(caught.exception),
            "FORWARD_TO_EMAIL must be a single valid email address",
        )
        self.assertNotIn(invalid, str(caught.exception))


class ServiceEnvironmentTests(unittest.TestCase):
    def test_destination_is_captured_from_dotenv(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            dotenv = Path(directory) / ".env"
            dotenv.write_text(
                "FORWARD_TO_EMAIL=recipient@example.com\n",
                encoding="utf-8",
            )
            self.assertEqual(
                read_dotenv(dotenv),
                {"FORWARD_TO_EMAIL": "recipient@example.com"},
            )


class GmailSenderTests(unittest.IsolatedAsyncioTestCase):
    async def test_configured_recipient_and_exact_argv_without_shell(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            argv_file = root / "argv.json"
            script = root / "gapi"
            script.write_text(
                "#!/usr/bin/env python3\n"
                "import json, pathlib, sys\n"
                f"pathlib.Path({str(argv_file)!r}).write_text(json.dumps(sys.argv[1:]))\n"
                "print(json.dumps({'id': 'provider-123'}))\n",
                encoding="utf-8",
            )
            script.chmod(script.stat().st_mode | stat.S_IXUSR)
            sender = GmailSender("recipient@example.com", str(script))
            provider_id = await sender.send(
                "[MESSAGE:SMS] sender@example.com",
                '{"type":"message.received"}',
            )
            self.assertEqual(provider_id, "provider-123")
            args = json.loads(argv_file.read_text(encoding="utf-8"))
            self.assertEqual(
                args,
                [
                    "gmail", "send", "--to", "recipient@example.com",
                    "--subject", "[MESSAGE:SMS] sender@example.com",
                    "--body", '{"type":"message.received"}',
                ],
            )

    def test_rejects_unsafe_or_multiple_recipients(self) -> None:
        invalid_recipients = (
            "first@example.com,second@example.com",
            "recipient@example.com\nBcc: attacker@example.com",
            "Display Name <recipient@example.com>",
        )
        for recipient in invalid_recipients:
            with self.subTest(recipient=recipient):
                with self.assertRaises(ValueError) as caught:
                    GmailSender(recipient)
                self.assertEqual(
                    str(caught.exception),
                    "recipient must be a single valid email address",
                )
                self.assertNotIn(recipient, str(caught.exception))


if __name__ == "__main__":
    unittest.main()
