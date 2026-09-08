from __future__ import annotations

import contextlib
import hashlib
import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import workspace_setup  # noqa: E402
import workspace_status  # noqa: E402
import feature_context  # noqa: E402
import workspace_verification  # noqa: E402


def snapshot(path: Path) -> dict[str, str]:
    return {
        item.relative_to(path).as_posix(): hashlib.sha256(item.read_bytes()).hexdigest()
        for item in path.rglob("*")
        if item.is_file() and ".git" not in item.parts
    }


class WorkspaceStatusTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.parent = Path(self.temp.name).resolve()
        self.root = self.parent / "kit"
        self.root.mkdir()
        (self.root / ".gitignore").write_text(".workspace/\ninput.json\n", encoding="utf-8")
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        subprocess.run(["git", "-C", str(self.root), "add", ".gitignore"], check=True)
        subprocess.run(
            ["git", "-C", str(self.root), "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "fixture"],
            check=True,
        )

    def tearDown(self):
        self.temp.cleanup()

    def write_feature(
        self, base: Path, maintenance: bool = False, slug: str = "demo-feature"
    ) -> Path:
        feature = base / slug
        (feature / "plans").mkdir(parents=True)
        (feature / "testing").mkdir()
        if maintenance:
            metadata = (
                "# Demo\n\n"
                "- 状态：development\n"
                f"- 需求短名：`{slug}`\n"
                f"- 工作分支：`codex/feature/{slug}`\n"
                "- 基线分支：`main`\n"
                "- 最后更新：2026-09-01\n"
            )
        else:
            metadata = (
                "# Demo\n\n"
                "- 状态：development\n"
                "- 涉及仓库：service\n"
                "- 工作分支：service -> owner/feature/demo\n"
                "- 基线分支：service -> main\n"
                "- 最后更新：2026-09-01\n"
            )
        (feature / "README.md").write_text(metadata, encoding="utf-8")
        requirements = feature / "requirements"
        requirements.mkdir()
        (requirements / "requirements.md").write_text("# 需求\n", encoding="utf-8")
        (feature / "plans/implementation.md").write_text(
            "- [x] complete\n- [ ] pending\n", encoding="utf-8"
        )
        (feature / "testing/verification.md").write_text(
            "# 验证记录\n", encoding="utf-8"
        )
        return feature

    def initialize_workspace(self) -> None:
        service = self.parent / "service"
        subprocess.run(["git", "init", "-q", str(service)], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(service),
                "remote",
                "add",
                "origin",
                "https://example.test/service.git",
            ],
            check=True,
        )
        source = self.root / "input.json"
        source.write_text(
            json.dumps(
                {
                    "version": {"major": 1, "minor": 0},
                    "workspace": {"name": "Demo"},
                    "local": {"branchOwner": "owner", "primaryRole": None, "extensions": {}},
                    "context": {},
                    "branchPolicy": {},
                    "extensions": {"providers": {}, "config": {}},
                    "repositories": [
                        {
                            "path": "service",
                            "aliases": [],
                            "remote": "https://example.test/service.git",
                            "category": "backend",
                            "description": "Service",
                            "instruction": "docs/repositories/service.md",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        preview = workspace_setup._preview_state(self.root, "init", source)[2]
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(0, workspace_setup.apply(self.root, "init", source, preview))

    def test_maintenance_mode_is_read_only_and_summarizes_progress(self):
        feature = self.write_feature(
            self.root / "docs/development/features", maintenance=True
        )
        archived = self.write_feature(
            self.root / "docs/development/features",
            maintenance=True,
            slug="old-feature",
        )
        archived_readme = archived / "README.md"
        archived_readme.write_text(
            archived_readme.read_text(encoding="utf-8").replace(
                "状态：development", "状态：done"
            ),
            encoding="utf-8",
        )
        before = snapshot(self.root)

        value = workspace_status.status_result(self.root)

        self.assertEqual(before, snapshot(self.root))
        self.assertEqual("maintenance", value["mode"])
        self.assertIsNone(value["workspace"])
        self.assertEqual([], value["repositories"])
        self.assertEqual([], value["candidateSiblingRepositories"])
        self.assertEqual(
            {
                "activeIds": [],
                "providers": {},
                "repositoryOverrides": {},
                "blockedCodes": [],
            },
            value["extensions"],
        )
        self.assertEqual({"enabled": False}, value["workflow"])
        self.assertEqual(1, value["schemaVersion"])
        self.assertEqual("feature.implement", value["currentStage"])
        self.assertEqual([], value["blockers"])
        self.assertEqual("local", value["confirmation"]["category"])
        self.assertEqual("feature.implement", value["nextActions"][0]["stage"])
        self.assertEqual(1, len(value["features"]))
        self.assertEqual(
            {
                "featureSlug": "demo-feature",
                "path": "docs/development/features/demo-feature",
                "status": "development",
                "repositories": ["kit"],
                "branches": [["kit", "codex/feature/demo-feature"]],
                "baseBranches": [["kit", "main"]],
                "lastUpdated": "2026-09-01",
                "designExists": False,
                "planExists": True,
                "progress": {"completed": 1, "total": 2},
                "documentReviews": {
                    "requirements": "未记录",
                    "design": "未记录",
                    "plan": "未记录",
                },
                "documentReviewsRecorded": False,
                "documentDiagnostics": [
                    {
                        "severity": "warning",
                        "code": "PLAN_UNNUMBERED_TASK",
                        "path": "implementation.md",
                        "line": 1,
                        "message": "未编号任务仍会统计；新计划请使用 T01 等稳定编号",
                    },
                    {
                        "severity": "warning",
                        "code": "PLAN_UNNUMBERED_TASK",
                        "path": "implementation.md",
                        "line": 2,
                        "message": "未编号任务仍会统计；新计划请使用 T01 等稳定编号",
                    },
                ],
                "verificationExists": True,
                "verificationPassed": False,
                "artifacts": [],
            },
            value["features"][0],
        )
        self.assertEqual(feature, self.root / value["features"][0]["path"])

        plan = feature / "plans/implementation.md"
        plan.unlink()
        plan.parent.rmdir()
        plan.parent.symlink_to(self.parent)
        with self.assertRaisesRegex(ValueError, "符号链接"):
            workspace_status.status_result(self.root)

    def test_feature_progress_reports_files_without_inferring_approval(self):
        feature = self.write_feature(
            self.root / "docs/development/features", maintenance=True
        )
        readme = feature / "README.md"
        readme.write_text(
            readme.read_text(encoding="utf-8").replace(
                "状态：development", "状态：planning"
            ),
            encoding="utf-8",
        )
        plan = feature / "plans/implementation.md"
        plan.unlink()

        result = workspace_status.status_result(self.root)
        self.assertEqual(
            "需求 demo-feature 的需求记录已存在；讨论并确认方案后生成设计文档",
            result["nextActions"][0]["reason"],
        )

        design = feature / "design/design.md"
        design.parent.mkdir()
        design.write_text("# 设计\n", encoding="utf-8")
        result = workspace_status.status_result(self.root)
        self.assertEqual(
            "需求 demo-feature 的设计文档已存在；审阅确认后生成实施计划",
            result["nextActions"][0]["reason"],
        )

        plan.write_text("- [ ] 实现\n", encoding="utf-8")
        result = workspace_status.status_result(self.root)
        self.assertEqual(
            "需求 demo-feature 的实施计划已存在；审阅并批准计划、基线、分支和执行方式后更新为 development",
            result["nextActions"][0]["reason"],
        )

        plan.unlink()
        readme.write_text(
            readme.read_text(encoding="utf-8").replace(
                "状态：planning", "状态：development"
            ),
            encoding="utf-8",
        )
        result = workspace_status.status_result(self.root)
        self.assertEqual(
            "需求 demo-feature 缺少可执行的实施计划",
            result["nextActions"][0]["reason"],
        )

    def test_planning_feature_requires_each_document_review_before_its_successor(self):
        feature = self.write_feature(
            self.root / "docs/development/features", maintenance=True
        )
        readme = feature / "README.md"
        readme.write_text(
            readme.read_text(encoding="utf-8")
            .replace("状态：development", "状态：planning")
            .replace(
                "- 最后更新：2026-09-01\n",
                "- 需求审阅：待审阅\n"
                "- 设计审阅：未生成\n"
                "- 计划审阅：未生成\n"
                "- 最后更新：2026-09-01\n",
            ),
            encoding="utf-8",
        )
        (feature / "plans/implementation.md").unlink()

        result = workspace_status.status_result(self.root)
        self.assertIn("审阅实际需求文件", result["nextActions"][0]["reason"])

        readme.write_text(
            readme.read_text(encoding="utf-8").replace("需求审阅：待审阅", "需求审阅：已批准"),
            encoding="utf-8",
        )
        result = workspace_status.status_result(self.root)
        self.assertIn("生成设计文档", result["nextActions"][0]["reason"])

        design = feature / "design/design.md"
        design.parent.mkdir()
        design.write_text("# 设计\n", encoding="utf-8")
        readme.write_text(
            readme.read_text(encoding="utf-8").replace("设计审阅：未生成", "设计审阅：待审阅"),
            encoding="utf-8",
        )
        result = workspace_status.status_result(self.root)
        self.assertIn("审阅实际设计文件", result["nextActions"][0]["reason"])

        readme.write_text(
            readme.read_text(encoding="utf-8").replace("设计审阅：待审阅", "设计审阅：已批准"),
            encoding="utf-8",
        )
        result = workspace_status.status_result(self.root)
        self.assertIn("生成实施计划", result["nextActions"][0]["reason"])

        plan = feature / "plans/implementation.md"
        plan.write_text("- [ ] T01 实现\n", encoding="utf-8")
        readme.write_text(
            readme.read_text(encoding="utf-8").replace("计划审阅：未生成", "计划审阅：待审阅"),
            encoding="utf-8",
        )
        result = workspace_status.status_result(self.root)
        self.assertIn("审阅并批准实际计划", result["nextActions"][0]["reason"])
        self.assertEqual(
            {"requirements": "已批准", "design": "已批准", "plan": "待审阅"},
            result["features"][0]["documentReviews"],
        )

    def test_document_review_diagnostics_do_not_infer_missing_files_are_approved(self):
        feature = self.write_feature(
            self.root / "docs/development/features", maintenance=True
        )
        readme = feature / "README.md"
        readme.write_text(
            readme.read_text(encoding="utf-8").replace(
                "- 最后更新：2026-09-01\n",
                "- 需求审阅：无法识别\n"
                "- 设计审阅：已批准\n"
                "- 计划审阅：未生成\n"
                "- 最后更新：2026-09-01\n",
            ),
            encoding="utf-8",
        )

        feature_state = workspace_status.status_result(self.root)["features"][0]

        self.assertEqual("未记录", feature_state["documentReviews"]["requirements"])
        self.assertEqual("已批准", feature_state["documentReviews"]["design"])
        codes = {item["code"] for item in feature_state["documentDiagnostics"]}
        self.assertIn("DOCUMENT_REVIEW_INVALID", codes)
        self.assertIn("DOCUMENT_REVIEW_MISSING_FILE", codes)

    def test_document_review_rejects_symlinked_requirement_file(self):
        feature = self.write_feature(
            self.root / "docs/development/features", maintenance=True
        )
        readme = feature / "README.md"
        readme.write_text(
            readme.read_text(encoding="utf-8").replace(
                "- 最后更新：2026-09-01\n",
                "- 需求审阅：已批准\n"
                "- 设计审阅：未生成\n"
                "- 计划审阅：未生成\n"
                "- 最后更新：2026-09-01\n",
            ),
            encoding="utf-8",
        )
        requirement = feature / "requirements/requirements.md"
        target = self.parent / "requirement.md"
        requirement.replace(target)
        requirement.symlink_to(target)

        diagnostics = workspace_status.status_result(self.root)["features"][0]["documentDiagnostics"]

        self.assertIn("DOCUMENT_REVIEW_MISSING_FILE", {item["code"] for item in diagnostics})

    def test_plan_analysis_counts_standard_and_legacy_tasks_without_fenced_examples(self):
        plan = self.root / "plan.md"
        plan.write_text(
            "# 实施计划\n\n"
            "- [ ] T01 标准任务\n\n"
            "  依赖：无\n"
            "  依据：[R1](../requirements/requirements.md)\n\n"
            "- [x] T02 已完成任务\n\n"
            "  依赖：T01\n\n"
            "```markdown\n"
            "- [ ] T99 示例任务\n"
            "```\n\n"
            "### [ ] 3. 历史标题任务\n",
            encoding="utf-8",
        )

        analysis = workspace_status.plan_analysis(plan)

        self.assertTrue(analysis["exists"])
        self.assertEqual({"completed": 1, "total": 3}, workspace_status.plan_progress(plan))
        self.assertEqual(
            [
                (False, "- [ ] T01 标准任务"),
                (True, "- [x] T02 已完成任务"),
                (False, "### [ ] 3. 历史标题任务"),
            ],
            workspace_status.plan_tasks(plan),
        )
        tasks = analysis["tasks"]
        self.assertEqual("T01", tasks[0]["id"])
        self.assertEqual(["T01"], tasks[1]["dependencies"])
        self.assertIsNone(tasks[2]["id"])
        self.assertFalse(any(item["severity"] == "error" for item in analysis["diagnostics"]))

    def test_plan_analysis_reports_invalid_tasks_and_dependencies(self):
        plan = self.root / "plan.md"
        plan.write_text(
            "- [ ] T01 第一项\n\n"
            "  依赖：T02\n\n"
            "- [ ] T01 重复编号\n\n"
            "  依赖：T99\n\n"
            "- [ ] T02 第二项\n\n"
            "  依赖：T02\n\n"
            "- [ ] T03 第三项\n\n"
            "  依赖：T04\n\n"
            "- [ ] T04 第四项\n\n"
            "  依赖：T03\n\n"
            "```\n"
            "- [ ] T03 围栏内任务\n",
            encoding="utf-8",
        )

        diagnostics = workspace_status.plan_analysis(plan)["diagnostics"]

        codes = {item["code"] for item in diagnostics}
        self.assertIn("PLAN_DUPLICATE_TASK_ID", codes)
        self.assertIn("PLAN_UNKNOWN_DEPENDENCY", codes)
        self.assertIn("PLAN_SELF_DEPENDENCY", codes)
        self.assertIn("PLAN_DEPENDENCY_CYCLE", codes)
        self.assertIn("PLAN_UNCLOSED_FENCE", codes)
        self.assertTrue(all(item["line"] > 0 for item in diagnostics))

    def test_plan_analysis_points_unclosed_fence_to_its_opening_line(self):
        plan = self.root / "plan.md"
        plan.write_text(
            "```\n示例\n```\n\n- [ ] T01 正常任务\n\n~~~~\n未闭合\n",
            encoding="utf-8",
        )

        diagnostics = workspace_status.plan_analysis(plan)["diagnostics"]

        fence = next(item for item in diagnostics if item["code"] == "PLAN_UNCLOSED_FENCE")
        self.assertEqual(7, fence["line"])

    def test_plan_analysis_stops_standard_task_before_next_group_heading(self):
        plan = self.root / "plan.md"
        plan.write_text(
            "## 任务\n\n"
            "### 1. 第一组\n\n"
            "- [ ] T01 第一项\n\n"
            "  ```markdown\n"
            "  ### 围栏内标题\n"
            "  ```\n\n"
            "### 2. 第二组\n\n"
            "- [ ] T02 第二项\n",
            encoding="utf-8",
        )

        tasks = workspace_status.plan_analysis(plan)["tasks"]

        self.assertEqual(2, len(tasks))
        self.assertEqual(10, tasks[0]["endLine"])
        self.assertEqual(13, tasks[1]["startLine"])

    def test_plan_analysis_rejects_dependency_ranges(self):
        for separator in ("-", "–", "—", "~", "至"):
            with self.subTest(separator=separator):
                plan = self.root / "plan.md"
                plan.write_text(
                    "- [ ] T01 第一项\n\n"
                    "  依赖：无\n\n"
                    "- [ ] T02 第二项\n\n"
                    "  依赖：无\n\n"
                    "- [ ] T03 第三项\n\n"
                    f"  依赖：T01{separator}T02\n",
                    encoding="utf-8",
                )

                analysis = workspace_status.plan_analysis(plan)

                ranges = [
                    item
                    for item in analysis["diagnostics"]
                    if item["code"] == "PLAN_DEPENDENCY_RANGE"
                ]
                self.assertEqual(1, len(ranges))
                self.assertEqual(11, ranges[0]["line"])
                self.assertEqual(["T01", "T02"], analysis["tasks"][2]["dependencies"])

        plan.write_text(
            "- [ ] T01 第一项\n\n"
            "  依赖：无\n\n"
            "- [ ] T02 第二项\n\n"
            "  依赖：无\n\n"
            "- [ ] T03 第三项\n\n"
            "  依赖：T01、T02\n",
            encoding="utf-8",
        )
        analysis = workspace_status.plan_analysis(plan)
        self.assertNotIn(
            "PLAN_DEPENDENCY_RANGE",
            {item["code"] for item in analysis["diagnostics"]},
        )
        self.assertEqual(["T01", "T02"], analysis["tasks"][2]["dependencies"])

    def test_current_batch_passes_until_maintenance_code_changes(self):
        feature = self.write_feature(
            self.root / "docs/development/features", maintenance=True
        )
        (feature / "plans/implementation.md").write_text("- [x] complete\n", encoding="utf-8")
        excluded = (
            "docs/development/features/demo-feature/README.md",
            "docs/development/features/demo-feature/plans/implementation.md",
            "docs/development/features/demo-feature/testing/verification.md",
        )
        states = {"kit": workspace_verification.git_fingerprint(self.root, excluded)}
        (feature / "testing/verification.md").write_text(
            "## 验证批次 2026-09-07T16:00:00+08:00\n"
            "- 总体结果：通过\n"
            "- 审查结论：通过\n"
            f"- 代码状态：{workspace_verification.encode_code_state(states)}\n\n"
            "### 检查 1\n"
            "- 工作目录：`/tmp/kit`\n"
            "- 命令：`python3 -m unittest`\n"
            "- 退出状态：0\n"
            "- 结果：通过\n",
            encoding="utf-8",
        )

        current = workspace_status.status_result(self.root)
        self.assertTrue(current["features"][0]["verificationPassed"])
        self.assertEqual("feature.complete", current["currentStage"])

        (self.root / "source.txt").write_text("changed\n", encoding="utf-8")
        stale = workspace_status.status_result(self.root)
        self.assertFalse(stale["features"][0]["verificationPassed"])
        self.assertEqual("feature.verify", stale["currentStage"])

    def test_dangling_artifact_directory_is_rejected(self):
        feature = self.write_feature(
            self.root / "docs/development/features", maintenance=True
        )
        (feature / "artifacts").symlink_to(self.parent / "missing-artifacts", target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "交付物目录不安全"):
            workspace_status.status_result(self.root)

    def test_multiple_active_features_block_automatic_routing(self):
        self.write_feature(self.root / "docs/development/features", maintenance=True)
        self.write_feature(
            self.root / "docs/development/features", maintenance=True, slug="second-feature"
        )
        value = workspace_status.status_result(self.root)
        self.assertIsNone(value["currentStage"])
        self.assertEqual(["MULTIPLE_ACTIVE_FEATURES"], value["blockers"])

        actions = value["nextActions"]
        self.assertEqual(1, len(actions))
        action = actions[0]
        self.assertEqual("feature.context", action["stage"])
        self.assertEqual("semantic", action["confirmation"])
        self.assertIn("demo-feature", action["reason"])
        self.assertIn("second-feature", action["reason"])

    def test_paused_feature_reports_action_to_resume_or_reselect(self):
        self.initialize_workspace()
        feature = self.write_feature(self.root / ".workspace/docs/features")
        readme = feature / "README.md"
        readme.write_text(
            readme.read_text(encoding="utf-8").replace(
                "状态：development", "状态：paused"
            ),
            encoding="utf-8",
        )

        value = workspace_status.status_result(self.root)

        self.assertEqual(["FEATURE_PAUSED"], value["blockers"])
        actions = value["nextActions"]
        self.assertEqual(1, len(actions))
        self.assertEqual("feature.context", actions[0]["stage"])
        self.assertEqual("semantic", actions[0]["confirmation"])
        self.assertIn("demo-feature", actions[0]["reason"])

    def test_next_action_runbooks_exist_in_repository(self):
        for stage in workspace_status.STAGE_RUNBOOKS:
            with self.subTest(stage=stage):
                runbook = ROOT / workspace_status.STAGE_RUNBOOKS[stage]
                self.assertTrue(runbook.is_file(), f"runbook 缺失：{runbook}")

    def test_implementation_stage_uses_dedicated_execution_skill(self):
        self.assertEqual(
            ".agents/skills/workspace-execute-plan/SKILL.md",
            workspace_status.STAGE_RUNBOOKS["feature.implement"],
        )

    def test_workspace_multiple_features_without_pointer_still_blocks(self):
        self.initialize_workspace()
        self.write_feature(self.root / ".workspace/docs/features", slug="alpha")
        self.write_feature(self.root / ".workspace/docs/features", slug="beta")

        value = workspace_status.status_result(self.root)

        self.assertIsNone(value["currentStage"])
        self.assertEqual(["MULTIPLE_ACTIVE_FEATURES"], value["blockers"])
        self.assertNotIn("otherActiveFeatures", value)

    def test_workspace_multiple_features_with_valid_pointer_routes_to_active_feature(self):
        self.initialize_workspace()
        self.write_feature(self.root / ".workspace/docs/features", slug="alpha")
        self.write_feature(self.root / ".workspace/docs/features", slug="beta")
        feature_context.set_active_feature(self.root, "alpha")

        value = workspace_status.status_result(self.root)

        self.assertEqual([], value["blockers"])
        self.assertEqual(["beta"], value["otherActiveFeatures"])
        self.assertEqual(1, len(value["nextActions"]))
        self.assertIn("alpha", value["nextActions"][0]["reason"])

    def test_workspace_multiple_features_with_invalid_pointer_is_blocked(self):
        self.initialize_workspace()
        self.write_feature(self.root / ".workspace/docs/features", slug="alpha")
        self.write_feature(self.root / ".workspace/docs/features", slug="beta")
        local = self.root / ".workspace/workspace.local.json"
        payload = json.loads(local.read_text(encoding="utf-8"))
        payload["activeFeature"] = "ghost-feature"
        local.write_text(json.dumps(payload), encoding="utf-8")

        value = workspace_status.status_result(self.root)

        self.assertEqual(["ACTIVE_FEATURE_INVALID"], value["blockers"])
        self.assertIn("ghost-feature", value["nextActions"][0]["reason"])
        self.assertIn("alpha", value["nextActions"][0]["reason"])
        self.assertIn("beta", value["nextActions"][0]["reason"])

    def test_workspace_single_feature_never_reports_other_active_features(self):
        self.initialize_workspace()
        self.write_feature(self.root / ".workspace/docs/features", slug="alpha")
        feature_context.set_active_feature(self.root, "alpha")

        value = workspace_status.status_result(self.root)

        self.assertEqual([], value["blockers"])
        self.assertNotIn("otherActiveFeatures", value)

    def test_workspace_one_degraded_feature_does_not_hide_the_healthy_one(self):
        self.initialize_workspace()
        self.write_feature(self.root / ".workspace/docs/features", slug="alpha")
        broken = self.write_feature(self.root / ".workspace/docs/features", slug="beta")
        (broken / "README.md").write_text(
            "# Demo\n\n- 状态：not-a-real-status\n", encoding="utf-8"
        )

        value = workspace_status.status_result(self.root)

        self.assertEqual(["alpha"], [item["featureSlug"] for item in value["features"]])
        self.assertEqual(1, len(value["degradedFeatures"]))
        degraded = value["degradedFeatures"][0]
        self.assertEqual(".workspace/docs/features/beta", degraded["path"])
        self.assertTrue(degraded["error"])

    def test_maintenance_one_degraded_feature_does_not_hide_the_healthy_one(self):
        self.write_feature(self.root / "docs/development/features", maintenance=True, slug="alpha")
        broken = self.write_feature(
            self.root / "docs/development/features", maintenance=True, slug="beta"
        )
        (broken / "README.md").write_text(
            "# Demo\n\n- 状态：not-a-real-status\n", encoding="utf-8"
        )

        value = workspace_status.status_result(self.root)

        self.assertEqual(["alpha"], [item["featureSlug"] for item in value["features"]])
        self.assertEqual(1, len(value["degradedFeatures"]))
        degraded = value["degradedFeatures"][0]
        self.assertEqual("docs/development/features/beta", degraded["path"])
        self.assertTrue(degraded["error"])

    def test_no_degraded_features_key_when_everything_is_healthy(self):
        self.write_feature(self.root / "docs/development/features", maintenance=True, slug="alpha")

        value = workspace_status.status_result(self.root)

        self.assertNotIn("degradedFeatures", value)

    def test_registry_itself_corrupted_still_fails_entirely(self):
        self.initialize_workspace()
        self.write_feature(self.root / ".workspace/docs/features", slug="alpha")
        registry = self.root / ".workspace/workspace.json"
        registry.write_text("not valid json", encoding="utf-8")

        value = workspace_status.status_result(self.root)

        # 损坏的 registry 会被既有的 Extension 兼容性检查识别为阻塞（PROVIDER_CONFLICT/
        # ADAPTER_DRIFT），走整体阻塞路径；不会静默把它当成某个 feature 的降级。
        self.assertTrue(value["blockers"])
        self.assertEqual([], value["features"])
        self.assertNotIn("degradedFeatures", value)

    def test_symlinked_feature_directory_still_fails_entirely(self):
        self.initialize_workspace()
        real = self.write_feature(self.root / ".workspace/docs/features", slug="alpha")
        features_root = self.root / ".workspace/docs/features"
        link = features_root / "linked"
        link.symlink_to(real, target_is_directory=True)

        with self.assertRaises(Exception):
            workspace_status.status_result(self.root)

    def test_workspace_mode_reports_repositories_features_and_json(self):
        service = self.parent / "service"
        subprocess.run(["git", "init", "-q", str(service)], check=True)
        subprocess.run(
            ["git", "-C", str(service), "checkout", "-q", "-b", "owner/feature/demo"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(service), "remote", "add", "origin", "https://example.test/service.git"],
            check=True,
        )
        sibling = self.parent / "unregistered"
        subprocess.run(["git", "init", "-q", str(sibling)], check=True)
        source = self.root / "input.json"
        source.write_text(
            json.dumps(
                {
                    "version": {"major": 1, "minor": 0},
                    "workspace": {"name": "Demo"},
                    "local": {"branchOwner": "owner", "primaryRole": None, "extensions": {}},
                    "context": {},
                    "branchPolicy": {},
                    "extensions": {"providers": {}, "config": {}},
                    "repositories": [
                        {
                            "path": "service",
                            "aliases": [],
                            "remote": "https://example.test/service.git",
                            "category": "backend",
                            "description": "Service",
                            "instruction": "docs/repositories/service.md",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        preview = workspace_setup._preview_state(self.root, "init", source)[2]
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(0, workspace_setup.apply(self.root, "init", source, preview))
        for filename in ("AGENTS.md", "CLAUDE.md", "GEMINI.md"):
            shutil.copy2(ROOT / filename, self.root / filename)
        for directory in (".agents", ".claude"):
            shutil.copytree(ROOT / directory, self.root / directory, symlinks=True)
        self.write_feature(self.root / ".workspace/docs/features")
        artifacts = self.root / ".workspace/docs/features/demo-feature/artifacts/sql"
        artifacts.mkdir(parents=True)
        (artifacts / "001-create.sql").write_text("create table demo;\n", encoding="utf-8")
        before = snapshot(self.root)

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = workspace_status.main(
                ["--root", str(self.root), "--json"]
            )
        value = json.loads(output.getvalue())

        self.assertEqual(0, code)
        self.assertEqual(before, snapshot(self.root))
        self.assertEqual("workspace", value["mode"])
        self.assertEqual("Demo", value["workspace"]["name"])
        self.assertEqual(
            [{"path": "service", "installed": True, "currentBranch": "owner/feature/demo"}],
            value["repositories"],
        )
        self.assertEqual([str(sibling)], value["candidateSiblingRepositories"])
        self.assertEqual("demo-feature", value["features"][0]["featureSlug"])
        self.assertEqual({"completed": 1, "total": 2}, value["features"][0]["progress"])
        self.assertEqual(
            [{"path": "artifacts/sql/001-create.sql", "type": "sql"}],
            value["features"][0]["artifacts"],
        )
        self.assertEqual(1, value["schemaVersion"])
        self.assertEqual("feature.implement", value["currentStage"])
        self.assertEqual("feature.implement", value["nextActions"][0]["stage"])
        self.assertEqual({"enabled": False}, value["workflow"])
        self.assertEqual({"errors": 0, "warnings": 0, "info": 1}, value["doctor"])


if __name__ == "__main__":
    unittest.main()
