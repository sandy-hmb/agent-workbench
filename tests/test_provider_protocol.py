from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from provider_protocol import MAX_OUTPUT_BYTES, run_provider  # noqa: E402


FIXTURE = ROOT / "tests" / "fixtures" / "provider"


class ProviderProtocolTest(unittest.TestCase):
    def run_fixture(
        self,
        name: str,
        *,
        environment: tuple[str, ...] = (),
        timeout: float = 1,
    ) -> dict[str, object]:
        return run_provider(
            [sys.executable, name],
            cwd=FIXTURE,
            provider="example-extension/team",
            request={"provider": "example-extension/team", "request": "value"},
            environment=environment,
            timeout=timeout,
        )

    def test_uses_json_argv_and_an_isolated_environment(self):
        with mock.patch.dict(os.environ, {"UNDECLARED_PROVIDER_TEST": "hidden"}, clear=False):
            result = self.run_fixture("echo.py")
        self.assertEqual("ok", result["status"])
        self.assertIsNone(result["result"]["undeclared"])
        self.assertEqual("value", result["result"]["request"]["request"])

    def test_missing_declared_environment_is_blocked_without_running_provider(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            result = self.run_fixture("echo.py", environment=("DECLARED_TOKEN",))
        self.assertEqual("blocked", result["status"])
        self.assertEqual("CREDENTIAL_MISSING", result["diagnostics"][0]["code"])

    def test_declared_environment_values_are_redacted_from_provider_result(self):
        secret = "provider-secret-123"
        with mock.patch.dict(os.environ, {"DECLARED_TOKEN": secret}, clear=False):
            result = self.run_fixture("secret-diagnostic.py", environment=("DECLARED_TOKEN",))
        self.assertEqual("ok", result["status"])
        self.assertNotIn(secret, json.dumps(result, ensure_ascii=False))
        self.assertEqual("[REDACTED]", result["result"]["secret"])
        self.assertIn("[REDACTED]", result["result"])
        self.assertEqual("[REDACTED]", result["diagnostics"][0]["message"])

    def test_timeout_and_output_limit_are_structured_failures(self):
        timeout = self.run_fixture("slow.py", timeout=0.05)
        limit = self.run_fixture("large-output.py")
        stderr_limit = run_provider(
            [
                sys.executable,
                "-c",
                "import sys; sys.stderr.write('x' * (1024 * 1024 + 1)); sys.stderr.flush()",
            ],
            cwd=FIXTURE,
            provider="example-extension/team",
            request={"provider": "example-extension/team"},
        )
        self.assertEqual("PROVIDER_TIMEOUT", timeout["diagnostics"][0]["code"])
        self.assertEqual("PROVIDER_OUTPUT_LIMIT", limit["diagnostics"][0]["code"])
        self.assertEqual("PROVIDER_OUTPUT_LIMIT", stderr_limit["diagnostics"][0]["code"])
        self.assertEqual(MAX_OUTPUT_BYTES, 1024 * 1024)

    def test_terminates_original_process_group_after_success_timeout_or_output_limit(self):
        expected = {
            "normal": None,
            "timeout": "PROVIDER_TIMEOUT",
            "limit": "PROVIDER_OUTPUT_LIMIT",
        }
        for mode, code in expected.items():
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                sentinel = Path(directory) / "child-sentinel"
                result = run_provider(
                    [sys.executable, "tree.py", mode, str(sentinel)],
                    cwd=FIXTURE,
                    provider="example-extension/team",
                    request={},
                    timeout=0.5 if mode in {"limit", "normal"} else 0.05,
                )
                if code is None:
                    self.assertEqual("ok", result["status"])
                else:
                    self.assertEqual(code, result["diagnostics"][0]["code"])
                time.sleep(1)
                self.assertFalse(sentinel.exists(), "Provider child escaped process cleanup")

    def test_large_stdin_timeout_has_no_blocked_writer_after_detached_child_keeps_read_end(self):
        with tempfile.TemporaryDirectory() as directory:
            sentinel = Path(directory) / "detached-child-exited"
            threads_before = {thread.ident for thread in threading.enumerate()}
            started = time.monotonic()
            result = run_provider(
                [sys.executable, "detached-stdin.py", str(sentinel)],
                cwd=FIXTURE,
                provider="example-extension/team",
                request={"payload": "x" * (MAX_OUTPUT_BYTES * 2)},
                timeout=0.5,
            )
            elapsed = time.monotonic() - started
            self.assertEqual("PROVIDER_TIMEOUT", result["diagnostics"][0]["code"])
            self.assertLess(elapsed, 1.5)
            self.assertEqual(threads_before, {thread.ident for thread in threading.enumerate()})
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                if sentinel.exists():
                    break
                time.sleep(0.05)
            self.assertTrue(sentinel.exists(), "test child did not finish independently")

    def test_nonzero_process_is_a_structured_failure_without_stderr(self):
        result = run_provider(
            [sys.executable, "-c", "import sys; sys.stderr.write('unsafe detail'); raise SystemExit(3)"],
            cwd=FIXTURE,
            provider="example-extension/team",
            request={"provider": "example-extension/team"},
        )
        self.assertEqual("failed", result["status"])
        self.assertEqual("PROVIDER_PROCESS_ERROR", result["diagnostics"][0]["code"])
        self.assertNotIn("unsafe detail", json.dumps(result, ensure_ascii=False))

    def test_waits_for_leader_exit_to_accept_fragmented_output_and_rejects_delayed_extra(self):
        fragmented = self.run_fixture("fragmented.py", timeout=1)
        delayed_extra = self.run_fixture("delayed-extra.py", timeout=1)
        self.assertEqual("ok", fragmented["status"])
        self.assertEqual("failed", delayed_extra["status"])
        self.assertEqual(
            "PROVIDER_PROTOCOL_ERROR", delayed_extra["diagnostics"][0]["code"]
        )

    def test_nonfinite_timeout_is_rejected_before_starting_provider(self):
        for timeout in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(timeout=timeout):
                result = run_provider(
                    [sys.executable, "echo.py"],
                    cwd=FIXTURE,
                    provider="example-extension/team",
                    request={"provider": "example-extension/team"},
                    timeout=timeout,
                )
                self.assertEqual("failed", result["status"])
                self.assertEqual("PROVIDER_PROTOCOL_ERROR", result["diagnostics"][0]["code"])

    def test_deep_provider_result_is_rejected_without_recursion_error(self):
        result = self.run_fixture("deep-result.py", timeout=1)
        self.assertEqual("failed", result["status"])
        self.assertEqual("PROVIDER_PROTOCOL_ERROR", result["diagnostics"][0]["code"])

    def test_deep_provider_request_is_rejected_before_starting_process(self):
        request: object = {}
        for _ in range(1_000):
            request = {"nested": request}
        marker = Path(tempfile.mkdtemp()) / "started"
        try:
            result = run_provider(
                [
                    sys.executable,
                    "-c",
                    "from pathlib import Path; import sys; Path(sys.argv[1]).write_text('started')",
                    str(marker),
                ],
                cwd=FIXTURE,
                provider="example-extension/team",
                request=request,
            )
        finally:
            if marker.exists():
                marker.unlink()
            marker.parent.rmdir()
        self.assertEqual("failed", result["status"])
        self.assertEqual("PROVIDER_PROTOCOL_ERROR", result["diagnostics"][0]["code"])

    def test_malformed_or_extra_stdout_is_a_protocol_failure(self):
        for source in (
            'import sys; sys.stdout.write("{\\\"apiVersion\\\":1,\\\"apiVersion\\\":1}")',
            'import sys; sys.stdout.write("{\\\"apiVersion\\\":NaN}")',
            'import sys; sys.stdout.write("{}\\nextra")',
            'import sys; sys.stdout.write("{\\\"apiVersion\\\":2,\\\"provider\\\":\\\"example-extension/team\\\",\\\"status\\\":\\\"ok\\\",\\\"result\\\":null,\\\"diagnostics\\\":[],\\\"effects\\\":[]}")',
            'import sys; sys.stdout.write("{\\\"apiVersion\\\":1,\\\"provider\\\":\\\"other/team\\\",\\\"status\\\":\\\"ok\\\",\\\"result\\\":null,\\\"diagnostics\\\":[],\\\"effects\\\":[]}")',
            'import sys; sys.stdout.write("{\\\"apiVersion\\\":1,\\\"provider\\\":\\\"example-extension/team\\\",\\\"status\\\":\\\"unknown\\\",\\\"result\\\":null,\\\"diagnostics\\\":[],\\\"effects\\\":[]}")',
            'import sys; sys.stdout.write("{\\\"apiVersion\\\":1,\\\"provider\\\":\\\"example-extension/team\\\",\\\"status\\\":\\\"ok\\\",\\\"result\\\":null,\\\"diagnostics\\\":[{}],\\\"effects\\\":[]}")',
            'import sys; sys.stdout.write("{\\\"apiVersion\\\":1,\\\"provider\\\":\\\"example-extension/team\\\",\\\"status\\\":\\\"failed\\\",\\\"result\\\":null,\\\"diagnostics\\\":[],\\\"effects\\\":[]}")',
        ):
            with self.subTest(source=source):
                result = run_provider(
                    [sys.executable, "-c", source],
                    cwd=FIXTURE,
                    provider="example-extension/team",
                    request={},
                )
                self.assertEqual("failed", result["status"])
                self.assertEqual("PROVIDER_PROTOCOL_ERROR", result["diagnostics"][0]["code"])

    def test_non_ok_result_may_have_multiple_diagnostics_when_one_is_error(self):
        source = (
            'import sys; sys.stdout.write("{\\\"apiVersion\\\":1,\\\"provider\\\":\\\"example-extension/team\\\",\\\"status\\\":\\\"blocked\\\",\\\"result\\\":null,\\\"diagnostics\\\":[{\\\"level\\\":\\\"warning\\\",\\\"code\\\":\\\"NOTICE\\\",\\\"message\\\":\\\"notice\\\"},{\\\"level\\\":\\\"error\\\",\\\"code\\\":\\\"BLOCKED\\\",\\\"message\\\":\\\"blocked\\\"}],\\\"effects\\\":[]}")'
        )
        result = run_provider(
            [sys.executable, "-c", source],
            cwd=FIXTURE,
            provider="example-extension/team",
            request={},
        )
        self.assertEqual("blocked", result["status"])
        self.assertEqual(2, len(result["diagnostics"]))


if __name__ == "__main__":
    unittest.main()
