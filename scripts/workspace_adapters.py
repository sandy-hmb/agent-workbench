#!/usr/bin/env python3
"""Build and transactionally install local Skill adapters for active extensions."""

from __future__ import annotations

import ctypes
import errno
import hashlib
import json
import os
import secrets
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence

from extension_model import (
    DIGEST_RE,
    ID_RE,
    SEMVER_RE,
    AdapterSpec as LockedAdapter,
    ExtensionError,
    ExtensionManifest,
    extension_digest,
    parse_json_bytes,
)
from workspace_model import WorkspaceError
from workspace_paths import adapters_root, cache_root, state_root


MANAGED_BY = "agent-workbench"
MARKER_NAME = ".workspace-adapter.json"
SKILL_NAME = "SKILL.md"
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK


class AdapterError(ExtensionError):
    """Raised when a local adapter is unsafe, unowned, or drifted."""


@dataclass(frozen=True)
class AdapterSpec:
    relative_path: str
    claude_path: str
    extension_id: str
    extension_version: str
    skill: str
    source_digest: str
    installed_digest: str

    def lock_entry(self) -> dict[str, str]:
        return {
            "agentPath": self.relative_path,
            "claudePath": self.claude_path,
            "skill": self.skill,
            "installedDigest": self.installed_digest,
        }


@dataclass(frozen=True)
class AdapterPlan:
    create: tuple[AdapterSpec, ...]
    remove: tuple[AdapterSpec, ...]
    lock_entries: tuple[dict[str, str], ...]
    manifests: tuple[ExtensionManifest, ...] = ()


@dataclass(frozen=True)
class _Directory:
    fd: int
    identity: tuple[int, int]


@dataclass
class _Layout:
    root: _Directory
    agents_parent: _Directory
    agents: _Directory
    claude_parent: _Directory
    claude: _Directory
    state: _Directory
    extensions: _Directory
    extension_state: _Directory
    cache: _Directory


@dataclass
class _MovedAdapter:
    spec: AdapterSpec
    agent_identity: tuple[int, int]
    claude_identity: tuple[int, int]
    agent_moved: bool = False
    claude_moved: bool = False


@dataclass
class _CreatedAdapter:
    spec: AdapterSpec
    directory: _Directory
    claude_created: bool = False


@dataclass(frozen=True)
class _VerifiedAdapter:
    spec: AdapterSpec
    agent_identity: tuple[int, int]
    claude_identity: tuple[int, int]


def _error(message: str) -> AdapterError:
    return AdapterError(f"ADAPTER_DRIFT: {message}")


def _root(root: Path) -> Path:
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise _error(f"治理仓必须是普通目录：{root}")
    try:
        return root.resolve()
    except (OSError, RuntimeError) as exc:
        raise _error(f"治理仓不可解析：{root}") from exc


