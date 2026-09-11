from __future__ import annotations

import contextlib
import io
import json
import hashlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))


def run(args, cwd):
    subprocess.run(args, cwd=str(cwd), check=True, capture_output=True, text=True)


class MaintenanceBriefTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = (Path(self.temp.name) / "kit").resolve()
        self.root.mkdir()
        run(["git", "init", "-q", "-b", "main", str(self.root)], self.root)
        (self.root / "README.md").write_text("# kit\n", encoding="utf-8")
        run(["git", "add", "README.md"], self.root)
        run(
            [
                "git", "-c", "user.name=Test", "-c", "user.email=test@example.com",
                "commit", "-qm", "init",
            ],
            self.root,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def write_feature(self, slug: str, *, include_design: bool = True) -> Path:
        feature = self.root / "docs/development/features" / slug
        (feature / "requirements").mkdir(parents=True)
        (feature / "plans").mkdir()
        (feature / "testing").mkdir()
        if include_design:
            (feature / "design").mkdir()
            (feature / "design/design.md").write_text("# 设计\n", encoding="utf-8")
        (feature / "README.md").write_text(
            "# Demo\n\n"
            "- 状态：development\n"
            f"- 需求短名：`{slug}`\n"
            f"- 工作分支：`main`\n"
            "- 基线分支：`main`\n"
            "- 最后更新：2026-09-01\n",
            encoding="utf-8",
        )
        (feature / "requirements/requirements.md").write_text("# 需求\n", encoding="utf-8")
        (feature / "plans/implementation.md").write_text(
            "- [x] 已完成\n- [ ] 待办\n", encoding="utf-8"
        )
        (feature / "testing/verification.md").write_text(
            "# 验证记录\n\n- 命令 A：通过\n- 命令 B：通过\n", encoding="utf-8"
        )
        artifacts = feature / "artifacts/sql"
        artifacts.mkdir(parents=True)
        (artifacts / "001-init.sql").write_text("select 1;\n", encoding="utf-8")
        return feature

    def set_reviews(self, feature: Path, design: str, plan: str) -> None:
        readme = feature / "README.md"
        content = "".join(
            line
            for line in readme.read_text(encoding="utf-8").splitlines(keepends=True)
            if not line.startswith(("- 需求审阅：", "- 设计审阅：", "- 计划审阅："))
        )
        readme.write_text(
            content.replace(
                "- 最后更新：2026-09-01\n",
                "- 需求审阅：已批准\n"
                f"- 设计审阅：{design}\n"
                f"- 计划审阅：{plan}\n"
                "- 最后更新：2026-09-01\n",
            ),
            encoding="utf-8",
        )

    def write_workspace_task(self) -> tuple[Path, Path]:
        state = self.root / ".workspace"
        service = self.root.parent / "service"
        worker = self.root.parent / "worker"
        (self.root / "AGENTS.md").write_text("# Kit rules\n", encoding="utf-8")
        service.mkdir()
        worker.mkdir()
        (service / "module").mkdir()
        (service / "module/file.py").write_text("value = 1\n", encoding="utf-8")
        (service / "AGENTS.md").write_text("# Service rules\n", encoding="utf-8")
        (service / "module/AGENTS.md").write_text("# Module rules\n", encoding="utf-8")
        (worker / "AGENTS.md").write_text("# Worker rules\n", encoding="utf-8")
        (state / "workspace.json").parent.mkdir(parents=True)
        (state / "workspace.json").write_text(
            json.dumps(
                {
                    "version": {"major": 2, "minor": 0},
                    "workspace": {"name": "Demo"},
                    "context": {},
                    "branchPolicy": {},
                    "extensions": {"providers": {}, "config": {}},
                    "repositories": [
                        {
                            "path": "service",
                            "aliases": [],
                            "remote": None,
                            "category": "backend",
                            "description": "Service",
                            "instruction": "docs/repositories/service.md",
                            "sourceInstruction": "AGENTS.md",
                        },
                        {
                            "path": "worker",
                            "aliases": [],
                            "remote": None,
                            "category": "backend",
                            "description": "Worker",
                            "instruction": "docs/repositories/worker.md",
                            "sourceInstruction": "AGENTS.md",
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )
        (state / "AGENTS.md").write_text("# Workspace rules\n", encoding="utf-8")
        (state / "CONTEXT.md").write_text("# Workspace context\n", encoding="utf-8")
        profiles = state / "docs/repositories"
        profiles.mkdir(parents=True)
        (profiles / "service.md").write_text("# Service profile\n", encoding="utf-8")
        (profiles / "worker.md").write_text("# Worker profile\n", encoding="utf-8")
        feature = state / "docs/features/demo-feature"
        (feature / "requirements").mkdir(parents=True)
        (feature / "design").mkdir()
        (feature / "plans").mkdir()
        (feature / "testing").mkdir()
        (feature / "README.md").write_text(
            "# Demo\n\n"
            "- 状态：development\n"
            "- 涉及仓库：`service`\n"
            "- 工作分支：`service` -> `main`\n"
            "- 基线分支：`service` -> `main`\n"
            "- 需求审阅：已批准\n"
            "- 设计审阅：已批准\n"
            "- 计划审阅：已批准\n"
            "- 最后更新：2026-09-10\n",
            encoding="utf-8",
        )
        (feature / "requirements/requirements.md").write_text("# 需求\n", encoding="utf-8")
        (feature / "design/design.md").write_text("# 设计\n", encoding="utf-8")
        (feature / "plans/implementation.md").write_text(
            "- 完成门禁：`task-evidence-v1`\n\n"
            "- [ ] T01 修改服务\n\n"
            "  依赖：无\n"
            "  目标仓：`service`\n"
            "  验证性质：行为\n\n"
            "  **文件**\n\n"
            "  - Modify：`module/file.py`（`Service#run`）\n",
            encoding="utf-8",
        )
        (feature / "testing/verification.md").write_text("# 验证记录\n", encoding="utf-8")
        return feature, service

    def test_brief_reports_full_feature(self) -> None:
        import kit_feature_brief

        self.write_feature("demo-feature")

        result = kit_feature_brief.brief_result(self.root, "demo-feature")

        self.assertEqual("demo-feature", result["featureSlug"])
        self.assertEqual("development", result["status"])
        self.assertTrue(result["files"]["design"]["exists"])
        self.assertGreater(result["files"]["design"]["bytes"], 0)
        self.assertTrue(result["files"]["readme"]["exists"])
        self.assertEqual(1, len(result["artifacts"]))
        self.assertTrue(result["verificationTail"])
        self.assertEqual({"completed": 1, "total": 2}, result["progress"])
        self.assertEqual(["- [ ] 待办"], result["pendingTasks"])
        self.assertIsNone(result["verificationSummary"])
        self.assertEqual(
            {"pendingTasks": False, "verificationSummary": False},
            result["summaryTruncated"],
        )
        self.assertIsNotNone(result["currentStage"])
        self.assertTrue(result["nextActions"])
        self.assertEqual(1, len(result["recentCommits"]))
        self.assertEqual("main", result["recentCommits"][0]["branch"])
        self.assertNotIn("executionDecision", result)

        json.dumps(result, ensure_ascii=False)

    def test_brief_reports_missing_files_without_erroring(self) -> None:
        import kit_feature_brief

        self.write_feature("demo-feature", include_design=False)

        result = kit_feature_brief.brief_result(self.root, "demo-feature")

        self.assertFalse(result["files"]["design"]["exists"])
        self.assertEqual(0, result["files"]["design"]["bytes"])
        self.assertTrue(result["files"]["readme"]["exists"])

    def test_check_accepts_complete_main_design_without_attachments(self) -> None:
        import kit_feature_brief

        feature = self.write_feature("demo-feature")
        (feature / "design/design.md").write_text(
            "# 设计\n\n<a id=\"d01\"></a>\n## D01 写入策略\n", encoding="utf-8"
        )
        (feature / "plans/implementation.md").write_text(
            "- [ ] T01 实现写入\n\n"
            "  依据：R1、[D01](../design/design.md#d01)\n"
            "  依赖：无\n",
            encoding="utf-8",
        )

        result = kit_feature_brief.brief_result(self.root, "demo-feature", "T01")
        with contextlib.redirect_stdout(io.StringIO()):
            code = kit_feature_brief.main(
                ["--root", str(self.root), "demo-feature", "--check", "--json"]
            )

        self.assertEqual(0, code)
        self.assertEqual("T01", result["selectedTask"]["id"])
        self.assertEqual(["../design/design.md#d01"], result["selectedTask"]["references"])

    def test_check_accepts_linked_design_attachments(self) -> None:
        import kit_feature_brief

        feature = self.write_feature("demo-feature")
        design = feature / "design/design.md"
        design.write_text(
            "# 设计\n\n"
            "[字段设计](data-model.md#d01-fields)\n"
            "[接口契约](api-integration.md#d02-contract)\n\n"
            "<a id=\"d01\"></a>\n## D01 数据写入\n"
            "<a id=\"d02\"></a>\n## D02 对外接口\n",
            encoding="utf-8",
        )
        (feature / "design/data-model.md").write_text(
            "[D01](design.md#d01)\n\n<a id=\"d01-fields\"></a>\n## D01 扩展：字段设计\n",
            encoding="utf-8",
        )
        (feature / "design/api-integration.md").write_text(
            "[D02](design.md#d02)\n\n<a id=\"d02-contract\"></a>\n## D02 扩展：接口契约\n",
            encoding="utf-8",
        )
        (feature / "plans/implementation.md").write_text(
            "- [ ] T01 交付接口\n\n"
            "  依据：[D02](../design/design.md#d02)；"
            "[接口细节](../design/api-integration.md#d02-contract)\n"
            "  依赖：无\n",
            encoding="utf-8",
        )

        result = kit_feature_brief.brief_result(self.root, "demo-feature", "T01")
        with contextlib.redirect_stdout(io.StringIO()):
            code = kit_feature_brief.main(
                ["--root", str(self.root), "demo-feature", "--check", "--json"]
            )

        self.assertEqual(0, code)
        self.assertEqual(
            ["../design/design.md#d02", "../design/api-integration.md#d02-contract"],
            result["selectedTask"]["references"],
        )

    def test_check_rejects_missing_design_attachment_link_target(self) -> None:
        import kit_feature_brief

        feature = self.write_feature("demo-feature")
        (feature / "design/design.md").write_text(
            "[字段设计](data-model.md#d01-fields)\n\n"
            "<a id=\"d01\"></a>\n## D01 数据写入\n",
            encoding="utf-8",
        )

        result = kit_feature_brief.brief_result(self.root, "demo-feature")
        with contextlib.redirect_stdout(io.StringIO()):
            code = kit_feature_brief.main(
                ["--root", str(self.root), "demo-feature", "--check", "--json"]
            )

        self.assertEqual(1, code)
        self.assertIn(
            "DOCUMENT_MISSING_LINK_TARGET",
            {item["code"] for item in result["documentDiagnostics"]},
        )

    def test_check_rejects_missing_attachment_anchor_from_plan(self) -> None:
        import kit_feature_brief

        feature = self.write_feature("demo-feature")
        (feature / "design/design.md").write_text(
            "[字段设计](data-model.md#d01-fields)\n\n"
            "<a id=\"d01\"></a>\n## D01 数据写入\n",
            encoding="utf-8",
        )
        (feature / "design/data-model.md").write_text(
            "[D01](design.md#d01)\n\n<a id=\"d01-fields\"></a>\n## D01 扩展\n",
            encoding="utf-8",
        )
        (feature / "plans/implementation.md").write_text(
            "- [ ] T01 迁移\n\n"
            "  依据：[D01](../design/design.md#d01)；"
            "[字段细节](../design/data-model.md#missing)\n"
            "  依赖：无\n",
            encoding="utf-8",
        )

        result = kit_feature_brief.brief_result(self.root, "demo-feature")
        with contextlib.redirect_stdout(io.StringIO()):
            code = kit_feature_brief.main(
                ["--root", str(self.root), "demo-feature", "--check", "--json"]
            )

        self.assertEqual(1, code)
        self.assertIn("DOCUMENT_MISSING_ANCHOR", {item["code"] for item in result["documentDiagnostics"]})

    def test_check_rejects_attachments_without_main_design_decision(self) -> None:
        import kit_feature_brief

        feature = self.write_feature("demo-feature")
        (feature / "design/design.md").write_text(
            "# 设计\n\n[字段设计](data-model.md#fields)\n", encoding="utf-8"
        )
        (feature / "design/data-model.md").write_text(
            "[主设计](design.md)\n\n<a id=\"fields\"></a>\n## 字段设计\n",
            encoding="utf-8",
        )

        result = kit_feature_brief.brief_result(self.root, "demo-feature")
        with contextlib.redirect_stdout(io.StringIO()):
            code = kit_feature_brief.main(
                ["--root", str(self.root), "demo-feature", "--check", "--json"]
            )

        self.assertEqual(1, code)
        self.assertIn(
            "DESIGN_MAIN_DECISION_MISSING",
            {item["code"] for item in result["documentDiagnostics"]},
        )

    def test_attachment_change_returns_design_package_and_plan_to_pending_review(self) -> None:
        import workspace_status

        feature = self.write_feature("demo-feature")
        self.set_reviews(feature, "已批准", "已批准")
        (feature / "design/design.md").write_text(
            "[字段设计](data-model.md#d01-fields)\n\n"
            "<a id=\"d01\"></a>\n## D01 数据写入\n",
            encoding="utf-8",
        )
        (feature / "design/data-model.md").write_text(
            "[D01](design.md#d01)\n\n<a id=\"d01-fields\"></a>\n## D01 扩展\n",
            encoding="utf-8",
        )
        self.set_reviews(feature, "待审阅", "已批准")

        stale = workspace_status.status_result(self.root)

        self.assertEqual("feature.design", stale["currentStage"])
        self.assertIn(
            "DOCUMENT_REVIEW_PLAN_STALE",
            {item["code"] for item in stale["features"][0]["documentDiagnostics"]},
        )

        self.set_reviews(feature, "待审阅", "待审阅")

        result = workspace_status.status_result(self.root)

        self.assertEqual(
            {"requirements": "已批准", "design": "待审阅", "plan": "待审阅"},
            result["features"][0]["documentReviews"],
        )
        self.assertEqual("feature.design", result["currentStage"])
        self.assertNotIn(
            "DOCUMENT_REVIEW_PLAN_STALE",
            {item["code"] for item in result["features"][0]["documentDiagnostics"]},
        )

    def test_check_keeps_legacy_d1_and_heading_tasks_compatible(self) -> None:
        import kit_feature_brief

        feature = self.write_feature("demo-feature")
        (feature / "design/design.md").write_text("# 设计\n\n## D1 旧决策\n", encoding="utf-8")
        (feature / "plans/implementation.md").write_text(
            "### [ ] 1. 历史任务\n\n依据：R1、D1\n", encoding="utf-8"
        )

        result = kit_feature_brief.brief_result(self.root, "demo-feature")
        with contextlib.redirect_stdout(io.StringIO()):
            code = kit_feature_brief.main(
                ["--root", str(self.root), "demo-feature", "--check", "--json"]
            )

        self.assertEqual(0, code)
        self.assertEqual({"completed": 0, "total": 1}, result["progress"])
        self.assertIn("PLAN_LEGACY_HEADING_TASK", {item["code"] for item in result["documentDiagnostics"]})

    def test_brief_exposes_the_same_document_review_state_as_status(self) -> None:
        import kit_feature_brief

        feature = self.write_feature("demo-feature")
        readme = feature / "README.md"
        readme.write_text(
            readme.read_text(encoding="utf-8").replace(
                "- 最后更新：2026-09-01\n",
                "- 需求审阅：已批准\n"
                "- 设计审阅：待审阅\n"
                "- 计划审阅：未生成\n"
                "- 最后更新：2026-09-01\n",
            ),
            encoding="utf-8",
        )

        result = kit_feature_brief.brief_result(self.root, "demo-feature")

        self.assertEqual(
            {"requirements": "已批准", "design": "待审阅", "plan": "未生成"},
            result["documentReviews"],
        )

    def test_status_and_brief_agree_on_deferred_documents_and_verification(self) -> None:
        import kit_feature_brief
        import workspace_status
        import workspace_verification

        feature = self.write_feature("demo-feature")
        plan = feature / "plans/implementation.md"
        verification = feature / "testing/verification.md"
        completed = "- [x] 实现\n- [x] 验证\n"
        success = (
            "## 执行记录 2026-09-07\n"
            "- 工作目录：`/tmp/demo`\n"
            "- 命令：`python3 -m unittest`\n"
            "- 退出状态：0\n"
            "- 结果：通过\n"
        )
        excluded = (
            "docs/development/features/demo-feature/README.md",
            "docs/development/features/demo-feature/plans/implementation.md",
            "docs/development/features/demo-feature/testing/verification.md",
        )
        states = {"kit": workspace_verification.git_fingerprint(self.root, excluded)}
        batch = (
            "## 验证批次 2026-09-07T16:00:00+08:00\n"
            "- 总体结果：通过\n"
            "- 审查结论：通过\n"
            f"- 代码状态：{workspace_verification.encode_code_state(states)}\n\n"
            "### 检查 1\n"
            "- 工作目录：`/tmp/demo`\n"
            "- 命令：`python3 -m unittest`\n"
            "- 退出状态：0\n"
            "- 结果：通过\n"
        )
        cases = (
            (None, success, "feature.design"),
            ("# 实施计划\n", success, "feature.design"),
            (completed, None, "feature.verify"),
            (completed, "# 验证记录\n\n尚未执行验证。\n", "feature.verify"),
            (completed, "- Workflow Action `review`：succeeded\n", "feature.verify"),
            (completed, success, "feature.verify"),
            (completed, batch, "feature.complete"),
            (completed, success + success.replace("退出状态：0", "退出状态：1"), "feature.verify"),
            (completed, success + "\n## 执行记录 2026-09-07\n- 命令：待完成\n", "feature.verify"),
        )
        for plan_text, verification_text, expected in cases:
            with self.subTest(plan=plan_text, verification=verification_text):
                for path, content in ((plan, plan_text), (verification, verification_text)):
                    if content is None:
                        path.unlink(missing_ok=True)
                    else:
                        path.write_text(content, encoding="utf-8")
                status = workspace_status.status_result(self.root)
                brief = kit_feature_brief.brief_result(self.root, "demo-feature")
                self.assertEqual(expected, status["currentStage"])
                self.assertEqual(status["currentStage"], brief["currentStage"])
                self.assertEqual(status["nextActions"], brief["nextActions"])

    def test_planning_feature_guides_design_then_plan(self) -> None:
        import kit_feature_brief
        import workspace_status

        feature = self.write_feature("demo-feature")
        readme = feature / "README.md"
        readme.write_text(
            readme.read_text(encoding="utf-8").replace("状态：development", "状态：planning"),
            encoding="utf-8",
        )
        design = feature / "design/design.md"
        plan = feature / "plans/implementation.md"

        design.unlink()
        status = workspace_status.status_result(self.root)
        self.assertEqual("feature.design", status["currentStage"])
        self.assertIn("设计文档", status["nextActions"][0]["reason"])

        design.write_text("# 设计\n", encoding="utf-8")
        plan.unlink()
        brief = kit_feature_brief.brief_result(self.root, "demo-feature")
        self.assertEqual("feature.design", brief["currentStage"])
        self.assertIn("实施计划", brief["nextActions"][0]["reason"])

        plan.write_text("- [ ] 实现\n", encoding="utf-8")
        status = workspace_status.status_result(self.root)
        self.assertEqual("feature.design", status["currentStage"])
        self.assertIn("development", status["nextActions"][0]["reason"])

    def test_brief_errors_when_slug_ambiguous(self) -> None:
        import kit_feature_brief

        self.write_feature("feature-a")
        self.write_feature("feature-b")

        with self.assertRaisesRegex(ValueError, "feature-a"):
            kit_feature_brief.brief_result(self.root, None)

    def test_brief_errors_for_unknown_slug(self) -> None:
        import kit_feature_brief

        self.write_feature("demo-feature")

        with self.assertRaises(ValueError):
            kit_feature_brief.brief_result(self.root, "not-a-real-feature")

    def test_brief_json_is_deterministic_across_calls(self) -> None:
        import kit_feature_brief

        self.write_feature("demo-feature")

        first = kit_feature_brief.brief_result(self.root, "demo-feature")
        second = kit_feature_brief.brief_result(self.root, "demo-feature")
        first.pop("recentCommits")
        second.pop("recentCommits")

        self.assertEqual(first, second)

    def test_brief_reports_pending_tasks_and_latest_valid_verification(self) -> None:
        import kit_feature_brief

        feature = self.write_feature("demo-feature")
        (feature / "plans/implementation.md").write_text(
            "\n".join(["- [x] 已完成"] + [f"- [ ] 待办 {index}" for index in range(12)]),
            encoding="utf-8",
        )
        (feature / "testing/verification.md").write_text(
            "# 验证记录\n\n"
            "## 执行记录 2026-09-01\n"
            "- 工作目录：`/tmp/demo`\n"
            "- 命令：`python3 -m unittest`\n"
            "- 退出状态：0\n"
            "- 结果：通过\n\n"
            "## 执行记录 2026-09-02\n"
            "- 工作目录：`/tmp/demo`\n"
            "- 命令：`python3 -m unittest`\n"
            "- 退出状态：1\n"
            "- 结果：失败，等待修复\n",
            encoding="utf-8",
        )

        result = kit_feature_brief.brief_result(self.root, "demo-feature")

        self.assertEqual({"completed": 1, "total": 13}, result["progress"])
        self.assertEqual(
            [f"- [ ] 待办 {index}" for index in range(10)],
            result["pendingTasks"],
        )
        self.assertTrue(result["summaryTruncated"]["pendingTasks"])
        self.assertIn("失败，等待修复", result["verificationSummary"])
        self.assertNotIn("- 结果：通过", result["verificationSummary"])
        self.assertFalse(result["summaryTruncated"]["verificationSummary"])

    def test_brief_counts_legacy_heading_tasks(self) -> None:
        import kit_feature_brief

        feature = self.write_feature("demo-feature")
        (feature / "plans/implementation.md").write_text(
            "\n".join(f"### [ ] {index}. 历史任务" for index in range(1, 9)),
            encoding="utf-8",
        )

        result = kit_feature_brief.brief_result(self.root, "demo-feature")

        self.assertEqual({"completed": 0, "total": 8}, result["progress"])
        self.assertEqual(8, len(result["pendingTasks"]))

    def test_brief_selects_runnable_task_and_expands_only_the_requested_task(self) -> None:
        import kit_feature_brief

        feature = self.write_feature("demo-feature")
        (feature / "plans/implementation.md").write_text(
            "- [x] T01 完成基础\n\n"
            "- [ ] T02 实现查询\n\n"
            "  依赖：T01\n"
            "  依据：[R2](../requirements/requirements.md)\n\n"
            "- [ ] T03 后续处理\n\n"
            "  依赖：T02\n",
            encoding="utf-8",
        )

        summary = kit_feature_brief.brief_result(
            self.root, "demo-feature", execution=True
        )
        expanded = kit_feature_brief.brief_result(self.root, "demo-feature", "T02")

        self.assertEqual("T02", summary["currentTask"]["id"])
        self.assertEqual("RUN", summary["executionDecision"])
        self.assertFalse(summary["confirmationRequired"])
        self.assertEqual(["T02"], summary["readyTasks"])
        self.assertIsNone(summary["selectedTask"])
        self.assertEqual("T02", expanded["selectedTask"]["id"])
        self.assertIn("依赖：T01", expanded["selectedTask"]["body"])
        self.assertNotIn("后续处理", expanded["selectedTask"]["body"])
        with self.assertRaisesRegex(ValueError, "没有任务"):
            kit_feature_brief.brief_result(self.root, "demo-feature", "T99")

    def test_task_instruction_context_deduplicates_maintenance_root(self) -> None:
        import kit_feature_brief

        feature = self.write_feature("demo-feature")
        (self.root / "AGENTS.md").write_text("# Root rules\n", encoding="utf-8")
        module = self.root / "src/module"
        module.mkdir(parents=True)
        (module / "AGENTS.md").write_text("# Module rules\n", encoding="utf-8")
        (module / "file.py").write_text("value = 1\n", encoding="utf-8")
        (feature / "plans/implementation.md").write_text(
            "- 完成门禁：`task-evidence-v1`\n\n"
            "- [ ] T01 修改模块\n\n"
            "  依赖：无\n"
            f"  目标仓：`{self.root.name}`\n"
            "  验证性质：行为\n\n"
            "  **文件**\n\n"
            "  - Modify：`src/module/file.py`（`Module#run`）\n",
            encoding="utf-8",
        )

        result = kit_feature_brief.brief_result(
            self.root, "demo-feature", "T01"
        )

        context = result["instructionContext"]
        self.assertEqual("monotonic-narrowing", context["policy"])
        self.assertEqual(
            [(1, "kit", "AGENTS.md"), (4, "scoped", "src/module/AGENTS.md")],
            [
                (rule["level"], rule["scope"], rule["path"])
                for rule in context["rules"]
            ],
        )
        self.assertTrue(all(rule["estTokens"] > 0 for rule in context["rules"]))

    def test_instruction_rules_are_ordered_by_narrowing_level(self) -> None:
        import kit_feature_brief

        self.write_workspace_task()

        result = kit_feature_brief.brief_result(
            self.root, "demo-feature", "T01"
        )

        context = result["instructionContext"]
        self.assertEqual("monotonic-narrowing", context["policy"])
        self.assertEqual(
            [1, 2, 3, 4],
            [rule["level"] for rule in context["rules"]],
        )
        self.assertEqual(
            [
                "AGENTS.md",
                ".workspace/AGENTS.md",
                "../service/AGENTS.md",
                "../service/module/AGENTS.md",
            ],
            [rule["path"] for rule in context["rules"]],
        )
        self.assertEqual(
            [None, None, "service", "service"],
            [rule.get("repository") for rule in context["rules"]],
        )
        self.assertTrue(all(rule["estTokens"] > 0 for rule in context["rules"]))
        self.assertNotIn("CONTEXT.md", json.dumps(context))
        self.assertNotIn("docs/repositories/service.md", json.dumps(context))
        self.assertNotIn("worker", json.dumps(context))

    def test_missing_workspace_context_is_not_an_error(self) -> None:
        import kit_feature_brief

        self.write_workspace_task()
        (self.root / ".workspace/CONTEXT.md").unlink()
        (self.root / ".workspace/docs/repositories/service.md").unlink()

        result = kit_feature_brief.brief_result(
            self.root, "demo-feature", "T01"
        )

        self.assertNotIn(
            "INSTRUCTION_SOURCE_MISSING",
            {item["code"] for item in result["documentDiagnostics"]},
        )
        self.assertEqual(
            [1, 2, 3, 4],
            [rule["level"] for rule in result["instructionContext"]["rules"]],
        )

    def test_task_instruction_context_rejects_missing_or_unsafe_required_sources(self) -> None:
        import kit_feature_brief

        _, service = self.write_workspace_task()
        source = service / "AGENTS.md"
        source.unlink()

        missing = kit_feature_brief.brief_result(
            self.root, "demo-feature", "T01"
        )

        self.assertIn(
            "INSTRUCTION_SOURCE_MISSING",
            {item["code"] for item in missing["documentDiagnostics"]},
        )

        source.symlink_to(self.root / "README.md")
        unsafe = kit_feature_brief.brief_result(
            self.root, "demo-feature", "T01"
        )

        self.assertIn(
            "INSTRUCTION_SOURCE_MISSING",
            {item["code"] for item in unsafe["documentDiagnostics"]},
        )

    def test_brief_orders_all_ready_tasks_by_plan_position(self) -> None:
        import kit_feature_brief

        feature = self.write_feature("demo-feature")
        (feature / "plans/implementation.md").write_text(
            "- [x] T01 已完成\n\n"
            "- [ ] T02 当前任务\n\n"
            "  依赖：T01\n\n"
            "- [ ] T03 后续独立任务\n\n"
            "  依赖：T01\n",
            encoding="utf-8",
        )

        result = kit_feature_brief.brief_result(
            self.root, "demo-feature", execution=True
        )

        self.assertEqual("T02", result["currentTask"]["id"])
        self.assertEqual(["T02", "T03"], result["readyTasks"])
        self.assertEqual("RUN", result["executionDecision"])

    def write_evidence_plan(self, feature: Path) -> None:
        tests = self.root / "tests"
        tests.mkdir(exist_ok=True)
        (tests / "test_service.py").write_text(
            "def test_service(): pass\n", encoding="utf-8"
        )
        (feature / "plans/implementation.md").write_text(
            "- 完成门禁：`task-evidence-v1`\n\n"
            "- [x] T01 已声明完成\n\n"
            "  依赖：无\n"
            f"  目标仓：`{self.root.name}`\n"
            "  验证性质：行为\n\n"
            "  **文件**\n\n"
            "  - Modify：`README.md`\n"
            "  - Test：`tests/test_service.py`\n\n"
            "- [ ] T02 后续任务\n\n"
            "  依赖：T01\n"
            f"  目标仓：`{self.root.name}`\n"
            "  验证性质：声明式\n\n"
            "  **文件**\n\n"
            "  - Modify：`README.md`\n",
            encoding="utf-8",
        )

    def write_valid_task_evidence(self, feature: Path) -> None:
        (feature / "testing/verification.md").write_text(
            "## 任务证据 T01 2026-09-10T10:00:00Z\n\n"
            "- 交付核对：通过\n"
            f'- 代码状态：{{"{self.root.name}":"sha256:{"a" * 64}"}}\n\n'
            "### 检查 1\n\n"
            "- 类型：测试\n"
            f"- 工作目录：`{self.root}`\n"
            "- 命令：`python3 -m unittest`\n"
            "- 目标：`tests/test_service.py`\n"
            "- 执行数：1\n"
            "- 跳过数：0\n"
            "- 退出状态：0\n"
            "- 结果：通过\n",
            encoding="utf-8",
        )

    def test_brief_blocks_checked_task_without_valid_evidence(self) -> None:
        import kit_feature_brief

        feature = self.write_feature("demo-feature")
        self.write_evidence_plan(feature)
        (feature / "testing/verification.md").write_text("# 验证记录\n", encoding="utf-8")

        result = kit_feature_brief.brief_result(
            self.root, "demo-feature", execution=True
        )

        self.assertEqual({"completed": 1, "total": 2}, result["progress"])
        self.assertEqual(
            {"applicable": True, "completed": 0, "total": 2},
            result["trustedProgress"],
        )
        self.assertEqual("T01", result["currentTask"]["id"])
        self.assertEqual("BLOCKED", result["executionDecision"])
        self.assertEqual([], result["readyTasks"])

    def test_brief_unlocks_dependency_with_valid_task_evidence(self) -> None:
        import kit_feature_brief

        feature = self.write_feature("demo-feature")
        self.write_evidence_plan(feature)
        self.write_valid_task_evidence(feature)

        result = kit_feature_brief.brief_result(
            self.root, "demo-feature", execution=True
        )

        self.assertEqual(
            {"applicable": True, "completed": 1, "total": 2},
            result["trustedProgress"],
        )
        self.assertEqual("T02", result["currentTask"]["id"])
        self.assertEqual(["T02"], result["readyTasks"])
        self.assertEqual("RUN", result["executionDecision"])

    def test_legacy_plan_keeps_checkbox_execution_semantics(self) -> None:
        import kit_feature_brief

        feature = self.write_feature("demo-feature")
        (feature / "plans/implementation.md").write_text(
            "- [x] T01 已完成\n\n"
            "- [ ] T02 后续任务\n\n"
            "  依赖：T01\n",
            encoding="utf-8",
        )

        result = kit_feature_brief.brief_result(
            self.root, "demo-feature", execution=True
        )

        self.assertNotIn("trustedProgress", result)
        self.assertNotIn("completionPolicy", result)
        self.assertNotIn("taskEvidence", result)
        self.assertEqual("T02", result["currentTask"]["id"])
        self.assertEqual(["T02"], result["readyTasks"])
        self.assertEqual("RUN", result["executionDecision"])

    def test_brief_blocks_execution_when_plan_approval_is_missing(self) -> None:
        import kit_feature_brief

        feature = self.write_feature("demo-feature")
        self.set_reviews(feature, "已批准", "待审阅")

        result = kit_feature_brief.brief_result(
            self.root, "demo-feature", execution=True
        )

        self.assertEqual("BLOCKED", result["executionDecision"])
        self.assertTrue(result["confirmationRequired"])

    def test_brief_marks_plan_execution_complete(self) -> None:
        import kit_feature_brief

        feature = self.write_feature("demo-feature")
        (feature / "plans/implementation.md").write_text(
            "- [x] T01 已完成\n- [x] T02 已完成\n", encoding="utf-8"
        )

        result = kit_feature_brief.brief_result(
            self.root, "demo-feature", execution=True
        )

        self.assertEqual("COMPLETE", result["executionDecision"])
        self.assertFalse(result["confirmationRequired"])
        self.assertEqual([], result["readyTasks"])
        self.assertIsNone(result["currentTask"])

    def test_brief_task_body_stops_before_the_next_group(self) -> None:
        import kit_feature_brief

        feature = self.write_feature("demo-feature")
        (feature / "plans/implementation.md").write_text(
            "## 任务\n\n"
            "### 1. 第一组\n\n"
            "- [ ] T01 第一项\n\n"
            "  依赖：无\n\n"
            "### 2. 第二组\n\n"
            "- [ ] T02 第二项\n\n"
            "  依赖：T01\n",
            encoding="utf-8",
        )

        result = kit_feature_brief.brief_result(self.root, "demo-feature", "T01")

        self.assertNotIn("第二组", result["selectedTask"]["body"])
        self.assertEqual(2, result["progress"]["total"])

    def test_dependency_range_blocks_current_task_selection(self) -> None:
        import kit_feature_brief

        feature = self.write_feature("demo-feature")
        (feature / "plans/implementation.md").write_text(
            "- [x] T01 第一项\n\n"
            "  依赖：无\n\n"
            "- [ ] T02 第二项\n\n"
            "  依赖：T01-T03\n\n"
            "- [ ] T03 第三项\n\n"
            "  依赖：无\n",
            encoding="utf-8",
        )

        result = kit_feature_brief.brief_result(
            self.root, "demo-feature", execution=True
        )

        self.assertIsNone(result["currentTask"])
        self.assertEqual([], result["readyTasks"])
        self.assertEqual("BLOCKED", result["executionDecision"])
        self.assertIn("结构错误", result["taskBlockers"][0])
        self.assertIn(
            "PLAN_DEPENDENCY_RANGE",
            {item["code"] for item in result["documentDiagnostics"]},
        )

    def test_legacy_tasks_follow_document_order(self) -> None:
        import kit_feature_brief

        feature = self.write_feature("demo-feature")
        (feature / "plans/implementation.md").write_text(
            "- [x] 已完成\n- [ ] 第一项\n- [ ] 第二项\n", encoding="utf-8"
        )

        result = kit_feature_brief.brief_result(self.root, "demo-feature")

        self.assertEqual("第一项", result["currentTask"]["title"])

    def test_brief_does_not_treat_template_as_verification(self) -> None:
        import kit_feature_brief

        feature = self.write_feature("demo-feature")
        (feature / "testing/verification.md").write_text(
            "# 验证记录\n\n- 命令：待补充\n- 结果：待补充\n",
            encoding="utf-8",
        )

        result = kit_feature_brief.brief_result(self.root, "demo-feature")

        self.assertIsNone(result["verificationSummary"])
        self.assertFalse(result["summaryTruncated"]["verificationSummary"])

    def test_brief_marks_truncated_verification_summary(self) -> None:
        import kit_feature_brief

        feature = self.write_feature("demo-feature")
        result_text = "x" * 1201
        (feature / "testing/verification.md").write_text(
            "## 执行记录 2026-09-02\n"
            "- 工作目录：`/tmp/demo`\n"
            "- 命令：`python3 -m unittest`\n"
            "- 退出状态：0\n"
            f"- 结果：{result_text}\n",
            encoding="utf-8",
        )

        result = kit_feature_brief.brief_result(self.root, "demo-feature")

        self.assertTrue(result["summaryTruncated"]["verificationSummary"])
        self.assertEqual(1200, len(result["verificationSummary"]))

    def test_brief_stops_verification_summary_at_next_section(self) -> None:
        import kit_feature_brief

        feature = self.write_feature("demo-feature")
        (feature / "testing/verification.md").write_text(
            "## 执行记录 2026-09-02\n"
            "- 工作目录：`/tmp/demo`\n"
            "- 命令：`python3 -m unittest`\n"
            "- 退出状态：0\n"
            "- 结果：通过\n\n"
            "## 补充说明\n"
            + "x" * 1201,
            encoding="utf-8",
        )

        result = kit_feature_brief.brief_result(self.root, "demo-feature")

        self.assertIn("- 结果：通过", result["verificationSummary"])
        self.assertNotIn("补充说明", result["verificationSummary"])
        self.assertFalse(result["summaryTruncated"]["verificationSummary"])

    def test_brief_removes_target_selection_blocker_for_explicit_slug(self) -> None:
        import kit_feature_brief

        self.write_feature("demo-feature")
        self.write_feature("other-feature")

        result = kit_feature_brief.brief_result(self.root, "demo-feature")

        self.assertEqual([], result["blockers"])

    def test_workspace_brief_uses_active_feature_and_preserves_explicit_slug(self) -> None:
        import kit_feature_brief

        state = self.root / ".workspace"
        state.mkdir()
        (state / "workspace.json").write_text(
            json.dumps(
                {
                    "version": {"major": 1, "minor": 0},
                    "workspace": {"name": "Demo"},
                    "context": {},
                    "branchPolicy": {},
                    "extensions": {"providers": {}, "config": {}},
                    "repositories": [
                        {
                            "path": "service",
                            "aliases": [],
                            "remote": None,
                            "category": "backend",
                            "description": "Service",
                            "instruction": "docs/repositories/service.md",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        (state / "workspace.local.json").write_text(
            json.dumps(
                {
                    "branchOwner": "owner",
                    "primaryRole": None,
                    "extensions": {},
                    "activeFeature": "beta",
                }
            ),
            encoding="utf-8",
        )
        for slug in ("alpha", "beta"):
            feature = state / "docs/features" / slug
            (feature / "plans").mkdir(parents=True)
            (feature / "testing").mkdir()
            (feature / "README.md").write_text(
                "# Demo\n\n"
                "- 状态：development\n"
                f"- 需求短名：`{slug}`\n"
                "- 涉及仓库：`service`\n"
                "- 工作分支：`service` -> `owner/feature/demo`\n"
                "- 基线分支：`service` -> `main`\n"
                "- 最后更新：2026-09-01\n",
                encoding="utf-8",
            )
            (feature / "plans/implementation.md").write_text("- [ ] 待办\n", encoding="utf-8")
            (feature / "testing/verification.md").write_text("# 验证记录\n", encoding="utf-8")

        active = kit_feature_brief.brief_result(self.root)
        explicit = kit_feature_brief.brief_result(self.root, "alpha")

        self.assertEqual("beta", active["featureSlug"])
        self.assertEqual("alpha", explicit["featureSlug"])
        self.assertEqual([], active["blockers"])

    def test_cli_json_output_is_valid(self) -> None:
        import kit_feature_brief

        self.write_feature("demo-feature")

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = kit_feature_brief.main(["--root", str(self.root), "demo-feature", "--json"])
        self.assertEqual(0, code)
        result = json.loads(output.getvalue())
        self.assertNotIn("executionDecision", result)

    def test_cli_execution_option_exposes_finish_gate(self) -> None:
        import kit_feature_brief

        self.write_feature("demo-feature")

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = kit_feature_brief.main(
                [
                    "--root",
                    str(self.root),
                    "demo-feature",
                    "--execution",
                    "--json",
                ]
            )

        result = json.loads(output.getvalue())
        self.assertEqual(0, code)
        self.assertEqual("RUN", result["executionDecision"])
        self.assertFalse(result["confirmationRequired"])
        self.assertTrue(result["readyTasks"])

    def test_task_instruction_errors_fail_check_exit_code(self) -> None:
        import kit_feature_brief

        _, service = self.write_workspace_task()
        (service / "AGENTS.md").unlink()

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = kit_feature_brief.main(
                [
                    "--root",
                    str(self.root),
                    "demo-feature",
                    "--task",
                    "T01",
                    "--execution",
                    "--check",
                    "--json",
                ]
            )

        self.assertEqual(1, code)
        result = json.loads(output.getvalue())
        self.assertIn(
            "INSTRUCTION_SOURCE_MISSING",
            {item["code"] for item in result["documentDiagnostics"]},
        )
        self.assertEqual("RUN", result["executionDecision"])

    def test_execution_projection_keeps_only_loop_fields(self) -> None:
        import kit_feature_brief

        self.write_workspace_task()

        def run(args: list[str]) -> tuple[int, dict]:
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = kit_feature_brief.main(
                    ["--root", str(self.root), "demo-feature", *args]
                )
            return code, json.loads(output.getvalue())

        code, projected = run(
            ["--task", "T01", "--execution", "--json", "--projection", "execution"]
        )
        self.assertEqual(0, code)
        self.assertEqual(
            {
                "featureSlug",
                "selectedTask",
                "readyTasks",
                "executionDecision",
                "confirmationRequired",
                "taskBlockers",
                "instructionContext",
                "trustedProgress",
                "documentDiagnostics",
            },
            set(projected),
        )
        self.assertTrue(
            all(item["severity"] == "error" for item in projected["documentDiagnostics"])
        )
        self.assertEqual("T01", projected["selectedTask"]["id"])
        self.assertNotIn("currentTask", projected)

        # 展开非当前任务时 currentTask 不是子集，必须保留
        plan = self.root / ".workspace/docs/features/demo-feature/plans/implementation.md"
        plan.write_text(
            plan.read_text(encoding="utf-8")
            + "\n- [ ] T02 后续任务\n\n"
            "  依赖：T01\n"
            "  目标仓：`service`\n"
            "  验证性质：行为\n\n"
            "  **文件**\n\n"
            "  - Modify：`module/other.py`（`Service#next`）\n",
            encoding="utf-8",
        )
        _, other = run(
            ["--task", "T02", "--execution", "--json", "--projection", "execution"]
        )
        self.assertEqual("T02", other["selectedTask"]["id"])
        self.assertEqual("T01", other["currentTask"]["id"])

        _, full_default = run(["--task", "T01", "--execution", "--json"])
        _, full_explicit = run(
            ["--task", "T01", "--execution", "--json", "--projection", "full"]
        )
        full_default.pop("recentCommits")
        full_explicit.pop("recentCommits")
        self.assertEqual(full_default, full_explicit)
        self.assertIn("verificationTail", full_default)

    def test_cli_accepts_task_option(self) -> None:
        import kit_feature_brief

        feature = self.write_feature("demo-feature")
        (feature / "plans/implementation.md").write_text(
            "- [ ] T01 实现\n", encoding="utf-8"
        )
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = kit_feature_brief.main(
                ["--root", str(self.root), "demo-feature", "--task", "T01", "--json"]
            )

        self.assertEqual(0, code)
        self.assertEqual("T01", json.loads(output.getvalue())["selectedTask"]["id"])

    def test_check_reports_missing_local_links_and_ignores_fenced_examples(self) -> None:
        import kit_feature_brief

        feature = self.write_feature("demo-feature")
        requirements = feature / "requirements/requirements.md"
        requirements.write_text(
            "[缺失文件](missing.md)\n[缺失锚点](../design/design.md#gone)\n"
            "```markdown\n[围栏示例](also-missing.md)\n```\n",
            encoding="utf-8",
        )
        before = {
            path.relative_to(feature).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in feature.rglob("*")
            if path.is_file()
        }

        result = kit_feature_brief.brief_result(self.root, "demo-feature")
        with contextlib.redirect_stdout(io.StringIO()):
            code = kit_feature_brief.main(
                ["--root", str(self.root), "demo-feature", "--check", "--json"]
            )

        codes = {item["code"] for item in result["documentDiagnostics"]}
        self.assertIn("DOCUMENT_MISSING_LINK_TARGET", codes)
        self.assertIn("DOCUMENT_MISSING_ANCHOR", codes)
        self.assertNotIn("also-missing.md", "\n".join(item["message"] for item in result["documentDiagnostics"]))
        self.assertEqual(1, code)
        after = {
            path.relative_to(feature).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in feature.rglob("*")
            if path.is_file()
        }
        self.assertEqual(before, after)

    def test_check_allows_missing_later_documents_and_accepts_explicit_anchor(self) -> None:
        import kit_feature_brief

        feature = self.write_feature("demo-feature")
        (feature / "testing/verification.md").unlink()
        (feature / "design/design.md").write_text(
            '<a id="decision"></a>\n# 设计\n', encoding="utf-8"
        )
        (feature / "requirements/requirements.md").write_text(
            "[设计](../design/design.md#decision)\n", encoding="utf-8"
        )

        result = kit_feature_brief.brief_result(self.root, "demo-feature")

        self.assertFalse(any(item["severity"] == "error" for item in result["documentDiagnostics"]))

    def test_text_output_marks_later_documents_as_stage_based(self) -> None:
        import kit_feature_brief

        self.write_feature("demo-feature")

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(0, kit_feature_brief.main(["--root", str(self.root), "demo-feature"]))

        self.assertIn("文档：", output.getvalue())
        self.assertIn("按阶段生成：", output.getvalue())


if __name__ == "__main__":
    unittest.main()
