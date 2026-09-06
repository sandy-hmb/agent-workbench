#!/usr/bin/env python3
"""Manage explicitly activated local Extensions and their Skill adapters."""

from __future__ import annotations

import argparse
import errno
import fcntl
import hashlib
import json
import os
import re
import secrets
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Optional, Sequence


sys.dont_write_bytecode = True


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from extension_model import (  # noqa: E402
    CAPABILITIES,
    DIGEST_RE,
    ID_RE,
    SEMVER_RE,
    ExtensionError,
    ExtensionManifest,
    extension_digest,
    lock_version_major,
    load_manifest,
    normalize_extensions_lock,
)
from extension_registry import discover_extensions, normalize_workspace_extensions  # noqa: E402
from schema_validation import SchemaValidationError, validate  # noqa: E402
from workspace_adapters import (  # noqa: E402
    AdapterError,
    AdapterPlan,
    adapter_plan,
    apply_adapter_plan,
)
from workspace_model import (  # noqa: E402
    Workspace,
    WorkspaceError,
    atomic_write_many,
    canonical_json,
    load_workspace,
    parse_json_bytes,
    parse_workspace,
)
from workspace_local import load_local_settings  # noqa: E402
from workspace_paths import (  # noqa: E402
    cache_root,
    extension_input_file,
    extension_sources_file,
    extensions_root,
    ignored_by_root_gitignore,
    lock_file,
    state_root,
    workspace_file,
)


DESIRED_FIELDS = frozenset({"extensions", "providers", "config"})
LOCK_FIELDS = frozenset({"lockVersion", "kitApi", "extensions", "providers"})
SOURCE_FIELDS = frozenset({"schemaVersion", "sources"})
LOCK_VERSION = {"major": 1, "minor": 0}
KIT_API = 1
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
_LOCK_FLAGS = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK
_APPLY_LOCK_NAME = "extension.apply.lock"


class ExtensionCommandError(ExtensionError):
    """Raised for desired-state command failures with stable diagnostic codes."""


@dataclass(frozen=True)
class ExtensionFinding:
    code: str
    message: str
    level: str = "ERROR"

    def as_dict(self) -> dict[str, str]:
        return {"level": self.level, "code": self.code, "message": self.message}


@dataclass(frozen=True)
class DesiredState:
    manifests: tuple[ExtensionManifest, ...]
    extensions: Mapping[str, object]


@dataclass(frozen=True)
class TargetState:
    workspace_content: str
    lock_content: str
    adapter_plan: AdapterPlan
    desired: DesiredState


def _command(code: str, message: str) -> ExtensionCommandError:
    return ExtensionCommandError(f"{code}: {message}")


def _root(root: Path) -> Path:
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise _command("EXTENSION_MISSING", f"治理仓必须是普通目录：{root}")
    try:
        return root.resolve()
    except (OSError, RuntimeError) as exc:
        raise _command("EXTENSION_MISSING", f"治理仓不可解析：{root}") from exc


