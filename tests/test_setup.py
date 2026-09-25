from __future__ import annotations

import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

from configure import (
    ENV_KEYS,
    GAPI_CHECK_QUERY,
    SetupError,
    collect_settings,
    run_setup,
)


class _ReadableMessages:
    def __init__(self, path: str) -> None:
        self.path = path

    def current_max_rowid(self) -> int:
        return 7



class SetupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.env_path = self.root / ".env"
        self.installs = 0

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def _install(self) -> None:
        self.installs += 1

    @staticmethod
    def _inputs(*values: str):
        entries = iter(values)
        return lambda _prompt: next(entries)

    @staticmethod
    def _successful_runner(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0)

    @staticmethod
    def _which(value: str) -> str | None:
        if value == "gapi":
            return "/example/bin/gapi"
        return value

    def test_invalid_destination_stops_before_external_checks_or_install(self) -> None:
        runner_called = False

        def runner(argv, **kwargs):
            nonlocal runner_called
            runner_called = True
            return subprocess.CompletedProcess(argv, 0)

        with self.assertRaisesRegex(SetupError, "Invalid setup value"):
            run_setup(
                env_path=self.env_path,
                input_fn=self._inputs("not-an-address", ""),
                output_fn=lambda _message: None,
                which=self._which,
                runner=runner,
                reader_factory=_ReadableMessages,
                installer=self._install,
            )

        self.assertFalse(runner_called)
        self.assertEqual(self.installs, 0)
        self.assertFalse(self.env_path.exists())

    def test_gapi_failure_stops_before_messages_check_write_or_install(self) -> None:
        reader_called = False

        def failed_runner(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 1)

        def reader_factory(path: str):
            nonlocal reader_called
            reader_called = True
            return _ReadableMessages(path)

        with self.assertRaisesRegex(SetupError, "Authenticate gapi"):
            run_setup(
                env_path=self.env_path,
                input_fn=self._inputs("you@example.com", ""),
                output_fn=lambda _message: None,
                which=self._which,
                runner=failed_runner,
                reader_factory=reader_factory,
                installer=self._install,
            )

        self.assertFalse(reader_called)
        self.assertEqual(self.installs, 0)
        self.assertFalse(self.env_path.exists())

    def test_messages_access_failure_stops_before_write_or_install(self) -> None:
        observed_command: list[str] = []
        observed_options: dict[str, object] = {}

        def runner(argv, **kwargs):
            observed_command.extend(argv)
            observed_options.update(kwargs)
            return subprocess.CompletedProcess(argv, 0)

        class UnreadableMessages:
            def __init__(self, path: str) -> None:
                self.path = path

            def current_max_rowid(self) -> int:
                raise PermissionError("denied")

        with self.assertRaisesRegex(SetupError, "Full Disk Access"):
            run_setup(
                env_path=self.env_path,
                input_fn=self._inputs("you@example.com", ""),
                output_fn=lambda _message: None,
                which=self._which,
                runner=runner,
                reader_factory=UnreadableMessages,
                installer=self._install,
            )

        self.assertEqual(
            observed_command,
            [
                "/example/bin/gapi",
                "gmail",
                "search",
                GAPI_CHECK_QUERY,
                "--max",
                "1",
            ],
        )
        self.assertEqual(observed_options["timeout"], 30.0)
        self.assertIs(observed_options["stdout"], subprocess.DEVNULL)
        self.assertIs(observed_options["stderr"], subprocess.DEVNULL)
        self.assertFalse(observed_options["shell"])
        self.assertEqual(self.installs, 0)
        self.assertFalse(self.env_path.exists())

    def test_success_writes_complete_private_env_before_install(self) -> None:
        installed_contents: list[str] = []

        def installer() -> None:
            self.assertTrue(self.env_path.exists())
            self.assertEqual(stat.S_IMODE(self.env_path.stat().st_mode), 0o600)
            installed_contents.append(self.env_path.read_text(encoding="utf-8"))
            self.installs += 1

        run_setup(
            env_path=self.env_path,
            input_fn=self._inputs("you@example.com", ""),
            output_fn=lambda _message: None,
            which=self._which,
            runner=self._successful_runner,
            reader_factory=_ReadableMessages,
            installer=installer,
        )

        expected = (
            "FORWARD_TO_EMAIL=you@example.com\n"
            "SMS_MESSAGES_DB=~/Library/Messages/chat.db\n"
            "SMS_STATE_DB=./sms_forwarder.sqlite3\n"
            "SMS_POLL_SECONDS=5\n"
            "SMS_BATCH_SIZE=100\n"
            "GAPI_BIN=/example/bin/gapi\n"
            "GAPI_TIMEOUT_SECONDS=120\n"
            "LOG_LEVEL=INFO\n"
        )
        self.assertEqual(installed_contents, [expected])
        self.assertEqual(self.env_path.read_text(encoding="utf-8"), expected)
        self.assertEqual(self.installs, 1)
        self.assertEqual(
            {line.partition("=")[0] for line in expected.splitlines()}, set(ENV_KEYS)
        )
        self.assertEqual(list(self.root.glob("..env.*.tmp")), [])

    def test_existing_destination_is_masked_and_retained(self) -> None:
        prompts: list[str] = []

        def input_fn(prompt: str) -> str:
            prompts.append(prompt)
            return ""

        values = collect_settings(
            {
                "FORWARD_TO_EMAIL": "saved@example.com",
                "GAPI_BIN": "/example/bin/gapi",
            },
            input_fn=input_fn,
            which=self._which,
        )

        self.assertEqual(values["FORWARD_TO_EMAIL"], "saved@example.com")
        rendered_prompts = "".join(prompts)
        self.assertNotIn("saved@example.com", rendered_prompts)
        self.assertIn("s***@example.com", rendered_prompts)


if __name__ == "__main__":
    unittest.main()
