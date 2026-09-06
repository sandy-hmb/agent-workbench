from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))

from test_happy_path import (  # noqa: E402
    create_public_clone,
    git_status,
    initialize_workspace,
    run_command,
    run_script,
)


def state_snapshot(root: Path) -> dict[str, tuple[str, str | None, int]]:
    """Capture local state content and timestamps, including empty directories."""
    snapshot: dict[str, tuple[str, str | None, int]] = {}
    for path in (root, *sorted(root.rglob("*"))):
        relative = "." if path == root else path.relative_to(root).as_posix()
        metadata = path.lstat()
        if path.is_symlink():
            raise AssertionError(f"unexpected symlink in state fixture: {path}")
        if path.is_dir():
            snapshot[relative] = ("directory", None, metadata.st_mtime_ns)
        elif path.is_file():
            snapshot[relative] = (
                "file",
                hashlib.sha256(path.read_bytes()).hexdigest(),
                metadata.st_mtime_ns,
            )
        else:
            raise AssertionError(f"unexpected special file in state fixture: {path}")
    return snapshot


class UpdateIsolationTest(unittest.TestCase):
    def test_fast_forward_only_updates_public_files_and_preserves_all_local_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary).resolve()
            source, _, user = create_public_clone(parent)
            initialize_workspace(user, include_repository=False)
            state = user / ".workspace"
            (state / "docs" / "local-note.md").write_text("local only\n", encoding="utf-8")
            (state / "extensions" / ".state" / "cache" / "local-note.txt").write_text("local cache\n", encoding="utf-8")
            (state / "workflow.json").write_text(
                json.dumps(
                    {"schemaVersion": {"major": 1, "minor": 0}, "workflow": "feature-development", "stages": []}
                )
                + "\n",
                encoding="utf-8",
            )
            (state / "runs").mkdir()
            (state / "runs/update-run.json").write_text(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "id": "update-run",
                        "workflow": "feature-development",
                        "featureSlug": None,
                        "repository": "service",
                        "branch": "smoke/feature/update-run",
                        "stages": {},
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            before = state_snapshot(state)
            self.assertIn("workspace.json", before)
            self.assertIn("extensions/.state/lock.json", before)
            self.assertIn("docs", before)
            self.assertEqual("", git_status(user))
            self.assertEqual("", run_command(["git", "-C", str(user), "ls-files", ".workspace"]).stdout)

            readme = source / "README.md"
            readme.write_text(
                readme.read_text(encoding="utf-8") + "\nFixture public update.\n",
                encoding="utf-8",
            )
            run_command(["git", "-C", str(source), "add", "README.md"])
            run_command(
                [
                    "git",
                    "-C",
                    str(source),
                    "-c",
                    "user.name=Fixture",
                    "-c",
                    "user.email=fixture@example.test",
                    "commit",
                    "-qm",
                    "public README update",
                ]
            )
            self.assertEqual(
                "README.md\n",
                run_command(
                    ["git", "-C", str(source), "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD"]
                ).stdout,
            )
            run_command(["git", "-C", str(source), "push", "-q"])

            plan = json.loads(
                run_script(user, "workspace_update.py", "plan", "--root", str(user), "--json").stdout
            )
            self.assertFalse(plan["blocked"])
            run_script(
                user,
                "workspace_update.py",
                "apply",
                "--root",
                str(user),
                "--plan-hash",
                str(plan["planHash"]),
                "--json",
            )
            self.assertEqual(before, state_snapshot(state))
            self.assertIn("Fixture public update.", (user / "README.md").read_text(encoding="utf-8"))
            self.assertEqual("", git_status(user))
            doctor = run_script(user, "workspace_doctor.py", "--root", str(user))
            self.assertIn("SUMMARY ERROR=0", doctor.stdout)


if __name__ == "__main__":
    unittest.main()
