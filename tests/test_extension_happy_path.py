from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))

from test_happy_path import (  # noqa: E402
    create_public_clone,
    git_status,
    initialize_workspace,
    run_script,
)


EXTENSION_FIXTURE = ROOT / "tests" / "fixtures" / "example-extension"
ACTION_FIXTURE = ROOT / "tests" / "fixtures" / "action-extension"
BRANCH_PROVIDER = ROOT / "tests" / "fixtures" / "provider" / "branch.py"


def extension_input(path: Path, *, active: bool) -> None:
    path.write_text(
        json.dumps(
            {
                "extensions": (
                    [{"id": "example-extension", "version": "1.0.0"}]
                    if active
                    else []
                ),
                "providers": (
                    {
                        "branch.naming": {
                            "default": "example-extension/team",
                            "repositories": {},
                        }
                    }
                    if active
                    else {}
                ),
                "config": {"example-extension": {}} if active else {},
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def preview(root: Path, config: Path) -> dict[str, object]:
    return json.loads(
        run_script(
            root,
            "workspace_extension.py",
            "preview",
            "--root",
            str(root),
            "--config",
            str(config),
            "--json",
        ).stdout
    )


def apply(root: Path, config: Path, preview_result: dict[str, object]) -> None:
    run_script(
        root,
        "workspace_extension.py",
        "apply",
        "--root",
        str(root),
        "--config",
        str(config),
        "--preview-hash",
        str(preview_result["previewHash"]),
    )


class ExtensionHappyPathTest(unittest.TestCase):
    def test_public_clone_installs_and_runs_a_local_action_workflow(self):
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary).resolve()
            _, _, kit = create_public_clone(parent)
            initialize_workspace(kit)
            source = parent / "action-extension"
            shutil.copytree(ACTION_FIXTURE, source)

            install = json.loads(
                run_script(
                    kit,
                    "workspace_extension.py",
                    "install",
                    "preview",
                    "--root",
                    str(kit),
                    "--source",
                    str(source),
                    "--json",
                ).stdout
            )
            run_script(
                kit,
                "workspace_extension.py",
                "install",
                "apply",
                "--root",
                str(kit),
                "--source",
                str(source),
                "--preview-hash",
                str(install["previewHash"]),
            )

            extension_config = kit / ".workspace/extensions/.state/input.json"
            extension_config.write_text(
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
            activation = preview(kit, extension_config)
            self.assertEqual([], activation["adapters"]["create"])
            apply(kit, extension_config, activation)

            workflow_config = kit / ".workspace/workflow-input.json"
            workflow_config.write_text(
                json.dumps(
                    {
                        "schemaVersion": {"major": 1, "minor": 0},
                        "workflow": "feature-development",
                        "stages": [
                            {
                                "id": "team-delivery.integration-test",
                                "after": "feature.implement",
                                "uses": "action-extension/integration-test",
                            }
                        ],
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            workflow_preview = json.loads(
                run_script(
                    kit,
                    "workspace_workflow.py",
                    "preview",
                    "--root",
                    str(kit),
                    "--config",
                    str(workflow_config),
                    "--json",
                ).stdout
            )
            run_script(
                kit,
                "workspace_workflow.py",
                "apply",
                "--root",
                str(kit),
                "--config",
                str(workflow_config),
                "--preview-hash",
                str(workflow_preview["previewHash"]),
            )
            run = json.loads(
                run_script(
                    kit,
                    "workspace_workflow.py",
                    "start",
                    "--root",
                    str(kit),
                    "--run-id",
                    "local-action",
                    "--repo",
                    "service",
                    "--branch",
                    "smoke/feature/local-action",
                    "--json",
                ).stdout
            )
            self.assertEqual("local-action", run["id"])
            plan = json.loads(
                run_script(
                    kit,
                    "workspace_workflow.py",
                    "plan",
                    "--root",
                    str(kit),
                    "--run",
                    "local-action",
                    "--after",
                    "feature.implement",
                    "--json",
                ).stdout
            )
            action = plan["pending"][0]
            self.assertTrue(Path(action["skillPath"]).is_file())
            run_script(
                kit,
                "workspace_workflow.py",
                "finish",
                "--root",
                str(kit),
                "--run",
                "local-action",
                "--stage",
                "team-delivery.integration-test",
                "--plan-hash",
                str(action["planHash"]),
                "--status",
                "succeeded",
                "--summary",
                "completed",
                "--json",
            )
            status = json.loads(
                run_script(kit, "workspace_status.py", "--root", str(kit), "--json").stdout
            )
            self.assertTrue(status["workflow"]["enabled"])
            self.assertEqual(0, status["workflow"]["pending"])
            self.assertEqual([], list((kit / ".agents/skills").glob("local-action-extension-*")))
            self.assertIn("SUMMARY ERROR=0", run_script(kit, "workspace_doctor.py", "--root", str(kit)).stdout)
            self.assertEqual("", git_status(kit))

    def test_local_extension_activation_drift_recovery_and_safe_deactivation(self):
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary).resolve()
            _, _, kit = create_public_clone(parent)
            initialize_workspace(kit)
            config = kit / ".workspace" / "extensions-input.json"

            installed = json.loads(
                run_script(
                    kit,
                    "workspace_extension.py",
                    "list",
                    "--root",
                    str(kit),
                    "--json",
                ).stdout
            )
            self.assertEqual({"ids": [], "extensions": []}, installed)
            self.assertFalse((kit / "plugins").exists())
            self.assertEqual(
                [], list((kit / ".agents" / "skills").glob("local-*"))
            )

            extension = kit / ".workspace" / "extensions" / "example-extension"
            shutil.copytree(EXTENSION_FIXTURE, extension)
            provider = extension / "provider"
            provider.mkdir()
            shutil.copy2(BRANCH_PROVIDER, provider / "branch.py")
            manifest_path = extension / "workspace-extension.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["provides"][0]["command"] = ["python3", "provider/branch.py"]
            manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
            extension_input(config, active=True)

            activation = preview(kit, config)
            self.assertEqual(1, len(activation["adapters"]["create"]))
            self.assertEqual([], activation["adapters"]["remove"])
            self.assertIn(
                ".agents/skills/local-example-extension-example-branching",
                activation["paths"],
            )
            apply(kit, config, activation)

            agent_adapter = (
                kit
                / ".agents"
                / "skills"
                / "local-example-extension-example-branching"
            )
            claude_adapter = (
                kit
                / ".claude"
                / "skills"
                / "local-example-extension-example-branching"
            )
            self.assertTrue((agent_adapter / "SKILL.md").is_file())
            self.assertTrue(claude_adapter.is_symlink())
            self.assertEqual(agent_adapter.resolve(), claude_adapter.resolve())
            self.assertEqual("", git_status(kit))

            named = json.loads(
                run_script(
                    kit,
                    "workspace_registry.py",
                    "--root",
                    str(kit),
                    "branch",
                    "svc",
                    "--type",
                    "feature",
                    "--slug",
                    "happy-path",
                    "--json",
                ).stdout
            )
            self.assertEqual("smoke/provider/happy-path", named["branch"])
            healthy = run_script(kit, "workspace_doctor.py", "--root", str(kit))
            self.assertIn("SUMMARY ERROR=0", healthy.stdout)

            skill = extension / "skills" / "example-branching" / "SKILL.md"
            original_skill = skill.read_text(encoding="utf-8")
            skill.write_text(original_skill + "\nlocal drift\n", encoding="utf-8")
            extension_doctor = run_script(
                kit,
                "workspace_extension.py",
                "doctor",
                "--root",
                str(kit),
                "--json",
                check=False,
            )
            self.assertEqual(1, extension_doctor.returncode)
            self.assertIn("EXTENSION_DRIFT", extension_doctor.stdout)
            workspace_doctor = run_script(
                kit, "workspace_doctor.py", "--root", str(kit), check=False
            )
            self.assertEqual(1, workspace_doctor.returncode)
            self.assertIn("EXTENSION_DRIFT", workspace_doctor.stdout)
            blocked = run_script(
                kit,
                "workspace_registry.py",
                "--root",
                str(kit),
                "branch",
                "svc",
                "--type",
                "feature",
                "--slug",
                "happy-path",
                "--json",
                check=False,
            )
            self.assertEqual(1, blocked.returncode)
            self.assertIn("EXTENSION_DRIFT", blocked.stdout)

            skill.write_text(original_skill, encoding="utf-8")
            recovered = preview(kit, config)
            self.assertEqual([], recovered["adapters"]["create"])
            self.assertEqual([], recovered["adapters"]["remove"])
            apply(kit, config, recovered)
            healthy = run_script(kit, "workspace_doctor.py", "--root", str(kit))
            self.assertIn("SUMMARY ERROR=0", healthy.stdout)

            extension_input(config, active=False)
            deactivation = preview(kit, config)
            self.assertEqual(1, len(deactivation["adapters"]["remove"]))
            apply(kit, config, deactivation)
            self.assertFalse(agent_adapter.exists())
            self.assertFalse(claude_adapter.exists())
            core_branch = json.loads(
                run_script(
                    kit,
                    "workspace_registry.py",
                    "--root",
                    str(kit),
                    "branch",
                    "svc",
                    "--type",
                    "feature",
                    "--slug",
                    "happy-path",
                    "--json",
                ).stdout
            )
            self.assertEqual("smoke/feature/happy-path", core_branch["branch"])
            healthy = run_script(kit, "workspace_doctor.py", "--root", str(kit))
            self.assertIn("SUMMARY ERROR=0", healthy.stdout)
            self.assertEqual("", git_status(kit))


if __name__ == "__main__":
    unittest.main()
