from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from extension_model import load_manifest  # noqa: E402
from workspace_adapters import (  # noqa: E402
    AdapterError,
    adapter_plan,
    apply_adapter_plan,
)


FIXTURE = ROOT / "tests" / "fixtures" / "example-extension"


class WorkspaceAdaptersTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "kit"
        (self.root / ".workspace/cache").mkdir(parents=True)
        (self.root / ".agents/skills").mkdir(parents=True)
        (self.root / ".claude/skills").mkdir(parents=True)
        self.manifest = load_manifest(FIXTURE / "workspace-extension.json")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_creates_marked_agent_and_claude_adapters(self) -> None:
        plan = adapter_plan(
            self.root, active_manifests=(self.manifest,), locked_adapters=()
        )

        self.assertEqual(1, len(plan.create))
        spec = plan.create[0]
        self.assertEqual(
            ".agents/skills/local-example-extension-example-branching",
            spec.relative_path,
        )
        self.assertEqual((), plan.remove)
        apply_adapter_plan(self.root, plan, lambda: None)

        adapter = self.root / spec.relative_path
        self.assertTrue((adapter / "SKILL.md").is_file())
        marker = json.loads(
            (adapter / ".workspace-adapter.json").read_text(encoding="utf-8")
        )
        self.assertEqual("agent-workbench", marker["managedBy"])
        self.assertEqual("example-extension", marker["extension"])
        self.assertEqual("example-branching", marker["skill"])
        self.assertEqual(spec.source_digest, marker["sourceDigest"])
        claude = self.root / spec.claude_path
        self.assertTrue(claude.is_symlink())
        self.assertEqual(
            Path("../../.agents/skills/local-example-extension-example-branching"),
            claude.readlink(),
        )
        self.assertEqual(adapter.resolve(), claude.resolve())
        self.assertEqual(
            {
                "agentPath",
                "claudePath",
                "skill",
                "installedDigest",
            },
            set(plan.lock_entries[0]),
        )

    def test_refuses_to_remove_a_drifted_adapter(self) -> None:
        plan = adapter_plan(
            self.root, active_manifests=(self.manifest,), locked_adapters=()
        )
        apply_adapter_plan(self.root, plan, lambda: None)
        (self.root / plan.create[0].relative_path / "SKILL.md").write_text(
            "manual drift\n", encoding="utf-8"
        )

        with self.assertRaisesRegex(AdapterError, "ADAPTER_DRIFT"):
            adapter_plan(
                self.root, active_manifests=(), locked_adapters=plan.lock_entries
            )

    def test_callback_failure_rolls_back_generated_adapters(self) -> None:
        plan = adapter_plan(
            self.root, active_manifests=(self.manifest,), locked_adapters=()
        )

        def fail() -> None:
            raise RuntimeError("injected state write failure")

        with self.assertRaisesRegex(RuntimeError, "injected"):
            apply_adapter_plan(self.root, plan, fail)
        self.assertFalse((self.root / plan.create[0].relative_path).exists())
        self.assertFalse((self.root / plan.create[0].claude_path).exists())

    def test_link_creation_failure_rolls_back_the_staged_directory(self) -> None:
        plan = adapter_plan(
            self.root, active_manifests=(self.manifest,), locked_adapters=()
        )

        with patch("workspace_adapters.os.symlink", side_effect=OSError("injected link failure")):
            with self.assertRaisesRegex(OSError, "injected"):
                apply_adapter_plan(self.root, plan, lambda: None)
        self.assertFalse((self.root / plan.create[0].relative_path).exists())
        self.assertFalse((self.root / plan.create[0].claude_path).exists())

    def test_never_removes_an_unmarked_local_path(self) -> None:
        path = self.root / ".agents/skills/local-example-extension-example-branching"
        path.mkdir()
        (path / "SKILL.md").write_text("manual\n", encoding="utf-8")
        locked = {
            "agentPath": path.relative_to(self.root).as_posix(),
            "claudePath": ".claude/skills/local-example-extension-example-branching",
            "skill": "example-branching",
            "installedDigest": "sha256:" + "0" * 64,
        }

        with self.assertRaisesRegex(AdapterError, "ADAPTER_DRIFT"):
            adapter_plan(self.root, active_manifests=(), locked_adapters=(locked,))
        self.assertTrue(path.is_dir())

    def test_parent_replacement_after_preflight_never_touches_external_target(self) -> None:
        initial = adapter_plan(
            self.root, active_manifests=(self.manifest,), locked_adapters=()
        )
        apply_adapter_plan(self.root, initial, lambda: None)
        remove = adapter_plan(
            self.root, active_manifests=(), locked_adapters=initial.lock_entries
        )
        cases = (
            ("agents", self.root / ".agents/skills"),
            ("claude", self.root / ".claude/skills"),
            ("cache", self.root / ".workspace/extensions/.state/cache"),
        )
        for label, original in cases:
            with self.subTest(parent=label):
                outside = self.root.parent / f"outside-{label}"
                outside.mkdir()
                sentinel = outside / "keep"
                sentinel.write_text("keep\n", encoding="utf-8")
                stolen = self.root.parent / f"stolen-{label}"

                def replace_parent() -> None:
                    original.rename(stolen)
                    original.symlink_to(outside, target_is_directory=True)

                try:
                    with self.assertRaisesRegex(AdapterError, "ADAPTER_DRIFT"):
                        apply_adapter_plan(
                            self.root,
                            remove,
                            lambda: None,
                            _stage_hook=replace_parent,
                        )
                    self.assertEqual("keep\n", sentinel.read_text(encoding="utf-8"))
                    self.assertEqual([], [path for path in outside.iterdir() if path != sentinel])
                    if label == "agents":
                        self.assertTrue(
                            (
                                stolen
                                / "local-example-extension-example-branching/SKILL.md"
                            ).is_file()
                        )
                    elif label == "claude":
                        self.assertTrue(
                            (stolen / "local-example-extension-example-branching").is_symlink()
                        )
                    else:
                        self.assertTrue(
                            (
                                self.root
                                / ".agents/skills/local-example-extension-example-branching/SKILL.md"
                            ).is_file()
                        )
                finally:
                    if original.is_symlink():
                        original.unlink()
                    if stolen.exists():
                        stolen.rename(original)

    def test_leaf_created_after_preflight_is_never_overwritten(self) -> None:
        plan = adapter_plan(
            self.root, active_manifests=(self.manifest,), locked_adapters=()
        )
        target = self.root / plan.create[0].relative_path

        def occupy_target() -> None:
            target.mkdir()
            (target / "keep").write_text("keep\n", encoding="utf-8")

        with self.assertRaisesRegex(AdapterError, "目标已被占用"):
            apply_adapter_plan(
                self.root, plan, lambda: None, _stage_hook=occupy_target
            )
        self.assertEqual("keep\n", (target / "keep").read_text(encoding="utf-8"))
        self.assertFalse((self.root / plan.create[0].claude_path).exists())

    def test_replaced_claude_leaf_is_not_moved_and_agent_is_rolled_back(self) -> None:
        initial = adapter_plan(
            self.root, active_manifests=(self.manifest,), locked_adapters=()
        )
        apply_adapter_plan(self.root, initial, lambda: None)
        remove = adapter_plan(
            self.root, active_manifests=(), locked_adapters=initial.lock_entries
        )
        spec = initial.create[0]
        agent = self.root / spec.relative_path
        claude = self.root / spec.claude_path
        saved = self.root / "saved-managed-claude"
        callbacks: list[str] = []

        def replace_claude_leaf() -> None:
            claude.rename(saved)
            claude.write_text("keep\n", encoding="utf-8")

        with self.assertRaisesRegex(AdapterError, "ADAPTER_DRIFT"):
            apply_adapter_plan(
                self.root,
                remove,
                lambda: callbacks.append("state"),
                _stage_hook=replace_claude_leaf,
            )
        self.assertEqual([], callbacks)
        self.assertTrue((agent / "SKILL.md").is_file())
        self.assertTrue(saved.is_symlink())
        self.assertEqual("keep\n", claude.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
