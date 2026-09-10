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


def task_evidence(
    *,
    task_id: str = "T01",
    repository: str = "service",
    check_type: str = "测试",
    executed: int | None = 3,
    skipped: int | None = 0,
    exit_status: int = 0,
    delivery: str = "通过",
) -> str:
    counts = []
    if executed is not None:
        counts.append(f"- 执行数：{executed}")
    if skipped is not None:
        counts.append(f"- 跳过数：{skipped}")
    return "\n".join(
        [
            f"## 任务证据 {task_id} 2026-09-10T10:00:00Z",
            "",
            f"- 交付核对：{delivery}",
            f'- 代码状态：{{"{repository}":"sha256:{"a" * 64}"}}',
            "",
            "### 检查 1",
            "",
            f"- 类型：{check_type}",
            f"- 工作目录：`/tmp/{repository}`",
            "- 命令：`run-check`",
            "- 目标：`tests/test_service.py`",
            *counts,
            f"- 退出状态：{exit_status}",
            "- 结果：目标检查通过" if exit_status == 0 else "- 结果：失败",
        ]
    )


def evidence_task(validation_kind: str = "行为") -> dict[str, object]:
    return {
        "id": "T01",
        "completed": True,
        "repository": "service",
        "validationKind": validation_kind,
        "deliverables": [
            {
                "repository": "service",
                "kind": "Modify",
                "path": "source.txt",
                "symbol": None,
                "line": 1,
            },
            {
                "repository": "service",
                "kind": "Test",
                "path": "tests/test_service.py",
                "symbol": None,
                "line": 2,
            },
        ],
    }


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

    def test_task_evidence_uses_latest_complete_record(self) -> None:
        tests = self.repository / "tests"
        tests.mkdir()
        (tests / "test_service.py").write_text("def test_service(): pass\n", encoding="utf-8")
        first = task_evidence(exit_status=1)
        latest = task_evidence(executed=4).replace(
            "- 结果：目标检查通过",
            "- 结果：已覆盖失败矩阵，目标检查通过",
        )
        text = f"# 验证记录\n\n{first}\n\n{latest}\n\n{batch({'service': 'sha256:' + 'b' * 64}, 0)}"

        described = workspace_verification.describe_task_evidence_document(text)
        result = workspace_verification.evaluate_task_evidence(
            evidence_task(), described["latestByTask"]["T01"], {"service": self.repository}
        )

        self.assertEqual(2, len(described["records"]))
        self.assertEqual(4, described["latestByTask"]["T01"]["checks"][0]["executed"])
        self.assertTrue(result["trusted"])
        self.assertEqual([], result["diagnostics"])

    def test_task_evidence_rejects_zero_skipped_failed_and_insufficient_checks(self) -> None:
        cases = (
            (task_evidence(executed=0), "行为", "TASK_EVIDENCE_ZERO_TESTS"),
            (task_evidence(skipped=1), "行为", "TASK_EVIDENCE_SKIPPED_TESTS"),
            (task_evidence(exit_status=1), "行为", "TASK_EVIDENCE_CHECK_FAILED"),
            (
                task_evidence().replace("- 结果：目标检查通过", "- 结果：失败，等待修复"),
                "行为",
                "TASK_EVIDENCE_CHECK_FAILED",
            ),
            (task_evidence(check_type="编译", executed=None, skipped=None), "行为",
             "TASK_EVIDENCE_KIND_INSUFFICIENT"),
            (task_evidence(check_type="单元 Mock"), "持久化",
             "TASK_EVIDENCE_KIND_INSUFFICIENT"),
        )

        for record, validation_kind, expected in cases:
            with self.subTest(expected=expected, validation_kind=validation_kind):
                evidence = workspace_verification.describe_task_evidence_document(
                    record
                )["latestByTask"]["T01"]
                result = workspace_verification.evaluate_task_evidence(
                    evidence_task(validation_kind),
                    evidence,
                    {"service": self.repository},
                )
                self.assertFalse(result["trusted"])
                self.assertIn(expected, {item["code"] for item in result["diagnostics"]})

    def test_task_evidence_checks_current_deliverable_paths(self) -> None:
        tests = self.repository / "tests"
        tests.mkdir()
        target = tests / "test_service.py"
        target.write_text("def test_service(): pass\n", encoding="utf-8")
        deleted = self.repository / "old.txt"
        task = evidence_task()
        task["deliverables"].extend(
            [
                {
                    "repository": "service",
                    "kind": "Verify",
                    "path": "tests",
                    "symbol": None,
                    "line": 3,
                },
                {
                    "repository": "service",
                    "kind": "Delete",
                    "path": "old.txt",
                    "symbol": None,
                    "line": 4,
                },
            ]
        )
        evidence = workspace_verification.describe_task_evidence_document(
            task_evidence()
        )["latestByTask"]["T01"]

        valid = workspace_verification.evaluate_task_evidence(
            task, evidence, {"service": self.repository}
        )
        target.unlink()
        deleted.write_text("still here\n", encoding="utf-8")
        invalid = workspace_verification.evaluate_task_evidence(
            task, evidence, {"service": self.repository}
        )

        self.assertTrue(valid["trusted"])
        codes = {item["code"] for item in invalid["diagnostics"]}
        self.assertIn("TASK_DELIVERABLE_MISSING", codes)
        self.assertIn("TASK_DELETED_PATH_PRESENT", codes)

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
