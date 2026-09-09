from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import contextlib
import io
import json
import os
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import workspace_verification  # noqa: E402


def run_git(*arguments: str, cwd: Path) -> None:
    subprocess.run(["git", *arguments], cwd=cwd, check=True, capture_output=True)


def batch(states: dict[str, str], *exit_codes: int) -> str:
    checks = []
    for index, code in enumerate(exit_codes, 1):
        checks.append(
            "\n".join(
                [
                    f"### 检查 {index}",
                    "- 工作目录：`/tmp/repo`",
                    f"- 命令：`check-{index}`",
                    f"- 退出状态：{code}",
                    "- 结果：完成",
                ]
            )
        )
    encoded = workspace_verification.encode_code_state(states)
    return "\n\n".join(
        [
            "## 验证批次 2026-09-07T16:00:00+08:00",
            "\n".join(
                [
                    "- 总体结果：通过",
                    "- 审查结论：通过",
                    f"- 代码状态：{encoded}",
                ]
            ),
            *checks,
        ]
    )


class WorkspaceVerificationTest(unittest.TestCase):
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
        with mock.patch("workspace_verification._git", side_effect=slow_git):
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

    def test_complete_current_batch_passes(self) -> None:
        states = {"service": workspace_verification.git_fingerprint(self.repository)}
        self.assertTrue(workspace_verification.verification_passed(batch(states, 0, 0), states))

    def test_old_incomplete_failed_or_stale_evidence_does_not_pass(self) -> None:
        states = {"service": workspace_verification.git_fingerprint(self.repository)}
        valid = batch(states, 0)
        cases = (
            "## 执行记录 2026-09-07\n- 工作目录：`/tmp`\n- 命令：`true`\n- 退出状态：0\n- 结果：通过",
            valid.replace("- 审查结论：通过\n", ""),
            valid.replace("- 总体结果：通过", "- 总体结果：失败"),
            valid.replace("- 审查结论：通过", "- 审查结论：不通过"),
            batch(states, 0, 1),
            batch(states),
            valid,
        )
        current = (
            states,
            states,
            states,
            states,
            states,
            states,
            {"service": "sha256:" + "0" * 64},
        )

        for record, code_state in zip(cases, current):
            with self.subTest(record=record[:40], code_state=code_state):
                self.assertFalse(workspace_verification.verification_passed(record, code_state))

    def test_code_state_requires_exact_repository_set_and_valid_digests(self) -> None:
        states = {"service": workspace_verification.git_fingerprint(self.repository)}
        record = batch(states, 0)

        self.assertFalse(workspace_verification.verification_passed(record, {}))
        self.assertFalse(
            workspace_verification.verification_passed(
                record, {**states, "web": "sha256:" + "1" * 64}
            )
        )
        self.assertFalse(
            workspace_verification.verification_passed(
                record.replace(states["service"], "invalid"), states
            )
        )

    def test_snapshot_cli_reports_current_maintenance_feature_state(self) -> None:
        feature = self.repository / "docs/development/features/demo-feature"
        (feature / "plans").mkdir(parents=True)
        (feature / "testing").mkdir()
        (feature / "README.md").write_text(
            "# Demo\n\n- 状态：development\n- 需求短名：`demo-feature`\n"
            "- 工作分支：`main`\n- 基线分支：`main`\n- 最后更新：2026-09-07\n",
            encoding="utf-8",
        )
        (feature / "plans/implementation.md").write_text("- [ ] task\n", encoding="utf-8")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = workspace_verification.main(
                ["snapshot", "demo-feature", "--root", str(self.repository), "--json"]
            )

        value = json.loads(output.getvalue())
        self.assertEqual(0, code)
        self.assertEqual("demo-feature", value["featureSlug"])
        self.assertEqual({self.repository.name}, set(value["codeState"]))


if __name__ == "__main__":
    unittest.main()
