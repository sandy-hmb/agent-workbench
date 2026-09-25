from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from workbench.extensions.workflow import WorkflowError, load_core_workflow, load_overlay, resolve_stages  # noqa: E402


class WorkflowModelTest(unittest.TestCase):
    def write_overlay(
        self,
        directory: Path,
        stages: list[dict[str, object]],
        skip_hints: list[dict[str, object]] | None = None,
    ) -> Path:
        path = directory / "workflow.json"
        payload = {
            "schemaVersion": {"major": 1, "minor": 0},
            "workflow": "item-development",
            "stages": stages,
        }
        if skip_hints is not None:
            payload["skipHints"] = skip_hints
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_public_workflow_has_stable_core_stages(self) -> None:
        workflow = load_core_workflow(ROOT / "workflows/item-development.json")

        self.assertEqual(
            (
                "item.context",
                "item.classify",
                "item.analyze",
                "item.design",
                "item.prepare-branch",
                "item.implement",
                "item.verify",
                "item.submit-test",
                "item.complete",
            ),
            workflow.stage_ids,
        )
        self.assertEqual(
            {"item.analyze", "item.submit-test"},
            {stage.id for stage in workflow.stages if stage.optional},
        )

    def test_custom_stages_are_inserted_stably_at_their_anchor(self) -> None:
        core = load_core_workflow(ROOT / "workflows/item-development.json")
        with tempfile.TemporaryDirectory() as directory:
            overlay = load_overlay(
                self.write_overlay(
                    Path(directory),
                    [
                        {
                            "id": "team-check.integration-test",
                            "after": "item.implement",
                            "uses": "team-check/integration-test",
                        },
                        {
                            "id": "team-check.deploy-test",
                            "after": "team-check.integration-test",
                            "uses": "team-check/deploy-test",
                            "trigger": "auto",
                        },
                        {
                            "id": "team-check.preverify",
                            "before": "item.verify",
                            "uses": "team-check/preverify",
                        },
                    ],
                )
            )

        resolved = resolve_stages(core, overlay)
        self.assertEqual(
            (
                "item.context",
                "item.classify",
                "item.analyze",
                "item.design",
                "item.prepare-branch",
                "item.implement",
                "team-check.integration-test",
                "team-check.deploy-test",
                "team-check.preverify",
                "item.verify",
                "item.submit-test",
                "item.complete",
            ),
            resolved.stage_ids,
        )

    def test_invalid_overlay_is_rejected_with_stable_error_codes(self) -> None:
        core = load_core_workflow(ROOT / "workflows/item-development.json")
        cases = (
            (
                "duplicate",
                [
                    {
                        "id": "team-check.same",
                        "after": "item.implement",
                        "uses": "team-check/one",
                    },
                    {
                        "id": "team-check.same",
                        "after": "item.verify",
                        "uses": "team-check/two",
                    },
                ],
                "WORKFLOW_INVALID",
            ),
            (
                "unknown",
                [
                    {
                        "id": "team-check.unknown",
                        "after": "item.missing",
                        "uses": "team-check/one",
                    }
                ],
                "WORKFLOW_ANCHOR_MISSING",
            ),
            (
                "both",
                [
                    {
                        "id": "team-check.both",
                        "before": "item.verify",
                        "after": "item.implement",
                        "uses": "team-check/one",
                    }
                ],
                "WORKFLOW_INVALID",
            ),
            (
                "cycle",
                [
                    {
                        "id": "team-check.first",
                        "after": "team-check.second",
                        "uses": "team-check/one",
                    },
                    {
                        "id": "team-check.second",
                        "after": "team-check.first",
                        "uses": "team-check/two",
                    },
                ],
                "WORKFLOW_CYCLE",
            ),
        )
        for name, stages, code in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                with self.assertRaisesRegex(WorkflowError, code):
                    overlay = load_overlay(self.write_overlay(Path(directory), stages))
                    resolve_stages(core, overlay)



    def test_removed_skip_hints_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(WorkflowError, "WORKFLOW_INVALID"):
                load_overlay(
                    self.write_overlay(
                        Path(directory),
                        [],
                        skip_hints=[
                            {
                                "stage": "item.analyze",
                                "when": {"repositoryCount": {"max": -1}},
                                "reason": "无效规则",
                            }
                        ],
                    )
                )






if __name__ == "__main__":
    unittest.main()
