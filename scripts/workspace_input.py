#!/usr/bin/env python3
"""Parse temporary init or add-repo input; only init requires local settings."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from workspace_local import LocalSettings, parse_local_settings
from workspace_model import (
    TOP_LEVEL_FIELDS,
    VERSION_VALUE,
    Workspace,
    WorkspaceError,
    _safe_name,
    is_independent_git,
    origin_remote,
    parse_workspace,
    read_json,
)


@dataclass(frozen=True)
class WorkspaceInput:
    workspace: Workspace
    local: LocalSettings | None


def load_workspace_input(
    root: Path,
    config: Path,
    *,
    require_local: bool,
    base: Workspace | None = None,
) -> WorkspaceInput:
    raw = read_json(config)
    allowed = TOP_LEVEL_FIELDS | {"local"}
    unknown = set(raw) - allowed
    if unknown:
        raise WorkspaceError("输入顶层存在未知字段：" + ", ".join(sorted(unknown)))
    if base is None:
        shared: dict[str, object] = {
            "version": dict(VERSION_VALUE),
            "context": {},
            "branchPolicy": {},
            "extensions": {"providers": {}, "config": {}},
            "repositories": [],
        }
    else:
        shared = base.as_dict()
    shared.update({key: value for key, value in raw.items() if key not in {"local", "version"}})
    shared["repositories"] = [
        _repository_defaults(root, item) for item in shared.get("repositories", [])
    ]

    if "local" in raw:
        local_raw = raw["local"]
        if isinstance(local_raw, dict):
            local_raw = {
                "branchOwner": local_raw.get("branchOwner"),
                "primaryRole": local_raw.get("primaryRole"),
                "extensions": local_raw.get("extensions", {}),
            }
        local = parse_local_settings(local_raw)
    elif require_local:
        local = parse_local_settings(
            {"branchOwner": None, "primaryRole": None, "extensions": {}}
        )
    else:
        local = None
    return WorkspaceInput(parse_workspace(shared, root), local)


def _repository_defaults(root: Path, raw: object) -> object:
    if not isinstance(raw, dict):
        return raw
    result = dict(raw)
    path = result.get("path")
    if not isinstance(path, str):
        return result
    result.setdefault("aliases", [])
    if "remote" not in result:
        result["remote"] = _existing_remote(root, path)
    result.setdefault("category", "repository")
    result.setdefault("description", "")
    result.setdefault("instruction", f"docs/repositories/{path}.md")
    return result


def _existing_remote(root: Path, path: str) -> str | None:
    if not _safe_name(path):
        return None
    candidate = root.resolve().parent / path
    if not candidate.is_dir() or not is_independent_git(candidate):
        return None
    return origin_remote(candidate)
