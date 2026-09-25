"""Small async adapter for the local gapi Gmail command."""

from __future__ import annotations

import asyncio
import json
import math
import re
from typing import Any
from config import validate_email_recipient


_MAX_CAPTURED_OUTPUT = 64 * 1024
_CLEANUP_TIMEOUT_SECONDS = 2.0
_SUCCESS_TOKEN = "sent"
_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+=-]{0,255}$")


class GmailSender:
    """Send mail through gapi without exposing message data in errors."""

    def __init__(
        self,
        recipient: str,
        gapi_bin: str = "gapi",
        timeout_seconds: float = 120,
    ) -> None:
        self._recipient = validate_email_recipient(recipient)
        if not gapi_bin.strip():
            raise ValueError("gapi_bin must be nonempty")
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be finite and positive")
        self._gapi_bin = gapi_bin
        self._timeout_seconds = timeout_seconds

    async def send(self, subject: str, body: str) -> str:
        argv = (
            self._gapi_bin,
            "gmail",
            "send",
            "--to",
            self._recipient,
            "--subject",
            subject,
            "--body",
            body,
        )
        try:
            return await asyncio.wait_for(self._send_argv(argv), self._timeout_seconds)
        except asyncio.TimeoutError as exc:
            raise RuntimeError("gapi send timed out") from exc

    async def _send_argv(self, argv: tuple[str, ...]) -> str:
        creation = asyncio.create_task(
            asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        )
        try:
            process = await asyncio.shield(creation)
        except asyncio.CancelledError as cancelled:
            try:
                process = await creation
            except BaseException:
                raise cancelled
            await _terminate_and_reap(process, [])
            raise
        except FileNotFoundError as exc:
            raise RuntimeError("gapi executable not found") from exc
        except OSError as exc:
            raise RuntimeError("unable to start gapi") from exc

        readers = [
            asyncio.create_task(_read_bounded(process.stdout)),
            asyncio.create_task(_read_bounded(process.stderr)),
        ]
        try:
            returncode = await process.wait()
            stdout, _stderr = await asyncio.gather(*readers)
        except asyncio.CancelledError:
            cleanup = asyncio.create_task(_terminate_and_reap(process, readers))
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                await cleanup
            raise

        if returncode != 0:
            raise RuntimeError("gapi send failed")
        return _identifier_from_output(stdout)


async def _read_bounded(stream: asyncio.StreamReader | None) -> bytes:
    if stream is None:
        return b""
    captured = bytearray()
    while True:
        chunk = await stream.read(8192)
        if not chunk:
            return bytes(captured)
        remaining = _MAX_CAPTURED_OUTPUT - len(captured)
        if remaining > 0:
            captured.extend(chunk[:remaining])


async def _terminate_and_reap(
    process: asyncio.subprocess.Process,
    readers: list[asyncio.Task[bytes]],
) -> None:
    if process.returncode is None:
        try:
            process.terminate()
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(process.wait(), _CLEANUP_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
            await process.wait()
    else:
        await process.wait()
    await asyncio.gather(*readers, return_exceptions=True)


def _identifier_from_output(output: bytes) -> str:
    """Extract only a provider identifier; never return arbitrary provider output."""
    try:
        parsed: Any = json.loads(output.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        parsed = None

    identifier = _find_identifier(parsed)
    if identifier is not None:
        return identifier

    for match in re.finditer(r"\b(?:message[_ -]?id|id)\s*[:=]\s*([A-Za-z0-9][A-Za-z0-9._:/+=-]{0,255})", output.decode("utf-8", "ignore")):
        value = match.group(1)
        if _SAFE_IDENTIFIER.fullmatch(value):
            return value
    return _SUCCESS_TOKEN


def _find_identifier(value: Any) -> str | None:
    if isinstance(value, dict):
        for key in ("id", "messageId", "message_id"):
            candidate = value.get(key)
            if isinstance(candidate, str) and _SAFE_IDENTIFIER.fullmatch(candidate):
                return candidate
        for child in value.values():
            found = _find_identifier(child)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_identifier(child)
            if found is not None:
                return found
    return None
