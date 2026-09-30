# Local Messages API

A bearer protected, read-only API for messages in macOS Messages. The API reads `~/Library/Messages/chat.db` directly; it does not poll, forward mail, or maintain a delivery queue. It includes both SMS and iMessage records. Attachment file contents are never opened.

## Access

Use the stable gateway at `https://ors-macbook-air.taila51d65.ts.net/login` with the browser username and password stored in the owner's vault. The gateway injects the shared bearer token server side. A logged-in browser can call:

- `GET /sms-api/health` — 200 when the API can read the Messages database; 503 with `messages_unavailable` when it cannot.
- `GET /sms-api/messages?limit=50` — newest incoming messages first, up to 100 per page.
- `GET /sms-api/messages/search?q=code` — case-insensitive search of decoded message text, including `attributedBody` text.

Both message endpoints accept `before=<rowid>` for older messages, `sender=<exact handle>`, `service=SMS|iMessage`, and `direction=incoming|outgoing|all` (default `incoming`). A response contains `messages` and `next_before`; pass the latter as `before` until it is `null`. Each message includes `id`, `rowid`, `service`, `sender`, `received_at` (UTC ISO 8601), `direction`, and `text`. The API excludes system and service metadata rows. The service never logs message bodies, and responses have `Cache-Control: no-store`.

The underlying HTTP service binds to `127.0.0.1:8004`. It requires `SMS_API_TOKEN` or the private `.api_token` provisioned by the local gateway setup. Direct HTTP callers must supply `Authorization: Bearer <token>`; the gateway handles this for browser clients. Keep the token and login credentials outside git.

The Python interpreter running this API needs **Full Disk Access** for `~/Library/Messages/chat.db`. If `/health` reports 503, grant access to the exact interpreter launched by `manage.py`, then restart `com.local.api.sms-api`. The API fails closed if the database cannot be read. It never writes to Messages.

## Local test

```sh
python3.11 -m unittest discover -s tests -p 'test_*.py'
```

The old `sms_forwarder.sqlite3` is historical local data. It is not used by this API and is intentionally left on disk for the owner to remove if desired.
