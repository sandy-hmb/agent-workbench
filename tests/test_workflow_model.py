from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from workflow_model import WorkflowError, load_core_workflow, load_overlay, resolve_stages  # noqa: E402
from workflow_model import evaluate_skip_hints  # noqa: E402


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
            "workflow": "feature-development",
            "stages": stages,
        }
        if skip_hints is not None:
            payload["skipHints"] = skip_hints
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_public_workflow_has_stable_core_stages(self) -> None:
        workflow = load_core_workflow(ROOT / "workflows/feature-development.json")

        self.assertEqual(
            (
                "feature.context",
                "feature.classify",
                "feature.analyze",
                "feature.design",
                "feature.prepare-branch",
                "feature.implement",
                "feature.verify",
                "feature.submit-test",
                "feature.complete",
            ),
            workflow.stage_ids,
        )
        self.assertEqual(
            {"feature.analyze", "feature.submit-test"},
            {stage.id for stage in workflow.stages if stage.optional},
        )

    def test_custom_stages_are_inserted_stably_at_their_anchor(self) -> None:
        core = load_core_workflow(ROOT / "workflows/feature-development.json")
        with tempfile.TemporaryDirectory() as directory:
            overlay = load_overlay(
                self.write_overlay(
                    Path(directory),
                    [
                        {
                            "id": "team-check.integration-test",
                            "after": "feature.implement",
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
                            "before": "feature.verify",
                            "uses": "team-check/preverify",
                        },
                    ],
                )
            )

        resolved = resolve_stages(core, overlay)
        self.assertEqual(
            (
                "feature.context",
                "feature.classify",
                "feature.analyze",
                "feature.design",
                "feature.prepare-branch",
                "feature.implement",
                "team-check.integration-test",
                "team-check.deploy-test",
                "team-check.preverify",
                "feature.verify",
                "feature.submit-test",
                "feature.complete",
            ),
            resolved.stage_ids,
        )

    def test_invalid_overlay_is_rejected_with_stable_error_codes(self) -> None:
        core = load_core_workflow(ROOT / "workflows/feature-development.json")
        cases = (
            (
                "duplicate",
                [
                    {
                        "id": "team-check.same",
                        "after": "feature.implement",
                        "uses": "team-check/one",
                    },
                    {
                        "id": "team-check.same",
                        "after": "feature.verify",
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
                        "after": "feature.missing",
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
                        "before": "feature.verify",
                        "after": "feature.implement",
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

    def test_overlay_without_skip_hints_defaults_to_empty_tuple(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            overlay = load_overlay(self.write_overlay(Path(directory), []))
        self.assertEqual((), overlay.skip_hints)

    def test_load_overlay_parses_skip_hints(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            overlay = load_overlay(
                self.write_overlay(
                    Path(directory),
                    [],
                    skip_hints=[
                        {
                            "stage": "feature.analyze",
                            "when": {"repositoryCount": {"max": 1}},
                            "reason": "单仓需求通常不需要跨仓分析",
                        }
                    ],
                )
            )
        self.assertEqual(1, len(overlay.skip_hints))
        hint = overlay.skip_hints[0]
        self.assertEqual("feature.analyze", hint.stage)
        self.assertEqual(1, hint.max_repository_count)
        self.assertEqual("单仓需求通常不需要跨仓分析", hint.reason)

    def test_load_overlay_rejects_negative_max_repository_count(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(WorkflowError, "WORKFLOW_SKIP_HINT_INVALID"):
                load_overlay(
                    self.write_overlay(
                        Path(directory),
                        [],
                        skip_hints=[
                            {
                                "stage": "feature.analyze",
                                "when": {"repositoryCount": {"max": -1}},
                                "reason": "无效规则",
                            }
                        ],
                    )
                )

    def test_resolve_stages_rejects_skip_hint_for_unknown_stage(self) -> None:
        core = load_core_workflow(ROOT / "workflows/feature-development.json")
        with tempfile.TemporaryDirectory() as directory:
            overlay = load_overlay(
                self.write_overlay(
                    Path(directory),
                    [],
                    skip_hints=[
                        {
                            "stage": "feature.unknown",
                            "when": {"repositoryCount": {"max": 1}},
                            "reason": "不存在的阶段",
                        }
                    ],
                )
            )
            with self.assertRaisesRegex(WorkflowError, "WORKFLOW_SKIP_HINT_INVALID"):
                resolve_stages(core, overlay)

    def test_resolve_stages_rejects_skip_hint_for_non_optional_stage(self) -> None:
        core = load_core_workflow(ROOT / "workflows/feature-development.json")
        with tempfile.TemporaryDirectory() as directory:
            overlay = load_overlay(
                self.write_overlay(
                    Path(directory),
                    [],
                    skip_hints=[
                        {
                            "stage": "feature.implement",
                            "when": {"repositoryCount": {"max": 1}},
                            "reason": "feature.implement 不是可选阶段",
                        }
                    ],
                )
            )
            with self.assertRaisesRegex(WorkflowError, "WORKFLOW_SKIP_HINT_INVALID"):
                resolve_stages(core, overlay)

    def test_resolve_stages_accepts_valid_skip_hint_without_changing_stage_ids(self) -> None:
        core = load_core_workflow(ROOT / "workflows/feature-development.json")
        with tempfile.TemporaryDirectory() as directory:
            overlay = load_overlay(
                self.write_overlay(
                    Path(directory),
                    [],
                    skip_hints=[
                        {
                            "stage": "feature.analyze",
                            "when": {"repositoryCount": {"max": 1}},
                            "reason": "单仓需求通常不需要跨仓分析",
                        }
                    ],
                )
            )
            resolved = resolve_stages(core, overlay)
        self.assertEqual(core.stage_ids, resolved.stage_ids)

    def test_evaluate_skip_hints_matches_only_when_condition_is_satisfied(self) -> None:
        core = load_core_workflow(ROOT / "workflows/feature-development.json")
        with tempfile.TemporaryDirectory() as directory:
            overlay = load_overlay(
                self.write_overlay(
                    Path(directory),
                    [],
                    skip_hints=[
                        {
                            "stage": "feature.analyze",
                            "when": {"repositoryCount": {"max": 1}},
                            "reason": "单仓需求通常不需要跨仓分析",
                        }
                    ],
                )
            )
        matched = evaluate_skip_hints(core, overlay, repository_count=1)
        self.assertEqual(
            [
                {
                    "stage": "feature.analyze",
                    "reason": "单仓需求通常不需要跨仓分析",
                    "rule": "repositoryCount<=1",
                }
            ],
            list(matched),
        )
        unmatched = evaluate_skip_hints(core, overlay, repository_count=2)
        self.assertEqual([], list(unmatched))


if __name__ == "__main__":
    unittest.main()
