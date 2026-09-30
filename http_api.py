"""Bearer protected, read-only REST access to Apple Messages."""

from __future__ import annotations

import json
import os
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from local_api_core.auth import verify_bearer_token
from messages_reader import MessagesReader


def messages_path() -> Path:
    return Path(os.environ.get("SMS_MESSAGES_DB", "~/Library/Messages/chat.db")).expanduser()


def health(path: Path) -> tuple[int, dict]:
    """Check actual Messages read permission; no delivery worker is required."""
    try:
        MessagesReader(path).current_max_rowid()
    except (sqlite3.Error, OSError):
        return 503, {"status": "degraded", "reason": "messages_unavailable"}
    return 200, {"status": "ok"}


def list_messages(path: Path, query_string: str) -> tuple[int, dict]:
    parameters = parse_qs(query_string, keep_blank_values=True)
    allowed = {"limit", "before", "q", "sender", "service", "direction"}
    if any(key not in allowed or len(value) != 1 for key, value in parameters.items()):
        return 400, {"error": "invalid_query"}
    try:
        raw_limit = parameters.get("limit", ["50"])[0]
        if not raw_limit.isascii() or not raw_limit.isdecimal():
            raise ValueError
        limit = int(raw_limit)
        raw_before = parameters.get("before", [None])[0]
        if raw_before is not None and (not raw_before.isascii() or not raw_before.isdecimal()):
            raise ValueError
        before = int(raw_before) if raw_before is not None else None
        q = parameters.get("q", [None])[0]
        if q is not None and (not q.strip() or len(q) > 256):
            raise ValueError
        sender = parameters.get("sender", [None])[0]
        service = parameters.get("service", [None])[0]
        if any(value is not None and len(value) > 256 for value in (sender, service)):
            raise ValueError
        direction = parameters.get("direction", ["incoming"])[0]
        messages, next_before = MessagesReader(path).list_messages(
            limit=limit, before=before, query=q, sender=sender,
            service=service, direction=direction,
        )
    except ValueError:
        return 400, {"error": "invalid_query"}
    except (sqlite3.Error, OSError):
        return 503, {"error": "messages_unavailable"}
    return 200, {"messages": messages, "next_before": next_before}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        # The request path can contain a private message search query.
        return

    def do_GET(self) -> None:
        if not verify_bearer_token(self.headers.get("Authorization"), _token()):
            self._send(401, {"error": "unauthorized"})
            return
        parsed = urlsplit(self.path)
        if parsed.path == "/health":
            code, body = health(messages_path())
        elif parsed.path in {"/messages", "/messages/search"}:
            code, body = list_messages(messages_path(), parsed.query)
        else:
            code, body = 404, {"error": "not_found"}
        self._send(code, body)

    def _send(self, code: int, body: dict) -> None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def _token() -> str:
    configured = os.environ.get("SMS_API_TOKEN", "").strip()
    if configured:
        return configured
    try:
        return Path(".api_token").read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return ""


def main() -> None:
    if not _token():
        raise RuntimeError("SMS_API_TOKEN is required")
    host = os.environ.get("SMS_API_HOST", "127.0.0.1")
    port = int(os.environ.get("SMS_API_PORT", "8004"))
    ThreadingHTTPServer((host, port), Handler).serve_forever()


if __name__ == "__main__":
    main()
