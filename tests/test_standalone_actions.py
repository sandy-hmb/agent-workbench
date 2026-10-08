from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.support.public_clone import create_public_clone, initialize_workspace
from workbench.extensions.adapters import AdapterError, adapter_plan
from workbench.extensions import management
from workbench.extensions.model import ExtensionError, load_manifest, normalize_extensions_lock
from workbench.schema_validation import validate

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/standalone-extension"


class StandaloneActionTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        parent = Path(self.temp.name)
        _, _, self.root = create_public_clone(parent)
        initialize_workspace(self.root, include_repository=False)
        source = parent / "standalone-extension"
        shutil.copytree(FIXTURE, source)
        install = self._run("extension", "install", "preview", "--source", str(source), "--json")
        self._run("extension", "install", "apply", "--source", str(source), "--preview-hash", install["previewHash"], "--json", parse=False)
        config = self.root / ".workspace/config/extensions.draft.json"
        config.write_text(json.dumps({"extensions": [{"id": "standalone-extension", "version": "1.0.0"}], "providers": {}, "config": {"standalone-extension": {}}}), encoding="utf-8")
        preview = self._run("extension", "preview", "--config", str(config), "--json")
        self._run("extension", "apply", "--config", str(config), "--preview-hash", preview["previewHash"], parse=False)

    def tearDown(self):
        self.temp.cleanup()

    def _run(self, *args, check=True, parse=True):
        result = subprocess.run([sys.executable, str(ROOT / "scripts/kit.py"), *args, "--root", str(self.root)], capture_output=True, text=True)
        if check and result.returncode:
            self.fail(result.stdout + result.stderr)
        return json.loads(result.stdout) if parse else {}

    def test_list_and_resolve_work_without_workitem_or_workflow(self):
        listed = self._run("action", "list", "--json")
        self.assertEqual(["standalone-extension/query"], [row["id"] for row in listed["actions"]])
        row = self._run("action", "resolve", "standalone-extension/query", "--json")
        self.assertEqual(".workspace/extensions/standalone-extension/skills/query/SKILL.md", row["action"]["skillPath"])
        self.assertEqual(".workspace/extensions/standalone-extension/skills/query", row["action"]["resourceRoot"])
        self.assertEqual(["network"], row["action"]["effects"])
        self.assertEqual({}, row["config"])
        self.assertFalse((self.root / ".workspace/runs").exists())
        adapter = self.root / ".agents/skills/local-standalone-extension-query"
        self.assertTrue((adapter / "SKILL.md").is_file())
        self.assertTrue((self.root / ".claude/skills/local-standalone-extension-query").is_symlink())
        source = self.root / row["action"]["skillPath"]
        self.assertEqual(source.read_bytes(), (adapter / "SKILL.md").read_bytes())
        marker = json.loads((adapter / ".workspace-adapter.json").read_text())
        self.assertEqual("standalone-extension", marker["extension"])
        self.assertEqual(row["action"]["digest"], marker["sourceDigest"])
        self.assertEqual([], self._run("extension", "doctor", "--json")["findings"])

        config = self.root / ".workspace/config/extensions.draft.json"
        config.write_text(json.dumps({"extensions": [], "providers": {}, "config": {}}), encoding="utf-8")
        preview = self._run("extension", "preview", "--config", str(config), "--json")
        self._run("extension", "apply", "--config", str(config), "--preview-hash", preview["previewHash"], parse=False)
        self.assertFalse(adapter.exists())

    def test_old_standalone_lock_is_repaired_by_preview_apply(self):
        adapter = self.root / ".agents/skills/local-standalone-extension-query"
        shutil.rmtree(adapter)
        (self.root / ".claude/skills/local-standalone-extension-query").unlink()
        lock_path = self.root / ".workspace/extensions/.state/lock.json"
        lock = json.loads(lock_path.read_text())
        lock["extensions"][0]["adapters"] = []
        lock_path.write_text(json.dumps(lock), encoding="utf-8")
        normalize_extensions_lock(lock)
        validate(lock, json.loads((ROOT / "schemas/extensions-lock.schema.json").read_text()))
        findings = self._run("extension", "doctor", "--json", check=False)["findings"]
        self.assertIn("ADAPTER_DRIFT", [row["code"] for row in findings])
        before = lock_path.read_bytes()
        config = self.root / ".workspace/config/extensions.draft.json"
        preview = self._run("extension", "preview", "--config", str(config), "--json")
        self.assertEqual(1, len(preview["adapters"]["create"]))
        self.assertEqual([], preview["adapters"]["remove"])
        self.assertFalse(adapter.exists())
        self.assertEqual(before, lock_path.read_bytes())
        self._run("extension", "apply", "--config", str(config), "--preview-hash", preview["previewHash"], parse=False)
        updated = json.loads(lock_path.read_text())
        self.assertEqual(1, len(updated["extensions"][0]["adapters"]))
        validate(updated, json.loads((ROOT / "schemas/extensions-lock.schema.json").read_text()))
        self.assertEqual([], self._run("extension", "doctor", "--json")["findings"])

    def test_adapter_drift_is_diagnosed_and_not_overwritten(self):
        skill = self.root / ".agents/skills/local-standalone-extension-query/SKILL.md"
        skill.write_text("manual change\n", encoding="utf-8")
        findings = self._run("extension", "doctor", "--json", check=False)["findings"]
        self.assertIn("ADAPTER_DRIFT", [row["code"] for row in findings])
        config = self.root / ".workspace/config/extensions.draft.json"
        with self.assertRaisesRegex(management.ExtensionCommandError, "ADAPTER_DRIFT"):
            management.preview_result(self.root, config)
        self.assertEqual("manual change\n", skill.read_text())

    def test_upgrade_transaction_failure_preserves_old_adapter_and_lock(self):
        adapter = self.root / ".agents/skills/local-standalone-extension-query"
        lock = self.root / ".workspace/extensions/.state/lock.json"
        before = {p: p.read_bytes() for p in (adapter / "SKILL.md", adapter / ".workspace-adapter.json", lock)}
        source = self.root / ".workspace/extensions/standalone-extension/skills/query/SKILL.md"
        source.write_text(source.read_text() + "\nNew tool instructions\n", encoding="utf-8")
        config = self.root / ".workspace/config/extensions.draft.json"
        preview = management.preview_result(self.root, config)
        self.assertEqual(1, len(preview["adapters"]["remove"]))
        self.assertEqual(1, len(preview["adapters"]["create"]))
        with mock.patch.object(management, "atomic_write_many", side_effect=OSError("fixture state failure")):
            with self.assertRaisesRegex(management.ExtensionCommandError, "fixture state failure"):
                management.apply(self.root, config, preview["previewHash"])
        self.assertEqual(before, {p: p.read_bytes() for p in before})

    def test_drift_and_non_standalone_actions_are_rejected(self):
        manifest = self.root / ".workspace/extensions/standalone-extension/workspace-extension.json"
        manifest.write_text(manifest.read_text(encoding="utf-8").replace("Read the external", "Changed external"), encoding="utf-8")
        result = self._run("action", "list", "--json", check=False)
        self.assertEqual("ACTION_DRIFT", result["error"]["code"])
        result = self._run("action", "resolve", "missing/action", "--json", check=False)
        self.assertEqual("ACTION_DRIFT", result["error"]["code"])

    def test_no_workspace_is_empty_and_command_standalone_is_invalid(self):
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run([sys.executable, str(ROOT / "scripts/kit.py"), "action", "list", "--json", "--root", directory], capture_output=True, text=True)
            self.assertEqual(0, result.returncode)
            self.assertEqual([], json.loads(result.stdout)["actions"])
        manifest = json.loads((FIXTURE / "workspace-extension.json").read_text())
        manifest["actions"][0]["command"] = ["python3", "query.py"]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "standalone-extension"
            shutil.copytree(FIXTURE, path)
            (path / "workspace-extension.json").write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(ExtensionError, "Skill-only"):
                load_manifest(path / "workspace-extension.json")

    def test_provider_and_standalone_skill_collision_is_rejected(self):
        manifest = json.loads((FIXTURE / "workspace-extension.json").read_text())
        manifest["provides"] = [{"capability": "branch.naming", "provider": "branching", "apiVersion": 1, "skill": "query"}]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "standalone-extension"
            shutil.copytree(FIXTURE, path)
            (path / "workspace-extension.json").write_text(json.dumps(manifest), encoding="utf-8")
            parsed = load_manifest(path / "workspace-extension.json")
            root = Path(directory) / "kit"
            (root / ".workspace/extensions/.state/cache").mkdir(parents=True)
            (root / ".agents/skills").mkdir(parents=True)
            (root / ".claude/skills").mkdir(parents=True)
            with self.assertRaisesRegex(AdapterError, "Provider 与 standalone Action"):
                adapter_plan(root, active_manifests=(parsed,), locked_adapters=())


if __name__ == "__main__":
    unittest.main()
