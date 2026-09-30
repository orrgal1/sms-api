from __future__ import annotations

import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

from http_api import Handler, health, list_messages


class MessageApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "chat.db"
        with sqlite3.connect(self.path) as db:
            db.executescript("""
                CREATE TABLE handle (ROWID INTEGER PRIMARY KEY, id TEXT);
                CREATE TABLE message (
                    ROWID INTEGER PRIMARY KEY, guid TEXT, text TEXT,
                    attributedBody BLOB, date INTEGER, is_from_me INTEGER,
                    service TEXT, handle_id INTEGER,
                    is_system_message INTEGER DEFAULT 0,
                    is_service_message INTEGER DEFAULT 0
                );
                INSERT INTO handle VALUES (1, '+15551212');
                INSERT INTO message(ROWID,guid,text,date,is_from_me,service,handle_id)
                VALUES (1,'one','first code 123',1000000000000000000,0,'SMS',1);
                INSERT INTO message(ROWID,guid,text,date,is_from_me,service,handle_id)
                VALUES (2,'two','outbound',1000000000000000001,1,'SMS',1);
                INSERT INTO message(ROWID,guid,text,date,is_from_me,service,handle_id)
                VALUES (3,'three','latest code 456',1000000000000000002,0,'SMS',1);
                INSERT INTO message(ROWID,guid,text,date,is_from_me,service,handle_id,is_system_message)
                VALUES (4,'four','system',1000000000000000003,0,'SMS',1,1);
            """)
            encoded = b'NSString\x01\x95\x84\x01\x2b\x0carchive code'
            db.execute("INSERT INTO message(ROWID,guid,text,attributedBody,date,is_from_me,service,handle_id) "
                       "VALUES (5,'five',NULL,?,1000000000000000004,0,'iMessage',1)", (encoded,))

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_recent_and_pagination(self) -> None:
        code, body = list_messages(self.path, "limit=2")
        self.assertEqual(code, 200)
        self.assertEqual([item["id"] for item in body["messages"]], ["five", "three"])
        self.assertEqual(body["next_before"], 3)
        self.assertEqual(body["messages"][0]["text"], "archive code")
        code, next_body = list_messages(self.path, f"before={body['next_before']}&limit=2")
        self.assertEqual(code, 200)
        self.assertEqual([item["id"] for item in next_body["messages"]], ["one"])
        self.assertIsNone(next_body["next_before"])

    def test_search_decodes_attributed_body_and_filters(self) -> None:
        code, body = list_messages(self.path, "q=ARCHIVE&service=iMessage")
        self.assertEqual(code, 200)
        self.assertEqual([item["id"] for item in body["messages"]], ["five"])
        code, body = list_messages(self.path, "direction=all&sender=%2B15551212&q=outbound")
        self.assertEqual(code, 200)
        self.assertEqual(body["messages"][0]["direction"], "outgoing")

    def test_unavailable_database_and_validation(self) -> None:
        self.assertEqual(health(self.path)[0], 200)
        self.assertEqual(health(self.path.with_name("missing.db")),
                         (503, {"status": "degraded", "reason": "messages_unavailable"}))
        self.assertEqual(list_messages(self.path, "limit=0"), (400, {"error": "invalid_query"}))
        self.assertEqual(list_messages(self.path, "direction=sideways"), (400, {"error": "invalid_query"}))
        self.assertEqual(list_messages(self.path, "q=x&q=y"), (400, {"error": "invalid_query"}))

    def test_http_requires_bearer_token(self) -> None:
        with patch.dict("os.environ", {"SMS_API_TOKEN": "test-only-token", "SMS_MESSAGES_DB": str(self.path)}):
            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                url = f"http://127.0.0.1:{server.server_port}/messages"
                with self.assertRaises(HTTPError) as raised:
                    urlopen(url, timeout=2)
                self.assertEqual(raised.exception.code, 401)
                request = Request(url, headers={"Authorization": "Bearer test-only-token"})
                with urlopen(request, timeout=2) as response:
                    self.assertEqual(response.status, 200)
                    self.assertEqual(response.headers["Cache-Control"], "no-store")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
