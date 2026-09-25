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
sys.path.insert(0, str(ROOT))

import workbench.extensions.management as workspace_extension  # noqa: E402
import workbench.cli.status as workspace_status  # noqa: E402
import workbench.extensions.runner as workspace_workflow  # noqa: E402
import workbench.work_items.evidence as workspace_evidence  # noqa: E402
import workbench.inspection.api as workspace_inspect  # noqa: E402


ACTION_FIXTURE = ROOT / "tests" / "fixtures" / "action-extension"


from tests.support.workflows import WorkflowFixture


class WorkspaceWorkflowTest(WorkflowFixture, unittest.TestCase):
    def setUp(self):
        self.open()
        self.addCleanup(self.close)

    def test_no_overlay_is_a_fast_disabled_status(self) -> None:
        self.assertEqual({"enabled": False}, workspace_workflow.status_result(self.root))

    def test_preview_apply_start_and_plan_lightweight_run(self) -> None:
        self.write_overlay(
            [
                {
                    "id": "team-delivery.integration-test",
                    "after": "item.implement",
                    "uses": "action-extension/integration-test",
                }
            ]
        )
        preview = self.activate_overlay()
        self.assertTrue(preview["previewHash"])
        self.assertTrue((self.root / ".workspace/config/workflow.json").is_file())

        run = workspace_workflow.start_run(
            self.root,
            run_id="light-change",
            repository="service",
            branch="owner/fix/light-change",
        )
        self.assertEqual("light-change", run["id"])
        self.assertIsNone(run["itemSlug"])
        self.assertFalse((self.root / ".workspace/items/light-change").exists())

        plan = workspace_workflow.plan_result(
            self.root, "light-change", after="item.implement"
        )
        self.assertEqual("item.implement", plan["anchor"])
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
                    "after": "item.implement",
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
                    "after": "item.implement",
                    "uses": "action-extension/integration-test",
                }
            ]
        )
        preview = workspace_workflow.preview_result(self.root, self.overlay_config)
        self.write_overlay(
            [
                {
                    "id": "team-delivery.integration-test",
                    "after": "item.verify",
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
                    "after": "item.implement",
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
                    "after": "item.implement",
                    "uses": "action-extension/integration-test",
                }
            ]
        )
        target = self.root / ".workspace/config/workflow.json"
        target.write_text(self.overlay_config.read_text(encoding="utf-8"), encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(self.root), "add", "-f", ".workspace/config/workflow.json"],
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
                    "after": "item.implement",
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
            self.root, "delivery-run", after="item.implement"
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
            self.root, "delivery-run", after="item.implement"
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
            self.root, "delivery-run", after="item.implement"
        )["pending"])

        self.write_overlay(
            [
                {
                    "id": "team-delivery.integration-test",
                    "after": "item.implement",
                    "uses": "action-extension/integration-test",
                    "with": {"suite": "changed"},
                }
            ]
        )
        self.activate_overlay()
        changed = workspace_workflow.plan_result(
            self.root, "delivery-run", after="item.implement"
        )
        self.assertEqual(["team-delivery.integration-test"], [item["stage"] for item in changed["pending"]])

    def test_unrelated_core_regions_do_not_block_each_other(self) -> None:
        self.write_overlay(
            [
                {
                    "id": "team-delivery.after-implement",
                    "after": "item.implement",
                    "uses": "action-extension/integration-test",
                },
                {
                    "id": "team-delivery.after-verify",
                    "after": "item.verify",
                    "uses": "action-extension/integration-test",
                },
                {
                    "id": "team-delivery.depends-on-implement",
                    "after": "team-delivery.after-implement",
                    "uses": "action-extension/integration-test",
                },
            ]
        )
        self.activate_overlay()
        workspace_workflow.start_run(self.root, run_id="region-run")

        verify_plan = workspace_workflow.plan_result(self.root, "region-run", after="item.verify")
        self.assertEqual(["team-delivery.after-verify"], [item["stage"] for item in verify_plan["pending"]])
        finished = workspace_workflow.finish(
            self.root, "region-run", "team-delivery.after-verify",
            verify_plan["pending"][0]["planHash"], status="succeeded", summary="ok",
        )
        self.assertEqual("succeeded", finished["status"])

        implement_plan = workspace_workflow.plan_result(self.root, "region-run", after="item.implement")
        self.assertEqual(["team-delivery.after-implement"], [item["stage"] for item in implement_plan["pending"]])
        _, _, _, blocked_item, _ = workspace_workflow._stage_context(
            self.root.resolve(), "region-run", "team-delivery.depends-on-implement"
        )
        with self.assertRaisesRegex(workspace_workflow.WorkflowCommandError, "ACTION_BLOCKED"):
            workspace_workflow.finish(
                self.root, "region-run", "team-delivery.depends-on-implement",
                blocked_item["planHash"], status="succeeded", summary="must wait",
            )

    def test_custom_stage_cannot_be_used_as_plan_anchor(self) -> None:
        self.write_overlay(
            [{
                "id": "team-delivery.integration-test",
                "after": "item.implement",
                "uses": "action-extension/integration-test",
            }]
        )
        self.activate_overlay()
        workspace_workflow.start_run(self.root, run_id="anchor-run")
        with self.assertRaisesRegex(workspace_workflow.WorkflowCommandError, "当前参数只接受 Core Stage"):
            workspace_workflow.plan_result(
                self.root, "anchor-run", after="team-delivery.integration-test"
            )


    def test_command_environment_and_summary_limits_are_safe(self) -> None:
        self.write_overlay(
            [
                {
                    "id": "team-delivery.deploy-test",
                    "after": "item.implement",
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
        plan = workspace_workflow.plan_result(self.root, "safe-run", after="item.implement")
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
            self.root, "safe-run", after="item.implement"
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






if __name__ == "__main__":
    unittest.main()
