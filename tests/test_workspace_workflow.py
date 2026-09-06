from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import workspace_extension  # noqa: E402
import workspace_status  # noqa: E402
import workspace_workflow  # noqa: E402


ACTION_FIXTURE = ROOT / "tests" / "fixtures" / "action-extension"


class WorkspaceWorkflowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "kit"
        self.root.mkdir()
        (self.root / ".gitignore").write_text("/.workspace/\n", encoding="utf-8")
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        state = self.root / ".workspace"
        (state / "extensions" / ".state").mkdir(parents=True)
        (state / "extensions" / ".state" / "cache").mkdir()
        (state / "docs/features").mkdir(parents=True)
        (state / "docs/repositories").mkdir()
        (state / "workspace.json").write_text(
            json.dumps(
                {
                    "version": {"major": 1, "minor": 0},
                    "workspace": {"name": "Demo"},
                    "context": {},
                    "branchPolicy": {},
                    "extensions": {"providers": {}, "config": {}},
                    "repositories": [],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        (state / "workspace.local.json").write_text(
            json.dumps({"branchOwner": "alice", "primaryRole": None, "extensions": {}})
            + "\n",
            encoding="utf-8",
        )
        (state / "extensions" / ".state" / "lock.json").write_text(
            json.dumps({"lockVersion": {"major": 1, "minor": 0}, "kitApi": 1, "extensions": [], "providers": {}})
            + "\n",
            encoding="utf-8",
        )
        (self.root / ".agents/skills").mkdir(parents=True)
        (self.root / ".claude/skills").mkdir(parents=True)
        shutil.copytree(ACTION_FIXTURE, state / "extensions/action-extension")
        self.extension_config = state / "extensions" / ".state" / "input.json"
        self.extension_config.write_text(
            json.dumps(
                {
                    "extensions": [{"id": "action-extension", "version": "1.0.0"}],
                    "providers": {},
                    "config": {"action-extension": {}},
                }
            )
            + "\n",
            encoding="utf-8",
        )
        preview = workspace_extension.preview_result(self.root, self.extension_config)
        workspace_extension.apply(self.root, self.extension_config, preview["previewHash"])
        self.overlay_config = state / "workflow-input.json"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def write_overlay(
        self,
        stages: list[dict[str, object]],
        skip_hints: list[dict[str, object]] | None = None,
    ) -> None:
        payload = {
            "schemaVersion": {"major": 1, "minor": 0},
            "workflow": "feature-development",
            "stages": stages,
        }
        if skip_hints is not None:
            payload["skipHints"] = skip_hints
        self.overlay_config.write_text(json.dumps(payload) + "\n", encoding="utf-8")

    def activate_overlay(self) -> dict[str, object]:
        preview = workspace_workflow.preview_result(self.root, self.overlay_config)
        workspace_workflow.apply(self.root, self.overlay_config, preview["previewHash"])
        return preview

    def test_no_overlay_is_a_fast_disabled_status(self) -> None:
        self.assertEqual({"enabled": False}, workspace_workflow.status_result(self.root))

    def test_preview_apply_start_and_plan_lightweight_run(self) -> None:
        self.write_overlay(
            [
                {
                    "id": "team-delivery.integration-test",
                    "after": "feature.implement",
                    "uses": "action-extension/integration-test",
                }
            ]
        )
        preview = self.activate_overlay()
        self.assertTrue(preview["previewHash"])
        self.assertTrue((self.root / ".workspace/workflow.json").is_file())

        run = workspace_workflow.start_run(
            self.root,
            run_id="light-change",
            repository="service",
            branch="owner/fix/light-change",
        )
        self.assertEqual("light-change", run["id"])
        self.assertIsNone(run["featureSlug"])
        self.assertFalse((self.root / ".workspace/docs/features/light-change").exists())

        plan = workspace_workflow.plan_result(
            self.root, "light-change", after="feature.implement"
        )
        self.assertEqual("feature.implement", plan["anchor"])
        self.assertEqual(
            ["team-delivery.integration-test"],
            [item["stage"] for item in plan["pending"]],
        )
        self.assertEqual("manual", plan["pending"][0]["trigger"])
        self.assertEqual(
            {
                "title": "Run integration tests",
                "summary": "Run the integration tests for the current change.",
            },
            plan["pending"][0]["confirmation"],
        )

    def test_standard_run_requires_and_updates_its_verification_record(self) -> None:
        self.write_overlay(
            [
                {
                    "id": "team-delivery.integration-test",
                    "after": "feature.implement",
                    "uses": "action-extension/integration-test",
                }
            ]
        )
        self.activate_overlay()
        with self.assertRaisesRegex(workspace_workflow.WorkflowCommandError, "WORKFLOW_FEATURE_MISSING"):
            workspace_workflow.start_run(self.root, run_id="feature-run", feature_slug="feature")

        verification = self.root / ".workspace/docs/features/feature/testing/verification.md"
        verification.parent.mkdir(parents=True)
        verification.write_text("# 验证记录\n", encoding="utf-8")
        run = workspace_workflow.start_run(self.root, feature_slug="feature")
        self.assertEqual("feature", run["id"])
        self.assertEqual(run, workspace_workflow.start_run(self.root, feature_slug="feature"))
        plan = workspace_workflow.plan_result(self.root, "feature", after="feature.implement")
        workspace_workflow.finish(
            self.root,
            "feature",
            "team-delivery.integration-test",
            plan["pending"][0]["planHash"],
            status="succeeded",
            summary="completed",
        )
        evidence = verification.read_text(encoding="utf-8")
        self.assertIn("Workflow Action `team-delivery.integration-test`", evidence)

        with self.assertRaisesRegex(workspace_workflow.WorkflowCommandError, "显式提供 run id"):
            workspace_workflow.start_run(self.root, repository="service", branch="owner/fix/light")

    def test_manifest_rejects_action_without_confirmation(self) -> None:
        extension = self.root / ".workspace/extensions/action-extension/workspace-extension.json"
        manifest = json.loads(extension.read_text(encoding="utf-8"))
        del manifest["actions"][0]["confirmation"]
        extension.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaisesRegex(Exception, "action confirmation"):
            workspace_extension.preview_result(self.root, self.extension_config)

    def test_preview_rejects_unknown_action_and_stale_hash(self) -> None:
        self.write_overlay(
            [
                {
                    "id": "team-delivery.unknown",
                    "after": "feature.implement",
                    "uses": "action-extension/missing",
                }
            ]
        )
        with self.assertRaisesRegex(workspace_workflow.WorkflowCommandError, "ACTION_MISSING"):
            workspace_workflow.preview_result(self.root, self.overlay_config)

        self.write_overlay(
            [
                {
                    "id": "team-delivery.integration-test",
                    "after": "feature.implement",
                    "uses": "action-extension/integration-test",
                }
            ]
        )
        preview = workspace_workflow.preview_result(self.root, self.overlay_config)
        self.write_overlay(
            [
                {
                    "id": "team-delivery.integration-test",
                    "after": "feature.verify",
                    "uses": "action-extension/integration-test",
                }
            ]
        )
        with self.assertRaisesRegex(workspace_workflow.WorkflowCommandError, "WORKFLOW_PLAN_STALE"):
            workspace_workflow.apply(self.root, self.overlay_config, preview["previewHash"])

        self.write_overlay(
            [
                {
                    "id": "team-delivery.sensitive",
                    "after": "feature.implement",
                    "uses": "action-extension/integration-test",
                    "with": {"token": "must-not-be-stored"},
                }
            ]
        )
        with self.assertRaisesRegex(workspace_workflow.WorkflowCommandError, "WORKFLOW_INVALID"):
            workspace_workflow.preview_result(self.root, self.overlay_config)

    def test_apply_refuses_a_tracked_workflow_state_file(self) -> None:
        self.write_overlay(
            [
                {
                    "id": "team-delivery.integration-test",
                    "after": "feature.implement",
                    "uses": "action-extension/integration-test",
                }
            ]
        )
        target = self.root / ".workspace/workflow.json"
        target.write_text(self.overlay_config.read_text(encoding="utf-8"), encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(self.root), "add", "-f", ".workspace/workflow.json"],
            check=True,
        )
        preview = workspace_workflow.preview_result(self.root, self.overlay_config)
        with self.assertRaisesRegex(workspace_workflow.WorkflowCommandError, "WORKFLOW_TRACKED"):
            workspace_workflow.apply(self.root, self.overlay_config, preview["previewHash"])

    def test_finish_skip_and_fingerprint_change_control_reexecution(self) -> None:
        self.write_overlay(
            [
                {
                    "id": "team-delivery.integration-test",
                    "after": "feature.implement",
                    "uses": "action-extension/integration-test",
                },
                {
                    "id": "team-delivery.deploy-test",
                    "after": "team-delivery.integration-test",
                    "uses": "action-extension/deploy-test",
                    "trigger": "auto",
                },
            ]
        )
        self.activate_overlay()
        workspace_workflow.start_run(self.root, run_id="delivery-run")
        first = workspace_workflow.plan_result(
            self.root, "delivery-run", after="feature.implement"
        )
        integration = first["pending"][0]
        with self.assertRaisesRegex(workspace_workflow.WorkflowCommandError, "ACTION_PLAN_STALE"):
            workspace_workflow.finish(
                self.root,
                "delivery-run",
                "team-delivery.integration-test",
                "0" * 64,
                status="succeeded",
                summary="completed",
            )
        finished = workspace_workflow.finish(
            self.root,
            "delivery-run",
            "team-delivery.integration-test",
            integration["planHash"],
            status="succeeded",
            summary="completed",
        )
        self.assertEqual("succeeded", finished["status"])
        second = workspace_workflow.plan_result(
            self.root, "delivery-run", after="feature.implement"
        )
        self.assertEqual(["team-delivery.deploy-test"], [item["stage"] for item in second["pending"]])
        skipped = workspace_workflow.skip(
            self.root,
            "delivery-run",
            "team-delivery.deploy-test",
            second["pending"][0]["planHash"],
            reason="deployment deferred by user",
        )
        self.assertEqual("skipped", skipped["status"])
        self.assertEqual([], workspace_workflow.plan_result(
            self.root, "delivery-run", after="feature.implement"
        )["pending"])

        self.write_overlay(
            [
                {
                    "id": "team-delivery.integration-test",
                    "after": "feature.implement",
                    "uses": "action-extension/integration-test",
                    "with": {"suite": "changed"},
                }
            ]
        )
        self.activate_overlay()
        changed = workspace_workflow.plan_result(
            self.root, "delivery-run", after="feature.implement"
        )
        self.assertEqual(["team-delivery.integration-test"], [item["stage"] for item in changed["pending"]])

    def test_command_action_runs_and_interrupted_state_blocks_followups(self) -> None:
        self.write_overlay(
            [
                {
                    "id": "team-delivery.deploy-test",
                    "after": "feature.implement",
                    "uses": "action-extension/deploy-test",
                },
                {
                    "id": "team-delivery.integration-test",
                    "after": "team-delivery.deploy-test",
                    "uses": "action-extension/integration-test",
                },
            ]
        )
        self.activate_overlay()
        workspace_workflow.start_run(self.root, run_id="command-run")
        plan = workspace_workflow.plan_result(self.root, "command-run", after="feature.implement")
        deploy = plan["pending"][0]
        integration_item = [
            item for item in plan["pending"] if item["stage"] == "team-delivery.integration-test"
        ][0]
        with self.assertRaisesRegex(workspace_workflow.WorkflowCommandError, "ACTION_BLOCKED"):
            workspace_workflow.run_action(
                self.root,
                "command-run",
                "team-delivery.integration-test",
                integration_item["planHash"],
            )
        with mock.patch.dict(os.environ, {"DEPLOY_TOKEN": "test-token"}):
            result = workspace_workflow.run_action(
                self.root,
                "command-run",
                "team-delivery.deploy-test",
                deploy["planHash"],
            )
        self.assertEqual("succeeded", result["status"])
        followup = workspace_workflow.plan_result(
            self.root, "command-run", after="feature.implement"
        )
        run_path = self.root / ".workspace/runs/command-run.json"
        run = json.loads(run_path.read_text(encoding="utf-8"))
        run["stages"]["team-delivery.integration-test"] = {
            "fingerprint": followup["pending"][0]["fingerprint"],
            "status": "running",
            "updatedAt": "2026-09-03T00:00:00Z",
            "summary": "interrupted",
        }
        run_path.write_text(json.dumps(run), encoding="utf-8")
        status = workspace_workflow.status_result(self.root)
        self.assertTrue(status["enabled"])
        self.assertEqual(1, status["interrupted"])
        compact = workspace_status.status_result(self.root)["workflow"]
        self.assertEqual(
            {"enabled", "runs", "actions", "pending", "failed", "interrupted", "nextStage"},
            set(compact),
        )
        self.assertNotIn("skillPath", compact)
        self.assertNotIn("manifest", compact)

    def test_command_environment_and_summary_limits_are_safe(self) -> None:
        self.write_overlay(
            [
                {
                    "id": "team-delivery.deploy-test",
                    "after": "feature.implement",
                    "uses": "action-extension/deploy-test",
                },
                {
                    "id": "team-delivery.integration-test",
                    "after": "team-delivery.deploy-test",
                    "uses": "action-extension/integration-test",
                },
            ]
        )
        self.activate_overlay()
        workspace_workflow.start_run(self.root, run_id="safe-run")
        plan = workspace_workflow.plan_result(self.root, "safe-run", after="feature.implement")
        result = workspace_workflow.run_action(
            self.root,
            "safe-run",
            "team-delivery.deploy-test",
            plan["pending"][0]["planHash"],
        )
        self.assertEqual("failed", result["status"])
        self.assertEqual(["CREDENTIAL_MISSING"], result["diagnosticCodes"])
        self.assertNotIn("test-token", result["summary"])
        workspace_workflow.skip(
            self.root,
            "safe-run",
            "team-delivery.deploy-test",
            plan["pending"][0]["planHash"],
            reason="credential setup deferred",
        )
        followup = workspace_workflow.plan_result(
            self.root, "safe-run", after="feature.implement"
        )
        with self.assertRaisesRegex(workspace_workflow.WorkflowCommandError, "8 KiB"):
            workspace_workflow.finish(
                self.root,
                "safe-run",
                "team-delivery.integration-test",
                followup["pending"][0]["planHash"],
                status="succeeded",
                summary="x" * (8 * 1024 + 1),
            )

    def test_skip_suggestions_disabled_without_active_overlay(self) -> None:
        self.assertEqual(
            {"enabled": False, "suggestions": []},
            workspace_workflow.skip_suggestions(self.root, repository_count=1),
        )

    def test_skip_suggestions_returns_matching_reason_for_single_repo(self) -> None:
        self.write_overlay(
            [],
            skip_hints=[
                {
                    "stage": "feature.analyze",
                    "when": {"repositoryCount": {"max": 1}},
                    "reason": "单仓需求通常不需要跨仓分析",
                }
            ],
        )
        self.activate_overlay()

        result = workspace_workflow.skip_suggestions(self.root, repository_count=1)
        self.assertEqual(
            {
                "enabled": True,
                "suggestions": [
                    {
                        "stage": "feature.analyze",
                        "reason": "单仓需求通常不需要跨仓分析",
                        "rule": "repositoryCount<=1",
                    }
                ],
            },
            result,
        )

        multi_repo = workspace_workflow.skip_suggestions(self.root, repository_count=2)
        self.assertEqual({"enabled": True, "suggestions": []}, multi_repo)

    def test_skip_suggestions_reports_blocked_code_for_invalid_overlay_skip_hint(self) -> None:
        self.write_overlay(
            [],
            skip_hints=[
                {
                    "stage": "feature.implement",
                    "when": {"repositoryCount": {"max": 1}},
                    "reason": "非法：feature.implement 不是可选阶段",
                }
            ],
        )
        # 直接写入未经 apply 的 overlay 校验路径：模拟已激活但内容非法的场景，
        # 通过 mock _active_overlay 让 skip_suggestions 读到未经 apply 校验的 overlay。
        overlay = workspace_workflow.load_overlay(self.overlay_config)
        with mock.patch("workspace_workflow._active_overlay", return_value=overlay):
            result = workspace_workflow.skip_suggestions(self.root, repository_count=1)
        self.assertEqual(["WORKFLOW_SKIP_HINT_INVALID"], result.get("blockedCodes"))

    def test_cli_skip_suggestions_reports_zero_and_rejects_negative_repositories(self) -> None:
        self.write_overlay(
            [],
            skip_hints=[
                {
                    "stage": "feature.analyze",
                    "when": {"repositoryCount": {"max": 1}},
                    "reason": "单仓需求通常不需要跨仓分析",
                }
            ],
        )
        self.activate_overlay()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = workspace_workflow.main(
                [
                    "skip-suggestions",
                    "--root",
                    str(self.root),
                    "--repositories",
                    "1",
                    "--json",
                ]
            )
        self.assertEqual(0, code)
        payload = json.loads(output.getvalue())
        self.assertEqual(1, len(payload["suggestions"]))

        negative_code = workspace_workflow.main(
            [
                "skip-suggestions",
                "--root",
                str(self.root),
                "--repositories",
                "-1",
            ]
        )
        self.assertNotEqual(0, negative_code)


if __name__ == "__main__":
    unittest.main()