def _safe_path(root: Path, path: Path, *, allow_leaf_symlink: bool = False) -> None:
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise _command("EXTENSION_MISSING", f"路径越出治理仓：{path}") from exc
    current = root
    for index, part in enumerate(relative.parts):
        current /= part
        if current.is_symlink() and not (
            allow_leaf_symlink and index == len(relative.parts) - 1
        ):
            raise _command("EXTENSION_MISSING", f"路径包含符号链接：{current}")
    try:
        path.resolve(strict=False).relative_to(root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise _command("EXTENSION_MISSING", f"路径不可安全解析：{path}") from exc


def _read_regular_json(root: Path, path: Path, *, code: str) -> dict[str, object]:
    _safe_path(root, path)
    if path.is_symlink() or not path.is_file():
        raise _command(code, f"缺少或不安全的 JSON 文件：{path}")
    try:
        return parse_json_bytes(path.read_bytes(), str(path))
    except (OSError, WorkspaceError) as exc:
        raise _command(code, f"无法读取 JSON：{path}：{exc}") from exc


def extension_sources(root: Path) -> dict[str, dict[str, str]]:
    root = _root(root)
    sources = load_local_settings(root, required=True).extension_sources
    result: dict[str, dict[str, str]] = {}
    for extension_id, record in sources.items():
        if (
            not isinstance(extension_id, str)
            or not ID_RE.fullmatch(extension_id)
            or not isinstance(record, dict)
            or not {"source", "digest"} <= set(record)
            or not isinstance(record.get("source"), str)
            or not Path(record["source"]).is_absolute()
            or "\0" in record["source"]
            or any(character in record["source"] for character in "\r\n")
            or not isinstance(record.get("digest"), str)
            or not DIGEST_RE.fullmatch(record["digest"])
        ):
            raise _command("EXTENSION_SOURCE_INVALID", "Extension 来源记录项无效")
        result[extension_id] = {"source": record["source"], "digest": record["digest"]}
    return result


def _sources_json(sources: Mapping[str, Mapping[str, str]]) -> str:
    return json.dumps(
        {
            "schemaVersion": 1,
            "sources": {key: dict(value) for key, value in sorted(sources.items())},
        },
        ensure_ascii=False,
        indent=2,
    ) + "\n"


def _inside_state(root: Path, path: Path) -> Path:
    state = state_root(root)
    _safe_path(root, state)
    if state.is_symlink() or not state.is_dir():
        raise _command("EXTENSION_MISSING", f"缺少本地状态目录：{state}")
    path = Path(path)
    if not path.is_absolute():
        path = root / path
    try:
        path = path.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise _command("EXTENSION_MISSING", f"Extension 配置不可解析：{path}") from exc
    try:
        path.relative_to(state)
    except ValueError as exc:
        raise _command("EXTENSION_MISSING", f"Extension 配置必须位于 .workspace：{path}") from exc
    _safe_path(root, path)
    return path


@contextmanager
def extension_lock(root: Path, mode: int, *, create_cache: bool):
    """Hold a verified local Extension lock without following path links."""
    root_fd = state_fd = cache_fd = lock_fd = None
    try:
        root = _root(root)
        root_fd = os.open(root, _DIRECTORY_FLAGS)
        state_fd = os.open(".workspace", _DIRECTORY_FLAGS, dir_fd=root_fd)
        try:
            extensions_fd = os.open("extensions", _DIRECTORY_FLAGS, dir_fd=state_fd)
            state_fd_inner = os.open(".state", _DIRECTORY_FLAGS, dir_fd=extensions_fd)
            os.close(extensions_fd)
            extensions_fd = None
            cache_fd = os.open("cache", _DIRECTORY_FLAGS, dir_fd=state_fd_inner)
            os.close(state_fd)
            state_fd = state_fd_inner
        except FileNotFoundError:
            if not create_cache:
                raise _command("EXTENSION_BUSY", "缺少 Extension cache 锁目录")
            try:
                for name in ("extensions",):
                    try:
                        os.mkdir(name, mode=0o700, dir_fd=state_fd)
                    except FileExistsError:
                        pass
                extensions_fd = os.open("extensions", _DIRECTORY_FLAGS, dir_fd=state_fd)
                try:
                    try:
                        os.mkdir(".state", mode=0o700, dir_fd=extensions_fd)
                    except FileExistsError:
                        pass
                    state_fd_inner = os.open(".state", _DIRECTORY_FLAGS, dir_fd=extensions_fd)
                    try:
                        os.mkdir("cache", mode=0o700, dir_fd=state_fd_inner)
                    except FileExistsError:
                        pass
                finally:
                    os.close(extensions_fd)
                os.close(state_fd)
                state_fd = state_fd_inner
            except FileExistsError:
                pass
            cache_fd = os.open("cache", _DIRECTORY_FLAGS, dir_fd=state_fd)
        flags = _LOCK_FLAGS if create_cache else _LOCK_FLAGS & ~os.O_CREAT
        lock_fd = os.open(_APPLY_LOCK_NAME, flags, 0o600, dir_fd=cache_fd)
        if not stat.S_ISREG(os.fstat(lock_fd).st_mode):
            raise _command("EXTENSION_BUSY", "Extension 锁必须是普通文件")
        try:
            fcntl.flock(lock_fd, mode | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in {errno.EACCES, errno.EAGAIN}:
                raise _command("EXTENSION_BUSY", "已有 Extension 状态变更或 Provider 正在执行") from exc
            raise _command("EXTENSION_BUSY", f"无法获取 Extension 锁：{exc}") from exc
        yield
    except ExtensionCommandError:
        raise
    except OSError as exc:
        raise _command("EXTENSION_BUSY", f"无法安全打开 Extension apply 锁：{exc}") from exc
    finally:
        if lock_fd is not None:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(lock_fd)
        for descriptor in (cache_fd, state_fd, root_fd):
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass


@contextmanager
def _apply_lock(root: Path):
    """Hold one non-blocking exclusive lock for the complete apply transaction."""
    with extension_lock(root, fcntl.LOCK_EX, create_cache=True):
        yield


def _active_dependencies(manifests: Sequence[ExtensionManifest]) -> None:
    capabilities = {
        provider.capability for manifest in manifests for provider in manifest.provides
    }
    for manifest in manifests:
        missing = sorted(set(manifest.requires) - capabilities)
        if missing:
            raise _command(
                "PROVIDER_INCOMPATIBLE",
                f"激活 Extension 缺少所需 capability：{manifest.id}/{missing[0]}",
            )


def _load_config_schema(root: Path, manifest: ExtensionManifest) -> dict[str, object]:
    assert manifest.config_schema is not None
    parts = Path(manifest.config_schema).parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise _command("EXTENSION_CONFIG_INVALID", f"configSchema 路径无效：{manifest.id}")
    root_fd = directory_fd = file_fd = None
    try:
        root_fd = os.open(manifest.root, _DIRECTORY_FLAGS)
        directory_fd = root_fd
        for part in parts[:-1]:
            next_fd = os.open(part, _DIRECTORY_FLAGS, dir_fd=directory_fd)
            if directory_fd != root_fd:
                os.close(directory_fd)
            directory_fd = next_fd
        file_fd = os.open(parts[-1], _FILE_FLAGS, dir_fd=directory_fd)
        if not stat.S_ISREG(os.fstat(file_fd).st_mode):
            raise _command("EXTENSION_CONFIG_INVALID", f"configSchema 必须是普通文件：{manifest.id}")
        chunks = []
        while chunk := os.read(file_fd, 64 * 1024):
            chunks.append(chunk)
        return parse_json_bytes(b"".join(chunks), f"{manifest.id}/{manifest.config_schema}")
    except ExtensionCommandError:
        raise
    except (OSError, WorkspaceError) as exc:
        raise _command(
            "EXTENSION_CONFIG_INVALID",
            f"无法安全读取 configSchema：{manifest.id}",
        ) from exc
    finally:
        for descriptor in (file_fd, directory_fd, root_fd):
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass


def validate_manifest_config(
    root: Path,
    manifest: ExtensionManifest,
    shared_config: Mapping[str, object],
    local_config: Mapping[str, object],
) -> dict[str, object]:
    """Merge and validate one active Extension's non-sensitive configuration."""
    config = {**shared_config, **local_config}
    if manifest.config_schema is None:
        if config:
            raise _command(
                "EXTENSION_CONFIG_INVALID",
                f"Extension 未声明 configSchema：{manifest.id}",
            )
        return config
    try:
        validate(config, _load_config_schema(root, manifest))
    except SchemaValidationError as exc:
        raise _command(
            "EXTENSION_CONFIG_INVALID",
            f"Extension config 校验失败：{manifest.id}：{exc}",
        ) from exc
    return config


def _validate_extension_config(
    root: Path,
    manifests: Sequence[ExtensionManifest],
    extensions: Mapping[str, object],
) -> None:
    try:
        local = load_local_settings(root, required=True)
    except WorkspaceError as exc:
        raise _command("EXTENSION_CONFIG_INVALID", str(exc)) from exc
    active = {manifest.id for manifest in manifests}
    inactive = sorted(set(local.extensions) - active)
    if inactive:
        raise _command(
            "EXTENSION_CONFIG_INVALID",
            f"本地 Extension 配置未激活：{inactive[0]}",
        )
    shared = extensions["config"]
    assert isinstance(shared, Mapping)
    inactive_shared = sorted(set(shared) - active)
    if inactive_shared:
        raise _command(
            "EXTENSION_CONFIG_INVALID",
            f"Extension 配置未激活：{inactive_shared[0]}",
        )
    for manifest in manifests:
        shared_config = shared.get(manifest.id, {})
        local_config = local.extensions.get(manifest.id, {})
        assert isinstance(shared_config, Mapping)
        assert isinstance(local_config, Mapping)
        validate_manifest_config(root, manifest, shared_config, local_config)


def load_desired_state(root: Path, config: Path) -> DesiredState:
    root = _root(root)
    config = _inside_state(root, config)
    raw = _read_regular_json(root, config, code="EXTENSION_MISSING")
    if set(raw) != DESIRED_FIELDS:
        raise _command("EXTENSION_MISSING", "extensions/.state/input.json 字段必须是 extensions、providers、config")
    requested = raw["extensions"]
    if not isinstance(requested, list):
        raise _command("EXTENSION_MISSING", "extensions 必须是数组")
    discovered = discover_extensions(root)
    manifests: list[ExtensionManifest] = []
    seen: set[str] = set()
    for item in requested:
        if not isinstance(item, dict) or set(item) != {"id", "version"}:
            raise _command("EXTENSION_MISSING", "extensions 项必须包含 id、version")
        extension_id = item["id"]
        version = item["version"]
        if (
            not isinstance(extension_id, str)
            or not ID_RE.fullmatch(extension_id)
            or not isinstance(version, str)
            or not SEMVER_RE.fullmatch(version)
            or extension_id in seen
        ):
            raise _command("EXTENSION_MISSING", "extensions 包含无效或重复 id/version")
        seen.add(extension_id)
        manifest = discovered.get(extension_id)
        if manifest is None:
            raise _command("EXTENSION_MISSING", f"未安装 Extension：{extension_id}")
        if manifest.version != version:
            raise _command(
                "EXTENSION_DRIFT",
                f"Extension 版本不匹配：{extension_id}（期望 {version}，实际 {manifest.version}）",
            )
        manifests.append(manifest)
    manifests.sort(key=lambda item: item.id)
    _active_dependencies(manifests)
    raw_config = raw["config"]
    if isinstance(raw_config, dict):
        inactive = sorted(set(raw_config) - {manifest.id for manifest in manifests})
        if inactive:
            raise _command(
                "EXTENSION_CONFIG_INVALID",
                f"Extension 配置未激活：{inactive[0]}",
            )
    try:
        extensions = normalize_workspace_extensions(
            {"providers": raw["providers"], "config": raw["config"]},
            {manifest.id: manifest for manifest in manifests},
        )
    except ExtensionError as exc:
        raise _command("PROVIDER_INCOMPATIBLE", str(exc)) from exc
    _validate_extension_config(root, manifests, extensions)
    return DesiredState(tuple(manifests), extensions)


def _lock_json(lock: Mapping[str, object]) -> str:
    return json.dumps(lock, ensure_ascii=False, indent=2) + "\n"


def _read_lock(root: Path) -> dict[str, object]:
    root = _root(root)
    raw = _read_regular_json(root, lock_file(root), code="EXTENSION_MISSING")
    try:
        return normalize_extensions_lock(raw)
    except ExtensionError as exc:
        raise _command("EXTENSION_DRIFT", str(exc)) from exc


def _lock_adapter_entries(lock: Mapping[str, object]) -> tuple[Mapping[str, object], ...]:
    items = lock["extensions"]
    assert isinstance(items, list)
    return tuple(
        adapter
        for extension in items
        if isinstance(extension, dict)
        for adapter in extension.get("adapters", [])
        if isinstance(adapter, dict)
    )


def _provider_rows(manifest: ExtensionManifest) -> list[dict[str, object]]:
    return [
        {
            "capability": provider.capability,
            "provider": provider.provider,
            "apiVersion": provider.api_version,
        }
        for provider in sorted(
            manifest.provides, key=lambda item: (item.capability, item.provider)
        )
    ]


def _action_rows(manifest: ExtensionManifest) -> list[dict[str, object]]:
    rows = []
    for action in sorted(manifest.actions, key=lambda item: item.action):
        row: dict[str, object] = {
            "id": action.action,
            "apiVersion": action.api_version,
            "skill": action.skill,
        }
        if action.confirmation_title is not None:
            row["confirmation"] = {
                "title": action.confirmation_title,
                "summary": action.confirmation_summary,
        }
        rows.append(row)
    return rows


def _locked_manifests(
    root: Path,
    lock: Mapping[str, object],
    discovered: Mapping[str, ExtensionManifest] | None = None,
    *,
    allow_snapshot_drift: frozenset[str] = frozenset(),
    allow_missing: frozenset[str] = frozenset(),
    skip_snapshot_validation: frozenset[str] = frozenset(),
    check_dependencies: bool = True,
) -> tuple[ExtensionManifest, ...]:
    discovered = discover_extensions(root) if discovered is None else discovered
    items = lock["extensions"]
    assert isinstance(items, list)
    manifests: list[ExtensionManifest] = []
    for item in items:
        assert isinstance(item, dict)
        extension_id = item["id"]
        assert isinstance(extension_id, str)
        manifest = discovered.get(extension_id)
        if manifest is None:
            if extension_id in allow_missing:
                continue
            raise _command("EXTENSION_MISSING", f"已激活 Extension 不存在：{extension_id}")
        if extension_id not in skip_snapshot_validation:
            snapshot_drift = (
                manifest.version != item["version"]
                or item["path"] != f"extensions/{extension_id}"
                or extension_digest(manifest.root) != item["digest"]
                or _provider_rows(manifest) != item["providers"]
            )
            if _action_rows(manifest) != item.get("actions"):
                snapshot_drift = True
            if snapshot_drift and extension_id not in allow_snapshot_drift:
                raise _command("EXTENSION_DRIFT", f"已激活 Extension 已漂移：{extension_id}")
        manifests.append(manifest)
    manifests.sort(key=lambda item: item.id)
    if check_dependencies:
        _active_dependencies(manifests)
    return tuple(manifests)


def _transition_workspace(root: Path) -> Workspace:
    raw = _read_regular_json(root, workspace_file(root), code="EXTENSION_DRIFT")
    try:
        return parse_workspace(raw, root, validate_extension_refs=False)
    except WorkspaceError as exc:
        raise _command("EXTENSION_DRIFT", str(exc)) from exc


def _validate_current_state(
    root: Path, desired: DesiredState
) -> tuple[Workspace, dict[str, object], AdapterPlan]:
    root = _root(root)
    workspace = _transition_workspace(root)
    lock = _read_lock(root)
    locked_ids = {
        item["id"]
        for item in lock["extensions"]
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    desired_ids = {manifest.id for manifest in desired.manifests}
    _locked_manifests(
        root,
        lock,
        allow_snapshot_drift=frozenset(desired_ids),
        allow_missing=frozenset(locked_ids - desired_ids),
        skip_snapshot_validation=frozenset(locked_ids - desired_ids),
        check_dependencies=False,
    )
    if workspace.extensions["providers"] != lock["providers"]:
        raise _command("PROVIDER_CONFLICT", "workspace.json 与 extensions/.state/lock.json 的 Provider 绑定不一致")
    try:
        plan = adapter_plan(
            root,
            active_manifests=desired.manifests,
            locked_adapters=_lock_adapter_entries(lock),
        )
    except AdapterError as exc:
        raise _command("ADAPTER_DRIFT", str(exc)) from exc
    return workspace, lock, plan


def _lock_for_desired(desired: DesiredState, plan: AdapterPlan) -> dict[str, object]:
    by_path = {item["agentPath"]: item for item in plan.lock_entries}
    extensions = []
    for manifest in desired.manifests:
        adapters = []
        for skill in sorted({provider.skill for provider in manifest.provides}):
            path = f".agents/skills/local-{manifest.id}-{skill}"
            adapters.append(by_path[path])
        extensions.append(
            {
                "id": manifest.id,
                "version": manifest.version,
                "path": f"extensions/{manifest.id}",
                "digest": extension_digest(manifest.root),
                "providers": _provider_rows(manifest),
                "actions": _action_rows(manifest),
                "adapters": adapters,
            }
        )
    candidate: dict[str, object] = {
        "lockVersion": LOCK_VERSION,
        "kitApi": KIT_API,
        "extensions": extensions,
        "providers": dict(desired.extensions["providers"]),
    }
    return normalize_extensions_lock(candidate)


def _target_state(root: Path, config: Path) -> TargetState:
    root = _root(root)
    desired = load_desired_state(root, config)
    workspace, lock, plan = _validate_current_state(root, desired)
    raw = workspace.as_dict()
    raw["extensions"] = {
        "providers": dict(desired.extensions["providers"]),
        "config": dict(desired.extensions["config"]),
    }
    target_workspace = parse_workspace(raw, root)
    target_lock = _lock_for_desired(desired, plan)
    return TargetState(
        canonical_json(target_workspace), _lock_json(target_lock), plan, desired
    )


def _preview_payload(root: Path, state: TargetState) -> dict[str, object]:
    paths = {".workspace/workspace.json", str(lock_file(root).relative_to(root))}
    for spec in (*state.adapter_plan.create, *state.adapter_plan.remove):
        paths.update((spec.relative_path, spec.claude_path))
    adapters = {
        "create": [
            {
                "path": spec.relative_path,
                "claudePath": spec.claude_path,
                "extension": spec.extension_id,
                "version": spec.extension_version,
                "skill": spec.skill,
                "sourceDigest": spec.source_digest,
                "installedDigest": spec.installed_digest,
            }
            for spec in state.adapter_plan.create
        ],
        "remove": [
            {
                "path": spec.relative_path,
                "claudePath": spec.claude_path,
                "extension": spec.extension_id,
                "version": spec.extension_version,
                "skill": spec.skill,
                "sourceDigest": spec.source_digest,
                "installedDigest": spec.installed_digest,
            }
            for spec in state.adapter_plan.remove
        ],
    }
    stable = {
        "workspace": state.workspace_content,
        "lock": state.lock_content,
        "adapters": adapters,
    }
    preview_hash = hashlib.sha256(
        json.dumps(stable, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()
    return {
        "paths": sorted(paths),
        "adapters": adapters,
        "lock": json.loads(state.lock_content),
        "previewHash": preview_hash,
    }


def preview_result(root: Path, config: Path) -> dict[str, object]:
    root = _root(root)
    state = _target_state(root, config)
    result = _preview_payload(root, state)
    result["applyCommand"] = shlex.join(
        [
            "python3",
            "scripts/workspace_extension.py",
            "apply",
            "--root",
            str(root),
            "--config",
            str(_inside_state(root, config)),
            "--preview-hash",
            str(result["previewHash"]),
        ]
    )
    return result


def _require_ignored_outputs(root: Path, state: TargetState) -> None:
    adapter_paths = {
        path
        for spec in (*state.adapter_plan.create, *state.adapter_plan.remove)
        for path in (spec.relative_path, spec.claude_path)
    }
    adapter_paths.update(
        str(entry[field])
        for entry in state.adapter_plan.lock_entries
        for field in ("agentPath", "claudePath")
    )
    state_paths = {".workspace/workspace.json", str(lock_file(root).relative_to(root))}
    targets = {".workspace/", *state_paths, *adapter_paths}
    for relative in targets:
        if not ignored_by_root_gitignore(root, relative):
            raise _command("EXTENSION_MISSING", f"本地产物未被根 .gitignore 忽略：{relative}")
    for relative in sorted((*adapter_paths, *state_paths)):
        result = subprocess.run(
            ["git", "-C", str(root), "ls-files", "--error-unmatch", "--", relative],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if result.returncode == 0:
            code = "ADAPTER_TRACKED" if relative in adapter_paths else "WORKSPACE_TRACKED"
            label = "受管 Adapter" if relative in adapter_paths else "本地状态文件"
            raise _command(code, f"Git 正在跟踪{label}：{relative}")
        if result.returncode != 1:
            code = "ADAPTER_TRACKED" if relative in adapter_paths else "WORKSPACE_TRACKED"
            raise _command(code, f"无法检查 Git 跟踪状态：{relative}")


def apply(
    root: Path,
    config: Path,
    expected_hash: str,
    *,
    _adapter_stage_hook: Callable[[], None] | None = None,
) -> int:
    root = _root(root)
    if not isinstance(expected_hash, str) or not DIGEST_RE.fullmatch(f"sha256:{expected_hash}"):
        raise _command("EXTENSION_MISSING", "preview hash 必须是 64 位小写十六进制")
    with _apply_lock(root):
        state = _target_state(root, config)
        preview = _preview_payload(root, state)
        if preview["previewHash"] != expected_hash:
            raise _command(
                "EXTENSION_MISSING",
                f"预览哈希不匹配：期望 {expected_hash}，实际 {preview['previewHash']}",
            )
        _require_ignored_outputs(root, state)

        def commit_state() -> None:
            atomic_write_many(
                (
                    (workspace_file(root), state.workspace_content),
                    (lock_file(root), state.lock_content),
                )
            )

        try:
            apply_adapter_plan(
                root,
                state.adapter_plan,
                commit_state,
                active_manifests=state.desired.manifests,
                _stage_hook=_adapter_stage_hook,
            )
        except AdapterError as exc:
            _, separator, message = str(exc).partition(": ")
            raise _command("ADAPTER_DRIFT", message if separator else str(exc)) from exc
    return 0


def _raw_provider_findings(
    raw: Mapping[str, object], discovered: Mapping[str, ExtensionManifest]
) -> list[ExtensionFinding]:
    known = {
        provider.ref: provider
        for manifest in discovered.values()
        for provider in manifest.provides
    }
    providers = raw.get("providers")
    if not isinstance(providers, dict):
        return [ExtensionFinding("PROVIDER_CONFLICT", "lock providers 必须是对象")]
    findings: list[ExtensionFinding] = []
    for capability, binding in providers.items():
        if capability not in CAPABILITIES or not isinstance(binding, dict):
            findings.append(ExtensionFinding("PROVIDER_CONFLICT", "lock Provider 绑定结构无效"))
            continue
        references = [binding.get("default")]
        repositories = binding.get("repositories")
        if not isinstance(repositories, dict):
            findings.append(ExtensionFinding("PROVIDER_CONFLICT", "仓级 Provider 覆盖必须是对象"))
            continue
        references.extend(repositories.values())
        for ref in references:
            if ref is None:
                continue
            provider = known.get(ref)
            if provider is None:
                findings.append(ExtensionFinding("PROVIDER_MISSING", f"Provider 不存在：{ref}"))
            elif provider.capability != capability:
                findings.append(
                    ExtensionFinding(
                        "PROVIDER_INCOMPATIBLE",
                        f"Provider capability 不兼容：{ref} 不能用于 {capability}",
                    )
                )
    return findings


def _append_once(findings: list[ExtensionFinding], item: ExtensionFinding) -> None:
    if not any(existing.code == item.code and existing.message == item.message for existing in findings):
        findings.append(item)


def extension_findings(root: Path) -> list[ExtensionFinding]:
    """Return read-only health findings for currently activated local extensions."""
    try:
        root = _root(root)
    except ExtensionCommandError as exc:
        return [ExtensionFinding("EXTENSION_MISSING", str(exc))]
    state = state_root(root)
    registry = workspace_file(root)
    if not state.exists() and not state.is_symlink() and not registry.exists() and not registry.is_symlink():
        return []
    findings: list[ExtensionFinding] = []
    try:
        extension_sources(root)
    except ExtensionCommandError as exc:
        code, _, message = str(exc).partition(": ")
        _append_once(findings, ExtensionFinding(code, message or str(exc)))
    try:
        discovered = discover_extensions(root)
    except ExtensionError as exc:
        return [ExtensionFinding("EXTENSION_DRIFT", str(exc))]
    try:
        raw_lock = _read_regular_json(root, lock_file(root), code="EXTENSION_MISSING")
    except ExtensionCommandError as exc:
        return [ExtensionFinding("EXTENSION_MISSING", str(exc))]
    if not LOCK_FIELDS <= set(raw_lock):
        _append_once(findings, ExtensionFinding("EXTENSION_DRIFT", "extensions/.state/lock.json 字段无效"))
    raw_extensions = raw_lock.get("extensions")
    if not isinstance(raw_extensions, list):
        _append_once(findings, ExtensionFinding("EXTENSION_DRIFT", "lock extensions 必须是数组"))
        raw_extensions = []
    for item in raw_extensions:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            _append_once(findings, ExtensionFinding("EXTENSION_DRIFT", "lock Extension 项无效"))
            continue
        extension_id = item["id"]
        manifest = discovered.get(extension_id)
        if manifest is None:
            _append_once(
                findings,
                ExtensionFinding("EXTENSION_MISSING", f"已激活 Extension 不存在：{extension_id}"),
            )
            continue
        try:
            expected = _provider_rows(manifest)
            if (
                item.get("version") != manifest.version
                or item.get("path") != f"extensions/{extension_id}"
                or item.get("digest") != extension_digest(manifest.root)
                or item.get("providers") != expected
                or (
                    item.get("actions") != _action_rows(manifest)
                )
            ):
                _append_once(
                    findings,
                    ExtensionFinding("EXTENSION_DRIFT", f"已激活 Extension 已漂移：{extension_id}"),
                )
        except ExtensionError as exc:
            _append_once(findings, ExtensionFinding("EXTENSION_DRIFT", str(exc)))
    for item in _raw_provider_findings(raw_lock, discovered):
        _append_once(findings, item)
    try:
        lock = normalize_extensions_lock(raw_lock)
    except ExtensionError as exc:
        _append_once(findings, ExtensionFinding("EXTENSION_DRIFT", str(exc)))
        return findings
    try:
        manifests = _locked_manifests(root, lock, discovered)
    except ExtensionCommandError as exc:
        code, _, message = str(exc).partition(": ")
        _append_once(findings, ExtensionFinding(code, message or str(exc)))
        return findings
    except ExtensionError as exc:
        _append_once(findings, ExtensionFinding("EXTENSION_DRIFT", str(exc)))
        return findings
    try:
        workspace = load_workspace(root)
        if workspace.extensions["providers"] != lock["providers"]:
            _append_once(
                findings,
                ExtensionFinding(
                    "PROVIDER_CONFLICT",
                    "workspace.json 与 extensions/.state/lock.json 的 Provider 绑定不一致",
                ),
            )
    except WorkspaceError as exc:
        _append_once(findings, ExtensionFinding("PROVIDER_CONFLICT", str(exc)))
    else:
        try:
            _validate_extension_config(root, manifests, workspace.extensions)
        except ExtensionCommandError as exc:
            code, _, message = str(exc).partition(": ")
            _append_once(findings, ExtensionFinding(code, message or str(exc)))
    try:
        plan = adapter_plan(
            root,
            active_manifests=manifests,
            locked_adapters=_lock_adapter_entries(lock),
        )
        if plan.create or plan.remove:
            _append_once(findings, ExtensionFinding("ADAPTER_DRIFT", "Adapter 与 lock 不一致"))
    except AdapterError as exc:
        _append_once(findings, ExtensionFinding("ADAPTER_DRIFT", str(exc)))
    return findings


def extension_status(root: Path) -> dict[str, object]:
    """Return status-safe Extension summary without executing any Provider."""
    root = _root(root)
    empty = {
        "activeIds": [],
        "providers": {},
        "repositoryOverrides": {},
        "blockedCodes": [],
    }
    lock_path = lock_file(root)
    if not lock_path.exists() and not lock_path.is_symlink():
        return empty
    try:
        raw = _read_regular_json(root, lock_path, code="EXTENSION_MISSING")
    except ExtensionCommandError as exc:
        return {**empty, "blockedCodes": ["EXTENSION_MISSING"]}
    active = sorted(
        item["id"]
        for item in raw.get("extensions", [])
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    )
    providers: dict[str, str] = {}
    overrides: dict[str, dict[str, str]] = {}
    raw_providers = raw.get("providers")
    if isinstance(raw_providers, dict):
        for capability, binding in raw_providers.items():
            if not isinstance(binding, dict):
                continue
            default = binding.get("default")
            if isinstance(default, str):
                providers[capability] = default
            repositories = binding.get("repositories")
            if isinstance(repositories, dict):
                values = {
                    repo: ref
                    for repo, ref in sorted(repositories.items())
                    if isinstance(repo, str) and isinstance(ref, str)
                }
                if values:
                    overrides[capability] = values
    return {
        "activeIds": active,
        "providers": dict(sorted(providers.items())),
        "repositoryOverrides": overrides,
        "blockedCodes": sorted({item.code for item in extension_findings(root)}),
    }


def list_result(root: Path) -> dict[str, object]:
    root = _root(root)
    manifests = discover_extensions(root)
    return {
        "ids": sorted(manifests),
        "extensions": [
            {
                "id": manifest.id,
                "version": manifest.version,
                "capabilities": sorted(
                    {provider.capability for provider in manifest.provides}
                ),
                "actions": sorted(action.action for action in manifest.actions),
            }
            for manifest in sorted(manifests.values(), key=lambda item: item.id)
        ],
    }


def validate_result(path: Path) -> dict[str, object]:
    path = Path(path)
    manifest = load_manifest(path / "workspace-extension.json")
    return {
        "id": manifest.id,
        "version": manifest.version,
        "digest": extension_digest(manifest.root),
        "capabilities": sorted({provider.capability for provider in manifest.provides}),
        "actions": sorted(action.action for action in manifest.actions),
    }


def _install_source(path: Path) -> tuple[ExtensionManifest, str, Path]:
    source = Path(path)
    if source.is_symlink() or not source.is_dir():
        raise _command("EXTENSION_MISSING", f"安装源必须是普通目录：{source}")
    try:
        source = source.resolve()
    except (OSError, RuntimeError) as exc:
        raise _command("EXTENSION_MISSING", f"安装源不可解析：{path}") from exc
    try:
        manifest = load_manifest(source / "workspace-extension.json")
        digest = extension_digest(source)
    except ExtensionError as exc:
        raise _command("EXTENSION_MISSING", str(exc)) from exc
    return manifest, digest, source


def _install_payload(root: Path, source: Path) -> dict[str, object]:
    root = _root(root)
    state = state_root(root)
    if state.is_symlink() or not state.is_dir():
        raise _command("EXTENSION_MISSING", f"缺少本地状态目录：{state}")
    manifest, digest, source = _install_source(source)
    target = extensions_root(root) / manifest.id
    _safe_path(root, target)
    try:
        if source == target.resolve(strict=False):
            raise _command("EXTENSION_MISSING", "安装源不能是当前 Extension 目标目录")
    except (OSError, RuntimeError) as exc:
        raise _command("EXTENSION_MISSING", f"Extension 目标不可解析：{target}") from exc
    target_digest: str | None = None
    if target.exists() or target.is_symlink():
        if target.is_symlink() or not target.is_dir():
            raise _command("EXTENSION_DRIFT", f"Extension 目标不是普通目录：{target}")
        try:
            target_digest = extension_digest(target)
        except ExtensionError as exc:
            raise _command("EXTENSION_DRIFT", str(exc)) from exc
    return {
        "source": str(source),
        "id": manifest.id,
        "version": manifest.version,
        "digest": digest,
        "target": str(target.relative_to(root)),
        "targetDigest": target_digest,
    }


def install_preview_result(root: Path, source: Path) -> dict[str, object]:
    root = _root(root)
    payload = _install_payload(root, source)
    preview_hash = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {
        **payload,
        "previewHash": preview_hash,
        "paths": [payload["target"]],
        "applyCommand": shlex.join(
            [
                "python3",
                "scripts/workspace_extension.py",
                "install",
                "apply",
                "--root",
                str(root),
                "--source",
                str(source),
                "--preview-hash",
                preview_hash,
            ]
        ),
    }


def install_apply(root: Path, source: Path, expected_hash: str) -> int:
    root = _root(root)
    if not isinstance(expected_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_hash):
        raise _command("EXTENSION_MISSING", "preview hash 必须是 64 位小写十六进制")
    with _apply_lock(root):
        preview = install_preview_result(root, source)
        if preview["previewHash"] != expected_hash:
            raise _command("EXTENSION_MISSING", "安装 preview hash 不匹配")
        target = root / str(preview["target"])
        settings = load_local_settings(root, required=True)
        source_records = extension_sources(root)
        source_records[str(preview["id"])] = {
            "source": str(preview["source"]),
            "digest": str(preview["digest"]),
        }
        local_path = state_root(root) / "workspace.local.json"
        _safe_path(root, local_path)
        cache = cache_root(root)
        _safe_path(root, cache)
        if cache.is_symlink() or not cache.is_dir():
            raise _command("EXTENSION_MISSING", f"缺少 Extension cache：{cache}")
        stage = Path(tempfile.mkdtemp(prefix=".extension-install-", dir=cache))
        backup: Path | None = None
        installed = False
        try:
            shutil.copytree(Path(str(preview["source"])), stage / "extension", symlinks=False)
            copied = stage / "extension"
            if extension_digest(copied) != preview["digest"]:
                raise _command("EXTENSION_DRIFT", "安装源在复制期间发生变化")
            if target.exists() or target.is_symlink():
                if target.is_symlink() or not target.is_dir():
                    raise _command("EXTENSION_DRIFT", f"Extension 目标不是普通目录：{target}")
                backup = cache / f".extension-backup-{secrets.token_hex(8)}"
                target.rename(backup)
            copied.rename(target)
            installed = True
            local = settings.as_dict()
            local["extensionSources"] = source_records
            atomic_write_many(((local_path, json.dumps(local, ensure_ascii=False, indent=2) + "\n"),))
            if backup is not None:
                shutil.rmtree(backup)
        except BaseException as exc:
            if installed and target.exists() and target.is_dir() and not target.is_symlink():
                shutil.rmtree(target)
            if backup is not None and backup.exists() and not target.exists():
                backup.rename(target)
            if isinstance(exc, OSError):
                raise _command("EXTENSION_INSTALL_FAILED", f"无法复制安装源：{exc}") from exc
            raise
        finally:
            if stage.exists():
                shutil.rmtree(stage)
    return 0


SCAFFOLD_ACTION_ID = "example-action"

_SCAFFOLD_SKILL_MD = """---
name: example-action
---

# Example Action

这是 `workspace_extension.py scaffold` 生成的占位 Skill，不执行任何操作。

替换本文件为团队真实操作步骤，并在 `workspace-extension.json` 中相应更新
`confirmation.title`/`confirmation.summary`/`effects`（Action 的 effects 必须是
Extension 顶层 effects 的子集）。
"""


def _scaffold_manifest_json(extension_id: str) -> str:
    manifest = {
        "schemaVersion": 1,
        "id": extension_id,
        "version": "0.1.0",
        "kitApi": 1,
        "provides": [],
        "actions": [
            {
                "id": SCAFFOLD_ACTION_ID,
                "apiVersion": 1,
                "skill": SCAFFOLD_ACTION_ID,
                "confirmation": {
                    "title": "示例 Action（scaffold 生成，需替换）",
                    "summary": "占位 Action，不执行任何操作；替换 manifest 与 Skill 内容后再声明真实 effects。",
                },
                "effects": [],
            }
        ],
        "requires": [],
        "effects": [],
    }
    return json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"


def scaffold_result(root: Path, extension_id: str) -> dict[str, object]:
    root = _root(root)
    if not isinstance(extension_id, str) or not ID_RE.fullmatch(extension_id):
        raise _command("EXTENSION_INVALID", f"Extension id 必须是小写 kebab-case：{extension_id}")
    state = state_root(root)
    if state.is_symlink() or not state.is_dir():
        raise _command("EXTENSION_MISSING", f"缺少本地状态目录，先完成初始化：{state}")
    target = extensions_root(root) / extension_id
    _safe_path(root, target)
    if target.exists() or target.is_symlink():
        raise _command("EXTENSION_ALREADY_EXISTS", f"Extension 目标已存在：{target}")
    cache = cache_root(root)
    _safe_path(root, cache)
    if cache.is_symlink() or not cache.is_dir():
        raise _command("EXTENSION_MISSING", f"缺少 Extension cache：{cache}")
    stage = Path(tempfile.mkdtemp(prefix=".extension-scaffold-", dir=cache))
    try:
        extension_dir = stage / extension_id
        skill_dir = extension_dir / "skills" / SCAFFOLD_ACTION_ID
        skill_dir.mkdir(parents=True)
        manifest_path = extension_dir / "workspace-extension.json"
        manifest_path.write_text(_scaffold_manifest_json(extension_id), encoding="utf-8")
        (skill_dir / "SKILL.md").write_text(_SCAFFOLD_SKILL_MD, encoding="utf-8")
        manifest = load_manifest(manifest_path)
        digest = extension_digest(extension_dir)
        extension_dir.rename(target)
    finally:
        if stage.exists():
            shutil.rmtree(stage)
    return {
        "id": extension_id,
        "version": manifest.version,
        "action": SCAFFOLD_ACTION_ID,
        "path": str(target.relative_to(root)),
        "digest": digest,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("list", "doctor"):
        command = commands.add_parser(name)
        command.add_argument("--root", type=Path, default=Path.cwd())
        command.add_argument("--json", action="store_true")
    validate = commands.add_parser("validate-extension")
    validate.add_argument("path", type=Path)
    validate.add_argument("--json", action="store_true")
    preview = commands.add_parser("preview")
    preview.add_argument("--root", type=Path, default=Path.cwd())
    preview.add_argument("--config", type=Path, required=True)
    preview.add_argument("--json", action="store_true")
    apply_parser = commands.add_parser("apply")
    apply_parser.add_argument("--root", type=Path, default=Path.cwd())
    apply_parser.add_argument("--config", type=Path, required=True)
    apply_parser.add_argument("--preview-hash", required=True)
    install = commands.add_parser("install")
    install_commands = install.add_subparsers(dest="install_command", required=True)
    for name in ("preview", "apply"):
        command = install_commands.add_parser(name)
        command.add_argument("--root", type=Path, default=Path.cwd())
        command.add_argument("--source", type=Path, required=True)
        command.add_argument("--json", action="store_true")
        if name == "apply":
            command.add_argument("--preview-hash", required=True)
    capability = commands.add_parser("capability")
    capability_commands = capability.add_subparsers(dest="capability_command", required=True)
    capability_list = capability_commands.add_parser("list")
    capability_list.add_argument("--root", type=Path, default=Path.cwd())
    capability_list.add_argument("--json", action="store_true")
    scaffold = commands.add_parser("scaffold")
    scaffold.add_argument("--root", type=Path, default=Path.cwd())
    scaffold.add_argument("--id", dest="extension_id", required=True)
    scaffold.add_argument("--json", action="store_true")
    return parser


def _print(result: Mapping[str, object], json_output: bool) -> None:
    if json_output:
        print(json.dumps(result, ensure_ascii=False))
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2))


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "list":
            _print(list_result(args.root), args.json)
            return 0
        if args.command == "validate-extension":
            _print(validate_result(args.path), args.json)
            return 0
        if args.command == "preview":
            _print(preview_result(args.root, args.config), args.json)
            return 0
        if args.command == "apply":
            return apply(args.root, args.config, args.preview_hash)
        if args.command == "install":
            if args.install_command == "preview":
                _print(install_preview_result(args.root, args.source), args.json)
                return 0
            return install_apply(args.root, args.source, args.preview_hash)
        if args.command == "doctor":
            findings = extension_findings(args.root)
            _print({"findings": [item.as_dict() for item in findings]}, args.json)
            return 1 if any(item.level == "ERROR" for item in findings) else 0
        if args.command == "scaffold":
            _print(scaffold_result(args.root, args.extension_id), args.json)
            return 0
        assert args.command == "capability" and args.capability_command == "list"
        _print({"capabilities": sorted(CAPABILITIES)}, args.json)
        return 0
    except (OSError, RuntimeError, UnicodeError, WorkspaceError, ExtensionError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