def _assert_safe_path(root: Path, path: Path, *, allow_leaf_symlink: bool = False) -> None:
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise _error(f"Adapter 路径越出治理仓：{path}") from exc
    current = root
    for index, part in enumerate(relative.parts):
        current /= part
        if current.is_symlink() and not (
            allow_leaf_symlink and index == len(relative.parts) - 1
        ):
            raise _error(f"Adapter 路径包含符号链接：{current}")
    try:
        path.resolve(strict=False).relative_to(root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise _error(f"Adapter 路径不可安全解析：{path}") from exc


def _assert_adapter_roots(root: Path, *, allow_missing_cache: bool) -> tuple[Path, Path, Path]:
    agents = adapters_root(root, "agents")
    claude = adapters_root(root, "claude")
    cache = cache_root(root)
    for path in (agents, claude):
        _assert_safe_path(root, path)
        if not path.is_dir():
            raise _error(f"缺少 Adapter 根目录：{path}")
    state = state_root(root)
    _assert_safe_path(root, state)
    if not state.is_dir():
        raise _error(f"缺少本地状态目录：{state}")
    _assert_safe_path(root, cache)
    if cache.exists() and not cache.is_dir():
        raise _error(f"Adapter cache 不是目录：{cache}")
    if not allow_missing_cache and not cache.is_dir():
        raise _error(f"缺少 Adapter cache：{cache}")
    return agents, claude, cache


def _adapter_name(extension_id: str, skill: str) -> str:
    return f"local-{extension_id}-{skill}"


def _marker(spec: AdapterSpec) -> dict[str, str]:
    return {
        "managedBy": MANAGED_BY,
        "extension": spec.extension_id,
        "version": spec.extension_version,
        "skill": spec.skill,
        "sourceDigest": spec.source_digest,
    }


def _marker_bytes(spec: AdapterSpec) -> bytes:
    return (
        json.dumps(_marker(spec), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def _adapter_digest(skill: bytes, marker: bytes) -> str:
    digest = hashlib.sha256()
    for name, content in ((MARKER_NAME, marker), (SKILL_NAME, skill)):
        encoded = name.encode("utf-8")
        digest.update(b"F")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return f"sha256:{digest.hexdigest()}"


def _skill_bytes(manifest: ExtensionManifest, skill: str) -> bytes:
    path = manifest.root / "skills" / skill / SKILL_NAME
    if path.is_symlink() or not path.is_file():
        raise _error(f"Extension Skill 不安全：{path}")
    try:
        return path.read_bytes()
    except OSError as exc:
        raise _error(f"无法读取 Extension Skill：{path}") from exc


def _spec(manifest: ExtensionManifest, skill: str, source_digest: str) -> AdapterSpec:
    name = _adapter_name(manifest.id, skill)
    relative = f".agents/skills/{name}"
    claude = f".claude/skills/{name}"
    provisional = AdapterSpec(
        relative, claude, manifest.id, manifest.version, skill, source_digest, ""
    )
    installed = _adapter_digest(_skill_bytes(manifest, skill), _marker_bytes(provisional))
    return AdapterSpec(
        relative, claude, manifest.id, manifest.version, skill, source_digest, installed
    )


def _spec_from_marker(
    root: Path,
    raw: Mapping[str, object],
    locked: LockedAdapter,
) -> AdapterSpec:
    fields = {"managedBy", "extension", "version", "skill", "sourceDigest"}
    if set(raw) != fields or raw.get("managedBy") != MANAGED_BY:
        raise _error(f"Adapter 管理标记无效：{locked.agent_path}")
    extension = raw["extension"]
    version = raw["version"]
    skill = raw["skill"]
    source_digest = raw["sourceDigest"]
    if (
        not isinstance(extension, str)
        or not ID_RE.fullmatch(extension)
        or not isinstance(version, str)
        or not SEMVER_RE.fullmatch(version)
        or not isinstance(skill, str)
        or not ID_RE.fullmatch(skill)
        or not isinstance(source_digest, str)
        or not DIGEST_RE.fullmatch(source_digest)
    ):
        raise _error(f"Adapter 管理标记字段无效：{locked.agent_path}")
    name = _adapter_name(extension, skill)
    if (
        locked.agent_path != f".agents/skills/{name}"
        or locked.claude_path != f".claude/skills/{name}"
        or locked.skill != skill
    ):
        raise _error(f"Adapter lock 与管理标记不匹配：{locked.agent_path}")
    return AdapterSpec(
        locked.agent_path,
        locked.claude_path,
        extension,
        version,
        skill,
        source_digest,
        locked.installed_digest,
    )


def _relative_locked_adapter(raw: Mapping[str, object]) -> LockedAdapter:
    try:
        adapter = LockedAdapter.from_dict(dict(raw))
    except ExtensionError as exc:
        raise _error(f"lock Adapter 无效：{exc}") from exc
    agent = Path(adapter.agent_path)
    claude = Path(adapter.claude_path)
    if (
        len(agent.parts) != 3
        or agent.parts[:2] != (".agents", "skills")
        or not agent.name.startswith("local-")
        or claude != Path(".claude") / "skills" / agent.name
    ):
        raise _error(f"lock Adapter 不是受管 local-* 路径：{adapter.agent_path}")
    return adapter


def _read_managed_adapter(root: Path, locked: LockedAdapter) -> AdapterSpec:
    agent = root / locked.agent_path
    claude = root / locked.claude_path
    _assert_safe_path(root, agent)
    _assert_safe_path(root, claude, allow_leaf_symlink=True)
    if agent.is_symlink() or not agent.is_dir():
        raise _error(f"Adapter 目录缺失或不安全：{agent}")
    entries = {path.name for path in agent.iterdir()}
    if entries != {MARKER_NAME, SKILL_NAME}:
        raise _error(f"Adapter 内容不是受管布局：{agent}")
    marker_path = agent / MARKER_NAME
    skill_path = agent / SKILL_NAME
    if (
        marker_path.is_symlink()
        or skill_path.is_symlink()
        or not marker_path.is_file()
        or not skill_path.is_file()
    ):
        raise _error(f"Adapter 文件不安全：{agent}")
    try:
        marker = parse_json_bytes(marker_path.read_bytes(), str(marker_path))
        skill = skill_path.read_bytes()
    except (OSError, WorkspaceError) as exc:
        raise _error(f"无法读取 Adapter：{agent}") from exc
    spec = _spec_from_marker(root, marker, locked)
    try:
        marker_bytes = marker_path.read_bytes()
    except OSError as exc:
        raise _error(f"无法重新读取 Adapter 标记：{agent}") from exc
    if _adapter_digest(skill, marker_bytes) != spec.installed_digest:
        raise _error(f"Adapter 内容摘要失配：{agent}")
    if not claude.is_symlink():
        raise _error(f"Claude Adapter 缺失或不是符号链接：{claude}")
    try:
        target = claude.readlink()
        expected = Path("../../.agents/skills") / Path(spec.relative_path).name
        if target.is_absolute() or target != expected or claude.resolve() != agent.resolve():
            raise _error(f"Claude Adapter 目标失配：{claude}")
    except (OSError, RuntimeError) as exc:
        raise _error(f"Claude Adapter 无法解析：{claude}") from exc
    return spec


def _scan_local_paths(root: Path, agents: Path, claude: Path, known: set[str]) -> None:
    for base, prefix in ((agents, ".agents/skills/"), (claude, ".claude/skills/")):
        try:
            entries = list(base.iterdir())
        except OSError as exc:
            raise _error(f"无法读取 Adapter 根目录：{base}") from exc
        for path in entries:
            if not path.name.startswith("local-"):
                continue
            relative = f"{prefix}{path.name}"
            if relative not in known:
                raise _error(f"发现未登记 local Adapter：{path}")


def adapter_plan(
    root: Path,
    *,
    active_manifests: Sequence[ExtensionManifest],
    locked_adapters: Sequence[Mapping[str, object]],
) -> AdapterPlan:
    """Return a fully preflighted adapter change set without writing."""
    root = _root(root)
    agents, claude, _ = _assert_adapter_roots(root, allow_missing_cache=True)
    desired: dict[str, AdapterSpec] = {}
    seen_extensions: set[str] = set()
    for manifest in sorted(active_manifests, key=lambda item: item.id):
        if manifest.id in seen_extensions:
            raise _error(f"重复激活 Extension：{manifest.id}")
        seen_extensions.add(manifest.id)
        source_digest = extension_digest(manifest.root)
        for skill in sorted({provider.skill for provider in manifest.provides}):
            spec = _spec(manifest, skill, source_digest)
            if spec.relative_path in desired:
                raise _error(f"重复 Adapter 目标：{spec.relative_path}")
            desired[spec.relative_path] = spec

    current: dict[str, AdapterSpec] = {}
    for raw in locked_adapters:
        locked = _relative_locked_adapter(raw)
        if locked.agent_path in current:
            raise _error(f"重复 lock Adapter：{locked.agent_path}")
        current[locked.agent_path] = _read_managed_adapter(root, locked)
    known_agents = set(current)
    known_claude = {spec.claude_path for spec in current.values()}
    _scan_local_paths(root, agents, claude, known_agents | known_claude)

    create: list[AdapterSpec] = []
    remove: list[AdapterSpec] = []
    for path, old in current.items():
        new = desired.get(path)
        if new is None or old.installed_digest != new.installed_digest:
            remove.append(old)
    for path, new in desired.items():
        old = current.get(path)
        if old is None:
            target = root / path
            link = root / new.claude_path
            _assert_safe_path(root, target)
            _assert_safe_path(root, link)
            if target.exists() or target.is_symlink() or link.exists() or link.is_symlink():
                raise _error(f"未登记 Adapter 占用目标：{path}")
            create.append(new)
        elif old.installed_digest != new.installed_digest:
            create.append(new)
    return AdapterPlan(
        tuple(sorted(create, key=lambda item: item.relative_path)),
        tuple(sorted(remove, key=lambda item: item.relative_path)),
        tuple(
            desired[path].lock_entry()
            for path in sorted(desired)
        ),
        tuple(sorted(active_manifests, key=lambda item: item.id)),
    )


def _manifest_by_spec(spec: AdapterSpec, manifests: Sequence[ExtensionManifest]) -> ExtensionManifest:
    for manifest in manifests:
        if manifest.id == spec.extension_id and manifest.version == spec.extension_version:
            return manifest
    raise _error(f"缺少激活 Extension：{spec.extension_id}@{spec.extension_version}")


def _directory_identity(fd: int, label: str) -> tuple[int, int]:
    metadata = os.fstat(fd)
    if not stat.S_ISDIR(metadata.st_mode):
        raise _error(f"Adapter 目录不是普通目录：{label}")
    return metadata.st_dev, metadata.st_ino


def _open_child_directory(parent_fd: int, name: str, label: str) -> _Directory:
    try:
        fd = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_fd)
    except OSError as exc:
        raise _error(f"无法安全打开 Adapter 目录：{label}：{exc}") from exc
    try:
        return _Directory(fd, _directory_identity(fd, label))
    except BaseException:
        os.close(fd)
        raise


def _open_or_create_child(parent_fd: int, name: str, label: str) -> _Directory:
    try:
        return _open_child_directory(parent_fd, name, label)
    except AdapterError as exc:
        if not isinstance(exc.__cause__, FileNotFoundError):
            raise
        try:
            os.mkdir(name, mode=0o700, dir_fd=parent_fd)
        except OSError as create_error:
            raise _error(f"无法创建 Adapter 目录：{label}：{create_error}") from create_error
        return _open_child_directory(parent_fd, name, label)


def _open_layout(root: Path) -> _Layout:
    descriptors: list[int] = []
    try:
        root_fd = os.open(root, _DIRECTORY_FLAGS)
        descriptors.append(root_fd)
        root_directory = _Directory(root_fd, _directory_identity(root_fd, str(root)))
        agents_parent = _open_child_directory(root_fd, ".agents", ".agents")
        descriptors.append(agents_parent.fd)
        agents = _open_child_directory(agents_parent.fd, "skills", ".agents/skills")
        descriptors.append(agents.fd)
        claude_parent = _open_child_directory(root_fd, ".claude", ".claude")
        descriptors.append(claude_parent.fd)
        claude = _open_child_directory(claude_parent.fd, "skills", ".claude/skills")
        descriptors.append(claude.fd)
        state = _open_child_directory(root_fd, ".workspace", ".workspace")
        descriptors.append(state.fd)
        extensions = _open_or_create_child(state.fd, "extensions", ".workspace/extensions")
        descriptors.append(extensions.fd)
        extension_state = _open_or_create_child(
            extensions.fd, ".state", ".workspace/extensions/.state"
        )
        descriptors.append(extension_state.fd)
        cache = _open_or_create_child(
            extension_state.fd, "cache", ".workspace/extensions/.state/cache"
        )
        descriptors.append(cache.fd)
        return _Layout(
            root_directory,
            agents_parent,
            agents,
            claude_parent,
            claude,
            state,
            extensions,
            extension_state,
            cache,
        )
    except BaseException:
        for fd in reversed(descriptors):
            try:
                os.close(fd)
            except OSError:
                pass
        raise


def _close_layout(layout: _Layout) -> None:
    for directory in (
        layout.cache,
        layout.extension_state,
        layout.extensions,
        layout.state,
        layout.claude,
        layout.claude_parent,
        layout.agents,
        layout.agents_parent,
        layout.root,
    ):
        try:
            os.close(directory.fd)
        except OSError:
            pass


def _named_directory_matches(parent_fd: int, name: str, identity: tuple[int, int]) -> bool:
    try:
        metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except OSError:
        return False
    return stat.S_ISDIR(metadata.st_mode) and (metadata.st_dev, metadata.st_ino) == identity


def _assert_layout_live(layout: _Layout) -> None:
    checks = (
        (layout.root.fd, ".agents", layout.agents_parent.identity, ".agents"),
        (layout.agents_parent.fd, "skills", layout.agents.identity, ".agents/skills"),
        (layout.root.fd, ".claude", layout.claude_parent.identity, ".claude"),
        (layout.claude_parent.fd, "skills", layout.claude.identity, ".claude/skills"),
        (layout.root.fd, ".workspace", layout.state.identity, ".workspace"),
        (layout.state.fd, "extensions", layout.extensions.identity, ".workspace/extensions"),
        (layout.extensions.fd, ".state", layout.extension_state.identity, ".workspace/extensions/.state"),
        (layout.extension_state.fd, "cache", layout.cache.identity, ".workspace/extensions/.state/cache"),
    )
    for parent_fd, name, identity, label in checks:
        if not _named_directory_matches(parent_fd, name, identity):
            raise _error(f"Adapter 父目录在预检后变化：{label}")


def _make_private_directory(parent: _Directory, prefix: str) -> tuple[str, _Directory]:
    for _ in range(32):
        name = f"{prefix}-{secrets.token_hex(16)}"
        try:
            os.mkdir(name, mode=0o700, dir_fd=parent.fd)
        except FileExistsError:
            continue
        except OSError as exc:
            raise _error(f"无法创建 Adapter 暂存目录：{exc}") from exc
        directory = _open_child_directory(parent.fd, name, name)
        if not _named_directory_matches(parent.fd, name, directory.identity):
            os.close(directory.fd)
            raise _error(f"Adapter 暂存目录身份不匹配：{name}")
        return name, directory
    raise _error(f"无法分配唯一 Adapter 暂存目录：{prefix}")


def _make_child_directory(parent: _Directory, name: str, label: str) -> _Directory:
    try:
        os.mkdir(name, mode=0o700, dir_fd=parent.fd)
    except OSError as exc:
        raise _error(f"无法创建 Adapter 暂存目录：{label}：{exc}") from exc
    return _open_child_directory(parent.fd, name, label)


def _write_file_at(parent_fd: int, name: str, content: bytes) -> None:
    try:
        fd = os.open(
            name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o644,
            dir_fd=parent_fd,
        )
    except OSError as exc:
        raise _error(f"无法写入 Adapter 暂存文件：{name}：{exc}") from exc
    try:
        offset = 0
        while offset < len(content):
            offset += os.write(fd, content[offset:])
        os.fsync(fd)
    except OSError as exc:
        raise _error(f"无法写入 Adapter 暂存文件：{name}：{exc}") from exc
    finally:
        os.close(fd)


def _read_file_at(parent_fd: int, name: str, label: str) -> bytes:
    try:
        fd = os.open(name, _FILE_FLAGS, dir_fd=parent_fd)
    except OSError as exc:
        raise _error(f"无法安全读取 Adapter 文件：{label}：{exc}") from exc
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise _error(f"Adapter 文件必须是普通文件：{label}")
        chunks = []
        while chunk := os.read(fd, 1024 * 1024):
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


def _entry_exists(parent_fd: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        return True
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise _error(f"无法检查 Adapter 路径：{name}：{exc}") from exc


def _entry_identity(
    parent_fd: int,
    name: str,
    label: str,
    *,
    expect_directory: bool = False,
    expect_symlink: bool = False,
) -> tuple[int, int]:
    try:
        metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except OSError as exc:
        raise _error(f"无法读取 Adapter leaf：{label}：{exc}") from exc
    if expect_directory and not stat.S_ISDIR(metadata.st_mode):
        raise _error(f"Adapter leaf 不是目录：{label}")
    if expect_symlink and not stat.S_ISLNK(metadata.st_mode):
        raise _error(f"Adapter leaf 不是符号链接：{label}")
    return metadata.st_dev, metadata.st_ino


def _require_entry_identity(
    parent_fd: int,
    name: str,
    expected: tuple[int, int],
    label: str,
    *,
    expect_directory: bool = False,
    expect_symlink: bool = False,
) -> None:
    if _entry_identity(
        parent_fd,
        name,
        label,
        expect_directory=expect_directory,
        expect_symlink=expect_symlink,
    ) != expected:
        raise _error(f"Adapter leaf 在预检后发生漂移：{label}")


def _rename_no_replace(src_fd: int, src_name: str, dst_fd: int, dst_name: str, label: str) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin":
        rename = getattr(libc, "renameatx_np", None)
        flag = 0x00000004
    elif sys.platform.startswith("linux"):
        rename = getattr(libc, "renameat2", None)
        flag = 1
    else:
        rename = None
        flag = 0
    if rename is None:
        raise _error("当前平台不支持 Adapter 排他重命名")
    rename.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    rename.restype = ctypes.c_int
    if rename(src_fd, os.fsencode(src_name), dst_fd, os.fsencode(dst_name), flag) == 0:
        return
    error = ctypes.get_errno()
    if error == errno.EEXIST:
        raise _error(f"Adapter 目标已被占用：{label}")
    raise _error(f"Adapter 排他重命名失败：{label}：{os.strerror(error)}")


def _read_managed_adapter_fd(
    root: Path,
    agents: _Directory,
    claude: _Directory,
    locked: LockedAdapter,
) -> _VerifiedAdapter:
    name = Path(locked.agent_path).name
    adapter = _open_child_directory(agents.fd, name, locked.agent_path)
    try:
        if set(os.listdir(adapter.fd)) != {MARKER_NAME, SKILL_NAME}:
            raise _error(f"Adapter 内容不是受管布局：{locked.agent_path}")
        marker_bytes = _read_file_at(adapter.fd, MARKER_NAME, locked.agent_path)
        skill_bytes = _read_file_at(adapter.fd, SKILL_NAME, locked.agent_path)
        try:
            marker = parse_json_bytes(marker_bytes, locked.agent_path)
        except WorkspaceError as exc:
            raise _error(f"无法读取 Adapter 标记：{locked.agent_path}") from exc
        spec = _spec_from_marker(root, marker, locked)
        if _adapter_digest(skill_bytes, marker_bytes) != spec.installed_digest:
            raise _error(f"Adapter 内容摘要失配：{locked.agent_path}")
        agent_identity = adapter.identity
    finally:
        os.close(adapter.fd)
    try:
        metadata = os.stat(name, dir_fd=claude.fd, follow_symlinks=False)
        target = os.readlink(name, dir_fd=claude.fd)
    except OSError as exc:
        raise _error(f"Claude Adapter 缺失或不安全：{locked.claude_path}") from exc
    expected = (Path("../../.agents/skills") / name).as_posix()
    if not stat.S_ISLNK(metadata.st_mode) or target != expected:
        raise _error(f"Claude Adapter 目标失配：{locked.claude_path}")
    return _VerifiedAdapter(
        spec,
        agent_identity,
        (metadata.st_dev, metadata.st_ino),
    )


def _scan_local_paths_fd(
    agents: _Directory,
    claude: _Directory,
    known_agents: set[str],
    known_claude: set[str],
) -> None:
    for directory, known in ((agents, known_agents), (claude, known_claude)):
        try:
            names = os.listdir(directory.fd)
        except OSError as exc:
            raise _error(f"无法读取 Adapter 根目录：{exc}") from exc
        unknown = sorted(name for name in names if name.startswith("local-") and name not in known)
        if unknown:
            raise _error(f"发现未登记 local Adapter：{unknown[0]}")


def _stage_adapter_fd(stage_agents: _Directory, spec: AdapterSpec, manifest: ExtensionManifest) -> None:
    if extension_digest(manifest.root) != spec.source_digest:
        raise _error(f"Extension 内容在预检后变化：{manifest.id}")
    source = _skill_bytes(manifest, spec.skill)
    marker = _marker_bytes(spec)
    if _adapter_digest(source, marker) != spec.installed_digest:
        raise _error(f"Extension Adapter 摘要在预检后变化：{manifest.id}/{spec.skill}")
    name = Path(spec.relative_path).name
    try:
        os.mkdir(name, mode=0o700, dir_fd=stage_agents.fd)
    except OSError as exc:
        raise _error(f"无法创建 Adapter 暂存目录：{name}：{exc}") from exc
    adapter = _open_child_directory(stage_agents.fd, name, name)
    try:
        _write_file_at(adapter.fd, SKILL_NAME, source)
        _write_file_at(adapter.fd, MARKER_NAME, marker)
    finally:
        os.close(adapter.fd)


def _remove_tree_fd(directory_fd: int) -> None:
    for name in os.listdir(directory_fd):
        metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if stat.S_ISDIR(metadata.st_mode):
            child = os.open(name, _DIRECTORY_FLAGS, dir_fd=directory_fd)
            try:
                _remove_tree_fd(child)
            finally:
                os.close(child)
            os.rmdir(name, dir_fd=directory_fd)
        else:
            os.unlink(name, dir_fd=directory_fd)


def _cleanup_owned_directory(parent: _Directory, name: str | None, directory: _Directory | None) -> None:
    if name is None or directory is None:
        return
    if _named_directory_matches(parent.fd, name, directory.identity):
        _remove_tree_fd(directory.fd)
        os.rmdir(name, dir_fd=parent.fd)


def _remove_created_adapter(layout: _Layout, created: _CreatedAdapter) -> None:
    name = Path(created.spec.relative_path).name
    if created.claude_created:
        try:
            target = os.readlink(name, dir_fd=layout.claude.fd)
            expected = (Path("../../.agents/skills") / name).as_posix()
            metadata = os.stat(name, dir_fd=layout.claude.fd, follow_symlinks=False)
            if not stat.S_ISLNK(metadata.st_mode) or target != expected:
                raise _error(f"回滚 Claude Adapter 目标已变化：{created.spec.claude_path}")
            os.unlink(name, dir_fd=layout.claude.fd)
        except OSError as exc:
            raise _error(f"无法回滚 Claude Adapter：{created.spec.claude_path}") from exc
    if _named_directory_matches(layout.agents.fd, name, created.directory.identity):
        _remove_tree_fd(created.directory.fd)
        os.rmdir(name, dir_fd=layout.agents.fd)
    else:
        raise _error(f"回滚 Adapter 目标已变化：{created.spec.relative_path}")


def _restore_entry(source: _Directory, target: _Directory, name: str, label: str) -> None:
    if not _entry_exists(source.fd, name):
        return
    if _entry_exists(target.fd, name):
        raise _error(f"回滚目标仍被占用：{label}")
    _rename_no_replace(source.fd, name, target.fd, name, label)


def apply_adapter_plan(
    root: Path,
    plan: AdapterPlan,
    commit_state: Callable[[], None],
    *,
    active_manifests: Sequence[ExtensionManifest] = (),
    _stage_hook: Callable[[], None] | None = None,
) -> None:
    """Apply adapters and state callback as one rollback-capable transaction."""
    root = _root(root)
    manifests = tuple(active_manifests) or plan.manifests
    old = {spec.relative_path: spec for spec in plan.remove}
    new = {spec.relative_path: spec for spec in plan.create}
    if len(old) != len(plan.remove) or len(new) != len(plan.create):
        raise _error("Adapter 计划包含重复路径")
    if any(spec.relative_path != new_path for new_path, spec in new.items()):
        raise _error("Adapter 计划路径无效")
    layout = _open_layout(root)
    stage_name = backup_name = None
    stage = backup = stage_agents = backup_agents = backup_claude = None
    moved: list[_MovedAdapter] = []
    created: list[_CreatedAdapter] = []
    try:
        _assert_layout_live(layout)
        stage_name, stage = _make_private_directory(layout.cache, "adapter-stage")
        backup_name, backup = _make_private_directory(layout.cache, "adapter-backup")
        stage_agents = _make_child_directory(stage, "agents", "stage/agents")
        backup_agents = _make_child_directory(backup, "agents", "backup/agents")
        backup_claude = _make_child_directory(backup, "claude", "backup/claude")
        for spec in new.values():
            _assert_layout_live(layout)
            _stage_adapter_fd(stage_agents, spec, _manifest_by_spec(spec, manifests))
        _assert_layout_live(layout)
        known_agents = {
            Path(spec.relative_path).name
            for spec in plan.remove
        } | {
            Path(str(entry["agentPath"])).name
            for entry in plan.lock_entries
        }
        known_claude = {
            Path(spec.claude_path).name
            for spec in plan.remove
        } | {
            Path(str(entry["claudePath"])).name
            for entry in plan.lock_entries
        }
        _scan_local_paths_fd(layout.agents, layout.claude, known_agents, known_claude)
        verified_old: dict[str, _VerifiedAdapter] = {}
        for spec in old.values():
            locked = LockedAdapter(
                spec.relative_path, spec.claude_path, spec.skill, spec.installed_digest
            )
            actual = _read_managed_adapter_fd(root, layout.agents, layout.claude, locked)
            if actual.spec != spec:
                raise _error(f"Adapter 在预检后发生漂移：{spec.relative_path}")
            verified_old[spec.relative_path] = actual
        for path, spec in new.items():
            if path not in old:
                name = Path(spec.relative_path).name
                if _entry_exists(layout.agents.fd, name) or _entry_exists(layout.claude.fd, name):
                    raise _error(f"Adapter 在预检后占用目标：{path}")
        if _stage_hook is not None:
            _stage_hook()
        _assert_layout_live(layout)
        for spec in old.values():
            assert backup_agents is not None and backup_claude is not None
            name = Path(spec.relative_path).name
            verified = verified_old[spec.relative_path]
            record = _MovedAdapter(
                spec,
                verified.agent_identity,
                verified.claude_identity,
            )
            moved.append(record)
            _assert_layout_live(layout)
            _require_entry_identity(
                layout.agents.fd,
                name,
                record.agent_identity,
                spec.relative_path,
                expect_directory=True,
            )
            _rename_no_replace(
                layout.agents.fd, name, backup_agents.fd, name, spec.relative_path
            )
            record.agent_moved = True
            _assert_layout_live(layout)
            _require_entry_identity(
                layout.claude.fd,
                name,
                record.claude_identity,
                spec.claude_path,
                expect_symlink=True,
            )
            _rename_no_replace(
                layout.claude.fd, name, backup_claude.fd, name, spec.claude_path
            )
            record.claude_moved = True
        for spec in new.values():
            assert stage_agents is not None
            name = Path(spec.relative_path).name
            _assert_layout_live(layout)
            _rename_no_replace(
                stage_agents.fd, name, layout.agents.fd, name, spec.relative_path
            )
            directory = _open_child_directory(layout.agents.fd, name, spec.relative_path)
            record = _CreatedAdapter(spec, directory)
            created.append(record)
            _assert_layout_live(layout)
            os.symlink((Path("../../.agents/skills") / name).as_posix(), name, dir_fd=layout.claude.fd)
            record.claude_created = True
        _assert_layout_live(layout)
        commit_state()
    except BaseException:
        rollback_error: BaseException | None = None
        for record in reversed(created):
            try:
                _remove_created_adapter(layout, record)
            except BaseException as exc:
                rollback_error = rollback_error or exc
        for record in reversed(moved):
            try:
                assert backup_agents is not None and backup_claude is not None
                name = Path(record.spec.relative_path).name
                if record.claude_moved:
                    _restore_entry(backup_claude, layout.claude, name, record.spec.claude_path)
                if record.agent_moved:
                    _restore_entry(backup_agents, layout.agents, name, record.spec.relative_path)
            except BaseException as exc:
                rollback_error = rollback_error or exc
        if rollback_error is not None:
            raise AdapterError("ADAPTER_DRIFT: Adapter 事务失败且回滚失败") from rollback_error
        raise
    finally:
        for record in created:
            try:
                os.close(record.directory.fd)
            except OSError:
                pass
        for directory in (stage_agents, backup_agents, backup_claude):
            if directory is not None:
                try:
                    os.close(directory.fd)
                except OSError:
                    pass
        try:
            _cleanup_owned_directory(layout.cache, stage_name, stage)
            _cleanup_owned_directory(layout.cache, backup_name, backup)
        except OSError:
            pass
        for directory in (stage, backup):
            if directory is not None:
                try:
                    os.close(directory.fd)
                except OSError:
                    pass
        _close_layout(layout)
