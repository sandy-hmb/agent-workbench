#!/usr/bin/env python3
"""Resolve public Kit and ignored local workspace paths."""
from __future__ import annotations

import subprocess
import json
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


def feature_work_item_file(feature: Path) -> Path:
    """Return the optional machine-readable activity container for a Feature."""
    return Path(feature) / ".work-item.json"


DOCUMENT_ROLES = {
    "requirements": ("requirements.md", "requirements/requirements.md"),
    "design": ("design.md", "design/design.md"),
    "plan": ("plan.md", "plans/implementation.md"),
    "verification": ("verification.md", "testing/verification.md"),
}


def feature_document_file(feature: Path, role: str) -> Path:
    """Resolve a single authoritative document without migrating existing records."""
    feature = Path(feature)
    alternatives = DOCUMENT_ROLES[role]
    existing = []
    for relative in alternatives:
        candidate = feature / relative
        if candidate.is_symlink() or candidate.parent.is_symlink():
            raise ValueError(f"DOCUMENT_UNSAFE_PATH: 文档路径包含符号链接：{candidate}")
        if candidate.exists():
            if not candidate.is_file():
                raise ValueError(f"DOCUMENT_UNSAFE_PATH: 文档不是普通文件：{candidate}")
            existing.append(candidate)
    if len(existing) > 1:
        raise ValueError(f"DOCUMENT_ROLE_CONFLICT: {role} 存在多个有效文档：" + ", ".join(str(x) for x in existing))
    if existing:
        return existing[0]
    marker = feature_work_item_file(feature)
    layout = None
    if marker.is_file() and not marker.is_symlink():
        try:
            raw = json.loads(marker.read_text(encoding="utf-8"))
            layout = raw.get("documentLayout") if isinstance(raw, dict) else None
        except (OSError, ValueError):
            pass  # The work-item reader reports malformed metadata separately.
    return feature / alternatives[0 if layout == "flat-v1" or role == "plan" else 1]


def feature_plan_file(feature: Path) -> Path:
    return feature_document_file(feature, "plan")


def feature_plan_relative(feature: Path) -> str:
    return feature_plan_file(feature).relative_to(feature).as_posix()


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
    # 版本无关的迁移进行中标记；来源与目标版本记录在文件内容里，而不是文件名上。
    return state_root(root) / ".migration-marker.json"


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
