from __future__ import annotations

import contextlib
import io
import json
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

        json.dumps(result, ensure_ascii=False)

    def test_brief_reports_missing_files_without_erroring(self) -> None:
        import kit_feature_brief

        self.write_feature("demo-feature", include_design=False)

        result = kit_feature_brief.brief_result(self.root, "demo-feature")

        self.assertFalse(result["files"]["design"]["exists"])
        self.assertEqual(0, result["files"]["design"]["bytes"])
        self.assertTrue(result["files"]["readme"]["exists"])

    def test_status_and_brief_agree_on_deferred_documents_and_verification(self) -> None:
        import kit_feature_brief
        import workspace_status

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
        cases = (
            (None, success, "feature.design"),
            ("# 实施计划\n", success, "feature.design"),
            (completed, None, "feature.verify"),
            (completed, "# 验证记录\n\n尚未执行验证。\n", "feature.verify"),
            (completed, "- Workflow Action `review`：succeeded\n", "feature.verify"),
            (completed, success, "feature.complete"),
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
        self.assertIn("方案设计", status["nextActions"][0]["reason"])

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

    def test_brief_keeps_all_status_blockers_for_explicit_slug(self) -> None:
        import kit_feature_brief

        self.write_feature("demo-feature")
        self.write_feature("other-feature")

        result = kit_feature_brief.brief_result(self.root, "demo-feature")

        self.assertEqual(["MULTIPLE_ACTIVE_FEATURES"], result["blockers"])

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
        json.loads(output.getvalue())

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
