from __future__ import annotations

import contextlib
import io
import json
import subprocess
import sys
import unittest
import tempfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(ROOT))

import workbench.cli.main as kit  # noqa: E402
import workbench.cli.status as workspace_status  # noqa: E402
import workbench.workspace.doctor as workspace_doctor  # noqa: E402


def _run(func, argv):
    stdout = io.StringIO()
    stderr = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        try:
            code = func(argv)
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
    return code, stdout.getvalue(), stderr.getvalue()


class KitForwardingTest(unittest.TestCase):
    def test_status_json_forwarding_matches_direct_call(self):
        forwarded_code, forwarded_out, forwarded_err = _run(
            kit.main, ["status", "--root", str(ROOT), "--json"]
        )
        direct_code, direct_out, direct_err = _run(
            workspace_status.main, ["--root", str(ROOT), "--json"]
        )
        self.assertEqual(forwarded_code, direct_code)
        self.assertEqual(forwarded_out, direct_out)
        self.assertEqual(forwarded_err, direct_err)
        json.loads(forwarded_out)

    def test_doctor_help_forwarding_matches_direct_call_byte_for_byte(self):
        forwarded_code, forwarded_out, forwarded_err = _run(
            kit.main, ["doctor", "--help"]
        )
        direct_code, direct_out, direct_err = _run(workspace_doctor.main, ["--help"])
        self.assertEqual(forwarded_code, direct_code)
        self.assertEqual(forwarded_out, direct_out)
        self.assertEqual(forwarded_err, direct_err)

    def test_nonzero_exit_code_is_forwarded(self):
        argv = ["complete", "missing-item", "--root", str(ROOT), "--state-revision", "sha256:missing"]
        forwarded_code, forwarded_out, _ = _run(kit.main, ["item", *argv])
        direct_code, direct_out, _ = _run(__import__("workbench.cli.items", fromlist=["main"]).main, argv)
        self.assertEqual(forwarded_code, direct_code)
        self.assertEqual(forwarded_out, direct_out)
        self.assertNotEqual(forwarded_code, 0)

    def test_no_args_lists_all_registered_subcommands(self):
        code, out, _ = _run(kit.main, [])
        self.assertEqual(code, 0)
        for name in kit.COMMANDS:
            self.assertIn(name, out)

    def test_help_flag_lists_all_registered_subcommands(self):
        code, out, _ = _run(kit.main, ["--help"])
        self.assertEqual(code, 0)
        for name in kit.COMMANDS:
            self.assertIn(name, out)

    def test_unknown_subcommand_returns_nonzero_and_does_not_import_modules(self):
        with mock.patch("workbench.cli.main.importlib.import_module") as mocked_import:
            code, out, err = _run(kit.main, ["not-a-real-subcommand"])
        self.assertNotEqual(code, 0)
        self.assertEqual(out, "")
        self.assertIn("not-a-real-subcommand", err)
        for name in sorted(kit.COMMANDS):
            self.assertIn(name, err)
        mocked_import.assert_not_called()

    def test_kit_prints_nothing_extra_on_successful_forward(self):
        forwarded_code, forwarded_out, _ = _run(
            kit.main, ["status", "--root", str(ROOT), "--json"]
        )
        direct_code, direct_out, _ = _run(
            workspace_status.main, ["--root", str(ROOT), "--json"]
        )
        self.assertEqual(forwarded_code, direct_code)
        # 转发路径下 kit.py 自身不额外打印任何内容：两侧输出应完全相同。
        self.assertEqual(forwarded_out, direct_out)

    def test_public_entrypoint_works_from_another_directory(self):
        result = subprocess.run(
            [sys.executable, str(SCRIPTS / "kit.py"), "status", "--root", str(ROOT), "--json"],
            capture_output=True,
            text=True,
            check=False,
            cwd=tempfile.gettempdir(),
        )
        self.assertEqual(result.returncode, 0)
        json.loads(result.stdout)


if __name__ == "__main__":
    unittest.main()
