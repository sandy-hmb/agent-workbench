from __future__ import annotations
import subprocess
import time
import os
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import workbench.git as workspace_verification

def run_git(*arguments, cwd):
    subprocess.run(['git', *arguments], cwd=cwd, check=True, capture_output=True)

class FingerprintTest(unittest.TestCase):
    def test_inspect_fingerprint_budget_rejects_large_untracked_content(self):
            path = self.repository / "large.bin"
            with path.open("wb") as handle:
                handle.truncate(65 * 1024 * 1024)
            with self.assertRaisesRegex(ValueError, "字节超过限制"):
                workspace_verification.git_fingerprint(
                    self.repository, max_bytes=64 * 1024 * 1024, max_untracked_files=10_000
                )

    def test_fingerprint_shares_one_timeout_across_git_steps(self):
            calls = [str(self.repository).encode() + b"\n"]
            def slow_git(*_args):
                import time
                time.sleep(0.02)
                return calls.pop(0)
            with mock.patch("workbench.git._git", side_effect=slow_git):
                with self.assertRaises(subprocess.TimeoutExpired):
                    workspace_verification.git_fingerprint(self.repository, timeout=0.01)

    def test_git_output_budget_kills_continuous_producer_before_timeout(self):
            fake = self.repository / "bin"; fake.mkdir()
            script = fake / "git"
            script.write_text(
                "#!/usr/bin/env python3\nimport sys,time\n"
                "sys.stdout.buffer.write(b'x' * 4096); sys.stdout.flush()\n"
                "sys.stdout.buffer.write(b'y' * 4096); sys.stdout.flush()\n"
                "time.sleep(10)\n", encoding="utf-8")
            script.chmod(0o755)
            before = time.monotonic()
            with mock.patch.dict(os.environ, {"PATH": f"{fake}:{os.environ['PATH']}"}):
                with self.assertRaisesRegex(ValueError, "Git 输出超过限制"):
                    workspace_verification._git(self.repository, ["anything"], timeout=2, max_output_bytes=4096)
            self.assertLess(time.monotonic() - before, 1)

    def setUp(self) -> None:
            self.temp = tempfile.TemporaryDirectory()
            self.repository = Path(self.temp.name).resolve()
            run_git("init", "-q", cwd=self.repository)
            (self.repository / "source.txt").write_text("one\n", encoding="utf-8")
            run_git("add", "source.txt", cwd=self.repository)
            run_git(
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.com",
                "commit",
                "-qm",
                "fixture",
                cwd=self.repository,
            )

    def tearDown(self) -> None:
            self.temp.cleanup()

    def test_fingerprint_tracks_committed_tracked_and_untracked_content(self) -> None:
            initial = workspace_verification.git_fingerprint(self.repository)

            (self.repository / "source.txt").write_text("two\n", encoding="utf-8")
            tracked = workspace_verification.git_fingerprint(self.repository)
            (self.repository / "new.txt").write_text("new\n", encoding="utf-8")
            untracked = workspace_verification.git_fingerprint(self.repository)

            self.assertRegex(initial, r"^sha256:[0-9a-f]{64}$")
            self.assertNotEqual(initial, tracked)
            self.assertNotEqual(tracked, untracked)

    def test_fingerprint_excludes_only_named_operational_files(self) -> None:
            initial = workspace_verification.git_fingerprint(
                self.repository, excluded=("README.md", "plans/implementation.md")
            )
            (self.repository / "README.md").write_text("state\n", encoding="utf-8")
            (self.repository / "plans").mkdir()
            (self.repository / "plans/implementation.md").write_text("- [x] done\n", encoding="utf-8")

            self.assertEqual(
                initial,
                workspace_verification.git_fingerprint(
                    self.repository, excluded=("plans/implementation.md", "README.md")
                ),
            )

            (self.repository / "requirements.md").write_text("changed\n", encoding="utf-8")
            self.assertNotEqual(
                initial,
                workspace_verification.git_fingerprint(
                    self.repository, excluded=("README.md", "plans/implementation.md")
                ),
            )
