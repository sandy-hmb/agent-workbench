from __future__ import annotations

import json
import subprocess
import sys
from types import SimpleNamespace
from pathlib import Path
from unittest import mock

import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import context_measure  # noqa: E402


class EstimateTokensTest(unittest.TestCase):
    def test_empty_string(self):
        result = context_measure.estimate_tokens("")
        self.assertEqual(
            result,
            {"bytes": 0, "chars": 0, "cjkChars": 0, "estTokens": 0},
        )

    def test_pure_ascii_multiple_of_four(self):
        result = context_measure.estimate_tokens("a" * 400)
        self.assertEqual(result["bytes"], 400)
        self.assertEqual(result["cjkChars"], 0)
        self.assertEqual(result["estTokens"], 100)

    def test_pure_ascii_rounds_up(self):
        result = context_measure.estimate_tokens("a" * 401)
        self.assertEqual(result["estTokens"], 101)

    def test_pure_cjk(self):
        result = context_measure.estimate_tokens("中" * 50)
        self.assertEqual(result["bytes"], 150)
        self.assertEqual(result["chars"], 50)
        self.assertEqual(result["cjkChars"], 50)
        self.assertEqual(result["estTokens"], 50)

    def test_mixed(self):
        result = context_measure.estimate_tokens("abc" + "中" * 2)
        self.assertEqual(result["bytes"], 3 + 2 * 3)
        self.assertEqual(result["cjkChars"], 2)
        # ascii "abc" -> 3 bytes -> ceil(3/4) = 1 token; + 2 cjk tokens = 3
        self.assertEqual(result["estTokens"], 3)


class ComponentsAndPathsTest(unittest.TestCase):
    def test_new_observations_use_existing_kit_entrypoint(self):
        self.assertEqual(
            ["kit.py", "describe", "--json"],
            context_measure.COMPONENTS["describe_json"]["argv"],
        )
        self.assertEqual(
            ["kit.py", "brief"],
            context_measure.COMPONENTS["brief_text"]["argv"],
        )
        self.assertIn("lightweight", context_measure.PATHS)
        self.assertIn("standard", context_measure.PATHS)
        self.assertEqual(
            ["status_json", "skill_writing_plan"], context_measure.PATHS["plan"]
        )
        self.assertEqual(
            ["status_json", "skill_execute_plan"], context_measure.PATHS["implement"]
        )
        self.assertIn("resume", context_measure.PATHS)

    def test_file_components_exist(self):
        for component_id, component in context_measure.COMPONENTS.items():
            if component["kind"] == "file":
                target = ROOT / component["path"]
                self.assertTrue(
                    target.is_file(), f"{component_id} -> {target} 不存在"
                )

    def test_command_components_reference_real_scripts(self):
        for component_id, component in context_measure.COMPONENTS.items():
            if component["kind"] == "command":
                script = ROOT / "scripts" / component["argv"][0]
                self.assertTrue(
                    script.is_file(), f"{component_id} -> {script} 不存在"
                )

    def test_paths_reference_known_components(self):
        for path_name, component_ids in context_measure.PATHS.items():
            for component_id in component_ids:
                self.assertIn(
                    component_id,
                    context_measure.COMPONENTS,
                    f"路径 {path_name} 引用了未知组件 {component_id}",
                )


class BuildReportTest(unittest.TestCase):
    def setUp(self):
        self.report = context_measure.build_report(ROOT)

    def test_top_level_keys(self):
        expected = {
            "schemaVersion",
            "tokenModel",
            "repoState",
            "components",
            "paths",
            "allSkills",
            "scripts",
        }
        self.assertEqual(expected, set(self.report.keys()))

    def test_token_model_values(self):
        self.assertEqual(
            self.report["tokenModel"],
            {"cjkCharsPerToken": 1, "otherBytesPerToken": 4},
        )

    def test_paths_have_bytes_and_tokens(self):
        for name in context_measure.PATHS:
            entry = self.report["paths"][name]
            self.assertIn("bytes", entry)
            self.assertIn("estTokens", entry)
            self.assertGreater(entry["bytes"], 0)

    def test_all_skills_matches_glob_count(self):
        skill_files = sorted(ROOT.glob(".agents/skills/*/SKILL.md"))
        self.assertEqual(len(skill_files), len(self.report["allSkills"]["files"]))
        self.assertGreater(self.report["allSkills"]["bytes"], 0)

    def test_scripts_largest_sorted_descending(self):
        largest = self.report["scripts"]["largest"]
        sizes = [entry["bytes"] for entry in largest]
        self.assertEqual(sizes, sorted(sizes, reverse=True))
        self.assertGreater(self.report["scripts"]["totalBytes"], 0)

    def test_repo_state_active_feature_count_matches_status_component(self):
        status_component = self.report["components"]["status_json"]
        parsed = json.loads(status_component["rawStdout"])
        self.assertEqual(
            self.report["repoState"]["activeFeatureCount"],
            len(parsed.get("features", [])),
        )


