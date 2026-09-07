from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import feature_context  # noqa: E402
PUBLIC_DIRECTORIES = (
    ".github",
    ".agents",
    ".claude",
    "docs",
    "examples",
    "migrations",
    "schemas",
    "scripts",
    "templates",
    "tests",
    "upgrades",
    "workflows",
)
PUBLIC_FILES = (
    ".gitignore",
    "AGENTS.md",
    "CLAUDE.md",
    "CHANGELOG.md",
    "CONTRIBUTING.md",
    "GEMINI.md",
    "LICENSE",
    "README.md",
    "SECURITY.md",
    "VERSION",
)
COPY_IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store", "development")


def run_command(
    arguments: list[str], *, cwd: Path | None = None, check: bool = True
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        arguments,
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if check and result.returncode:
        raise AssertionError(
            f"command failed ({result.returncode}): {arguments}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result


def git_status(root: Path) -> str:
    return run_command(["git", "-C", str(root), "status", "--porcelain"]).stdout


def run_script(
    root: Path, script: str, *arguments: str, check: bool = True
) -> subprocess.CompletedProcess[str]:
    return run_command(
        [sys.executable, str(root / "scripts" / script), *arguments],
        cwd=root,
        check=check,
    )


def create_public_clone(parent: Path) -> tuple[Path, Path, Path]:
    """Create a committed public source, bare remote, and ordinary user clone."""
    source = parent / "public-source"
    remote = parent / "public.git"
    clone = parent / "kit"
    source.mkdir()
    for relative in PUBLIC_DIRECTORIES:
        shutil.copytree(
            ROOT / relative,
            source / relative,
            symlinks=True,
            ignore=COPY_IGNORE,
        )
    for relative in PUBLIC_FILES:
        shutil.copy2(ROOT / relative, source / relative)

    run_command(["git", "init", "-q", str(source)])
    run_command(["git", "-C", str(source), "add", "."])
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
            "public fixture",
        ]
    )
    branch = run_command(
        ["git", "-C", str(source), "branch", "--show-current"]
    ).stdout.strip()
    run_command(["git", "init", "-q", "--bare", str(remote)])
    run_command(["git", "-C", str(source), "remote", "add", "origin", str(remote)])
    run_command(["git", "-C", str(source), "push", "-q", "-u", "origin", branch])
    run_command(
        ["git", "--git-dir", str(remote), "symbolic-ref", "HEAD", f"refs/heads/{branch}"]
    )
    run_command(["git", "clone", "-q", str(remote), str(clone)])
    return source, remote, clone


