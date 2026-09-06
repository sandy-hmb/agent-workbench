from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from workspace_local import load_local_settings  # noqa: E402
from workspace_model import WorkspaceError  # noqa: E402


class WorkspaceLocalTest(unittest.TestCase):
    def write(self, root: Path, value: object) -> Path:
        state = root / ".workspace"
        state.mkdir(exist_ok=True)
        path = state / "workspace.local.json"
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def test_loads_valid_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write(
                root,
                {
                    "branchOwner": "alice",
                    "primaryRole": "backend",
                    "extensions": {"sample": {"endpoint": "local"}},
                },
            )
            settings = load_local_settings(root)
            self.assertEqual("alice", settings.branch_owner)
            self.assertEqual("backend", settings.primary_role)
            self.assertEqual({"sample": {"endpoint": "local"}}, settings.extensions)

    def test_rejects_sensitive_names_anywhere_recursively(self):
        suffixes = ("accesskey", "apikey", "cookie", "password", "privatekey", "secret", "token")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for suffix in suffixes:
                with self.subTest(suffix=suffix):
                    path = self.write(
                        root,
                        {
                            "branchOwner": "alice",
                            "primaryRole": None,
                            "extensions": {"sample": {f"my_{suffix}": "value"}},
                        },
                    )
                    with self.assertRaisesRegex(WorkspaceError, "敏感字段"):
                        load_local_settings(root)
                    path.unlink()
            for key in ("clientSecretValue", "tokenEndpoint"):
                with self.subTest(key=key):
                    path = self.write(
                        root,
                        {
                            "branchOwner": "alice",
                            "primaryRole": None,
                            "extensions": {"sample": {key: "value"}},
                        },
                    )
                    with self.assertRaisesRegex(WorkspaceError, key):
                        load_local_settings(root)
                    path.unlink()

            self.write(
                root,
                {
                    "branchOwner": "alice",
                    "primaryRole": None,
                    "extensions": {"sample": {"settingsFile": "path"}},
                },
            )
            self.assertEqual(
                "path", load_local_settings(root).extensions["sample"]["settingsFile"]
            )

    def test_rejects_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / ".workspace"
            state.mkdir()
            target = root / "outside.json"
            target.write_text("{}", encoding="utf-8")
            (state / "workspace.local.json").symlink_to(target)
            with self.assertRaisesRegex(WorkspaceError, "普通文件"):
                load_local_settings(root)

    def test_missing_required_and_optional(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(WorkspaceError, "缺少"):
                load_local_settings(root)
            self.assertEqual(
                (None, None, {}, None, {}),
                tuple(load_local_settings(root, required=False).__dict__.values()),
            )

    def test_legacy_settings_without_active_feature_key_default_to_none(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write(
                root,
                {"branchOwner": "alice", "primaryRole": None, "extensions": {}},
            )
            settings = load_local_settings(root)
            self.assertIsNone(settings.active_feature)

    def test_loads_active_feature_when_present(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write(
                root,
                {
                    "branchOwner": "alice",
                    "primaryRole": None,
                    "extensions": {},
                    "activeFeature": "payment-feature",
                },
            )
            settings = load_local_settings(root)
            self.assertEqual("payment-feature", settings.active_feature)

    def test_rejects_invalid_active_feature(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write(
                root,
                {
                    "branchOwner": "alice",
                    "primaryRole": None,
                    "extensions": {},
                    "activeFeature": "Not Safe!",
                },
            )
            with self.assertRaisesRegex(WorkspaceError, "activeFeature"):
                load_local_settings(root)

    def test_rejects_unknown_field(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write(
                root,
                {
                    "branchOwner": "alice",
                    "primaryRole": None,
                    "extensions": {},
                    "unexpectedField": True,
                },
            )
            with self.assertRaisesRegex(WorkspaceError, "本地配置"):
                load_local_settings(root)


if __name__ == "__main__":
    unittest.main()
