from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
import sys

sys.path.insert(0, str(ROOT / "scripts"))

import workspace_submit  # noqa: E402


def run_git(*arguments: str, cwd: Path) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return result.stdout.strip()


class WorkspaceSubmitTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.parent = Path(self.temp.name).resolve()
        self.root = self.parent / "kit"
        self.service = self.parent / "service"
        self.remote = self.parent / "service.git"
        self.root.mkdir()
        self.service.mkdir()
        run_git("init", "-q", str(self.service), cwd=self.parent)
        (self.service / "README.md").write_text("initial\n", encoding="utf-8")
        run_git("add", "README.md", cwd=self.service)
        run_git(
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-qm",
            "initial",
            cwd=self.service,
        )
        run_git("branch", "-M", "main", cwd=self.service)
        run_git("config", "user.name", "Test", cwd=self.service)
        run_git("config", "user.email", "test@example.com", cwd=self.service)
        run_git("init", "-q", "--bare", str(self.remote), cwd=self.parent)
        run_git("remote", "add", "origin", "https://example.test/service.git", cwd=self.service)
        self.env = mock.patch.dict(
            os.environ,
            {
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "url.file://" + str(self.parent) + "/.insteadOf",
                "GIT_CONFIG_VALUE_0": "https://example.test/",
            },
        )
        self.env.start()
        run_git("push", "-q", "-u", "origin", "main", cwd=self.service)
        run_git("switch", "-q", "-c", "test", cwd=self.service)
        run_git("push", "-q", "-u", "origin", "test", cwd=self.service)
        run_git("switch", "-q", "main", cwd=self.service)
        run_git("switch", "-q", "-c", "owner/feature/demo", cwd=self.service)
        run_git("remote", "set-url", "origin", "https://example.test/service.git", cwd=self.service)

        state = self.root / ".workspace"
        (state / "docs/repositories").mkdir(parents=True)
        (state / "docs/features/demo-feature/testing").mkdir(parents=True)
        (state / "docs/features/demo-feature/plans").mkdir(parents=True)
        (state / "docs/features/demo-feature/artifacts/sql").mkdir(parents=True)
        (state / "workspace.json").write_text(
            json.dumps(
                {
                    "version": {"major": 1, "minor": 0},
                    "workspace": {"name": "Demo"},
                    "context": {},
                    "branchPolicy": {
                        "workBase": "main",
                        "testTarget": "test",
                        "hotfixBase": "main",
                        "namePattern": "{owner}/{type}/{slug}",
                    },
                    "extensions": {"providers": {}, "config": {}},
                    "repositories": [
                        {
                            "path": "service",
                            "aliases": [],
                            "remote": "https://example.test/service.git",
                            "category": "service",
                            "description": "Service",
                            "instruction": "docs/repositories/service.md",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        (state / "workspace.local.json").write_text(
            json.dumps({"branchOwner": "owner", "primaryRole": None, "extensions": {}}),
            encoding="utf-8",
        )
        (state / "docs/features/demo-feature/README.md").write_text(
            "# Demo\n\n"
            "- 状态：development\n"
            "- 需求短名：`demo-feature`\n"
            "- 涉及仓库：`service`\n"
            "- 工作分支：`service` -> `owner/feature/demo`\n"
            "- 基线分支：`service` -> `main`\n"
            "- 最后更新：2026-09-04\n",
            encoding="utf-8",
        )
        (state / "docs/features/demo-feature/plans/implementation.md").write_text(
            "- [x] implement\n", encoding="utf-8"
        )
        (state / "docs/features/demo-feature/testing/verification.md").write_text(
            "# Verification\n", encoding="utf-8"
        )
        (state / "docs/features/demo-feature/artifacts/sql/001-create.sql").write_text(
            "create table demo;\n", encoding="utf-8"
        )

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def run_command(self, arguments: list[str]) -> tuple[int, dict[str, object] | None, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = workspace_submit.main(arguments)
        value = json.loads(stdout.getvalue()) if stdout.getvalue().strip() else None
        return code, value, stderr.getvalue()

    def test_plan_excludes_feature_sql_and_apply_delivers_business_path_only(self):
        (self.service / "README.md").write_text("updated\n", encoding="utf-8")
        arguments = [
            "plan",
            "--root",
            str(self.root),
            "--repo",
            "service",
            "--branch",
            "owner/feature/demo",
            "--feature",
            "demo-feature",
            "--path",
            "README.md",
            "--message",
            "feat: update demo",
            "--json",
        ]
        code, plan, error = self.run_command(arguments)
        self.assertEqual(0, code, error)
        assert plan is not None
        self.assertEqual(["README.md"], plan["changedFiles"])
        self.assertEqual(["artifacts/sql/001-create.sql"], plan["featureArtifacts"])
        self.assertNotIn("artifacts/sql/001-create.sql", plan["paths"])

        code, result, error = self.run_command(
            [
                "apply",
                *arguments[1:-1],
                "--plan-hash",
                plan["planHash"],
                "--json",
            ]
        )
        self.assertEqual(0, code, error)
        self.assertEqual("testing", result["status"])
        self.assertEqual("owner/feature/demo", run_git("branch", "--show-current", cwd=self.service))
        self.assertEqual("testing", (self.root / ".workspace/docs/features/demo-feature/README.md").read_text(encoding="utf-8").split("状态：", 1)[1].splitlines()[0])
        self.assertEqual(0, run_git("--git-dir", str(self.remote), "show-ref", "--verify", "--quiet", "refs/heads/test", cwd=self.parent).__len__())
        self.assertEqual("updated", run_git("show", "test:README.md", cwd=self.service))

    def test_apply_rejects_paths_outside_business_repository(self):
        code, result, error = self.run_command(
            [
                "plan",
                "--root",
                str(self.root),
                "--repo",
                "service",
                "--branch",
                "owner/feature/demo",
                "--feature",
                "demo-feature",
                "--path",
                "../kit/.workspace/docs/features/demo-feature/artifacts/sql/001-create.sql",
                "--json",
            ]
        )
        self.assertEqual(1, code)
        self.assertIsNone(result)
        error_value = json.loads(error)
        self.assertEqual("SUBMIT_PATH_INVALID", error_value["error"]["code"])
        self.assertIn("hint", error_value["error"])

    def test_plan_requires_message_for_changed_business_paths(self):
        (self.service / "README.md").write_text("updated\n", encoding="utf-8")
        code, result, error = self.run_command(
            [
                "plan",
                "--root",
                str(self.root),
                "--repo",
                "service",
                "--branch",
                "owner/feature/demo",
                "--feature",
                "demo-feature",
                "--path",
                "README.md",
                "--json",
            ]
        )
        self.assertEqual(1, code)
        self.assertIsNone(result)
        self.assertIn("SUBMIT_MESSAGE_REQUIRED", error)

    def test_patch_mode_requires_testing_feature(self):
        readme = self.root / ".workspace/docs/features/demo-feature/README.md"
        readme.write_text(readme.read_text(encoding="utf-8").replace("状态：development", "状态：planning"), encoding="utf-8")
        code, result, error = self.run_command(
            [
                "plan",
                "--root",
                str(self.root),
                "--repo",
                "service",
                "--branch",
                "owner/feature/demo",
                "--feature",
                "demo-feature",
                "--mode",
                "patch",
                "--json",
            ]
        )
        self.assertEqual(1, code)
        self.assertIsNone(result)
        self.assertIn("SUBMIT_STATUS_INVALID", error)

    def test_plan_rejects_missing_test_target(self):
        workspace = self.root / ".workspace/workspace.json"
        value = json.loads(workspace.read_text(encoding="utf-8"))
        value["branchPolicy"]["testTarget"] = None
        workspace.write_text(json.dumps(value), encoding="utf-8")
        code, result, error = self.run_command(
            [
                "plan",
                "--root",
                str(self.root),
                "--repo",
                "service",
                "--branch",
                "owner/feature/demo",
                "--feature",
                "demo-feature",
                "--json",
            ]
        )
        self.assertEqual(1, code)
        self.assertIsNone(result)
        self.assertIn("SUBMIT_TARGET_MISSING", error)

    def test_plan_allows_unactivated_valid_action(self):
        extension = self.root / ".workspace/extensions/legacy-action"
        skill = extension / "skills/legacy/SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text("---\nname: legacy\n---\n", encoding="utf-8")
        (extension / "workspace-extension.json").write_text(
            json.dumps(
                {
                    "schemaVersion": 1,
                    "id": "legacy-action",
                    "version": "1.0.0",
                    "kitApi": 1,
                    "provides": [],
                    "actions": [{
                        "id": "legacy", "apiVersion": 1, "skill": "legacy", "effects": [],
                        "confirmation": {"title": "Run", "summary": "Run the action."},
                    }],
                    "requires": [],
                    "effects": [],
                }
            ),
            encoding="utf-8",
        )

        code, plan, error = self.run_command(
            [
                "plan", "--root", str(self.root), "--repo", "service",
                "--branch", "owner/feature/demo", "--feature", "demo-feature", "--json",
            ]
        )

        self.assertEqual(0, code, error)
        self.assertIsNotNone(plan)

    def test_apply_keeps_feature_development_when_test_push_is_rejected(self):
        hook = self.remote / "hooks/pre-receive"
        hook.write_text(
            "#!/bin/sh\nwhile read old new ref; do\n  [ \"$ref\" = \"refs/heads/test\" ] && exit 1\ndone\nexit 0\n",
            encoding="utf-8",
        )
        hook.chmod(0o755)
        (self.service / "README.md").write_text("updated\n", encoding="utf-8")
        arguments = [
            "plan",
            "--root",
            str(self.root),
            "--repo",
            "service",
            "--branch",
            "owner/feature/demo",
            "--feature",
            "demo-feature",
            "--path",
            "README.md",
            "--message",
            "feat: update demo",
            "--json",
        ]
        code, plan, error = self.run_command(arguments)
        self.assertEqual(0, code, error)
        assert plan is not None
        code, result, error = self.run_command(
            ["apply", *arguments[1:-1], "--plan-hash", plan["planHash"], "--json"]
        )
        self.assertEqual(1, code)
        self.assertIsNone(result)
        self.assertIn("SUBMIT_GIT_FAILED", error)
        self.assertEqual("owner/feature/demo", run_git("branch", "--show-current", cwd=self.service))
        self.assertEqual(
            "owner/feature/demo", json.loads(error)["error"]["currentBranch"]
        )
        self.assertIn(
            "状态：development",
            (self.root / ".workspace/docs/features/demo-feature/README.md").read_text(encoding="utf-8"),
        )

    def test_apply_keeps_merge_conflict_on_test_branch(self):
        peer = self.parent / "peer"
        subprocess.run(["git", "clone", "-q", str(self.remote), str(peer)], check=True)
        run_git("switch", "-q", "test", cwd=peer)
        run_git("config", "user.name", "Peer", cwd=peer)
        run_git("config", "user.email", "peer@example.com", cwd=peer)
        (peer / "README.md").write_text("test branch\n", encoding="utf-8")
        run_git("add", "README.md", cwd=peer)
        run_git("commit", "-qm", "test change", cwd=peer)
        run_git("push", "-q", "origin", "test", cwd=peer)

        (self.service / "README.md").write_text("work branch\n", encoding="utf-8")
        arguments = [
            "plan",
            "--root",
            str(self.root),
            "--repo",
            "service",
            "--branch",
            "owner/feature/demo",
            "--feature",
            "demo-feature",
            "--path",
            "README.md",
            "--message",
            "feat: conflicting change",
            "--json",
        ]
        code, plan, error = self.run_command(arguments)
        self.assertEqual(0, code, error)
        assert plan is not None
        code, result, error = self.run_command(
            ["apply", *arguments[1:-1], "--plan-hash", plan["planHash"], "--json"]
        )
        self.assertEqual(1, code)
        self.assertIsNone(result)
        self.assertIn("SUBMIT_GIT_FAILED", error)
        self.assertEqual("test", run_git("branch", "--show-current", cwd=self.service))
        self.assertEqual("test", json.loads(error)["error"]["currentBranch"])
        self.assertTrue((self.service / ".git/MERGE_HEAD").is_file())
        self.assertIn(
            "状态：development",
            (self.root / ".workspace/docs/features/demo-feature/README.md").read_text(encoding="utf-8"),
        )


if __name__ == "__main__":
    unittest.main()
