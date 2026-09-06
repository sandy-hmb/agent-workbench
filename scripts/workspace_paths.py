#!/usr/bin/env python3
"""Resolve public Kit and ignored local workspace paths."""
from __future__ import annotations

import subprocess
from pathlib import Path


STATE_DIRECTORY = ".workspace"


def kit_root(root: Path) -> Path:
    return Path(root).resolve()


def state_root(root: Path) -> Path:
    return kit_root(root) / STATE_DIRECTORY


def workspace_file(root: Path) -> Path:
    return state_root(root) / "workspace.json"


def local_file(root: Path) -> Path:
    return state_root(root) / "workspace.local.json"


def context_file(root: Path) -> Path:
    return state_root(root) / "CONTEXT.md"


def features_root(root: Path) -> Path:
    return state_root(root) / "docs" / "features"


def profiles_root(root: Path) -> Path:
    return state_root(root) / "docs" / "repositories"


def extensions_root(root: Path) -> Path:
    return state_root(root) / "extensions"


def extension_state_root(root: Path) -> Path:
    return extensions_root(root) / ".state"


def extension_input_file(root: Path) -> Path:
    return extension_state_root(root) / "input.json"


def lock_file(root: Path) -> Path:
    return extension_state_root(root) / "lock.json"


def extension_sources_file(root: Path) -> Path:
    return extension_state_root(root) / "sources.json"


def workflow_file(root: Path) -> Path:
    return state_root(root) / "workflow.json"


def workflow_runs_root(root: Path) -> Path:
    return state_root(root) / "runs"


def workflow_run_file(root: Path, run_id: str) -> Path:
    return workflow_runs_root(root) / f"{run_id}.json"


def migration_marker_file(root: Path) -> Path:
    return state_root(root) / ".migration-v1-to-v2.json"


def cache_root(root: Path) -> Path:
    return extension_state_root(root) / "cache"


def ignored_by_root_gitignore(root: Path, relative: str) -> bool:
    root = kit_root(root)
    expected = root / ".gitignore"
    if expected.is_symlink() or not expected.is_file():
        return False
    result = subprocess.run(
        [
            "git",
            "-c",
            "core.excludesFile=/dev/null",
            "-C",
            str(root),
            "check-ignore",
            "-v",
            "--no-index",
            "--",
            relative,
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode or not result.stdout:
        return False
    source = result.stdout.split("\t", 1)[0].split(":", 1)[0]
    source_path = Path(source)
    if not source_path.is_absolute():
        source_path = root / source_path
    try:
        return source_path.resolve() == expected.resolve()
    except (OSError, RuntimeError):
        return False


def adapters_root(root: Path, client: str) -> Path:
    if client == "agents":
        return kit_root(root) / ".agents" / "skills"
    if client == "claude":
        return kit_root(root) / ".claude" / "skills"
    raise ValueError(f"不支持的 Agent Adapter：{client}")
