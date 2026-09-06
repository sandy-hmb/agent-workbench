from __future__ import annotations

import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / "scripts/hooks/pre-push"

EXPECTED_COMMANDS = (
    "python3 -m unittest discover -s tests -p 'test_*.py'",
    "python3 -m py_compile scripts/*.py migrations/*.py",
    "python3 -m json.tool schemas/workspace.schema.json",
    "python3 -m json.tool schemas/workspace-input.schema.json",
    "python3 -m json.tool schemas/workspace-init-input.schema.json",
    "python3 -m json.tool schemas/workspace-extension.schema.json",
    "python3 -m json.tool schemas/extensions-lock.schema.json",
    "python3 -m json.tool schemas/provider-result.schema.json",
    "python3 -m json.tool schemas/workspace-workflow.schema.json",
    "python3 -m json.tool upgrades/manifest.json",
    "python3 -m json.tool workflows/feature-development.json",
    "python3 scripts/workspace_doctor.py --root .",
    "git diff --check",
)


class PrePushHookTest(unittest.TestCase):
    def test_hook_exists_executable_and_has_valid_syntax(self):
        self.assertTrue(HOOK.is_file())
        mode = HOOK.stat().st_mode
        self.assertTrue(mode & stat.S_IXUSR, "pre-push 必须可执行")
        result = subprocess.run(
            ["bash", "-n", str(HOOK)], capture_output=True, text=True
        )
        self.assertEqual(0, result.returncode, result.stderr)

    def test_hook_contains_full_check_list(self):
        content = HOOK.read_text(encoding="utf-8")
        for command in EXPECTED_COMMANDS:
            with self.subTest(command=command):
                self.assertIn(command, content)

    def test_ci_does_not_skip_an_empty_test_suite(self):
        workflow = (ROOT / ".github/workflows/governance.yml").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("hashFiles('tests/test_*.py')", workflow)


class PrePushHookExecutionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.copy = Path(self.temp.name) / "kit"
        shutil.copytree(
            ROOT,
            self.copy,
            symlinks=True,
            ignore=shutil.ignore_patterns(
                ".git", "tests", "docs", "README.md", "__pycache__", ".workspace", "*.pyc"
            ),
        )
        (self.copy / "tests").mkdir()
        (self.copy / "tests/test_ok.py").write_text(
            "import unittest\n\n"
            "class T(unittest.TestCase):\n"
            "    def test_ok(self):\n"
            "        self.assertTrue(True)\n",
            encoding="utf-8",
        )
        subprocess.run(["git", "init", "-q", str(self.copy)], check=True)
        subprocess.run(["git", "-C", str(self.copy), "add", "-A"], check=True)
        subprocess.run(
            [
                "git", "-C", str(self.copy),
                "-c", "user.name=Test", "-c", "user.email=test@example.com",
                "commit", "-qm", "fixture",
            ],
            check=True,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _run_hook(self) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["bash", str(self.copy / "scripts/hooks/pre-push")],
            cwd=self.copy,
            capture_output=True,
            text=True,
        )

    def test_hook_passes_when_all_checks_are_healthy(self):
        result = self._run_hook()
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)

    def test_hook_fails_fast_on_broken_schema(self):
        broken = self.copy / "schemas/provider-result.schema.json"
        broken.write_text("{not valid json", encoding="utf-8")
        result = self._run_hook()
        self.assertNotEqual(0, result.returncode)
        self.assertIn(
            "Expecting property name enclosed in double quotes",
            result.stdout + result.stderr,
        )


class ContributingDocumentationTest(unittest.TestCase):
    def test_documents_optional_hook_and_skip_escape_hatch(self):
        content = (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
        self.assertIn("core.hooksPath scripts/hooks", content)
        self.assertIn("--no-verify", content)

    def test_verification_checklist_matches_governance_workflow(self):
        content = (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
        for command in EXPECTED_COMMANDS:
            if command == "git diff --check":
                continue
            with self.subTest(command=command):
                self.assertIn(command, content)


if __name__ == "__main__":
    unittest.main()
