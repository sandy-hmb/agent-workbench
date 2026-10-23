"""Task implementation progress stays separate from verification evidence."""
from __future__ import annotations

import json
import subprocess
import sys
import unittest

from tests.support import ROOT
from tests.support.items import ItemFixture
import workbench.work_items.commands as actions
from workbench.inspection.api import projection
from workbench.cli.brief import brief_result
from workbench.work_items.evidence import history_page
from workbench.work_items.query import WorkItemQuery
from workbench.work_items.store import load_state, save_state


class TaskProgressTest(ItemFixture, unittest.TestCase):
    def setUp(self):
        self.open()
        self.addCleanup(self.close)
        self.plan()
        self.review()

    def progress(self, task_id="T01"):
        return next(row for row in WorkItemQuery(self.root, "demo").task_progress() if row["id"] == task_id)

    def test_explicit_implementation_progress_does_not_claim_verification(self):
        current = self.progress()
        self.assertEqual("unknown", current["executionStatus"])
        self.assertEqual("unverified", current["verificationStatus"])
        self.assertFalse(current["completed"])

        actions.task_progress(
            self.root,
            "demo",
            "T01",
            status="in-progress",
            summary="正在实现目标行为",
            expected_revision=self.state()["stateRevision"],
        )
        self.assertEqual("in-progress", self.progress()["executionStatus"])

        actions.task_progress(
            self.root,
            "demo",
            "T01",
            status="implemented",
            summary="代码已实现，等待验证",
            expected_revision=self.state()["stateRevision"],
        )
        implemented = self.progress()
        self.assertEqual("implemented", implemented["executionStatus"])
        self.assertEqual("unverified", implemented["verificationStatus"])
        self.assertFalse(implemented["completed"])

        self.record("T01")
        verified = self.progress()
        self.assertEqual("implemented", verified["executionStatus"])
        self.assertEqual("passed", verified["verificationStatus"])
        self.assertTrue(verified["completed"])

    def test_invalidated_verification_keeps_history_and_reason(self):
        actions.task_progress(
            self.root,
            "demo",
            "T01",
            status="implemented",
            summary="第一版已实现",
            expected_revision=self.state()["stateRevision"],
        )
        self.record("T01")
        evidence_id = self.state()["tasks"]["T01"]["evidence"]

        change = self.item / "change.md"
        change.write_text(change.read_text() + "\n范围发生实质调整。\n")
        actions.review(
            self.root,
            "demo",
            decision="approved",
            reason="范围调整后重新批准",
            roles=["change"],
            affected_tasks=["T01"],
            expected_revision=self.state()["stateRevision"],
        )

        current = self.progress()
        self.assertEqual("implemented", current["executionStatus"])
        self.assertEqual("needs-reassessment", current["executionApplicability"])
        self.assertEqual("needs-revalidation", current["verificationStatus"])
        self.assertEqual(evidence_id, current["lastVerification"]["evidenceId"])
        self.assertEqual("passed", current["lastVerification"]["result"])
        self.assertIn("范围调整后重新批准", current["invalidation"]["reason"])
        self.assertEqual("passed", history_page(self.item, task_id="T01")["items"][0]["result"])

    def test_legacy_invalidated_evidence_is_not_shown_as_never_started(self):
        self.record("T01")
        state = load_state(self.item)
        state["tasks"]["T01"]["evidence"] = None
        save_state(self.item, state)

        current = self.progress()
        self.assertEqual("implemented", current["executionStatus"])
        self.assertEqual("needs-revalidation", current["verificationStatus"])
        self.assertEqual("passed", current["lastVerification"]["result"])
        self.assertIn("原因未记录", current["invalidation"]["reason"])

    def test_failed_latest_verification_does_not_erase_prior_implementation_fact(self):
        self.record("T01")
        self.record("T01", "failed")
        current = self.progress()
        self.assertEqual("implemented", current["executionStatus"])
        self.assertEqual("failed", current["verificationStatus"])
        self.assertFalse(current["completed"])

    def test_pending_plan_task_is_visible_without_changing_approved_denominator(self):
        self.plan(("T01", "T02"))
        reader = WorkItemQuery(self.root, "demo")
        progress = reader.task_progress()
        self.assertEqual(["approved", "pending"], [row["approvalStatus"] for row in progress])
        self.assertEqual("unknown", progress[1]["executionStatus"])

        summary = reader.summary()
        self.assertEqual(1, summary["progress"]["total"])
        self.assertEqual(2, summary["progress"]["plannedTotal"])
        self.assertEqual(1, summary["progress"]["pendingApproval"])
        self.assertEqual(progress, projection(self.root, "demo", "task")["tasks"])
        brief_progress = brief_result(self.root, "demo")["taskProgress"]
        self.assertEqual([row["id"] for row in progress], [row["id"] for row in brief_progress])
        self.assertEqual([row["verificationStatus"] for row in progress],
                         [row["verificationStatus"] for row in brief_progress])
        self.assertNotIn("deliverables", brief_progress[0])

        actions.render(self.root, "demo")
        readme = (self.item / "README.md").read_text()
        self.assertIn("## 进展与验证摘要", readme)
        self.assertIn("T02", readme)
        self.assertIn("待审批", readme)

    def test_task_brief_uses_the_same_separated_progress(self):
        actions.task_progress(
            self.root,
            "demo",
            "T01",
            status="implemented",
            summary="等待验证",
            expected_revision=self.state()["stateRevision"],
        )
        brief = brief_result(self.root, "demo", task_id="T01")
        selected = brief["selectedTask"]
        self.assertEqual("approved", selected["approvalStatus"])
        self.assertEqual("implemented", selected["executionStatus"])
        self.assertEqual("unverified", selected["verificationStatus"])
        self.assertEqual(selected["executionStatus"], brief["taskProgress"][0]["executionStatus"])

    def test_real_cli_records_task_progress(self):
        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/kit.py"),
                "item",
                "task-progress",
                "demo",
                "--task",
                "T01",
                "--status",
                "in-progress",
                "--summary",
                "开始实现",
                "--state-revision",
                self.state()["stateRevision"],
                "--root",
                str(self.root),
                "--json",
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["committed"])
        self.assertEqual("in-progress", self.progress()["executionStatus"])


if __name__ == "__main__":
    unittest.main()
