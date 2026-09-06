from __future__ import annotations
import json, tempfile, unittest
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from core_capabilities import CORE_CAPABILITIES
from extension_model import AdapterSpec, ExtensionError, extension_digest, load_manifest, normalize_extensions_lock

FIXTURE = Path(__file__).resolve().parent / "fixtures/example-extension"
ACTION_FIXTURE = Path(__file__).resolve().parent / "fixtures/action-extension"

class ExtensionModelTest(unittest.TestCase):
    def test_v1_actions_require_confirmation_and_reject_future_api_versions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "e"
            (root / "skills/s").mkdir(parents=True)
            (root / "skills/s/SKILL.md").write_text("---\nname: s\n---\n", encoding="utf-8")
            path = root / "workspace-extension.json"
            manifest = {
                "schemaVersion": 1,
                "id": "e",
                "version": "1.0.0",
                "kitApi": 1,
                "provides": [],
                "actions": [{"id": "a", "apiVersion": 1, "skill": "s", "effects": []}],
                "requires": [],
                "effects": [],
            }
            path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(ExtensionError, "action confirmation"):
                load_manifest(path)

            manifest["actions"][0]["confirmation"] = {"title": "Run", "summary": "Run."}
            manifest["actions"][0]["apiVersion"] = 2
            path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(ExtensionError, "action apiVersion must be 1"):
                load_manifest(path)

    def test_manifest_and_digest(self):
        manifest = load_manifest(FIXTURE / "workspace-extension.json")
        self.assertEqual("example-extension", manifest.id)
        self.assertEqual("example-extension/team", manifest.providers[0].ref)
        first = extension_digest(FIXTURE); second = extension_digest(FIXTURE)
        self.assertEqual(first, second); self.assertTrue(first.startswith("sha256:"))

    def test_actions_are_normalized_without_adapter_semantics(self):
        manifest = load_manifest(ACTION_FIXTURE / "workspace-extension.json")

        self.assertEqual({"branch.naming", "context.term-router"}, CORE_CAPABILITIES)
        self.assertEqual((), manifest.providers)
        self.assertEqual(
            ("action-extension/integration-test", "action-extension/deploy-test"),
            manifest.action_refs,
        )
        deploy = manifest.actions[1]
        self.assertEqual(1, deploy.api_version)
        self.assertEqual("Deploy to test", deploy.confirmation_title)
        self.assertEqual(
            "Deploy the current change to the test environment.",
            deploy.confirmation_summary,
        )
        self.assertEqual(("python3", "commands/deploy.py"), deploy.command)
        self.assertEqual(("DEPLOY_TOKEN",), deploy.environment)
        self.assertEqual(("network", "process.exec"), deploy.effects)

    def test_removed_capability_and_action_effects_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "e"
            (root / "skills/s").mkdir(parents=True)
            (root / "skills/s/SKILL.md").write_text("---\nname: s\n---\n", encoding="utf-8")
            path = root / "workspace-extension.json"
            removed = {
                "schemaVersion": 1,
                "id": "e",
                "version": "1.0.0",
                "kitApi": 1,
                "provides": [{"capability": "test.end-to-end", "provider": "p", "apiVersion": 1, "skill": "s"}],
                "actions": [],
                "requires": [],
                "effects": [],
            }
            path.write_text(json.dumps(removed), encoding="utf-8")
            with self.assertRaisesRegex(ExtensionError, "EXTENSION_CAPABILITY_REMOVED"):
                load_manifest(path)

            invalid_action = {
                "schemaVersion": 1,
                "id": "e",
                "version": "1.0.0",
                "kitApi": 1,
                "provides": [],
                "actions": [
                    {
                        "id": "a",
                        "apiVersion": 1,
                        "skill": "s",
                        "effects": ["network"],
                        "confirmation": {"title": "Run", "summary": "Run the action."},
                    }
                ],
                "requires": [],
                "effects": [],
            }
            path.write_text(json.dumps(invalid_action), encoding="utf-8")
            with self.assertRaisesRegex(ExtensionError, "action effects"):
                load_manifest(path)

            missing_confirmation = {
                **invalid_action,
                "effects": [],
                "actions": [{"id": "a", "apiVersion": 1, "skill": "s", "effects": []}],
            }
            path.write_text(json.dumps(missing_confirmation), encoding="utf-8")
            with self.assertRaisesRegex(ExtensionError, "action confirmation"):
                load_manifest(path)

            valid_action = {
                **missing_confirmation,
                "actions": [{
                    "id": "a", "apiVersion": 1, "skill": "s", "effects": [],
                    "confirmation": {"title": "Run", "summary": "Run the action."},
                }],
            }
            path.write_text(json.dumps(valid_action), encoding="utf-8")
            self.assertEqual(1, load_manifest(path).actions[0].api_version)

            invalid_action["effects"] = ["network"]
            invalid_action["actions"].append(
                {
                    "id": "a", "apiVersion": 1, "skill": "s", "effects": [],
                    "confirmation": {"title": "Run", "summary": "Run the action."},
                }
            )
            path.write_text(json.dumps(invalid_action), encoding="utf-8")
            with self.assertRaisesRegex(ExtensionError, "duplicate provider or action"):
                load_manifest(path)

            invalid_action["provides"] = [
                {"capability": "branch.naming", "provider": "a", "apiVersion": 1, "skill": "s"}
            ]
            invalid_action["actions"] = [
                {
                    "id": "a", "apiVersion": 1, "skill": "s", "effects": [],
                    "confirmation": {"title": "Run", "summary": "Run the action."},
                }
            ]
            path.write_text(json.dumps(invalid_action), encoding="utf-8")
            with self.assertRaisesRegex(ExtensionError, "duplicate provider or action"):
                load_manifest(path)

            invalid_action["provides"] = []
            invalid_action["actions"] = [
                {
                    "id": "a",
                    "apiVersion": 1,
                    "skill": "s",
                    "effects": [],
                    "confirmation": {"title": "Run", "summary": "Run the action."},
                    "environment": ["BAD", "BAD"],
                }
            ]
            path.write_text(json.dumps(invalid_action), encoding="utf-8")
            with self.assertRaisesRegex(ExtensionError, "environment"):
                load_manifest(path)

            invalid_action["actions"] = [
                {
                    "id": "a",
                    "apiVersion": 1,
                    "skill": "missing",
                    "effects": [],
                    "confirmation": {"title": "Run", "summary": "Run the action."},
                }
            ]
            path.write_text(json.dumps(invalid_action), encoding="utf-8")
            with self.assertRaisesRegex(ExtensionError, "missing extension skill"):
                load_manifest(path)

    def test_lock_records_actions_without_adapters(self):
        digest = "sha256:" + "a" * 64
        lock = {
            "lockVersion": {"major": 1, "minor": 0},
            "kitApi": 1,
            "extensions": [
                {
                    "id": "e",
                    "version": "1.0.0",
                    "path": "extensions/e",
                    "digest": digest,
                    "providers": [],
                    "actions": [
                        {
                            "id": "a",
                    "apiVersion": 1,
                            "skill": "s",
                            "confirmation": {"title": "Run", "summary": "Run the action."},
                        }
                    ],
                    "adapters": [],
                }
            ],
            "providers": {},
        }
        self.assertEqual(lock, normalize_extensions_lock(lock))
        missing_confirmation = json.loads(json.dumps(lock))
        del missing_confirmation["extensions"][0]["actions"][0]["confirmation"]
        with self.assertRaisesRegex(ExtensionError, "locked action fields"):
            normalize_extensions_lock(missing_confirmation)
    def test_manifest_strictness_and_paths(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "bad"; (root / "skills/s").mkdir(parents=True)
            (root / "skills/s/SKILL.md").write_text("---\nname: s\n---\n", encoding="utf-8")
            data = {"schemaVersion": 1,"id":"bad","version":"1.0.0","kitApi": 1,"provides":[{"capability":"branch.naming","provider":"p","apiVersion":1,"skill":"s"}], "actions": [], "requires":[],"effects":[],"unknown":1}
            path = root / "workspace-extension.json"; path.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaises(ExtensionError): load_manifest(path)
            path.unlink(); data.pop("unknown"); data["configSchema"] = "../secret"; path.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaises(ExtensionError): load_manifest(path)
            data["configSchema"] = "./schemas/config.json"; path.write_text(json.dumps(data), encoding="utf-8")
            self.assertEqual("./schemas/config.json", load_manifest(path).config_schema)
    def test_digest_changes_and_rejects_symlink(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "x"; root.mkdir(); (root / "a").write_text("1")
            first = extension_digest(root); (root / "a").write_text("2"); self.assertNotEqual(first, extension_digest(root))
            (root / "link").symlink_to(root / "a")
            with self.assertRaises(ExtensionError): extension_digest(root)

    def test_digest_includes_empty_directories_and_entry_types(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "x"; root.mkdir()
            original = extension_digest(root)
            (root / "empty").mkdir()
            directory_digest = extension_digest(root)
            self.assertNotEqual(original, directory_digest)
            (root / "empty").rmdir(); (root / "empty").write_bytes(b"")
            self.assertNotEqual(directory_digest, extension_digest(root))

    def test_command_executable_cannot_escape_extension(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "e"; (root / "skills/s").mkdir(parents=True)
            (root / "skills/s/SKILL.md").write_text("---\nname: s\n---\n")
            base = {"schemaVersion": 1,"id":"e","version":"1.0.0","kitApi": 1,"provides":[{"capability":"branch.naming","provider":"p","apiVersion":1,"skill":"s"}], "actions": [], "requires":[],"effects":[]}
            path = root / "workspace-extension.json"
            for command in (["python3", "script.py"], ["./skills/s/run", "arg with space"]):
                base["provides"][0]["command"] = command; path.write_text(json.dumps(base))
                self.assertEqual(tuple(command), load_manifest(path).providers[0].command)
            for command in (["/bin/sh"], ["../outside"], ["scripts/../../outside"]):
                base["provides"][0]["command"] = command; path.write_text(json.dumps(base))
                with self.subTest(command=command), self.assertRaises(ExtensionError): load_manifest(path)
            base["provides"][0]["command"] = ["python3\0bad"]; path.write_text(json.dumps(base))
            with self.assertRaises(ExtensionError): load_manifest(path)

    def test_command_option_paths_cannot_escape_but_urls_and_plain_options_are_allowed(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "e"; (root / "skills/s").mkdir(parents=True)
            (root / "skills/s/SKILL.md").write_text("---\nname: s\n---\n")
            base = {"schemaVersion": 1,"id":"e","version":"1.0.0","kitApi": 1,"provides":[{"capability":"branch.naming","provider":"p","apiVersion":1,"skill":"s"}], "actions": [], "requires":[],"effects":[]}
            path = root / "workspace-extension.json"
            for command in (
                ["curl", "--url=https://example.test/v1", "--flag=value", "-XPOST"],
                ["git", "ssh://git@example.test/team/repo.git"],
                ["python3", "-c", "import sys; print('/not-a-path-argument')"],
            ):
                base["provides"][0]["command"] = command; path.write_text(json.dumps(base))
                with self.subTest(command=command):
                    self.assertEqual(tuple(command), load_manifest(path).providers[0].command)
            for command in (
                ["curl", "--config=/etc/passwd"],
                ["curl", "--config=../outside"],
                ["curl", "--file=../../outside"],
                ["git", "-C/etc"],
                ["git", "-C", "/etc"],
                ["curl", "-f", "../outside"],
                ["curl", "file:///etc/passwd"],
                ["curl", "--input=file:///etc/passwd"],
                ["curl", "--data-binary=@/tmp/secret"],
                ["curl", "--data=@../outside"],
                ["curl", "--data", "@/tmp/secret"],
                ["curl", "-d@/tmp/secret"],
                ["@/tmp/secret"],
                ["curl", "C:\\tmp\\secret"],
                ["curl", "--data=@C:/tmp/secret"],
            ):
                base["provides"][0]["command"] = command; path.write_text(json.dumps(base))
                with self.subTest(command=command), self.assertRaises(ExtensionError):
                    load_manifest(path)
            for command in (
                ["curl", "--data-binary=@simpledata"],
                ["curl", "--data-binary=@provider/data.json"],
            ):
                base["provides"][0]["command"] = command; path.write_text(json.dumps(base))
                with self.subTest(command=command):
                    self.assertEqual(tuple(command), load_manifest(path).providers[0].command)

    def test_adapter_lock_contract(self):
        digest = "sha256:" + "a" * 64
        adapter = {"agentPath":".agents/skills/local-e-s","claudePath":".claude/skills/local-e-s","skill":"s","installedDigest":digest}
        provider = {"capability":"branch.naming","provider":"team","apiVersion":1}
        binding = {"default":"e/team","repositories":{"service":"e/team"}}
        lock = {"lockVersion": {"major": 1, "minor": 0}, "kitApi": 1,"extensions":[{"id":"e","version":"1.0.0","path":"extensions/e","digest":digest,"providers":[provider],"actions":[],"adapters":[adapter]}],"providers":{"branch.naming":binding}}
        self.assertEqual(adapter, AdapterSpec.from_dict(adapter).as_dict())
        with self.assertRaises(ExtensionError): AdapterSpec.from_dict({**adapter, "agentPath": ".agents/skills/bad\0path"})
        self.assertEqual(lock, normalize_extensions_lock(lock))
        invalid = json.loads(json.dumps(lock)); invalid["providers"] = {"policy.pack": []}
        with self.assertRaises(ExtensionError): normalize_extensions_lock(invalid)
        cases = []
        invalid = json.loads(json.dumps(lock)); del invalid["extensions"][0]["adapters"]; cases.append(invalid)
        invalid = json.loads(json.dumps(lock)); invalid["extensions"][0]["adapters"][0]["agentPath"] = ".agents/skills/workspace-core"; cases.append(invalid)
        invalid = json.loads(json.dumps(lock)); invalid["providers"]["branch.naming"]["default"] = "e/missing"; cases.append(invalid)
        invalid = json.loads(json.dumps(lock)); invalid["providers"]["branch.naming"]["repositories"]["service"] = "e/missing"; cases.append(invalid)
        invalid = json.loads(json.dumps(lock)); invalid["providers"] = {"policy.pack":{"default":"e/team","repositories":{}}}; cases.append(invalid)
        invalid = json.loads(json.dumps(lock)); invalid["extensions"][0]["providers"].append(dict(provider)); cases.append(invalid)
        for invalid in cases:
            with self.subTest(invalid=invalid), self.assertRaises(ExtensionError): normalize_extensions_lock(invalid)

    def test_skill_and_manifest_symlinks_are_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "e"; (root / "skills/s").mkdir(parents=True)
            skill = root / "skills/s/SKILL.md"
            skill.write_text("---\nname: s\n---\n")
            manifest = {"schemaVersion": 1,"id":"e","version":"1.0.0","kitApi": 1,"provides":[{"capability":"branch.naming","provider":"p","apiVersion":1,"skill":"s"}], "actions": [], "requires":[],"effects":[]}
            path = root / "workspace-extension.json"; path.write_text(json.dumps(manifest))
            skill.unlink(); skill.symlink_to(Path(d) / "outside")
            with self.assertRaises(ExtensionError): load_manifest(path)
            path.unlink(); path.symlink_to(Path(d) / "outside-manifest")
            with self.assertRaises(ExtensionError): load_manifest(path)
            real = Path(d) / "real"; real.mkdir(); linked = Path(d) / "linked"; linked.symlink_to(real, target_is_directory=True)
            with self.assertRaises(ExtensionError): load_manifest(linked / "workspace-extension.json")

if __name__ == "__main__": unittest.main()
