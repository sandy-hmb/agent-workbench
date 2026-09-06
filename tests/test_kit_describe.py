from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import kit  # noqa: E402
import kit_describe  # noqa: E402
import workspace_extension  # noqa: E402
from test_happy_path import create_public_clone, initialize_workspace  # noqa: E402


ACTION_FIXTURE = ROOT / "tests/fixtures/action-extension"


class KitDescribeTest(unittest.TestCase):
    def test_describe_result_includes_all_registered_commands_sorted(self) -> None:
        result = kit_describe.describe_result(ROOT)
        names = [command["name"] for command in result["commands"]]
        self.assertEqual(sorted(kit.COMMANDS), names)

    def test_describe_runbooks_point_to_real_files(self) -> None:
        result = kit_describe.describe_result(ROOT)
        for command in result["commands"]:
            if command["name"] in kit_describe.EXEMPT:
                continue
            runbook = ROOT / command["runbook"]
            self.assertTrue(runbook.is_file(), f"{command['name']}: {runbook}")

    def test_describe_parameters_reflect_argparse_structure(self) -> None:
        result = kit_describe.describe_result(ROOT)
        by_name = {command["name"]: command for command in result["commands"]}
        setup = by_name["setup"]
        self.assertIn("init", setup["parameters"]["subcommands"])
        self.assertIn("add-repo", setup["parameters"]["subcommands"])
        status = by_name["status"]
        self.assertIn("--json", status["parameters"]["options"])

    def test_describe_reports_empty_extension_actions_without_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            result = kit_describe.describe_result(Path(temporary))
        self.assertEqual([], result["extensionActions"])

    def test_describe_reports_empty_extension_actions_when_none_activated(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary).resolve()
            _, _, root = create_public_clone(parent)
            initialize_workspace(root, include_repository=False)
            result = kit_describe.describe_result(root)
        self.assertEqual([], result["extensionActions"])

    def test_describe_includes_activated_extension_actions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary).resolve()
            _, _, root = create_public_clone(parent)
            initialize_workspace(root, include_repository=False)

            source = parent / "action-extension"
            shutil.copytree(ACTION_FIXTURE, source)
            install = workspace_extension.install_preview_result(root, source)
            workspace_extension.install_apply(root, source, str(install["previewHash"]))
            config = root / ".workspace/extensions/.state/input.json"
            config.write_text(
                json.dumps(
                    {
                        "extensions": [{"id": "action-extension", "version": "1.0.0"}],
                        "providers": {},
                        "config": {"action-extension": {}},
                    }
                ),
                encoding="utf-8",
            )
            activation = workspace_extension.preview_result(root, config)
            workspace_extension.apply(root, config, str(activation["previewHash"]))

            result = kit_describe.describe_result(root)

        actions = {item["id"]: item for item in result["extensionActions"]}
        self.assertEqual(
            {"action-extension/integration-test", "action-extension/deploy-test"},
            set(actions),
        )
        deploy = actions["action-extension/deploy-test"]
        self.assertEqual("Deploy the current change to the test environment.", deploy["summary"])
        self.assertEqual(
            ".workspace/extensions/action-extension/skills/deploy-test/SKILL.md",
            deploy["skill"],
        )


if __name__ == "__main__":
    unittest.main()