def workspace_input(kit: Path, *, include_repository: bool) -> Path:
    repositories: list[dict[str, object]] = []
    if include_repository:
        repository = kit.parent / "service"
        run_command(["git", "init", "-q", str(repository)])
        repositories.append(
            {
                "path": "service",
                "aliases": ["svc"],
                "remote": None,
                "category": "backend",
                "description": "Fixture service",
                "instruction": "docs/repositories/service.md",
            }
        )
    config = kit / "workspace-input.json"
    config.write_text(
        json.dumps(
            {
                "version": {"major": 1, "minor": 0},
                "workspace": {"name": "Clone Fixture"},
                "local": {
                    "branchOwner": "smoke",
                    "primaryRole": None,
                    "extensions": {},
                },
                "context": {"description": "Temporary public clone fixture"},
                "branchPolicy": {
                    "workBase": "main",
                    "testTarget": None,
                    "hotfixBase": None,
                    "namePattern": "{owner}/{type}/{slug}",
                },
                "extensions": {"providers": {}, "config": {}},
                "repositories": repositories,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return config


def initialize_workspace(kit: Path, *, include_repository: bool = True) -> Path:
    config = workspace_input(kit, include_repository=include_repository)
    preview = json.loads(
        run_script(
            kit,
            "workspace_setup.py",
            "--root",
            str(kit),
            "init",
            "preview",
            "--config",
            str(config),
            "--json",
        ).stdout
    )
    run_script(
        kit,
        "workspace_setup.py",
        "--root",
        str(kit),
        "init",
        "apply",
        "--config",
        str(config),
        "--preview-hash",
        str(preview["previewHash"]),
    )
    return config


class HappyPathTest(unittest.TestCase):
    def test_zero_extension_public_clone_initializes_without_public_git_changes(self):
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary).resolve()
            _, _, kit = create_public_clone(parent)

            self.assertEqual("", git_status(kit))
            self.assertFalse((kit / "docs/development").exists())
            initialize_workspace(kit)
            self.assertEqual("", git_status(kit))

            expected_state = (
                ".workspace/workspace.json",
                ".workspace/workspace.local.json",
                ".workspace/extensions/.state/lock.json",
                ".workspace/AGENTS.md",
                ".workspace/CONTEXT.md",
                ".workspace/docs/repositories/service.md",
                ".workspace/docs/features",
                ".workspace/extensions",
            )
            for relative in expected_state:
                self.assertTrue((kit / relative).exists(), relative)
            for relative in ("workspace.json", "workspace.local.json", "CONTEXT.md"):
                self.assertFalse((kit / relative).exists(), relative)
            self.assertEqual([".state"], [path.name for path in (kit / ".workspace/extensions").iterdir()])
            self.assertEqual(
                [],
                [path.name for path in (kit / ".agents/skills").glob("local-*")],
            )
            self.assertEqual(
                [],
                [path.name for path in (kit / ".claude/skills").glob("local-*")],
            )

            resolved = json.loads(
                run_script(
                    kit,
                    "workspace_registry.py",
                    "--root",
                    str(kit),
                    "resolve",
                    "svc",
                    "--json",
                ).stdout
            )
            self.assertEqual("service", resolved["name"])
            self.assertEqual(str(parent / "service"), resolved["path"])
            branch = json.loads(
                run_script(
                    kit,
                    "workspace_registry.py",
                    "--root",
                    str(kit),
                    "branch",
                    "svc",
                    "--type",
                    "feature",
                    "--slug",
                    "clone-flow",
                    "--json",
                ).stdout
            )
            self.assertEqual("smoke/feature/clone-flow", branch["branch"])
            self.assertEqual("main", branch["baseBranch"])

            service = parent / "service"
            (service / "README.md").write_text("# service\n", encoding="utf-8")
            run_command(["git", "-C", str(service), "add", "README.md"])
            run_command(
                [
                    "git",
                    "-C",
                    str(service),
                    "-c",
                    "user.name=Fixture",
                    "-c",
                    "user.email=fixture@example.test",
                    "commit",
                    "-qm",
                    "initial service",
                ]
            )

            feature = feature_context.create_feature(
                kit,
                "clone-flow",
                ["service"],
                title="Clone Flow",
                summary="Keep feature artifacts with the requirement.",
            )
            sql = feature / "artifacts/sql/001-create.sql"
            sql.parent.mkdir(parents=True)
            sql.write_text("create table clone_flow;\n", encoding="utf-8")
            self.assertTrue(feature.is_dir())
            self.assertFalse((kit / "docs/features/clone-flow").exists())
            status = json.loads(
                run_script(
                    kit, "workspace_status.py", "--root", str(kit), "--json"
                ).stdout
            )
            self.assertEqual("workspace", status["mode"])
            self.assertEqual([], status["extensions"]["activeIds"])
            self.assertEqual(["clone-flow"], [item["featureSlug"] for item in status["features"]])
            self.assertEqual(
                [{"path": "artifacts/sql/001-create.sql", "type": "sql"}],
                status["features"][0]["artifacts"],
            )

            (feature / "plans").mkdir()
            (feature / "testing").mkdir()
            (feature / "plans/implementation.md").write_text(
                "# 实施计划\n\n- [x] 完成实现\n- [x] 完成验证\n",
                encoding="utf-8",
            )
            snapshot = json.loads(
                run_script(
                    kit,
                    "kit.py",
                    "verify",
                    "snapshot",
                    "clone-flow",
                    "--root",
                    str(kit),
                    "--json",
                ).stdout
            )
            (feature / "testing/verification.md").write_text(
                "# 验证记录\n\n"
                "## 验证批次 2026-09-06T12:00:00+08:00\n"
                "- 总体结果：通过\n"
                "- 审查结论：通过\n"
                f"- 代码状态：{json.dumps(snapshot['codeState'], separators=(',', ':'))}\n\n"
                "### 检查 1\n"
                f"- 工作目录：`{parent / 'service'}`\n"
                "- 命令：`python3 -m unittest`\n"
                "- 退出状态：0\n"
                "- 结果：通过\n",
                encoding="utf-8",
            )
            run_script(
                kit,
                "feature_context.py",
                "--root",
                str(kit),
                "set-status",
                "clone-flow",
                "development",
            )

            resumed = json.loads(
                run_script(
                    kit,
                    "kit.py",
                    "brief",
                    "clone-flow",
                    "--root",
                    str(kit),
                    "--json",
                ).stdout
            )
            self.assertEqual({"completed": 2, "total": 2}, resumed["progress"])
            self.assertEqual([], resumed["pendingTasks"])
            self.assertIn("- 结果：通过", resumed["verificationSummary"])
            self.assertEqual("feature.submit-test", resumed["currentStage"])

            run_script(
                kit,
                "feature_context.py",
                "--root",
                str(kit),
                "set-status",
                "clone-flow",
                "done",
            )
            finished = json.loads(
                run_script(
                    kit, "workspace_status.py", "--root", str(kit), "--json"
                ).stdout
            )
            self.assertEqual([], finished["features"])
            self.assertEqual("feature.context", finished["currentStage"])

            doctor = run_script(kit, "workspace_doctor.py", "--root", str(kit))
            self.assertIn("SUMMARY ERROR=0", doctor.stdout)
            self.assertEqual("", git_status(kit))


if __name__ == "__main__":
    unittest.main()
