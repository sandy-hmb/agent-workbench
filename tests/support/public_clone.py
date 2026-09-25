from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


from tests.support import ROOT
sys.path.insert(0, str(ROOT))
import workbench.cli.items as item_context  # noqa: E402
PUBLIC_DIRECTORIES = (
    ".github",
    ".agents",
    ".claude",
    "docs",
    "examples",
    "schemas",
    "scripts",
    "workbench",
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
    names = {'workspace_setup.py': 'setup', 'workspace_extension.py': 'extension', 'workspace_workflow.py': 'workflow',
             'workspace_status.py': 'status', 'workspace_doctor.py': 'doctor', 'workspace_registry.py': 'registry',
             'workspace_provider.py': 'provider', 'workspace_update.py': 'update', 'kit_describe.py': 'describe',
             'feature_context.py': 'item', 'item_context.py': 'item'}
    command = names.get(script)
    argv = [sys.executable, str(root / 'scripts/kit.py'), *([command] if command else []), *arguments]
    return run_command(
        argv,
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
                "instruction": "repositories/service.md",
            }
        )
    config = kit / "workspace-input.json"
    config.write_text(
        json.dumps(
            {
                "version": {"major": 4, "minor": 0},
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
