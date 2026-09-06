from __future__ import annotations

import contextlib
import fcntl
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import workspace_doctor  # noqa: E402
import workspace_extension  # noqa: E402
import workspace_status  # noqa: E402


FIXTURE = ROOT / "tests" / "fixtures" / "example-extension"
ACTION_FIXTURE = ROOT / "tests" / "fixtures" / "action-extension"
INITIAL_LOCK = {"lockVersion": {"major": 1, "minor": 0}, "kitApi": 1, "extensions": [], "providers": {}}
EMPTY_LOCK_V2 = {"lockVersion": {"major": 1, "minor": 0}, "kitApi": 1, "extensions": [], "providers": {}}


def snapshot(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.rglob("*")
        if path.is_file() and ".git" not in path.parts
    }


class WorkspaceExtensionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "kit"
        self.root.mkdir()
        (self.root / ".gitignore").write_text(
            "/.workspace/\n/.agents/skills/local-*\n/.claude/skills/local-*\n",
            encoding="utf-8",
        )
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        state = self.root / ".workspace"
        (state / "docs/features").mkdir(parents=True)
        (state / "docs/repositories").mkdir()
        (state / "extensions" / ".state").mkdir(parents=True)
        (state / "extensions" / ".state" / "cache").mkdir()
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
            json.dumps(
                {"branchOwner": "alice", "primaryRole": None, "extensions": {}}
            )
            + "\n",
            encoding="utf-8",
        )
        (state / "extensions" / ".state" / "lock.json").write_text(
            json.dumps(INITIAL_LOCK) + "\n", encoding="utf-8"
        )
        shutil.copytree(FIXTURE, state / "extensions/example-extension")
        (self.root / ".agents/skills").mkdir(parents=True)
        (self.root / ".claude/skills").mkdir(parents=True)
        self.config = state / "extensions" / ".state" / "input.json"
        self.write_desired()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def write_desired(
        self,
        providers: dict[str, object] | None = None,
        *,
        version: str = "1.0.0",
        config: dict[str, object] | None = None,
        extensions: list[dict[str, str]] | None = None,
    ) -> None:
        self.config.write_text(
            json.dumps(
                {
                    "extensions": extensions
                    if extensions is not None
                    else [{"id": "example-extension", "version": version}],
                    "providers": providers
                    if providers is not None
                    else {
                        "branch.naming": {
                            "default": "example-extension/team",
                            "repositories": {},
                        }
                    },
                    "config": {"example-extension": {}}
                    if config is None
                    else config,
                }
            )
            + "\n",
            encoding="utf-8",
        )

    def apply(self) -> dict[str, object]:
        preview = workspace_extension.preview_result(self.root, self.config)
        workspace_extension.apply(self.root, self.config, preview["previewHash"])
        return preview

    def test_preview_is_read_only_and_apply_uses_reviewed_hash(self) -> None:
        before = snapshot(self.root)
        preview = workspace_extension.preview_result(self.root, self.config)

        self.assertEqual(before, snapshot(self.root))
        self.assertTrue(preview["previewHash"])
        self.assertIn(".workspace/extensions/.state/lock.json", preview["paths"])
        self.assertIn(
            ".agents/skills/local-example-extension-example-branching",
            preview["paths"],
        )
        workspace_extension.apply(self.root, self.config, preview["previewHash"])
        lock = json.loads(
            (self.root / ".workspace/extensions/.state/lock.json").read_text(encoding="utf-8")
        )
        self.assertEqual(["example-extension"], [item["id"] for item in lock["extensions"]])
        self.assertEqual(
            "example-extension/team", lock["providers"]["branch.naming"]["default"]
        )
        self.assertTrue(
            (
                self.root
                / ".agents/skills/local-example-extension-example-branching/SKILL.md"
            ).is_file()
        )
        doctor_codes = {item.code for item in workspace_doctor.audit(self.root)}
        self.assertNotIn("SKILL_UNEXPECTED", doctor_codes)
        self.assertNotIn("CLIENT_SKILL_ADAPTER_UNEXPECTED", doctor_codes)

    def test_action_extension_locks_actions_without_global_adapters(self) -> None:
        shutil.copytree(ACTION_FIXTURE, self.root / ".workspace/extensions/action-extension")
        self.write_desired(
            providers={},
            extensions=[{"id": "action-extension", "version": "1.0.0"}],
            config={"action-extension": {}},
        )

        preview = self.apply()
        self.assertEqual([], preview["adapters"]["create"])
        lock = json.loads(
            (self.root / ".workspace/extensions/.state/lock.json").read_text(encoding="utf-8")
        )
        self.assertEqual({"major": 1, "minor": 0}, lock["lockVersion"])
        self.assertEqual(
            [
                {
                    "id": "deploy-test",
                    "apiVersion": 1,
                    "skill": "deploy-test",
                    "confirmation": {
                        "title": "Deploy to test",
                        "summary": "Deploy the current change to the test environment.",
                    },
                },
                {
                    "id": "integration-test",
                    "apiVersion": 1,
                    "skill": "integration-test",
                    "confirmation": {
                        "title": "Run integration tests",
                        "summary": "Run the integration tests for the current change.",
                    },
                },
            ],
            lock["extensions"][0]["actions"],
        )
        self.assertEqual([], list((self.root / ".agents/skills").glob("local-action-extension-*")))

    def test_install_preview_and_apply_copy_explicit_local_extension_only(self) -> None:
        source = Path(self.temp.name) / "action-extension"
        shutil.copytree(ACTION_FIXTURE, source)
        before = snapshot(self.root)

        preview = workspace_extension.install_preview_result(self.root, source)
        self.assertEqual(before, snapshot(self.root))
        self.assertEqual("action-extension", preview["id"])
        self.assertFalse((self.root / ".workspace/extensions/action-extension").exists())

        workspace_extension.install_apply(self.root, source, preview["previewHash"])
        installed = self.root / ".workspace/extensions/action-extension"
        self.assertTrue((installed / "workspace-extension.json").is_file())
        self.assertEqual(
            workspace_extension.extension_digest(source),
            workspace_extension.extension_digest(installed),
        )
        self.assertEqual(
            {
                "action-extension": {
                    "source": str(source.resolve()),
                    "digest": workspace_extension.extension_digest(source),
                }
            },
            workspace_extension.extension_sources(self.root),
        )
        self.assertFalse((self.root / ".agents/skills/local-action-extension-deploy-test").exists())

    def test_install_rejects_symlink_source_and_stale_hash(self) -> None:
        source = Path(self.temp.name) / "action-extension"
        shutil.copytree(ACTION_FIXTURE, source)
        linked = Path(self.temp.name) / "linked-extension"
        linked.symlink_to(source, target_is_directory=True)
        with self.assertRaisesRegex(workspace_extension.ExtensionCommandError, "普通目录"):
            workspace_extension.install_preview_result(self.root, linked)

        preview = workspace_extension.install_preview_result(self.root, source)
        (source / "skills/integration-test/SKILL.md").write_text(
            "---\nname: integration-test\n---\nchanged\n", encoding="utf-8"
        )
        with self.assertRaisesRegex(workspace_extension.ExtensionCommandError, "preview hash"):
            workspace_extension.install_apply(self.root, source, preview["previewHash"])

    def test_install_copy_failure_preserves_existing_target(self) -> None:
        source = Path(self.temp.name) / "action-extension"
        shutil.copytree(ACTION_FIXTURE, source)
        target = self.root / ".workspace/extensions/action-extension"
        target.mkdir()
        (target / "keep.txt").write_text("original", encoding="utf-8")
        preview = workspace_extension.install_preview_result(self.root, source)

        with mock.patch.object(workspace_extension.shutil, "copytree", side_effect=OSError("copy failed")):
            with self.assertRaisesRegex(workspace_extension.ExtensionCommandError, "EXTENSION_INSTALL_FAILED"):
                workspace_extension.install_apply(self.root, source, preview["previewHash"])

        self.assertEqual("original", (target / "keep.txt").read_text(encoding="utf-8"))

    def test_install_rejects_special_source_and_target_drift(self) -> None:
        source = Path(self.temp.name) / "action-extension"
        shutil.copytree(ACTION_FIXTURE, source)
        special = source / "named-pipe"
        os.mkfifo(special)
        with self.assertRaisesRegex(workspace_extension.ExtensionCommandError, "special file"):
            workspace_extension.install_preview_result(self.root, source)
        special.unlink()

        preview = workspace_extension.install_preview_result(self.root, source)
        target = self.root / ".workspace/extensions/action-extension"
        target.mkdir()
        (target / "new-state.txt").write_text("keep", encoding="utf-8")
        with self.assertRaisesRegex(workspace_extension.ExtensionCommandError, "preview hash"):
            workspace_extension.install_apply(self.root, source, preview["previewHash"])
        self.assertEqual("keep", (target / "new-state.txt").read_text(encoding="utf-8"))

    def test_doctor_reports_invalid_extension_source_record(self) -> None:
        (self.root / ".workspace/workspace.local.json").write_text(
            json.dumps({"branchOwner": "alice", "primaryRole": None, "extensions": {}, "extensionSources": {"bad": {"source": "relative", "digest": "bad"}}}),
            encoding="utf-8",
        )

        codes = {item.code for item in workspace_extension.extension_findings(self.root)}

        self.assertIn("EXTENSION_SOURCE_INVALID", codes)

    def test_apply_rejects_stale_preview_after_extension_changes(self) -> None:
        preview = workspace_extension.preview_result(self.root, self.config)
        skill = self.root / ".workspace/extensions/example-extension/skills/example-branching/SKILL.md"
        skill.write_text(skill.read_text(encoding="utf-8") + "\nchanged\n", encoding="utf-8")

        with self.assertRaisesRegex(workspace_extension.ExtensionCommandError, "预览哈希不匹配"):
            workspace_extension.apply(self.root, self.config, preview["previewHash"])

    def test_apply_refuses_concurrent_workspace_lock_then_recomputes_state(self) -> None:
        preview = workspace_extension.preview_result(self.root, self.config)
        lock_path = self.root / ".workspace/extensions/.state/cache/extension.apply.lock"
        holder = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import fcntl, os, sys; "
                "fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600); "
                "fcntl.flock(fd, fcntl.LOCK_EX); "
                "print('locked', flush=True); "
                "sys.stdin.buffer.read(1); "
                "fcntl.flock(fd, fcntl.LOCK_UN); os.close(fd)",
                str(lock_path),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            assert holder.stdout is not None
            self.assertEqual(b"locked\n", holder.stdout.readline())
            before = snapshot(self.root)
            with self.assertRaisesRegex(workspace_extension.ExtensionCommandError, "EXTENSION_BUSY"):
                workspace_extension.apply(self.root, self.config, preview["previewHash"])
            self.assertEqual(before, snapshot(self.root))
        finally:
            if holder.stdin is not None:
                holder.stdin.write(b"x")
                holder.stdin.flush()
            holder.wait(timeout=5)
            for stream in (holder.stdin, holder.stdout, holder.stderr):
                if stream is not None:
                    stream.close()
        self.assertEqual(0, workspace_extension.apply(self.root, self.config, preview["previewHash"]))
        lock = json.loads(
            (self.root / ".workspace/extensions/.state/lock.json").read_text(encoding="utf-8")
        )
        self.assertEqual(["example-extension"], [item["id"] for item in lock["extensions"]])
        self.assertTrue(
            (
                self.root
                / ".agents/skills/local-example-extension-example-branching/SKILL.md"
            ).is_file()
        )
        with self.assertRaisesRegex(workspace_extension.ExtensionCommandError, "预览哈希不匹配"):
            workspace_extension.apply(self.root, self.config, preview["previewHash"])

    def test_apply_rejects_a_symlinked_workspace_lock_file(self) -> None:
        lock = self.root / ".workspace/extensions/.state/cache/extension.apply.lock"
        outside = self.root.parent / "outside-lock"
        outside.write_text("keep\n", encoding="utf-8")
        lock.symlink_to(outside)
        preview = workspace_extension.preview_result(self.root, self.config)

        with self.assertRaisesRegex(workspace_extension.ExtensionCommandError, "EXTENSION_BUSY"):
            workspace_extension.apply(self.root, self.config, preview["previewHash"])
        self.assertEqual("keep\n", outside.read_text(encoding="utf-8"))

    def test_apply_refuses_replaced_managed_agent_before_state_write(self) -> None:
        self.apply()
        self.config.write_text(
            json.dumps({"extensions": [], "providers": {}, "config": {}}) + "\n",
            encoding="utf-8",
        )
        preview = workspace_extension.preview_result(self.root, self.config)
        registry = self.root / ".workspace/workspace.json"
        lock = self.root / ".workspace/extensions/.state/lock.json"
        before = {path: path.read_bytes() for path in (registry, lock)}
        agent = self.root / ".agents/skills/local-example-extension-example-branching"
        saved = self.root / "saved-managed-agent"

        def replace_agent_leaf() -> None:
            agent.rename(saved)
            agent.mkdir()
            (agent / "keep").write_text("keep\n", encoding="utf-8")

        with self.assertRaisesRegex(workspace_extension.ExtensionCommandError, "ADAPTER_DRIFT"):
            workspace_extension.apply(
                self.root,
                self.config,
                preview["previewHash"],
                _adapter_stage_hook=replace_agent_leaf,
            )
        self.assertEqual(before, {path: path.read_bytes() for path in (registry, lock)})
        self.assertTrue((saved / "SKILL.md").is_file())
        self.assertEqual("keep\n", (agent / "keep").read_text(encoding="utf-8"))

    def test_explicit_desired_version_upgrades_a_drifted_lock_and_adapter(self) -> None:
        self.apply()
        old_lock = json.loads(
            (self.root / ".workspace/extensions/.state/lock.json").read_text(encoding="utf-8")
        )
        extension = self.root / ".workspace/extensions/example-extension"
        manifest_path = extension / "workspace-extension.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["version"] = "1.0.1"
        manifest["provides"][0]["provider"] = "team-v2"
        manifest_path.write_text(json.dumps(manifest) + "\n", encoding="utf-8")
        skill = extension / "skills/example-branching/SKILL.md"
        skill.write_text(skill.read_text(encoding="utf-8") + "\nupgrade\n", encoding="utf-8")
        self.write_desired(
            version="1.0.1",
            providers={
                "branch.naming": {
                    "default": "example-extension/team-v2",
                    "repositories": {},
                }
            },
        )

        preview = workspace_extension.preview_result(self.root, self.config)
        self.assertEqual(1, len(preview["adapters"]["remove"]))
        self.assertEqual(1, len(preview["adapters"]["create"]))
        self.assertNotEqual(
            old_lock["extensions"][0]["digest"], preview["lock"]["extensions"][0]["digest"]
        )
        workspace_extension.apply(self.root, self.config, preview["previewHash"])

        lock = json.loads(
            (self.root / ".workspace/extensions/.state/lock.json").read_text(encoding="utf-8")
        )
        self.assertEqual("1.0.1", lock["extensions"][0]["version"])
        marker = json.loads(
            (
                self.root
                / ".agents/skills/local-example-extension-example-branching/.workspace-adapter.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual("1.0.1", marker["version"])
        self.assertEqual(
            "example-extension/team-v2", lock["providers"]["branch.naming"]["default"]
        )

    def test_deactivation_refuses_force_tracked_managed_adapters(self) -> None:
        self.apply()
        agent = ".agents/skills/local-example-extension-example-branching"
        claude = ".claude/skills/local-example-extension-example-branching"
        subprocess.run(
            ["git", "-C", str(self.root), "add", "-f", "--", agent, claude],
            check=True,
        )
        self.config.write_text(
            json.dumps({"extensions": [], "providers": {}, "config": {}}) + "\n",
            encoding="utf-8",
        )
        preview = workspace_extension.preview_result(self.root, self.config)

        with self.assertRaisesRegex(workspace_extension.ExtensionCommandError, "ADAPTER_TRACKED"):
            workspace_extension.apply(self.root, self.config, preview["previewHash"])
        self.assertTrue((self.root / agent / "SKILL.md").is_file())
        self.assertTrue((self.root / claude).is_symlink())

    def test_transition_keeps_provider_and_adapter_drift_as_hard_stops(self) -> None:
        self.apply()
        registry = self.root / ".workspace/workspace.json"
        workspace = json.loads(registry.read_text(encoding="utf-8"))
        workspace["extensions"]["providers"]["branch.naming"]["default"] = None
        registry.write_text(json.dumps(workspace) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(workspace_extension.ExtensionCommandError, "PROVIDER_CONFLICT"):
            workspace_extension.preview_result(self.root, self.config)

        workspace["extensions"]["providers"]["branch.naming"]["default"] = "example-extension/team"
        registry.write_text(json.dumps(workspace) + "\n", encoding="utf-8")
        adapter = self.root / ".agents/skills/local-example-extension-example-branching/SKILL.md"
        adapter.write_text("manual drift\n", encoding="utf-8")
        with self.assertRaisesRegex(workspace_extension.ExtensionCommandError, "ADAPTER_DRIFT"):
            workspace_extension.preview_result(self.root, self.config)

    def test_config_schema_validates_merged_shared_and_local_extension_config(self) -> None:
        extension = self.root / ".workspace/extensions/example-extension"
        manifest_path = extension / "workspace-extension.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["configSchema"] = "schemas/config.json"
        manifest_path.write_text(json.dumps(manifest) + "\n", encoding="utf-8")
        schema = {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": "example-config",
            "title": "Example config",
            "type": "object",
            "required": ["team"],
            "additionalProperties": False,
            "properties": {"team": {"type": "string"}},
        }
        (extension / "schemas").mkdir()
        (extension / "schemas/config.json").write_text(
            json.dumps(schema) + "\n", encoding="utf-8"
        )
        self.write_desired(config={"example-extension": {}})
        local_path = self.root / ".workspace/workspace.local.json"
        local = json.loads(local_path.read_text(encoding="utf-8"))
        local["extensions"] = {"example-extension": {"team": "local"}}
        local_path.write_text(json.dumps(local) + "\n", encoding="utf-8")

        workspace_extension.preview_result(self.root, self.config)
        self.write_desired(config={"example-extension": {"team": "shared", "extra": True}})
        with self.assertRaisesRegex(workspace_extension.ExtensionCommandError, "config"):
            workspace_extension.preview_result(self.root, self.config)

    def test_rejects_nonempty_or_inactive_extension_config(self) -> None:
        self.write_desired(config={"example-extension": {"team": "unexpected"}})
        with self.assertRaisesRegex(workspace_extension.ExtensionCommandError, "configSchema"):
            workspace_extension.preview_result(self.root, self.config)

        self.write_desired(config={"example-extension": {}})
        local_path = self.root / ".workspace/workspace.local.json"
        local = json.loads(local_path.read_text(encoding="utf-8"))
        local["extensions"] = {"inactive-extension": {"team": "local"}}
        local_path.write_text(json.dumps(local) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(workspace_extension.ExtensionCommandError, "未激活"):
            workspace_extension.preview_result(self.root, self.config)

        local["extensions"] = {}
        local_path.write_text(json.dumps(local) + "\n", encoding="utf-8")
        self.write_desired(config={"inactive-extension": {}})
        with self.assertRaisesRegex(workspace_extension.ExtensionCommandError, "未激活"):
            workspace_extension.preview_result(self.root, self.config)

    def test_config_schema_must_be_a_regular_file_inside_the_extension(self) -> None:
        extension = self.root / ".workspace/extensions/example-extension"
        manifest_path = extension / "workspace-extension.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["configSchema"] = "schemas/config.json"
        manifest_path.write_text(json.dumps(manifest) + "\n", encoding="utf-8")
        outside = self.root / "outside-config.json"
        outside.write_text('{"type":"object"}\n', encoding="utf-8")
        (extension / "schemas").mkdir()
        (extension / "schemas/config.json").symlink_to(outside)

        with self.assertRaisesRegex(workspace_extension.ExtensionCommandError, "EXTENSION_CONFIG_INVALID"):
            workspace_extension.preview_result(self.root, self.config)

    def test_malformed_config_schema_is_reported_without_doctor_or_status_crash(self) -> None:
        extension = self.root / ".workspace/extensions/example-extension"
        manifest_path = extension / "workspace-extension.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["configSchema"] = "schemas/config.json"
        manifest_path.write_text(json.dumps(manifest) + "\n", encoding="utf-8")
        (extension / "schemas").mkdir()
        schema_path = extension / "schemas/config.json"
        schema_path.write_text(
            json.dumps({"type": "object", "properties": []}) + "\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(workspace_extension.ExtensionCommandError, "EXTENSION_CONFIG_INVALID"):
            workspace_extension.preview_result(self.root, self.config)

        schema_path.write_text(json.dumps({"type": "object"}) + "\n", encoding="utf-8")
        self.apply()
        schema_path.write_text(
            json.dumps({"type": "object", "properties": []}) + "\n",
            encoding="utf-8",
        )
        lock_path = self.root / ".workspace/extensions/.state/lock.json"
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
        lock["extensions"][0]["digest"] = workspace_extension.extension_digest(extension)
        lock_path.write_text(json.dumps(lock) + "\n", encoding="utf-8")

        self.assertIn(
            "EXTENSION_CONFIG_INVALID",
            {item.code for item in workspace_extension.extension_findings(self.root)},
        )
        self.assertIn(
            "EXTENSION_CONFIG_INVALID",
            {item.code for item in workspace_doctor.audit(self.root)},
        )
        self.assertIn(
            "EXTENSION_CONFIG_INVALID",
            workspace_status.status_result(self.root)["extensions"]["blockedCodes"],
        )

    def test_unselected_incomplete_extension_does_not_block_selected_activation(self) -> None:
        incomplete = self.root / ".workspace/extensions/incomplete-extension"
        shutil.copytree(FIXTURE, incomplete)
        manifest_path = incomplete / "workspace-extension.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["id"] = "incomplete-extension"
        manifest["requires"] = ["context.term-router"]
        manifest_path.write_text(json.dumps(manifest) + "\n", encoding="utf-8")

        self.apply()
        self.write_desired(
            extensions=[{"id": "incomplete-extension", "version": "1.0.0"}],
            providers={
                "branch.naming": {
                    "default": "incomplete-extension/team",
                    "repositories": {},
                }
            },
            config={"incomplete-extension": {}},
        )
        with self.assertRaisesRegex(workspace_extension.ExtensionCommandError, "PROVIDER_INCOMPATIBLE"):
            workspace_extension.preview_result(self.root, self.config)

    def test_validate_and_cli_commands_are_read_only(self) -> None:
        before = snapshot(self.root)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(
                0,
                workspace_extension.main(
                    ["list", "--root", str(self.root), "--json"]
                ),
            )
        self.assertEqual(["example-extension"], json.loads(output.getvalue())["ids"])
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(
                0,
                workspace_extension.main(
                    [
                        "validate-extension",
                        str(self.root / ".workspace/extensions/example-extension"),
                        "--json",
                    ]
                ),
            )
        self.assertEqual("example-extension", json.loads(output.getvalue())["id"])
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(
                0,
                workspace_extension.main(
                    ["capability", "list", "--root", str(self.root), "--json"]
                ),
            )
        self.assertEqual(
            ["branch.naming", "context.term-router"],
            json.loads(output.getvalue())["capabilities"],
        )
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(
                0,
                workspace_extension.main(
                    [
                        "preview",
                        "--root",
                        str(self.root),
                        "--config",
                        str(self.config),
                        "--json",
                    ]
                ),
            )
        preview = json.loads(output.getvalue())
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(
                0,
                workspace_extension.main(
                    [
                        "doctor",
                        "--root",
                        str(self.root),
                        "--json",
                    ]
                ),
            )
        self.assertEqual([], json.loads(output.getvalue())["findings"])
        self.assertEqual(before, snapshot(self.root))
        self.assertEqual(
            0,
            workspace_extension.main(
                [
                    "apply",
                    "--root",
                    str(self.root),
                    "--config",
                    str(self.config),
                    "--preview-hash",
                    preview["previewHash"],
                ]
            ),
        )
        self.assertNotEqual(before, snapshot(self.root))

    def test_doctor_reports_missing_drift_provider_and_adapter_conditions(self) -> None:
        self.apply()
        self.assertEqual([], workspace_extension.extension_findings(self.root))

        extension = self.root / ".workspace/extensions/example-extension"
        shutil.rmtree(extension)
        self.assertIn(
            "EXTENSION_MISSING",
            {item.code for item in workspace_extension.extension_findings(self.root)},
        )
        self.assertIn(
            "EXTENSION_MISSING",
            {item.code for item in workspace_doctor.audit(self.root)},
        )
        shutil.copytree(FIXTURE, extension)

        skill = extension / "skills/example-branching/SKILL.md"
        skill.write_text(skill.read_text(encoding="utf-8") + "\ndrift\n", encoding="utf-8")
        self.assertIn(
            "EXTENSION_DRIFT",
            {item.code for item in workspace_extension.extension_findings(self.root)},
        )
        shutil.rmtree(extension)
        shutil.copytree(FIXTURE, extension)

        lock_path = self.root / ".workspace/extensions/.state/lock.json"
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
        lock["providers"]["branch.naming"]["default"] = "example-extension/missing"
        lock_path.write_text(json.dumps(lock), encoding="utf-8")
        self.assertIn(
            "PROVIDER_MISSING",
            {item.code for item in workspace_extension.extension_findings(self.root)},
        )
        lock["providers"]["branch.naming"]["default"] = "example-extension/team"
        lock["providers"]["context.term-router"] = {
            "default": "example-extension/team",
            "repositories": {},
        }
        lock_path.write_text(json.dumps(lock), encoding="utf-8")
        self.assertIn(
            "PROVIDER_INCOMPATIBLE",
            {item.code for item in workspace_extension.extension_findings(self.root)},
        )
        del lock["providers"]["context.term-router"]
        lock_path.write_text(json.dumps(lock), encoding="utf-8")

        registry = self.root / ".workspace/workspace.json"
        workspace = json.loads(registry.read_text(encoding="utf-8"))
        workspace["extensions"]["providers"]["branch.naming"]["default"] = None
        registry.write_text(json.dumps(workspace), encoding="utf-8")
        self.assertIn(
            "PROVIDER_CONFLICT",
            {item.code for item in workspace_extension.extension_findings(self.root)},
        )
        workspace["extensions"]["providers"]["branch.naming"]["default"] = "example-extension/team"
        registry.write_text(json.dumps(workspace), encoding="utf-8")

        adapter = self.root / ".agents/skills/local-example-extension-example-branching/SKILL.md"
        adapter.write_text("manual drift\n", encoding="utf-8")
        codes = {item.code for item in workspace_extension.extension_findings(self.root)}
        self.assertIn("ADAPTER_DRIFT", codes)
        self.assertIn("ADAPTER_DRIFT", {item.code for item in workspace_doctor.audit(self.root)})

    def test_status_reports_active_providers_overrides_and_blocks(self) -> None:
        self.write_desired(
            {
                "branch.naming": {
                    "default": "example-extension/team",
                    "repositories": {"service": "example-extension/team"},
                }
            }
        )
        self.apply()
        result = workspace_status.status_result(self.root)
        self.assertEqual(
            {
                "activeIds": ["example-extension"],
                "providers": {"branch.naming": "example-extension/team"},
                "repositoryOverrides": {
                    "branch.naming": {"service": "example-extension/team"}
                },
                "blockedCodes": [],
            },
            result["extensions"],
        )

        adapter = self.root / ".agents/skills/local-example-extension-example-branching/SKILL.md"
        adapter.write_text("manual drift\n", encoding="utf-8")
        result = workspace_status.status_result(self.root)
        self.assertIn("ADAPTER_DRIFT", result["extensions"]["blockedCodes"])

    def test_empty_desired_state_deactivates_only_managed_adapters(self) -> None:
        self.apply()
        adapter = self.root / ".agents/skills/local-example-extension-example-branching"
        self.assertTrue(adapter.is_dir())
        self.config.write_text(
            json.dumps({"extensions": [], "providers": {}, "config": {}}) + "\n",
            encoding="utf-8",
        )
        preview = workspace_extension.preview_result(self.root, self.config)
        self.assertIn(adapter.relative_to(self.root).as_posix(), preview["paths"])
        workspace_extension.apply(self.root, self.config, preview["previewHash"])

        self.assertFalse(adapter.exists())
        lock = json.loads(
            (self.root / ".workspace/extensions/.state/lock.json").read_text(encoding="utf-8")
        )
        self.assertEqual(EMPTY_LOCK_V2, lock)

    def test_deactivation_uses_locked_adapter_when_source_extension_has_drifted(self) -> None:
        self.apply()
        extension = self.root / ".workspace/extensions/example-extension"
        manifest_path = extension / "workspace-extension.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["version"] = "1.0.1"
        manifest["provides"][0]["skill"] = "new-branching"
        manifest_path.write_text(json.dumps(manifest) + "\n", encoding="utf-8")
        (extension / "skills/new-branching").mkdir()
        (extension / "skills/new-branching/SKILL.md").write_text(
            "---\nname: new-branching\n---\n", encoding="utf-8"
        )
        shutil.rmtree(extension / "skills/example-branching")
        self.config.write_text(
            json.dumps({"extensions": [], "providers": {}, "config": {}}) + "\n",
            encoding="utf-8",
        )

        preview = workspace_extension.preview_result(self.root, self.config)
        self.assertEqual(1, len(preview["adapters"]["remove"]))
        workspace_extension.apply(self.root, self.config, preview["previewHash"])
        self.assertFalse(
            (self.root / ".agents/skills/local-example-extension-example-branching").exists()
        )

    def test_deactivation_allows_a_missing_source_extension_with_intact_locked_adapter(self) -> None:
        self.apply()
        extension = self.root / ".workspace/extensions/example-extension"
        adapter = self.root / ".agents/skills/local-example-extension-example-branching"
        claude = self.root / ".claude/skills/local-example-extension-example-branching"
        shutil.rmtree(extension)
        self.config.write_text(
            json.dumps({"extensions": [], "providers": {}, "config": {}}) + "\n",
            encoding="utf-8",
        )

        preview = workspace_extension.preview_result(self.root, self.config)
        self.assertEqual(1, len(preview["adapters"]["remove"]))
        workspace_extension.apply(self.root, self.config, preview["previewHash"])

        self.assertFalse(adapter.exists())
        self.assertFalse(claude.exists() or claude.is_symlink())
        self.assertEqual(
            EMPTY_LOCK_V2,
            json.loads(
                (self.root / ".workspace/extensions/.state/lock.json").read_text(encoding="utf-8")
            ),
        )
        workspace = json.loads(
            (self.root / ".workspace/workspace.json").read_text(encoding="utf-8")
        )
        self.assertEqual({"providers": {}, "config": {}}, workspace["extensions"])

    def test_deactivation_requires_explicit_local_config_cleanup(self) -> None:
        self.apply()
        local_path = self.root / ".workspace/workspace.local.json"
        local = json.loads(local_path.read_text(encoding="utf-8"))
        local["extensions"] = {"example-extension": {}}
        local_path.write_text(json.dumps(local) + "\n", encoding="utf-8")
        self.config.write_text(
            json.dumps({"extensions": [], "providers": {}, "config": {}}) + "\n",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(workspace_extension.ExtensionCommandError, "未激活"):
            workspace_extension.preview_result(self.root, self.config)
        self.assertTrue(
            (
                self.root
                / ".agents/skills/local-example-extension-example-branching/SKILL.md"
            ).is_file()
        )

    def test_extension_findings_report_persisted_config_without_manifest_schema(self) -> None:
        self.apply()
        registry = self.root / ".workspace/workspace.json"
        workspace = json.loads(registry.read_text(encoding="utf-8"))
        workspace["extensions"]["config"] = {"example-extension": {"team": "platform"}}
        registry.write_text(json.dumps(workspace) + "\n", encoding="utf-8")

        self.assertIn(
            "EXTENSION_CONFIG_INVALID",
            {item.code for item in workspace_extension.extension_findings(self.root)},
        )
        self.assertIn(
            "EXTENSION_CONFIG_INVALID",
            {item.code for item in workspace_doctor.audit(self.root)},
        )
        self.assertIn(
            "EXTENSION_CONFIG_INVALID",
            workspace_status.status_result(self.root)["extensions"]["blockedCodes"],
        )

    def test_extension_findings_report_persisted_config_schema_violation(self) -> None:
        extension = self.root / ".workspace/extensions/example-extension"
        manifest_path = extension / "workspace-extension.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["configSchema"] = "schemas/config.json"
        manifest_path.write_text(json.dumps(manifest) + "\n", encoding="utf-8")
        (extension / "schemas").mkdir()
        (extension / "schemas/config.json").write_text(
            json.dumps(
                {
                    "type": "object",
                    "required": ["team"],
                    "additionalProperties": False,
                    "properties": {"team": {"type": "string"}},
                }
            )
            + "\n",
            encoding="utf-8",
        )
        self.write_desired(config={"example-extension": {"team": "platform"}})
        self.apply()
        registry = self.root / ".workspace/workspace.json"
        workspace = json.loads(registry.read_text(encoding="utf-8"))
        workspace["extensions"]["config"] = {"example-extension": {"extra": True}}
        registry.write_text(json.dumps(workspace) + "\n", encoding="utf-8")

        self.assertIn(
            "EXTENSION_CONFIG_INVALID",
            {item.code for item in workspace_extension.extension_findings(self.root)},
        )
        self.assertIn(
            "EXTENSION_CONFIG_INVALID",
            {item.code for item in workspace_doctor.audit(self.root)},
        )
        self.assertIn(
            "EXTENSION_CONFIG_INVALID",
            workspace_status.status_result(self.root)["extensions"]["blockedCodes"],
        )

    def test_apply_refuses_force_tracked_workspace_state_files(self) -> None:
        self.apply()
        registry = self.root / ".workspace/workspace.json"
        lock = self.root / ".workspace/extensions/.state/lock.json"
        before = {path: path.read_bytes() for path in (registry, lock)}
        subprocess.run(
            [
                "git",
                "-C",
                str(self.root),
                "add",
                "-f",
                "--",
                ".workspace/workspace.json",
                ".workspace/extensions/.state/lock.json",
            ],
            check=True,
        )
        self.config.write_text(
            json.dumps({"extensions": [], "providers": {}, "config": {}}) + "\n",
            encoding="utf-8",
        )
        preview = workspace_extension.preview_result(self.root, self.config)

        with self.assertRaisesRegex(workspace_extension.ExtensionCommandError, "WORKSPACE_TRACKED"):
            workspace_extension.apply(self.root, self.config, preview["previewHash"])
        self.assertEqual(before, {path: path.read_bytes() for path in (registry, lock)})
        self.assertTrue(
            (
                self.root
                / ".agents/skills/local-example-extension-example-branching/SKILL.md"
            ).is_file()
        )

    def test_status_keeps_extension_blocks_when_registry_provider_is_missing(self) -> None:
        self.apply()
        shutil.rmtree(self.root / ".workspace/extensions/example-extension")

        result = workspace_status.status_result(self.root)
        self.assertEqual("workspace", result["mode"])
        self.assertIn("EXTENSION_MISSING", result["extensions"]["blockedCodes"])
        self.assertEqual(sorted(result["extensions"]["blockedCodes"]), sorted(result["blockers"]))
        self.assertEqual(1, len(result["nextActions"]))
        action = result["nextActions"][0]
        self.assertEqual("extension.blocked", action["stage"])
        self.assertIn("EXTENSION_MISSING", action["reason"])
        self.assertEqual("semantic", action["confirmation"])
        self.assertTrue((ROOT / action["runbook"]).is_file())

    def test_scaffold_generates_valid_action_extension_structure(self) -> None:
        result = workspace_extension.scaffold_result(self.root, "demo-ext")

        target = self.root / ".workspace/extensions/demo-ext"
        self.assertTrue((target / "workspace-extension.json").is_file())
        self.assertTrue((target / "skills/example-action/SKILL.md").is_file())
        self.assertEqual("demo-ext", result["id"])
        self.assertEqual(str(target.relative_to(self.root)), result["path"])

        validated = workspace_extension.validate_result(target)
        self.assertEqual("demo-ext", validated["id"])
        self.assertEqual(["example-action"], validated["actions"])
        self.assertEqual([], validated["capabilities"])

    def test_scaffold_output_passes_activation_preview(self) -> None:
        workspace_extension.scaffold_result(self.root, "demo-ext")
        self.write_desired(
            providers={},
            extensions=[
                {"id": "example-extension", "version": "1.0.0"},
                {"id": "demo-ext", "version": "0.1.0"},
            ],
            config={"example-extension": {}, "demo-ext": {}},
        )

        preview = workspace_extension.preview_result(self.root, self.config)

        self.assertTrue(preview["previewHash"])
        self.assertEqual([], list((self.root / ".agents/skills").glob("local-demo-ext-*")))

    def test_scaffold_refuses_to_overwrite_existing_target(self) -> None:
        workspace_extension.scaffold_result(self.root, "demo-ext")
        target = self.root / ".workspace/extensions/demo-ext"
        before = snapshot(target)

        with self.assertRaisesRegex(
            workspace_extension.ExtensionCommandError, "EXTENSION_ALREADY_EXISTS"
        ):
            workspace_extension.scaffold_result(self.root, "demo-ext")

        self.assertEqual(before, snapshot(target))

    def test_scaffold_requires_initialized_workspace_state(self) -> None:
        bare_root = Path(self.temp.name) / "bare"
        bare_root.mkdir()
        before = snapshot(bare_root)

        with self.assertRaisesRegex(workspace_extension.ExtensionCommandError, "EXTENSION_MISSING"):
            workspace_extension.scaffold_result(bare_root, "demo-ext")

        self.assertEqual(before, snapshot(bare_root))
        self.assertFalse((bare_root / ".workspace").exists())

    def test_scaffold_rejects_invalid_extension_id(self) -> None:
        before = snapshot(self.root)

        with self.assertRaisesRegex(workspace_extension.ExtensionCommandError, "EXTENSION_INVALID"):
            workspace_extension.scaffold_result(self.root, "Demo_Ext")

        self.assertEqual(before, snapshot(self.root))
        self.assertFalse((self.root / ".workspace/extensions/Demo_Ext").exists())

    def test_scaffold_only_writes_target_directory(self) -> None:
        before = snapshot(self.root)

        workspace_extension.scaffold_result(self.root, "demo-ext")

        after = snapshot(self.root)
        new_paths = set(after) - set(before)
        self.assertTrue(new_paths)
        for path in new_paths:
            self.assertTrue(path.startswith(".workspace/extensions/demo-ext/"), path)
        changed = {path for path in before if before[path] != after.get(path)}
        self.assertEqual(set(), changed)

    def test_scaffold_cli_routes_and_returns_zero(self) -> None:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = workspace_extension.main(
                ["scaffold", "--root", str(self.root), "--id", "demo-ext", "--json"]
            )
        self.assertEqual(0, code)
        payload = json.loads(output.getvalue())
        self.assertEqual("demo-ext", payload["id"])


if __name__ == "__main__":
    unittest.main()
