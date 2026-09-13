from __future__ import annotations

import hashlib
import contextlib
import io
import json
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import workspace_evidence  # noqa: E402
import workspace_inspect  # noqa: E402
import workspace_status  # noqa: E402
import workspace_verification  # noqa: E402


class EvidenceMigrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "kit"
        feature = self.root / "docs/development/features/demo-feature"
        (feature / "plans").mkdir(parents=True)
        (feature / "testing").mkdir()
        (self.root / "README.md").write_text("# Kit\n", encoding="utf-8")
        (feature / "README.md").write_text(
            "# Demo\n\n- 状态：development\n- 需求短名：`demo-feature`\n"
            "- 工作分支：`main`\n- 基线分支：`main`\n- 最后更新：2026-09-13\n",
            encoding="utf-8",
        )
        (feature / "plans/implementation.md").write_text(
            "- 完成门禁：`task-evidence-v1`\n\n"
            "- [x] T01 完成\n\n依赖：无\n目标仓：`kit`\n验证性质：行为\n\n"
            "**文件**\n\n- Modify：`README.md`\n",
            encoding="utf-8",
        )
        (feature / "testing/verification.md").write_text(
            "## 任务证据 T01 2026-09-13T10:00:00Z\n\n"
            "- 交付核对：通过\n"
            f'- 代码状态：{{"kit":"sha256:{"a" * 64}"}}\n\n'
            "### 检查 1\n\n- 类型：测试\n- 工作目录：`kit`\n"
            "- 命令：`python3 -m unittest`\n- 目标：`tests/test_kit.py`\n"
            "- 执行数：1\n- 跳过数：0\n- 退出状态：0\n- 结果：通过\n\n"
            "## 验证批次 2026-09-13T11:00:00Z\n"
            "- 总体结果：失败\n- 审查结论：通过\n"
            f'- 代码状态：{{"kit":"sha256:{"a" * 64}"}}\n\n'
            "### 检查 1\n- 工作目录：`kit`\n- 命令：`python3 -m unittest`\n"
            "- 退出状态：1\n- 结果：失败\n",
            encoding="utf-8",
        )
        self.feature = feature

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_preview_is_read_only_and_apply_preserves_machine_state(self) -> None:
        before_bytes = {
            path.relative_to(self.feature).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in self.feature.rglob("*") if path.is_file()
        }
        preview = workspace_evidence.migrate_feature(self.feature, preview=True)
        self.assertTrue(preview["preview"])
        self.assertEqual(before_bytes, {
            path.relative_to(self.feature).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in self.feature.rglob("*") if path.is_file()
        })
        before_status = workspace_status.status_result(self.root)
        before_feature = before_status["features"][0]
        before = workspace_status.plan_analysis(self.feature / "plans/implementation.md")["completionPolicy"]
        result = workspace_evidence.migrate_feature(self.feature, preview=False)
        self.assertFalse(result["preview"])
        self.assertEqual("task-evidence-v1", before)
        self.assertEqual("task-evidence-v2", workspace_status.plan_analysis(self.feature / "plans/implementation.md")["completionPolicy"])
        self.assertTrue((self.feature / result["archive"]).is_file())
        self.assertTrue((self.feature / "testing/evidence/index.json").is_file())
        self.assertEqual("# 验证摘要\n", (self.feature / "testing/verification.md").read_text(encoding="utf-8")[:7])
        after_feature = workspace_status.status_result(self.root)["features"][0]
        for field in ("trustedProgress", "verificationPassed", "documentDiagnostics"):
            self.assertEqual(before_feature[field], after_feature[field], field)
        history = workspace_evidence.history_page(self.feature)
        self.assertEqual(2, history["total"])

    def test_cli_migrate_and_compact_default_to_preview(self) -> None:
        before = {
            path.relative_to(self.feature).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in self.feature.rglob("*") if path.is_file()
        }
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(0, workspace_verification.main([
                "migrate", "demo-feature", "--root", str(self.root), "--json"
            ]))
        self.assertEqual(before, {
            path.relative_to(self.feature).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in self.feature.rglob("*") if path.is_file()
        })
        workspace_evidence.migrate_feature(self.feature, preview=False)
        before_compact = {
            path.relative_to(self.feature).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in self.feature.rglob("*") if path.is_file()
        }
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(0, workspace_verification.main([
                "compact", "demo-feature", "--root", str(self.root), "--json"
            ]))
        self.assertEqual(before_compact, {
            path.relative_to(self.feature).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in self.feature.rglob("*") if path.is_file()
        })

    def test_empty_legacy_document_gets_a_valid_v2_index(self) -> None:
        (self.feature / "plans/implementation.md").write_text("- 完成门禁：`task-evidence-v1`\n", encoding="utf-8")
        (self.feature / "testing/verification.md").write_text("# 验证记录\n", encoding="utf-8")
        workspace_evidence.migrate_feature(self.feature, preview=False)
        self.assertEqual(0, workspace_evidence.load_index(self.feature)["summary"]["records"])
        self.assertEqual([], workspace_evidence.history_page(self.feature)["items"])

    def test_compact_preview_and_apply_keep_history_and_do_not_delete_records(self) -> None:
        workspace_evidence.migrate_feature(self.feature, preview=False)
        workspace_evidence.record(self.feature, {
            "kind": "taskEvidence", "taskId": "T01", "recordedAt": "2026-09-13T12:00:00Z",
            "repository": "kit", "codeState": {"kit": "sha256:" + "b" * 64},
            "checks": [{"type": "测试", "workingDirectory": "kit", "command": "true", "target": "README.md", "executed": 1, "skipped": 0, "exitStatus": 0, "result": "通过"}],
            "artifactRefs": [], "validationKind": "行为", "deliveryCheck": "passed", "result": "passed",
        })
        before_status = workspace_status.status_result(self.root)["features"][0]
        before = workspace_evidence.history_page(self.feature)["total"]
        preview = workspace_evidence.compact_feature(self.feature, preview=True)
        self.assertTrue(preview["preview"])
        applied = workspace_evidence.compact_feature(self.feature, preview=False)
        self.assertTrue(applied["changed"])
        self.assertEqual(before, workspace_evidence.history_page(self.feature)["total"])
        self.assertTrue((self.feature / applied["archive"]).is_file())
        workspace_evidence.record(self.feature, {
            "kind": "taskEvidence", "taskId": "T01", "recordedAt": "2026-09-13T13:00:00Z",
            "repository": "kit", "codeState": {"kit": "sha256:" + "c" * 64},
            "checks": [{"type": "测试", "workingDirectory": "kit", "command": "true", "target": "README.md", "executed": 1, "skipped": 0, "exitStatus": 0, "result": "通过"}],
            "artifactRefs": [], "validationKind": "行为", "deliveryCheck": "passed", "result": "passed",
        })
        self.assertEqual(before + 1, workspace_evidence.load_index(self.feature)["summary"]["records"])
        after_status = workspace_status.status_result(self.root)["features"][0]
        for field in ("trustedProgress", "verificationPassed", "documentDiagnostics"):
            self.assertEqual(before_status[field], after_status[field], field)

    def test_incomplete_transaction_is_a_hard_read_failure(self) -> None:
        evidence = self.feature / "testing/evidence"
        evidence.mkdir()
        (evidence / ".transaction.json").write_text('{"operation":"migrate"}\n', encoding="utf-8")
        with self.assertRaisesRegex(workspace_evidence.EvidenceError, "EVIDENCE_TRANSACTION_INCOMPLETE"):
            workspace_evidence.load_store(self.feature)

    def test_archived_batch_remains_queryable_by_id(self) -> None:
        workspace_evidence.migrate_feature(self.feature, preview=False)
        old_batch = workspace_evidence.load_index(self.feature)["latestBatch"]["id"]
        workspace_evidence.record(self.feature, {
            "kind": "verificationBatch", "recordedAt": "2026-09-13T13:00:00Z",
            "overallResult": "failed", "reviewResult": "passed",
            "codeState": {"kit": "sha256:" + "b" * 64},
            "checks": [{"workingDirectory": "kit", "command": "false", "exitStatus": 1, "result": "失败"}],
            "blockers": [], "artifactRefs": [],
        })
        workspace_evidence.compact_feature(self.feature, preview=False)
        self.assertEqual(old_batch, workspace_evidence.get_evidence(self.feature, batch_id=old_batch)["id"])

    def test_archived_task_record_can_be_read_by_task_and_id(self) -> None:
        workspace_evidence.migrate_feature(self.feature, preview=False)
        historical = workspace_evidence.load_index(self.feature)["tasks"]["T01"]["latestObserved"]["id"]
        workspace_evidence.record(self.feature, {
            "kind": "taskEvidence", "taskId": "T01", "recordedAt": "2026-09-13T12:00:00Z",
            "repository": "kit", "codeState": {"kit": "sha256:" + "b" * 64},
            "checks": [{"type": "测试", "workingDirectory": "kit", "command": "true", "target": "README.md",
                        "executed": 1, "skipped": 0, "exitStatus": 0, "result": "通过"}],
            "artifactRefs": [], "validationKind": "行为", "deliveryCheck": "passed", "result": "passed",
        })
        workspace_evidence.compact_feature(self.feature, preview=False)
        result = workspace_evidence.get_evidence(self.feature, task_id="T01", evidence_id=historical)
        self.assertEqual(historical, result["id"])

    def test_migrate_all_batches_and_preserve_latest_legacy_result(self) -> None:
        verification = self.feature / "testing/verification.md"
        original = verification.read_text(encoding="utf-8")
        successful = (
            "## 验证批次 2026-09-13T09:00:00Z\n"
            "- 总体结果：通过\n- 审查结论：通过\n"
            f'- 代码状态：{{"kit":"sha256:{"a" * 64}"}}\n\n'
            "### 检查 1\n- 工作目录：`kit`\n- 命令：`python3 -m unittest`\n"
            "- 退出状态：0\n- 结果：通过\n\n"
        )
        verification.write_text(successful + original + "\n## 执行记录 2026-09-13\n- 结果：旧记录\n", encoding="utf-8")
        before = workspace_status.status_result(self.root)["features"][0]
        preview = workspace_evidence.migrate_feature(self.feature, preview=True)
        self.assertEqual(3, preview["batchCount"])
        workspace_evidence.migrate_feature(self.feature, preview=False)
        after = workspace_status.status_result(self.root)["features"][0]
        self.assertEqual(before["verificationPassed"], after["verificationPassed"])
        page = workspace_evidence.history_page(self.feature, limit=10)
        batches = [item for item in page["items"] if item["kind"] == "batch"]
        self.assertEqual(3, len(batches))
        self.assertEqual("unknown", workspace_evidence.get_evidence(self.feature, batch_id=batches[0]["id"])["overallResult"])
        self.assertEqual("passed", workspace_evidence.get_evidence(self.feature, batch_id=batches[-1]["id"])["overallResult"])

    def test_migration_keeps_v1_batch_exit_code_semantics(self) -> None:
        verification = self.feature / "testing/verification.md"
        verification.write_text(
            verification.read_text(encoding="utf-8")
            .replace("- 总体结果：失败", "- 总体结果：通过")
            .replace("- 退出状态：1\n- 结果：失败", "- 退出状态：0\n- 结果：失败"),
            encoding="utf-8",
        )
        self.assertTrue(workspace_verification.verification_passed(
            workspace_status.verification_record(self.feature), {"kit": "sha256:" + "a" * 64}
        ))
        workspace_evidence.migrate_feature(self.feature, preview=False)
        batch = workspace_evidence.load_store(self.feature)["latestBatch"]
        self.assertTrue(workspace_verification.structured_verification_passed(
            batch, {"kit": "sha256:" + "a" * 64}
        ))

    def test_migration_failure_restores_v1_entrypoints(self) -> None:
        old_plan = (self.feature / "plans/implementation.md").read_bytes()
        old_verification = (self.feature / "testing/verification.md").read_bytes()
        with mock.patch("workspace_evidence.render_summary", side_effect=OSError("injected")):
            with self.assertRaisesRegex(workspace_evidence.EvidenceError, "EVIDENCE_MIGRATION_FAILED"):
                workspace_evidence.migrate_feature(self.feature, preview=False)
        self.assertEqual(old_plan, (self.feature / "plans/implementation.md").read_bytes())
        self.assertEqual(old_verification, (self.feature / "testing/verification.md").read_bytes())
        self.assertFalse((self.feature / "testing/evidence").exists())

    def test_interrupted_migration_recovers_on_next_apply(self) -> None:
        with mock.patch("workspace_evidence.render_summary", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                workspace_evidence.migrate_feature(self.feature, preview=False)
        with self.assertRaisesRegex(workspace_evidence.EvidenceError, "EVIDENCE_TRANSACTION_INCOMPLETE"):
            workspace_evidence.load_store(self.feature)

        result = workspace_evidence.migrate_feature(self.feature, preview=False)
        self.assertFalse(result["preview"])
        self.assertEqual("task-evidence-v2", workspace_status.plan_analysis(self.feature / "plans/implementation.md")["completionPolicy"])
        self.assertFalse((self.feature / "testing/evidence/.transaction.json").exists())

    def test_cli_apply_recovers_interrupted_migration_before_status(self) -> None:
        with mock.patch("workspace_evidence.render_summary", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                workspace_evidence.migrate_feature(self.feature, preview=False)
        with self.assertRaisesRegex(ValueError, "EVIDENCE_TRANSACTION_INCOMPLETE"):
            workspace_status.status_result(self.root)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(0, workspace_verification.main([
                "migrate", "demo-feature", "--root", str(self.root), "--apply", "--json"
            ]))
        self.assertFalse((self.feature / "testing/evidence/.transaction.json").exists())

    def test_corrupted_backup_is_not_overwritten_on_retry(self) -> None:
        with mock.patch("workspace_evidence.render_summary", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                workspace_evidence.migrate_feature(self.feature, preview=False)
        transactions = self.feature / "testing/archive/transactions"
        backup = next(transactions.glob("*/verification.md"))
        backup.write_text("damaged backup\n", encoding="utf-8")
        with self.assertRaisesRegex(workspace_evidence.EvidenceError, "EVIDENCE_TRANSACTION_INCOMPLETE"):
            workspace_evidence.migrate_feature(self.feature, preview=False)
        self.assertEqual("damaged backup\n", backup.read_text(encoding="utf-8"))
        self.assertTrue((self.feature / "testing/.evidence-transaction.json").is_file())

    def test_migration_preserves_failed_task_diagnostics(self) -> None:
        verification = self.feature / "testing/verification.md"
        verification.write_text(
            verification.read_text(encoding="utf-8").replace("- 跳过数：0", "- 跳过数：1"),
            encoding="utf-8",
        )
        before_status = workspace_status.status_result(self.root)
        before = before_status["features"][0]
        workspace_evidence.migrate_feature(self.feature, preview=False)
        after_status = workspace_status.status_result(self.root)
        after = after_status["features"][0]
        self.assertEqual(before["trustedProgress"], after["trustedProgress"])
        self.assertEqual(before["verificationPassed"], after["verificationPassed"])
        self.assertEqual(before_status["currentStage"], after_status["currentStage"])
        self.assertEqual(before_status["blockers"], after_status["blockers"])
        self.assertEqual(
            {(item["severity"], item["code"], item["message"]) for item in before["documentDiagnostics"]},
            {(item["severity"], item["code"], item["message"]) for item in after["documentDiagnostics"]},
        )

    def test_migration_preserves_incomplete_legacy_evidence(self) -> None:
        verification = self.feature / "testing/verification.md"
        verification.write_text(
            verification.read_text(encoding="utf-8").replace(
                f'- 代码状态：{{"kit":"sha256:{"a" * 64}"}}\n', "", 1
            ),
            encoding="utf-8",
        )
        before = workspace_status.status_result(self.root)["features"][0]
        workspace_evidence.migrate_feature(self.feature, preview=False)
        after = workspace_status.status_result(self.root)["features"][0]
        self.assertEqual(before["trustedProgress"], after["trustedProgress"])
        self.assertEqual(
            {(item["severity"], item["code"], item["message"]) for item in before["documentDiagnostics"]},
            {(item["severity"], item["code"], item["message"]) for item in after["documentDiagnostics"]},
        )

    def test_inspect_reads_v2_index_instead_of_human_markdown(self) -> None:
        workspace_evidence.migrate_feature(self.feature, preview=False)
        (self.feature / "testing/verification.md").write_text("manually changed\n", encoding="utf-8")
        detail = workspace_inspect.feature(self.root.resolve(), "demo-feature")
        verification = workspace_inspect.verification(self.root.resolve(), "demo-feature", False)
        self.assertEqual("task-evidence-v2", detail["summary"]["planSummary"]["completionPolicy"])
        self.assertTrue(detail["tasks"][0]["trusted"])
        self.assertIsNotNone(verification["selectedBatch"])
        self.assertTrue(detail["summary"]["verificationSummary"]["exists"])
        self.assertEqual("failed", verification["selectedBatch"]["recordedResult"])

    def test_render_preview_is_read_only_and_apply_restores_human_view(self) -> None:
        workspace_evidence.migrate_feature(self.feature, preview=False)
        human = self.feature / "testing/verification.md"
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(0, workspace_verification.main(["render", "demo-feature", "--root", str(self.root), "--apply", "--json"]))
        expected = human.read_bytes()
        human.write_text("manual edit\n", encoding="utf-8")
        before_state = workspace_status.status_result(self.root)["features"][0]["trustedProgress"]
        self.assertEqual("drifted", workspace_status.status_result(self.root)["features"][0]["humanViewState"])
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(0, workspace_verification.main(["render", "demo-feature", "--root", str(self.root), "--json"]))
        self.assertTrue(json.loads(output.getvalue())["changed"])
        self.assertEqual(b"manual edit\n", human.read_bytes())
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(0, workspace_verification.main(["render", "demo-feature", "--root", str(self.root), "--apply", "--json"]))
        self.assertEqual(expected, human.read_bytes())
        self.assertEqual(before_state, workspace_status.status_result(self.root)["features"][0]["trustedProgress"])
        self.assertEqual("matched", workspace_status.status_result(self.root)["features"][0]["humanViewState"])

    def test_cli_rolls_back_when_migration_state_differs(self) -> None:
        with mock.patch("workspace_verification._state_signature", side_effect=[{"state": 1}, {"state": 2}]):
            with mock.patch("sys.stderr"):
                code = workspace_verification.main([
                    "migrate", "demo-feature", "--root", str(self.root), "--apply", "--json"
                ])
        self.assertEqual(1, code)
        self.assertEqual("task-evidence-v1", workspace_status.plan_analysis(self.feature / "plans/implementation.md")["completionPolicy"])
        self.assertFalse((self.feature / "testing/evidence").exists())


if __name__ == "__main__":
    unittest.main()
