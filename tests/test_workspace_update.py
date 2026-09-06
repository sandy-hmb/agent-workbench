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

import workspace_extension  # noqa: E402
import workspace_update  # noqa: E402
from test_extension_happy_path import (  # noqa: E402
    BRANCH_PROVIDER,
    EXTENSION_FIXTURE,
)
from test_extension_happy_path import apply as apply_extension  # noqa: E402
from test_extension_happy_path import extension_input, preview  # noqa: E402
from test_happy_path import create_public_clone, initialize_workspace, run_command  # noqa: E402


ACTION_FIXTURE = ROOT / "tests/fixtures/action-extension"


def publish_update(source: Path) -> None:
    readme = source / "README.md"
    readme.write_text(readme.read_text(encoding="utf-8") + "\nUpdate fixture.\n", encoding="utf-8")
    run_command(["git", "-C", str(source), "add", "README.md"])
    run_command(
        [
            "git", "-C", str(source), "-c", "user.name=Fixture",
            "-c", "user.email=fixture@example.test", "commit", "-qm", "update fixture",
        ]
    )
    run_command(["git", "-C", str(source), "push", "-q"])


class WorkspaceUpdateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.parent = Path(self.temp.name).resolve()
        self.source, _, self.root = create_public_clone(self.parent)
        initialize_workspace(self.root, include_repository=False)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def publish_manifest_update(self, kit_version: str, manual_steps=None) -> None:
        version_path = self.source / "VERSION"
        version_path.write_text(f"{kit_version}\n", encoding="utf-8")
        manifest_path = self.source / "upgrades/manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["kitVersion"] = kit_version
        if manual_steps is not None:
            manifest["manualSteps"] = manual_steps
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        run_command(["git", "-C", str(self.source), "add", "VERSION", "upgrades/manifest.json"])
        run_command(
            [
                "git", "-C", str(self.source), "-c", "user.name=Fixture",
                "-c", "user.email=fixture@example.test", "commit", "-qm", "manifest fixture",
            ]
        )
        run_command(["git", "-C", str(self.source), "push", "-q"])

    def test_plan_rejects_an_invalid_active_extension(self) -> None:
        extension = self.root / ".workspace/extensions/legacy"
        (extension / "skills/branch").mkdir(parents=True)
        (extension / "skills/branch/SKILL.md").write_text("---\nname: branch\n---\n", encoding="utf-8")
        (extension / "workspace-extension.json").write_text(
            json.dumps({"schemaVersion": 1, "id": "legacy", "version": "1.0.0", "kitApi": 1, "provides": [], "requires": [], "effects": []}),
            encoding="utf-8",
        )
        lock = self.root / ".workspace/extensions/.state/lock.json"
        lock.write_text(
            json.dumps({"lockVersion": {"major": 1, "minor": 0}, "kitApi": 1, "extensions": [{"id": "legacy", "version": "1.0.0", "path": "extensions/legacy", "digest": "sha256:" + "a" * 64, "providers": [], "actions": [], "adapters": []}], "providers": {}}),
            encoding="utf-8",
        )
        publish_update(self.source)

        with self.assertRaises(workspace_update.UpdateError) as raised:
            workspace_update.plan_result(self.root)
        self.assertEqual("UPDATE_EXTENSION_INVALID", raised.exception.code)

    def test_plan_ignores_unactivated_invalid_extension(self) -> None:
        inactive = self.root / ".workspace/extensions/inactive"
        inactive.mkdir(parents=True)
        (inactive / "workspace-extension.json").write_text("not json", encoding="utf-8")
        publish_update(self.source)

        plan = workspace_update.plan_result(self.root)

        self.assertFalse(plan["blocked"])
        self.assertIn("applyCommand", plan)

    def test_apply_fast_forwards_when_no_extension_blocks_update(self) -> None:
        before = run_command(["git", "-C", str(self.root), "rev-parse", "HEAD"]).stdout.strip()
        publish_update(self.source)

        plan = workspace_update.plan_result(self.root)

        self.assertFalse(plan["blocked"])
        self.assertIn("applyCommand", plan)
        result = workspace_update.apply_result(self.root, str(plan["planHash"]))
        self.assertTrue(result["updated"])
        self.assertNotEqual(before, result["commit"])
        self.assertIn("Update fixture.", (self.root / "README.md").read_text(encoding="utf-8"))

    def test_apply_rejects_plan_when_target_advances(self) -> None:
        publish_update(self.source)
        plan = workspace_update.plan_result(self.root)
        publish_update(self.source)

        with self.assertRaisesRegex(workspace_update.UpdateError, "更新计划已变化"):
            workspace_update.apply_result(self.root, str(plan["planHash"]))

    def test_plan_includes_changelog_section_after_current_version(self) -> None:
        marker = "标记：changelog 段落测试专用条目"
        changelog = self.source / "CHANGELOG.md"
        text = changelog.read_text(encoding="utf-8")
        changelog.write_text(text.replace("## 未发布\n", f"## 未发布\n\n- {marker}\n", 1), encoding="utf-8")
        run_command(["git", "-C", str(self.source), "add", "CHANGELOG.md"])
        run_command(
            [
                "git", "-C", str(self.source), "-c", "user.name=Fixture",
                "-c", "user.email=fixture@example.test", "commit", "-qm", "changelog fixture",
            ]
        )
        run_command(["git", "-C", str(self.source), "push", "-q"])

        plan = workspace_update.plan_result(self.root)

        self.assertFalse(plan["blocked"])
        self.assertIn("changelog", plan)
        self.assertIn(marker, plan["changelog"])
        self.assertIn("commits", plan)

    def test_plan_omits_changelog_when_section_is_empty(self) -> None:
        version = (self.root / "VERSION").read_text(encoding="utf-8").strip()
        changelog = self.source / "CHANGELOG.md"
        changelog.write_text(
            f"# 变更记录\n\n## 未发布\n\n## {version} - 2026-01-01\n\n- 初始版本\n",
            encoding="utf-8",
        )
        run_command(["git", "-C", str(self.source), "add", "CHANGELOG.md"])
        run_command(
            [
                "git", "-C", str(self.source), "-c", "user.name=Fixture",
                "-c", "user.email=fixture@example.test", "commit", "-qm", "empty changelog fixture",
            ]
        )
        run_command(["git", "-C", str(self.source), "push", "-q"])
        publish_update(self.source)

        plan = workspace_update.plan_result(self.root)

        self.assertFalse(plan["blocked"])
        self.assertNotIn("changelog", plan)

    def test_plan_and_apply_show_manual_steps_within_version_window(self) -> None:
        self.publish_manifest_update(
            "3.1.0",
            manual_steps=[
                {"sinceVersion": "3.1.0", "summary": "in-window", "runbook": "docs/guides/example.md"},
                {"sinceVersion": "3.2.0", "summary": "out-of-window", "runbook": "docs/guides/example.md"},
            ],
        )

        plan = workspace_update.plan_result(self.root)

        self.assertFalse(plan["blocked"])
        self.assertIn("manualSteps", plan)
        self.assertEqual(["in-window"], [step["summary"] for step in plan["manualSteps"]])

        result = workspace_update.apply_result(self.root, str(plan["planHash"]))

        self.assertEqual(plan["manualSteps"], result["manualSteps"])

    def test_plan_omits_manual_steps_when_manifest_lacks_field(self) -> None:
        publish_update(self.source)

        plan = workspace_update.plan_result(self.root)

        self.assertFalse(plan["blocked"])
        self.assertNotIn("manualSteps", plan)

    def test_plan_rejects_invalid_manual_step_version(self) -> None:
        self.publish_manifest_update(
            "2.2.0",
            manual_steps=[{"sinceVersion": "not-a-version", "summary": "x", "runbook": "docs/guides/example.md"}],
        )

        with self.assertRaises(workspace_update.UpdateError) as ctx:
            workspace_update.plan_result(self.root)
        self.assertEqual("UPDATE_MANIFEST_INVALID", ctx.exception.code)

    def test_plan_rejects_manual_step_with_unsafe_runbook_path(self) -> None:
        self.publish_manifest_update(
            "2.2.0",
            manual_steps=[{"sinceVersion": "2.2.0", "summary": "x", "runbook": "../escape.md"}],
        )

        with self.assertRaises(workspace_update.UpdateError) as ctx:
            workspace_update.plan_result(self.root)
        self.assertEqual("UPDATE_MANIFEST_INVALID", ctx.exception.code)

    def test_plan_dirty_workspace_lists_blocking_files(self) -> None:
        (self.root / "scratch-note.md").write_text("note\n", encoding="utf-8")

        with self.assertRaises(workspace_update.UpdateError) as ctx:
            workspace_update.plan_result(self.root)

        self.assertEqual("UPDATE_DIRTY", ctx.exception.code)
        message = str(ctx.exception)
        self.assertIn("scratch-note.md", message)
        self.assertIn("共 1 个", message)

    def test_plan_dirty_workspace_truncates_long_file_list(self) -> None:
        for index in range(12):
            (self.root / f"scratch-{index}.md").write_text("note\n", encoding="utf-8")

        with self.assertRaises(workspace_update.UpdateError) as ctx:
            workspace_update.plan_result(self.root)

        self.assertEqual("UPDATE_DIRTY", ctx.exception.code)
        self.assertIn("前 10 个，共 12 个", str(ctx.exception))

    def test_apply_reports_remediation_for_extension_drift(self) -> None:
        extension = self.root / ".workspace" / "extensions" / "example-extension"
        shutil.copytree(EXTENSION_FIXTURE, extension)
        provider = extension / "provider"
        provider.mkdir()
        shutil.copy2(BRANCH_PROVIDER, provider / "branch.py")
        manifest_path = extension / "workspace-extension.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["provides"][0]["command"] = ["python3", "provider/branch.py"]
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        config = self.root / ".workspace" / "extensions-input.json"
        extension_input(config, active=True)
        activation = preview(self.root, config)
        apply_extension(self.root, config, activation)

        skill = extension / "skills" / "example-branching" / "SKILL.md"
        skill.write_text(skill.read_text(encoding="utf-8") + "\nlocal drift\n", encoding="utf-8")

        publish_update(self.source)
        plan = workspace_update.plan_result(self.root)
        self.assertFalse(plan["blocked"])
        result = workspace_update.apply_result(self.root, str(plan["planHash"]))

        drift = next(item for item in result["doctor"] if item["code"] == "EXTENSION_DRIFT")
        self.assertIsNotNone(drift["remediation"])
        self.assertIn("workspace_extension.py preview", drift["remediation"]["detail"])


if __name__ == "__main__":
    unittest.main()
