from __future__ import annotations
import json, tempfile, unittest
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from extension_registry import ExtensionError, discover_extensions, normalize_workspace_extensions

class ExtensionRegistryTest(unittest.TestCase):
    def test_discovery_missing_and_duplicate(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "kit"; root.mkdir()
            self.assertEqual({}, discover_extensions(root))
            ext = root / ".workspace/extensions/e"; (ext / "skills/s").mkdir(parents=True)
            (ext / "skills/s/SKILL.md").write_text("---\nname: s\n---\n")
            (ext / "workspace-extension.json").write_text(json.dumps({"schemaVersion": 1,"id":"e","version":"1.0.0","kitApi": 1,"provides":[{"capability":"branch.naming","provider":"p","apiVersion":1,"skill":"s"}], "actions": [], "requires":[],"effects":[]}))
            self.assertIn("e", discover_extensions(root))
            (root / ".workspace/extensions/other").write_text("x")
            with self.assertRaises(ExtensionError): discover_extensions(root)
    def test_binding_single_and_policy_array_rejection(self):
        valid = {"providers":{"branch.naming":{"default":"e/p","repositories":{}}},"config":{"e":{}}}
        self.assertEqual(valid, normalize_workspace_extensions(valid))
        with self.assertRaises(ExtensionError): normalize_workspace_extensions({"providers":{"branch.naming":[]},"config":{}})
        with self.assertRaises(ExtensionError): normalize_workspace_extensions({"providers":{"unknown":{"default":None,"repositories":{}}},"config":{}})
    def test_binding_refs_are_checked_against_discovered_provider(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "kit"; root.mkdir(); ext = root / ".workspace/extensions/e"; (ext / "skills/s").mkdir(parents=True)
            (ext / "skills/s/SKILL.md").write_text("---\nname: s\n---\n")
            (ext / "workspace-extension.json").write_text(json.dumps({"schemaVersion": 1,"id":"e","version":"1.0.0","kitApi": 1,"provides":[{"capability":"branch.naming","provider":"p","apiVersion":1,"skill":"s"}], "actions": [], "requires":[],"effects":[]}))
            found = discover_extensions(root)
            with self.assertRaises(ExtensionError): normalize_workspace_extensions({"providers":{"policy.pack":{"default":"e/p","repositories":{}}},"config":{}}, found)

    def test_multiple_candidates_do_not_create_discovery_cycle(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "kit"; base = root / ".workspace/extensions"; base.mkdir(parents=True)
            specifications = (("a", "branch.naming", ["context.term-router"]), ("b", "context.term-router", ["branch.naming"]), ("c", "context.term-router", []))
            for extension_id, capability, requires in specifications:
                ext = base / extension_id; (ext / "skills/s").mkdir(parents=True)
                (ext / "skills/s/SKILL.md").write_text("---\nname: s\n---\n")
                (ext / "workspace-extension.json").write_text(json.dumps({"schemaVersion": 1,"id":extension_id,"version":"1.0.0","kitApi": 1,"provides":[{"capability":capability,"provider":"p","apiVersion":1,"skill":"s"}], "actions": [], "requires":requires,"effects":[]}))
            self.assertEqual({"a", "b", "c"}, set(discover_extensions(root)))

    def test_discovery_allows_an_unselected_extension_with_missing_dependency(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "kit"
            base = root / ".workspace/extensions"
            for extension_id, requires in (("selected", []), ("incomplete", ["context.term-router"])):
                extension = base / extension_id
                (extension / "skills/s").mkdir(parents=True)
                (extension / "skills/s/SKILL.md").write_text("---\nname: s\n---\n")
                (extension / "workspace-extension.json").write_text(
                    json.dumps(
                        {
                            "schemaVersion": 1,
                            "id": extension_id,
                            "version": "1.0.0",
                            "kitApi": 1,
                            "provides": [
                                {
                                    "capability": "branch.naming",
                                    "provider": "p",
                                    "apiVersion": 1,
                                    "skill": "s",
                                }
                            ],
                            "actions": [],
                            "requires": requires,
                            "effects": [],
                        }
                    )
                )
            self.assertEqual({"selected", "incomplete"}, set(discover_extensions(root)))

    def test_discovered_registry_rejects_unknown_config_and_workspace_symlinks(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "kit"; root.mkdir()
            with self.assertRaises(ExtensionError): normalize_workspace_extensions({"providers":{},"config":{"missing":{}}}, {})
            target = Path(d) / "target"; target.mkdir(); linked = Path(d) / "linked"; linked.symlink_to(target, target_is_directory=True)
            with self.assertRaises(ExtensionError): discover_extensions(linked)
            state = root / ".workspace-real"; state.mkdir(); (root / ".workspace").symlink_to(state, target_is_directory=True)
            with self.assertRaises(ExtensionError): discover_extensions(root)
            (root / ".workspace").unlink(); (root / ".workspace/extensions").mkdir(parents=True)
            (root / ".workspace/extensions/e").symlink_to(target, target_is_directory=True)
            with self.assertRaises(ExtensionError): discover_extensions(root)

if __name__ == "__main__": unittest.main()
