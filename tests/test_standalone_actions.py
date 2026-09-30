from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.support.public_clone import create_public_clone, initialize_workspace
from workbench.extensions.model import ExtensionError, load_manifest

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


if __name__ == "__main__":
    unittest.main()
