# macOS Messages Email Forwarder

A small, local macOS service that reads incoming messages from Apple Messages and forwards them to an email address through an already authenticated `gapi` installation. The project uses only the Python 3.11+ standard library at runtime. It does not run an HTTP server or require a hosted component.

## How it works

The forwarder opens the Apple Messages database (`~/Library/Messages/chat.db`) read-only, polls for new incoming rows, stages complete email payloads in a local SQLite database, and sends them one at a time. It accepts all incoming services represented by Messages, including iMessage and SMS. Outgoing messages, system messages, and service metadata rows are excluded. The Messages database is never modified.

On the first run with a new state database, the cursor is initialized to the current highest Messages row ID. Existing history is therefore skipped; only messages arriving afterward are forwarded.

Attachment files are never opened, copied, attached, or forwarded. Attachment metadata recorded by Messages may be included in the JSON body.

## Requirements

- macOS with Apple Messages receiving the conversations you want to forward
- Python 3.11 or newer
- `gapi`, installed and authenticated for Gmail by the macOS user who runs the service
- Full Disk Access for the exact Python interpreter used by the service

### Identify the Python interpreter for Full Disk Access

Use the same interpreter for setup, foreground runs, and service installation. Resolve its actual executable path without relying on a machine-specific path:

```sh
python3.11 -c 'import os, sys; print(os.path.realpath(sys.executable))'
python3.11 --version
```

In **System Settings → Privacy & Security → Full Disk Access**, add and enable the Python binary printed by the first command. Adding only Terminal is not sufficient: launchd starts Python directly. After changing Full Disk Access, reinstall the service or restart any foreground worker.

### Authenticate gapi

This project does not perform Google OAuth. Install `gapi` and complete its authentication flow for the macOS account that will run the LaunchAgent. Verify the authenticated account can access Gmail using a harmless read-only command supported by your installation, for example:

```sh
gapi gmail search "newer_than:1d" --max 1
```

The setup flow accepts either `gapi` (resolved from `PATH`) or an absolute executable path. Check the path with:

```sh
command -v gapi
```

## Setup

Clone or download this repository, then run the interactive setup with the interpreter that has Full Disk Access:

```sh
python3.11 configure.py
```

Configuration prompts for the destination email address (an existing value is masked rather than printed) and an optional `gapi` path (defaulting to the `PATH`-resolved `gapi`). It validates the address locally, runs a read-only Gmail check, verifies read-only Messages access, writes a mode-0600 `.env` atomically, and installs and starts the per-user LaunchAgent. If validation fails, configuration stops before writing `.env` or installing the service; follow the reported `gapi` authentication or Full Disk Access remediation. Re-run configuration after changing the destination, Python installation, repository location, or other settings.

For a manual configuration, copy the example and edit the destination:

```sh
cp .env.example .env
chmod 600 .env
```

`FORWARD_TO_EMAIL` is required. Other settings and their defaults are:

| Variable | Default | Purpose |
| --- | --- | --- |
| `FORWARD_TO_EMAIL` | *(required)* | One destination email address |
| `SMS_MESSAGES_DB` | `~/Library/Messages/chat.db` | Apple Messages source database |
| `SMS_STATE_DB` | `./sms_forwarder.sqlite3` | Durable cursor and delivery queue |
| `SMS_POLL_SECONDS` | `5` | Delay between polls |
| `SMS_BATCH_SIZE` | `100` | Maximum source rows read per poll |
| `GAPI_BIN` | `gapi` | `gapi` executable or absolute path |
| `GAPI_TIMEOUT_SECONDS` | `120` | Send-command timeout |
| `LOG_LEVEL` | `INFO` | Python logging level |

## Run in the foreground

A foreground run is useful for troubleshooting. Load the private `.env` into the process environment and start the same Python interpreter granted Full Disk Access:

```sh
set -a
. ./.env
set +a
python3.11 main.py
```

Stop it with `Control-C`.

## Install and manage the LaunchAgent

The service uses the existing compatibility label `com.local.sms-forwarder` and writes its per-user plist to:

```text
~/Library/LaunchAgents/com.local.sms-forwarder.plist
```

The installer stores the documented `.env` settings in the plist and records absolute paths for the selected Python interpreter and this repository's `main.py`. Install with the interpreter granted Full Disk Access. Reinstall after changing `.env`, Python installations, or the repository location.

Install and start:

```sh
python3.11 service.py install
```

Inspect status:

```sh
python3.11 service.py status
```

Follow the combined service log:

```sh
tail -f forwarder.log
```

Stop the service and remove its plist:

```sh
python3.11 service.py uninstall
```

## Forwarded event format

Each email subject is `[MESSAGE:<service>] <sender>`, using `unknown` when a service or sender is missing. The body is compact, deterministic JSON with this shape (the values below are fictional):

```json
{
  "type": "message.received",
  "id": "example-message-guid-001",
  "service": "iMessage",
  "sender": "+15550001111",
  "received_at": "2026-01-02T03:04:05+00:00",
  "text": "Example message",
  "attachments": [
    {
      "guid": "example-attachment-guid-001",
      "filename": "~/Library/Messages/Attachments/example/photo.jpg",
      "transfer_name": "photo.jpg",
      "mime_type": "image/jpeg",
      "uti": "public.jpeg",
      "total_bytes": 12345
    }
  ]
}
```

Attachment fields may be `null` when Messages did not record that metadata. Referenced files are never opened or forwarded.

## Delivery semantics

The separate state database stores the source cursor and complete pending payloads. Messages are staged transactionally before delivery. Failed sends remain pending across restarts, and a row is marked sent only after `gapi` reports success. Successfully marked deliveries are not resent.

This is **at-least-once** delivery, not exactly-once delivery. A duplicate is possible if Gmail accepts a message and the process exits before the local success marker is committed. Consumers that need deduplication should use the stable event `id`.

## Privacy and security

- Keep `.env`, `sms_forwarder.sqlite3*`, and `forwarder.log` private; they can contain message content or recipient settings.
- The Messages database is opened read-only.
- Message text is sent to the configured Gmail destination through `gapi`; it is not written to application logs.
- Attachment contents are not read or transmitted.
- The LaunchAgent runs as your macOS user and inherits that user's access to Messages, Gmail credentials, and local files.
- Review the `gapi` executable and its authentication account before enabling the service.

## Troubleshooting

- **Messages database access fails:** confirm Full Disk Access is granted to the exact executable printed by the `python3.11 -c` command, then reinstall the LaunchAgent with that interpreter.
- **`gapi` cannot send:** run the read-only Gmail verification command, check `command -v gapi`, and set `GAPI_BIN` to the authenticated executable path in `.env`; reinstall afterward.
- **No old messages are forwarded:** this is expected for a new state database. The initial cursor intentionally skips existing history.
- **A failed send keeps retrying:** inspect `forwarder.log` and fix the underlying `gapi` or connectivity problem. Pending rows are retained for retry.
- **Settings changes have no effect:** launchd receives a snapshot of `.env`; reinstall with `python3.11 service.py install`.

## Tests

Run the standard-library test suite with:

```sh
python3.11 -m unittest discover -s tests -p 'test_*.py'
```

## License

This project is licensed under the MIT License; see [LICENSE](LICENSE).
