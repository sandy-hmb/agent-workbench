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
