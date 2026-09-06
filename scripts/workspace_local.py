#!/usr/bin/env python3
"""Validate ignored per-user workspace settings."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

from workspace_model import (
    WorkspaceError,
    _git_branch_valid,
    _safe_name,
    read_json,
    reject_sensitive_fields,
)
from workspace_paths import local_file


FIELDS = frozenset({"branchOwner", "primaryRole", "extensions"})
OPTIONAL_FIELDS = frozenset({"activeFeature", "extensionSources"})
ALL_FIELDS = FIELDS | OPTIONAL_FIELDS


@dataclass(frozen=True)
class LocalSettings:
    branch_owner: str | None
    primary_role: str | None
    extensions: Mapping[str, object]
    active_feature: str | None = None
    extension_sources: Mapping[str, object] = field(default_factory=dict)

    def as_dict(self) -> dict[str, object]:
        return {
            "branchOwner": self.branch_owner,
            "primaryRole": self.primary_role,
            "extensions": dict(self.extensions),
            "activeFeature": self.active_feature,
            "extensionSources": dict(self.extension_sources or {}),
        }


def parse_local_settings(raw: object) -> LocalSettings:
    if not isinstance(raw, dict) or set(raw) - ALL_FIELDS or FIELDS - set(raw):
        raise WorkspaceError("本地配置必须且只能包含 branchOwner、primaryRole、extensions，可选 activeFeature")
    owner = raw["branchOwner"]
    role = raw["primaryRole"]
    extensions = raw["extensions"]
    active_feature = raw.get("activeFeature")
    if owner is not None and (
        not _safe_name(owner) or not _git_branch_valid(f"{owner}/probe")
    ):
        raise WorkspaceError("branchOwner 必须为 null 或安全 Git 路径段")
    if role is not None and not _safe_name(role):
        raise WorkspaceError("primaryRole 必须为 null 或安全名称")
    if active_feature is not None and not _safe_name(active_feature):
        raise WorkspaceError("activeFeature 必须为 null 或安全的需求短名")
    if not isinstance(extensions, dict) or any(
        not isinstance(extension_id, str)
        or not _safe_name(extension_id)
        or not isinstance(config, dict)
        for extension_id, config in extensions.items()
    ):
        raise WorkspaceError("extensions 的每个扩展 id 必须映射到对象")
    reject_sensitive_fields(extensions, "local.extensions")
    sources = raw.get("extensionSources", {})
    if not isinstance(sources, dict):
        raise WorkspaceError("extensionSources 必须是对象")
    return LocalSettings(
        None if owner is None else str(owner),
        None if role is None else str(role),
        dict(extensions),
        None if active_feature is None else str(active_feature),
        dict(sources),
    )


def load_local_settings(root: Path, *, required: bool = True) -> LocalSettings:
    path = local_file(root)
    if not path.exists() and not path.is_symlink():
        if required:
            raise WorkspaceError(f"缺少本地配置：{path}")
        return LocalSettings(None, None, {})
    if path.parent.is_symlink() or path.is_symlink() or not path.is_file():
        raise WorkspaceError(f"本地配置必须是普通文件且不能是符号链接：{path}")
    return parse_local_settings(read_json(path))


def canonical_local_json(settings: LocalSettings) -> str:
    return json.dumps(settings.as_dict(), ensure_ascii=False, indent=2) + "\n"