class OptionalBriefObservationTest(unittest.TestCase):
    def _report_for_status(self, status: dict[str, object]):
        def readonly(argv, root):
            if argv == ["workspace_status.py", "--root", ".", "--json"]:
                return subprocess.CompletedProcess(argv, 0, json.dumps(status), "")
            if argv[:2] == ["kit.py", "brief"]:
                return subprocess.CompletedProcess(argv, 0, "demo-feature（development）\n", "")
            return subprocess.CompletedProcess(argv, 0, "{}", "")

        with mock.patch.object(context_measure, "_run_readonly", side_effect=readonly) as run:
            report = context_measure.build_report(ROOT)
        return report, run

    def test_no_feature_marks_brief_observation_not_applicable(self):
        report, run = self._report_for_status({"mode": "maintenance", "features": []})

        brief = report["components"]["brief_text"]
        self.assertFalse(brief["applicable"])
        self.assertEqual(0, brief["bytes"])
        self.assertEqual("", brief["rawStdout"])
        self.assertFalse(any(call.args[0][:2] == ["kit.py", "brief"] for call in run.call_args_list))
        self.assertEqual(["brief_text"], report["paths"]["resume"]["notApplicable"])

    def test_single_feature_measures_explicit_brief(self):
        report, run = self._report_for_status(
            {"mode": "maintenance", "features": [{"featureSlug": "demo-feature"}]}
        )

        brief = report["components"]["brief_text"]
        self.assertTrue(brief["applicable"])
        self.assertEqual("demo-feature（development）\n", brief["rawStdout"])
        self.assertTrue(any(call.args[0] == ["kit.py", "brief", "demo-feature"] for call in run.call_args_list))
        self.assertEqual(2, report["paths"]["resume"]["commandCalls"])

    def test_multiple_features_without_pointer_skips_brief(self):
        report, run = self._report_for_status(
            {
                "mode": "maintenance",
                "features": [{"featureSlug": "one"}, {"featureSlug": "two"}],
            }
        )

        self.assertFalse(report["components"]["brief_text"]["applicable"])
        self.assertFalse(any(call.args[0][:2] == ["kit.py", "brief"] for call in run.call_args_list))

    def test_workspace_active_feature_measures_selected_brief(self):
        status = {
            "mode": "workspace",
            "features": [{"featureSlug": "one"}, {"featureSlug": "two"}],
        }
        with mock.patch.object(
            context_measure,
            "load_local_settings",
            return_value=SimpleNamespace(active_feature="two"),
        ):
            report, run = self._report_for_status(status)

        self.assertTrue(report["components"]["brief_text"]["applicable"])
        self.assertTrue(any(call.args[0] == ["kit.py", "brief", "two"] for call in run.call_args_list))


class DeterminismTest(unittest.TestCase):
    def test_two_builds_are_byte_identical_json(self):
        first = json.dumps(context_measure.build_report(ROOT), sort_keys=True)
        second = json.dumps(context_measure.build_report(ROOT), sort_keys=True)
        self.assertEqual(first, second)


class CliTest(unittest.TestCase):
    def _run(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "context_measure.py"), *args],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_json_mode_exit_zero_and_valid_json(self):
        result = self._run("--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertIn("paths", payload)

    def test_text_mode_exit_zero(self):
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotEqual(result.stdout.strip(), "")
        with self.assertRaises(json.JSONDecodeError):
            json.loads(result.stdout)


if __name__ == "__main__":
    unittest.main()
