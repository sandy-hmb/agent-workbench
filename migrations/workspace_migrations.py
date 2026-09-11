"""Versioned migrations for an existing ``.workspace`` state tree."""

from __future__ import annotations

import hashlib
import hmac
import json
import shutil
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable, Mapping, Sequence

from workspace_model import (
    WorkspaceError,
    atomic_write_many,
    parse_json_bytes,
    parse_workspace,
    render_context,
    render_repository_profile,
    workspace_schema_version,
)
from workspace_paths import state_root


WorkspaceFiles = dict[str, bytes]
TransformResult = WorkspaceFiles | tuple[WorkspaceFiles, list[str]]
Transform = Callable[[WorkspaceFiles], TransformResult]


@dataclass(frozen=True)
class MigrationStep:
    from_version: int
    to_version: int
    name: str
    transform: Transform


def _major(value: object, label: str) -> int:
    if type(value) is int and value >= 1:
        return value
    if (
        isinstance(value, dict)
        and set(value) == {"major", "minor"}
        and type(value["major"]) is int
        and value["major"] >= 1
        and type(value["minor"]) is int
        and value["minor"] >= 0
    ):
        return value["major"]
    raise WorkspaceError(f"{label} 版本表达无效")


def _json(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


LEGACY_TEMPLATE = Path(__file__).resolve().parent / "legacy/workspace_agents_v1.md"
CURRENT_TEMPLATE = Path(__file__).resolve().parents[1] / "templates/workspace/AGENTS.md"


def _legacy_prefix() -> str:
    content = LEGACY_TEMPLATE.read_text(encoding="utf-8")
    return content.split("\n## ", 1)[0].rstrip("\n") + "\n"


def _migrate_agents(content: bytes) -> tuple[bytes, list[str]]:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        return content, ["AGENTS.md"]
    marker = text.find("\n## ")
    prefix = text if marker < 0 else text[:marker] + "\n"
    suffix = "" if marker < 0 else text[marker + 1 :]
    try:
        legacy = _legacy_prefix()
        current = CURRENT_TEMPLATE.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise WorkspaceError(f"迁移模板不可读：{exc}") from exc
    if text.startswith(legacy):
        return (current + text[len(legacy) :]).encode("utf-8"), []
    if not prefix.startswith("# 用户治理工作区\n"):
        return content, ["AGENTS.md"]

    legacy_lines = {
        line.strip()
        for line in legacy.splitlines()
        if line.strip().startswith("-")
    }
    custom = [
        line.rstrip("\r\n")
        for line in prefix.splitlines()
        if line.strip() and not line.lstrip().startswith("#") and line.strip() not in legacy_lines
    ]
    migrated = current.rstrip("\n")
    if custom:
        migrated += "\n\n## 本工作区保留的自定义条款\n\n" + "\n".join(custom)
    if suffix:
        migrated += "\n\n" + suffix
    else:
        migrated += "\n"
    return migrated.encode("utf-8"), []


def _migrate_v1(files: WorkspaceFiles) -> TransformResult:
    result = dict(files)
    manual_review: list[str] = []
    if "AGENTS.md" in result:
        result["AGENTS.md"], review = _migrate_agents(result["AGENTS.md"])
        manual_review.extend(review)

    registry = parse_json_bytes(result["workspace.json"], ".workspace/workspace.json")
    registry["version"] = {"major": 2, "minor": 0}
    workspace = parse_workspace(registry, Path("/migration"), validate_extension_refs=False)
    result["workspace.json"] = _json(registry)
    result["CONTEXT.md"] = render_context(workspace).encode("utf-8")
    for repository in workspace.repositories:
        result[repository.instruction] = render_repository_profile(
            workspace, repository
        ).encode("utf-8")
    return (result, manual_review)


MIGRATION_STEPS: tuple[MigrationStep, ...] = (
    MigrationStep(1, 2, "workspace-v1-to-v2", _migrate_v1),
)


def workspace_version(root: Path) -> int:
    return workspace_schema_version(root)


def _relative(value: str) -> str:
    path = PurePosixPath(value)
    if not value or path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise WorkspaceError(f"迁移文件路径无效：{value}")
    return path.as_posix()


def _read_files(root: Path) -> WorkspaceFiles:
    state = state_root(root)
    if state.is_symlink() or not state.is_dir():
        raise WorkspaceError(f".workspace 必须是非符号链接目录：{state}")
    files: WorkspaceFiles = {}
    for path in sorted(state.rglob("*")):
        if path.is_symlink():
            raise WorkspaceError(f"迁移状态不允许符号链接：{path}")
        if path.is_file():
            files[_relative(path.relative_to(state).as_posix())] = path.read_bytes()
        elif not path.is_dir():
            raise WorkspaceError(f"迁移状态必须是普通文件或目录：{path}")
    if "workspace.json" not in files:
        raise WorkspaceError("迁移状态缺少 .workspace/workspace.json")
    return files


def _steps_between(
    source: int, target: int, steps: Sequence[MigrationStep]
) -> list[MigrationStep]:
    if source > target:
        raise WorkspaceError(f"当前 workspace 版本 {source} 高于 Kit 支持的 {target}")
    by_source: dict[int, MigrationStep] = {}
    for step in steps:
        if (
            type(step.from_version) is not int
            or step.to_version != step.from_version + 1
            or not step.name
            or step.from_version in by_source
        ):
            raise WorkspaceError("迁移步骤注册无效")
        by_source[step.from_version] = step
    result = []
    current = source
    while current < target:
        step = by_source.get(current)
        if step is None:
            raise WorkspaceError(f"缺少 workspace v{current}→v{current + 1} 迁移步骤")
        result.append(step)
        current = step.to_version
    return result


def _plan(root: Path, target_version: int, steps: Sequence[MigrationStep]) -> tuple[dict[str, object], WorkspaceFiles]:
    files = _read_files(root)
    source = workspace_version(root)
    selected = _steps_between(source, target_version, steps)
    manual_review: list[str] = []
    for step in selected:
        transformed = step.transform(dict(files))
        if isinstance(transformed, tuple):
            transformed, review = transformed
            manual_review.extend(review)
        files = dict(transformed)
        if not all(isinstance(path, str) and isinstance(content, bytes) for path, content in files.items()):
            raise WorkspaceError(f"迁移步骤 {step.name} 必须返回文件字节映射")
        files = {_relative(path): content for path, content in files.items()}
    registry = parse_json_bytes(files["workspace.json"], ".workspace/workspace.json")
    if _major(registry.get("version"), "workspace.json") != target_version:
        raise WorkspaceError("迁移步骤未将 workspace.json 更新到目标版本")
    summary = {
        "migration": "workspace-schema",
        "fromVersion": source,
        "toVersion": target_version,
        "steps": [step.name for step in selected],
        "files": [
            {"path": path, "sha256": hashlib.sha256(content).hexdigest()}
            for path, content in sorted(files.items())
        ],
    }
    if manual_review:
        summary["manualReview"] = sorted(set(manual_review))
    digest = hashlib.sha256(
        json.dumps(summary, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {**summary, "previewHash": digest}, files


def preview(
    root: Path,
    *,
    target_version: int,
    steps: Sequence[MigrationStep] = MIGRATION_STEPS,
    include_diff: bool = False,
) -> dict[str, object]:
    root = Path(root).resolve()
    before = _read_files(root) if include_diff else None
    plan, files = _plan(root, target_version, steps)
    if before is not None:
        changes = []
        for path in sorted(set(before) | set(files)):
            old = before.get(path, b"")
            new = files.get(path, b"")
            if old == new:
                continue
            try:
                old_text = old.decode("utf-8")
                new_text = new.decode("utf-8")
            except UnicodeDecodeError:
                diff = "（二进制或非 UTF-8 文件，无法显示文本差异）"
            else:
                import difflib

                diff = "".join(
                    difflib.unified_diff(
                        old_text.splitlines(keepends=True),
                        new_text.splitlines(keepends=True),
                        fromfile=f"current/{path}",
                        tofile=f"migrated/{path}",
                    )
                )
            changes.append({"path": path, "diff": diff})
        plan = {**plan, "diff": changes}
    return plan


def _backup(state: Path, backup_dir: Path | None) -> Path:
    if backup_dir is None:
        raise WorkspaceError("迁移前必须提供 --backup-dir 备份路径")
    backup_dir = Path(backup_dir).resolve()
    if backup_dir.exists() or backup_dir.is_symlink() or not backup_dir.parent.is_dir():
        raise WorkspaceError("备份路径必须不存在，且其父目录必须存在")
    if backup_dir.is_relative_to(state):
        raise WorkspaceError("备份路径不能位于 .workspace 内")
    shutil.copytree(state, backup_dir)
    return backup_dir


def apply(
    root: Path,
    preview_hash: str,
    *,
    target_version: int,
    steps: Sequence[MigrationStep] = MIGRATION_STEPS,
    backup_dir: Path | None = None,
) -> dict[str, object]:
    if not isinstance(preview_hash, str) or len(preview_hash) != 64:
        raise WorkspaceError("迁移预览哈希格式无效")
    root = Path(root).resolve()
    plan, files = _plan(root, target_version, steps)
    if not hmac.compare_digest(str(plan["previewHash"]), preview_hash):
        raise WorkspaceError("迁移预览哈希不匹配")
    if plan["steps"]:
        backup = _backup(state_root(root), backup_dir)
        state = state_root(root)
        for relative in files:
            (state / relative).parent.mkdir(parents=True, exist_ok=True)
        atomic_write_many([(state / relative, content) for relative, content in files.items()])
        for path in sorted(state.rglob("*"), reverse=True):
            if path.is_file() and path.relative_to(state).as_posix() not in files:
                path.unlink()
        return {**plan, "backupDir": str(backup)}
    return plan
