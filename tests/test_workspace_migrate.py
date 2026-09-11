from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

from migrations import workspace_migrations  # noqa: E402
import workspace_status  # noqa: E402


class WorkspaceMigrationsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "kit"
        state = self.root / ".workspace"
        state.mkdir(parents=True)
        (state / "workspace.json").write_text(
            json.dumps({"version": {"major": 1, "minor": 0}}) + "\n",
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def write_full_v1_workspace(self, agents: bytes | None = None) -> None:
        state = self.root / ".workspace"
        registry = {
            "version": {"major": 1, "minor": 0},
            "workspace": {"name": "Demo Workspace"},
            "context": {"description": "Old context", "guardrails": ["keep"]},
            "branchPolicy": {},
            "extensions": {"providers": {}, "config": {}},
            "repositories": [
                {
                    "path": "service",
                    "aliases": [],
                    "remote": None,
                    "category": "backend",
                    "description": "Service",
                    "instruction": "docs/repositories/service.md",
                }
            ],
        }
        (state / "workspace.json").write_text(
            json.dumps(registry, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        legacy = (
            (ROOT / "migrations/legacy/workspace_agents_v1.md")
            .read_text(encoding="utf-8")
            .split("\n## ", 1)[0]
            .rstrip("\n")
            .encode()
            + b"\n"
        )
        (state / "AGENTS.md").write_bytes(legacy if agents is None else agents)
        (state / "CONTEXT.md").write_text("stale context\n", encoding="utf-8")
        profile = state / "docs/repositories/service.md"
        profile.parent.mkdir(parents=True)
        profile.write_text("stale profile\n", encoding="utf-8")

    def test_v1_workspace_preserves_user_section_and_rerenders_generated_files(self):
        legacy = (
            (ROOT / "migrations/legacy/workspace_agents_v1.md")
            .read_text(encoding="utf-8")
            .split("\n## ", 1)[0]
            .rstrip("\n")
            + "\n"
        )
        self.write_full_v1_workspace(
            (legacy + "\n## 用户段\n\n用户内容必须逐字节保留。\n").encode()
        )
        status = workspace_status.status_result(self.root)
        self.assertIn("WORKSPACE_MIGRATION_REQUIRED", status["blockers"])

        plan = workspace_migrations.preview(self.root, target_version=2)
        self.assertEqual(["workspace-v1-to-v2"], plan["steps"])
        result = workspace_migrations.apply(
            self.root,
            plan["previewHash"],
            target_version=2,
            backup_dir=self.root.parent / "backup",
        )

        migrated = (self.root / ".workspace/AGENTS.md").read_text(encoding="utf-8")
        self.assertIn("## 用户段\n\n用户内容必须逐字节保留。\n", migrated)
        self.assertTrue(
            migrated.startswith(
                (ROOT / "templates/workspace/AGENTS.md").read_text(encoding="utf-8")
            )
        )
        self.assertEqual(
            2,
            json.loads((self.root / ".workspace/workspace.json").read_text())["version"]["major"],
        )
        self.assertIn(
            "Demo Workspace 业务上下文",
            (self.root / ".workspace/CONTEXT.md").read_text(),
        )
        self.assertIn(
            "service", (self.root / ".workspace/docs/repositories/service.md").read_text()
        )
        self.assertTrue((self.root.parent / "backup/AGENTS.md").is_file())
        self.assertEqual(plan["previewHash"], result["previewHash"])

    def test_missing_legacy_entries_are_replaced_by_current_template(self):
        self.write_full_v1_workspace(
            "# 用户治理工作区\n\n- custom rule\n\n## 用户段\n\nkeep\n".encode()
        )

        plan = workspace_migrations.preview(self.root, target_version=2)
        migrated = workspace_migrations.apply(
            self.root,
            plan["previewHash"],
            target_version=2,
            backup_dir=self.root.parent / "backup",
        )

        self.assertNotIn("manualReview", migrated)
        agents = (self.root / ".workspace/AGENTS.md").read_text(encoding="utf-8")
        self.assertIn("## 本工作区保留的自定义条款", agents)
        self.assertIn("- custom rule", agents)
        self.assertNotIn("category=backend", agents)

    def test_unclassifiable_agents_are_preserved_and_marked_for_manual_review(self):
        original = b"\xff\xfe\x00\x01"
        self.write_full_v1_workspace(original)

        plan = workspace_migrations.preview(self.root, target_version=2)

        self.assertEqual(original, (self.root / ".workspace/AGENTS.md").read_bytes())
        self.assertIn("AGENTS.md", plan["manualReview"])
        self.assertNotEqual(
            plan["previewHash"],
            workspace_migrations.preview(
                self.root,
                target_version=2,
                steps=(
                    workspace_migrations.MigrationStep(
                        1,
                        2,
                        "without-review",
                        lambda files: workspace_migrations._migrate_v1(files)[0],
                    ),
                ),
            )["previewHash"],
        )

    def test_preview_diff_is_available_without_changing_apply_hash(self):
        self.write_full_v1_workspace()

        plain = workspace_migrations.preview(self.root, target_version=2)
        with_diff = workspace_migrations.preview(
            self.root, target_version=2, include_diff=True
        )

        self.assertEqual(plain["previewHash"], with_diff["previewHash"])
        self.assertTrue(with_diff["diff"])
        self.assertTrue(any(item["path"] == "AGENTS.md" for item in with_diff["diff"]))

    def test_current_version_has_no_registered_steps(self) -> None:
        plan = workspace_migrations.preview(self.root, target_version=1)
        self.assertEqual([], plan["steps"])
        self.assertEqual(
            plan,
            workspace_migrations.apply(
                self.root, str(plan["previewHash"]), target_version=1
            ),
        )

    def test_synthetic_step_preserves_preview_apply_protection(self) -> None:
        def upgrade(
            files: workspace_migrations.WorkspaceFiles,
        ) -> workspace_migrations.WorkspaceFiles:
            result = dict(files)
            state = json.loads(result["workspace.json"])
            state["version"] = {"major": 2, "minor": 0}
            result["workspace.json"] = (json.dumps(state) + "\n").encode()
            return result

        step = workspace_migrations.MigrationStep(1, 2, "synthetic", upgrade)
        plan = workspace_migrations.preview(self.root, target_version=2, steps=(step,))
        self.assertEqual(["synthetic"], plan["steps"])
        result = workspace_migrations.apply(
            self.root,
            str(plan["previewHash"]),
            target_version=2,
            steps=(step,),
            backup_dir=self.root.parent / "backup",
        )
        self.assertEqual(plan["previewHash"], result["previewHash"])
        self.assertTrue((self.root.parent / "backup" / "workspace.json").is_file())
        self.assertEqual(
            2,
            json.loads((self.root / ".workspace/workspace.json").read_text())["version"]["major"],
        )


if __name__ == "__main__":
    unittest.main()
