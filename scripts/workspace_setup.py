#!/usr/bin/env python3
"""Plan, clone, and apply agent-workbench configuration."""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Optional, Sequence


sys.dont_write_bytecode = True


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from workspace_model import (  # noqa: E402
    Workspace,
    WorkspaceError,
    atomic_write_many,
    canonical_json,
    clone_command,
    is_independent_git,
    load_workspace,
    origin_remote,
    parse_workspace,
    render_context,
    render_repository_profile,
    validate_repository_state,
)
from workspace_input import WorkspaceInput, load_workspace_input  # noqa: E402
from workspace_local import (  # noqa: E402
    LocalSettings,
    canonical_local_json,
    load_local_settings,
)
from workspace_paths import (  # noqa: E402
    cache_root,
    context_file,
    extensions_root,
    features_root,
    ignored_by_root_gitignore,
    local_file,
    lock_file,
    profiles_root,
    state_root,
    workspace_file,
)


INITIAL_LOCK = '{"lockVersion":{"major":1,"minor":0},"kitApi":1,"extensions":[],"providers":{}}\n'


def explanation(operation: str) -> dict[str, object]:
    repository_fields = [
        {
            "path": "repositories[].path",
            "label": "仓库目录名",
            "description": "治理仓父目录下的独立 Git 仓目录名。",
            "required": True,
            "default": None,
            "choices": [],
            "example": "backend",
            "source": "用户选择或父目录发现",
        },
        {
            "path": "repositories[].remote",
            "label": "仓库 remote",
            "description": "仓库缺失时用于 clone 的无凭据 HTTPS 或 SSH 地址；已有仓可自动读取 origin。",
            "required": True,
            "default": None,
            "choices": ["已有仓 origin", "HTTPS/SSH 地址"],
            "example": "https://example.test/backend.git",
            "source": "已有仓 Git 配置或用户提供",
        },
        {
            "path": "repositories[].aliases",
            "label": "仓库别名",
            "description": "可选的仓库短名称，供 Agent 查询时使用。",
            "required": False,
            "default": [],
            "choices": [],
            "example": ["api"],
            "source": "用户提供",
        },
        {
            "path": "repositories[].category",
            "label": "仓库类别",
            "description": "用于上下文展示的自由文本标签，不参与执行逻辑。",
            "required": False,
            "default": "repository",
            "choices": ["service", "frontend", "library", "repository"],
            "example": "service",
            "source": "用户提供或默认值",
        },
        {
            "path": "repositories[].description",
            "label": "仓库描述",
            "description": "用于跨仓上下文的简短说明，不参与执行逻辑。",
            "required": False,
            "default": "",
            "choices": [],
            "example": "Backend service",
            "source": "用户提供或后续 onboarding 推导",
        },
    ]
    if operation == "add-repo":
        fields = repository_fields
        inherited = [
            "workspace",
            "context",
            "branchPolicy",
            "extensions",
            "local",
            "version",
        ]
    else:
        fields = [
            {
                "path": "workspace.name",
                "label": "工作区名称",
                "description": "当前治理工作区的人类可读名称。",
                "required": True,
                "default": None,
                "choices": [],
                "example": "Payment Platform",
                "source": "用户提供",
            },
            {
                "path": "local.branchOwner",
                "label": "分支 owner",
                "description": "分支模板中的 owner；不使用 owner 模板时可留空。",
                "required": False,
                "default": None,
                "choices": [None, "从 Git user.name 推导"],
                "example": "alice",
                "source": "用户提供或本机 Git 配置",
            },
            {
                "path": "branchPolicy.workBase",
                "label": "开发基线",
                "description": "feature/fix 分支默认从哪个基线分支创建。",
                "required": False,
                "default": "develop",
                "choices": ["develop", "main", "master", "已有仓默认分支"],
                "example": "develop",
                "source": "用户选择或已有仓检测",
            },
            {
                "path": "branchPolicy.testTarget",
                "label": "测试目标",
                "description": "正式提测合入的目标分支；不提测时选择 null。",
                "required": False,
                "default": "test",
                "choices": ["test", "qa", None],
                "example": "test",
                "source": "用户选择或已有仓检测",
            },
            {
                "path": "branchPolicy.hotfixBase",
                "label": "热修基线",
                "description": "hotfix 分支默认从哪个基线分支创建。",
                "required": False,
                "default": "main",
                "choices": ["main", "master", None],
                "example": "main",
                "source": "用户选择或默认值",
            },
            {
                "path": "branchPolicy.namePattern",
                "label": "分支命名模板",
                "description": "只允许使用 owner、type、slug 三个模板变量。",
                "required": False,
                "default": "{owner}/{type}/{slug}",
                "choices": ["{owner}/{type}/{slug}"],
                "example": "{owner}/{type}/{slug}",
                "source": "默认值或用户提供",
            },
            *repository_fields,
        ]
        inherited = []
    return {
        "schemaVersion": 1,
        "operation": operation,
        "fields": fields,
        "derivedFields": [
            "version",
            "repositories[].instruction",
        ],
        "inheritedFields": inherited,
        "notes": [
            "remote 不允许包含凭据；已有本地仓库优先读取 origin。",
            "未知或不安全字段仍会被拒绝，不会静默修复。",
        ],
    }


def explain(root: Path, operation: str, json_output: bool = False) -> int:
    result = explanation(operation)
    if json_output:
        print(json.dumps(result, ensure_ascii=False))
        return 0
    print("配置参数说明：")
    for field in result["fields"]:
        required = "必填" if field["required"] else "可选"
        print(f"- {field['path']}（{required}）：{field['description']}")
        print(f"  默认：{field['default']!r}；示例：{field['example']!r}")
    print("自动生成：" + ", ".join(result["derivedFields"]))
    if result["inheritedFields"]:
        print("从现有工作区继承：" + ", ".join(result["inheritedFields"]))
    return 0


def discover_sibling_repositories(root: Path) -> list[Path]:
    root = root.resolve()
    business_root = root.parent
    result = []
    for child in sorted(business_root.iterdir(), key=lambda path: path.name):
        if child.is_symlink() or child.resolve() == root:
            continue
        if is_independent_git(child):
            result.append(child.resolve())
    return result


def _load_candidate(
    root: Path,
    config: Path,
    operation: str,
    base: Workspace | None = None,
) -> WorkspaceInput:
    return load_workspace_input(
        root,
        config,
        require_local=operation == "init",
        base=base,
    )


def _registry_file(root: Path) -> Path:
    return workspace_file(root)


def _initialized(root: Path) -> bool:
    registry = _registry_file(root)
    return registry.exists() or registry.is_symlink()


def _existing_workspace(root: Path) -> Workspace:
    registry = _registry_file(root)
    if registry.is_symlink() or not registry.is_file():
        raise WorkspaceError("工作区尚未初始化或 workspace.json 不是普通文件")
    return load_workspace(root)


def _combine(existing: Workspace, additions: Workspace) -> Workspace:
    used = {
        name
        for repository in existing.repositories
        for name in (repository.path, *repository.aliases)
    }
    pending: set[str] = set()
    for repository in additions.repositories:
        identifiers = {repository.path, *repository.aliases}
        conflict = sorted(identifiers & (used | pending))
        if conflict:
            raise WorkspaceError(f"add-repo path/alias 与现有登记冲突：{conflict[0]}")
        pending.update(identifiers)
    raw = existing.as_dict()
    raw["repositories"] = [
        *[repository.as_dict() for repository in existing.repositories],
        *[repository.as_dict() for repository in additions.repositories],
    ]
    return parse_workspace(raw, existing.root)


def operation_workspaces(
    root: Path, operation: str, config: Path
) -> tuple[Workspace | None, Workspace, Workspace]:
    existing, additions, combined, _ = operation_inputs(root, operation, config)
    return existing, additions, combined


def operation_inputs(
    root: Path, operation: str, config: Path
) -> tuple[Workspace | None, Workspace, Workspace, LocalSettings]:
    root = root.resolve()
    if operation == "init":
        candidate = _load_candidate(root, config, operation)
        if _initialized(root):
            raise WorkspaceError("init 拒绝覆盖现有 workspace.json")
        assert candidate.local is not None
        return None, candidate.workspace, candidate.workspace, candidate.local
    existing = _existing_workspace(root)
    candidate = _load_candidate(root, config, operation, existing)
    additions = candidate.workspace
    if additions.identity != existing.identity:
        raise WorkspaceError("add-repo 配置的 workspace 身份必须与现有工作区一致")
    local = load_local_settings(root, required=True)
    if candidate.local is not None and candidate.local != local:
        raise WorkspaceError("add-repo 输入的 local 必须与现有本地配置一致")
    return existing, additions, _combine(existing, additions), local


def validate_all_states(workspace: Workspace) -> None:
    for repository in workspace.repositories:
        validate_repository_state(workspace, repository, allow_missing_remote=False)


def _plan_result(root: Path, operation: str, config: Path) -> dict[str, object]:
    _, additions, combined, local = operation_inputs(root, operation, config)
    validate_all_states(combined)
    registered = {repository.path for repository in combined.repositories}
    candidates = [
        path
        for path in discover_sibling_repositories(root)
        if path.name not in registered
    ]
    commands = []
    for repository in additions.repositories:
        if not validate_repository_state(
            combined, repository, allow_missing_remote=False
        ):
            command = clone_command(combined, repository)
            if command is not None:
                commands.append(command)
    return {
        "schemaVersion": 1,
        "operation": operation,
        "workspace": {
            **combined.identity.as_dict(),
            "branchOwner": local.branch_owner,
            "primaryRole": local.primary_role,
        },
        "registeredRepositories": [
            repository.path for repository in combined.repositories
        ],
        "candidateSiblingRepositories": [str(path) for path in candidates],
        "cloneCommands": commands,
    }


def plan(root: Path, operation: str, config: Path, json_output: bool = False) -> int:
    result = _plan_result(root, operation, config)
    if json_output:
        print(json.dumps(result, ensure_ascii=False))
        return 0
    workspace = result["workspace"]
    assert isinstance(workspace, dict)
    print(f"工作区：{workspace['name']}")
    print(f"分支 owner：{workspace['branchOwner']}")
    print(f"主要角色：{workspace['primaryRole'] or '未配置'}")
    print("候选兄弟 Git 仓：")
    candidates = result["candidateSiblingRepositories"]
    assert isinstance(candidates, list)
    if candidates:
        for path in candidates:
            print(f"- {path}")
    else:
        print("- （无）")
    print("精确 clone 清单：")
    commands = result["cloneCommands"]
    assert isinstance(commands, list)
    if commands:
        for command in commands:
            print(f"- {command}")
    else:
        print("- （无）")
    return 0


def clone(root: Path, operation: str, config: Path) -> int:
    _, additions, combined, _ = operation_inputs(root, operation, config)
    for repository in additions.repositories:
        if validate_repository_state(
            combined, repository, allow_missing_remote=False
        ):
            print(f"已存在且匹配，跳过：{repository.path}")
            continue
        assert repository.remote is not None
        target = combined.business_root / repository.path
        result = subprocess.run(
            ["git", "clone", "--quiet", "--", repository.remote, str(target)],
            cwd=combined.business_root,
            check=False,
        )
        if result.returncode:
            raise WorkspaceError(f"clone 失败并停止：{repository.path}")
        try:
            validate_repository_state(
                combined, repository, allow_missing_remote=False
            )
            print(f"已 clone：{repository.path}")
        except (OSError, WorkspaceError) as exc:
            raise WorkspaceError(str(exc)) from exc
    return 0


def _ensure_write_target(root: Path, path: Path) -> None:
    root = root.resolve()
    try:
        path.resolve(strict=False).relative_to(root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise WorkspaceError(f"写入目标越出治理仓：{path}") from exc
    current = path
    while current != root:
        if current.is_symlink():
            raise WorkspaceError(f"写入目标链路不允许符号链接：{current}")
        current = current.parent


def _preflight_file(root: Path, path: Path) -> None:
    _ensure_write_target(root, path)
    relative = path.relative_to(root)
    current = root
    for part in relative.parts[:-1]:
        current /= part
        if current.is_symlink():
            raise WorkspaceError(f"写入父路径不允许符号链接：{current}")
        if current.exists() and not current.is_dir():
            raise WorkspaceError(f"写入父路径不是目录：{current}")
    if path.is_symlink():
        raise WorkspaceError(f"写入目标不允许符号链接：{path}")
    if path.exists() and not path.is_file():
        raise WorkspaceError(f"写入目标不是普通文件：{path}")


def _preflight_directory(root: Path, path: Path) -> None:
    _ensure_write_target(root, path)
    relative = path.relative_to(root)
    current = root
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise WorkspaceError(f"目录路径不允许符号链接：{current}")
        if current.exists() and not current.is_dir():
            raise WorkspaceError(f"目录路径不是目录：{current}")


def _prepare_outputs(
    root: Path,
    operation: str,
    additions: Workspace,
    combined: Workspace,
    local: LocalSettings | None = None,
) -> list[tuple[Path, str]]:
    outputs = [
        (context_file(root), render_context(combined)),
        (workspace_file(root), canonical_json(combined)),
    ]
    if operation == "init":
        if local is None:
            raise WorkspaceError("init 缺少本地配置")
        template = Path(__file__).resolve().parents[1] / "templates" / "workspace" / "AGENTS.md"
        if template.is_symlink() or not template.is_file():
            raise WorkspaceError(f"缺少工作区 AGENTS 模板：{template}")
        outputs.extend(
            (
                (state_root(root) / "AGENTS.md", template.read_text(encoding="utf-8")),
                (local_file(root), canonical_local_json(local)),
                (lock_file(root), INITIAL_LOCK),
            )
        )
    profiles = (
        combined.repositories if operation == "init" else additions.repositories
    )
    outputs.extend(
        (
            profiles_root(root) / f"{repository.path}.md",
            render_repository_profile(combined, repository),
        )
        for repository in profiles
    )
    for _, content in outputs:
        content.encode("utf-8")
    return outputs


def _unified_diff(old: str, new: str, fromfile: str, tofile: str) -> str:
    return "".join(
        difflib.unified_diff(
            old.splitlines(keepends=True),
            new.splitlines(keepends=True),
            fromfile=fromfile,
            tofile=tofile,
        )
    )


def _preflight_apply(
    root: Path,
    operation: str,
    outputs: list[tuple[Path, str]],
    directories: Sequence[Path],
    existing: Workspace | None,
) -> None:
    for path, _ in outputs:
        _preflight_file(root, path)
    for directory in directories:
        _preflight_directory(root, directory)
    if operation == "init":
        conflict = next(
            (path for path, _ in outputs if path.exists() or path.is_symlink()),
            None,
        )
        if conflict is not None:
            raise WorkspaceError(f"init 拒绝覆盖已有产物：{conflict}")
        return
    assert existing is not None
    generated = [
        (workspace_file(root), canonical_json(existing)),
        (context_file(root), render_context(existing)),
        *(
            (
                profiles_root(root) / f"{repository.path}.md",
                render_repository_profile(existing, repository),
            )
            for repository in existing.repositories
        ),
    ]
    for path, expected in generated:
        _preflight_file(root, path)
        current = path.read_text(encoding="utf-8") if path.is_file() else ""
        expected_values = (expected,) if isinstance(expected, str) else expected
        if current not in expected_values:
            relative = path.relative_to(root).as_posix()
            diff = _unified_diff(
                current,
                expected_values[0],
                f"current/{relative}",
                f"expected/{relative}",
            )
            raise WorkspaceError(
                f"add-repo 拒绝覆盖已漂移的生成文件：{path}\n{diff}"
            )
    for path, content in outputs[2:]:
        if path.exists() and path.read_text(encoding="utf-8") != content:
            raise WorkspaceError(f"拒绝覆盖已有未登记仓 profile：{path}")


def _preview_state(
    root: Path, operation: str, config: Path
) -> tuple[list[tuple[Path, str]], list[dict[str, object]], str]:
    root = root.resolve()
    existing, additions, combined, local = operation_inputs(root, operation, config)
    validate_all_states(combined)
    outputs = _prepare_outputs(root, operation, additions, combined, local)
    directories = (features_root(root), extensions_root(root), cache_root(root))
    _preflight_apply(root, operation, outputs, directories, existing)
    payload = [
        {"path": path.relative_to(root).as_posix(), "content": content}
        for path, content in sorted(
            outputs, key=lambda item: item[0].relative_to(root).as_posix()
        )
    ]
    preview_hash = hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    changes = []
    for path, content in outputs:
        old = path.read_text(encoding="utf-8") if path.exists() else ""
        if old == content:
            continue
        relative = path.relative_to(root).as_posix()
        diff = _unified_diff(
            old,
            content,
            relative if path.exists() else "/dev/null",
            relative,
        )
        diff_lines = diff.splitlines()
        changes.append(
            {
                "path": relative,
                "action": "update" if path.exists() else "create",
                "additions": sum(line.startswith("+") for line in diff_lines[2:]),
                "deletions": sum(line.startswith("-") for line in diff_lines[2:]),
                "diff": diff,
            }
        )
    return outputs, changes, preview_hash


def preview(
    root: Path,
    operation: str,
    config: Path,
    json_output: bool = False,
    include_diff: bool = False,
) -> int:
    _, changes, preview_hash = _preview_state(root, operation, config)
    apply_command = shlex.join(
        [
            "python3",
            "scripts/workspace_setup.py",
            operation,
            "apply",
            "--config",
            str(config),
            "--preview-hash",
            preview_hash,
        ]
    )
    result = {
        "schemaVersion": 1,
        "operation": operation,
        "changes": (
            changes
            if include_diff
            else [
                {key: change[key] for key in ("path", "action", "additions", "deletions")}
                for change in changes
            ]
        ),
        "directories": [
            path.relative_to(root.resolve()).as_posix() + "/"
            for path in (cache_root(root), features_root(root), extensions_root(root))
        ],
        "preservedPaths": [".workspace/docs/features/"],
        "previewHash": preview_hash,
        "applyCommand": apply_command,
    }
    if json_output:
        print(json.dumps(result, ensure_ascii=False))
        return 0
    for change in changes:
        sys.stdout.write(str(change["diff"]))
    print(f"预览哈希：{preview_hash}")
    print(f"应用命令：{apply_command}")
    return 0


def apply(root: Path, operation: str, config: Path, expected_hash: str) -> int:
    root = root.resolve()
    before = _git_status(root) if operation == "init" else None
    if operation == "init" and before:
        raise WorkspaceError("init apply 前治理仓 Git 状态必须为空")
    outputs, _, preview_hash = _preview_state(root, operation, config)
    if preview_hash != expected_hash:
        raise WorkspaceError(
            f"预览哈希不匹配：期望 {expected_hash}，实际 {preview_hash}"
        )
    directories_required = {features_root(root), extensions_root(root), cache_root(root)}
    if operation == "init":
        _require_managed_state_ignored(root, outputs, directories_required)
    created_directories: list[Path] = []
    try:
        directories = {*(directories_required if operation == "init" else ()), *(path.parent for path, _ in outputs)}
        for directory in sorted(directories, key=lambda path: len(path.parts)):
            missing = []
            current = directory
            while current != root and not current.exists():
                missing.append(current)
                current = current.parent
            for path in reversed(missing):
                path.mkdir()
                created_directories.append(path)
        pending = [
            (path, content)
            for path, content in outputs
            if not path.exists() or path.read_text(encoding="utf-8") != content
        ]
        pending.sort(key=lambda item: item[0].name == "workspace.json")
        atomic_write_many(pending)
    except BaseException:
        if operation == "init":
            for path, _ in outputs:
                try:
                    if path.is_symlink() or path.is_file():
                        path.unlink()
                except OSError:
                    pass
        for directory in reversed(created_directories):
            try:
                directory.rmdir()
            except OSError:
                pass
        raise
    if operation == "init":
        _require_managed_state_ignored(root, outputs, directories_required)
        if _git_status(root) != before:
            raise WorkspaceError("init apply 后治理仓 Git 状态发生变化")
    print("初始化完成" if operation == "init" else "新增仓库登记完成")
    return 0


def _git_status(root: Path) -> str:
    if not is_independent_git(root):
        raise WorkspaceError("治理仓必须是独立 Git 仓库")
    result = subprocess.run(
        ["git", "-C", str(root), "status", "--porcelain", "--untracked-files=all"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        env=dict(os.environ, GIT_OPTIONAL_LOCKS="0"),
    )
    if result.returncode:
        raise WorkspaceError("无法读取治理仓 Git 状态")
    return result.stdout


def _require_managed_state_ignored(
    root: Path,
    outputs: Sequence[tuple[Path, str]],
    directories: Sequence[Path],
) -> None:
    paths = {state_root(root), *directories, *(path for path, _ in outputs)}
    for path in paths:
        relative = path.relative_to(root).as_posix()
        if path == state_root(root):
            relative += "/"
        if not ignored_by_root_gitignore(root, relative):
            raise WorkspaceError(".workspace 状态树未被完整 Git ignore")


def _detected_branch_owner(root: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "config", "--get", "user.name"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            env=dict(os.environ, GIT_OPTIONAL_LOCKS="0"),
        )
    except OSError:
        return None
    if result.returncode:
        return None
    value = result.stdout.strip()
    return value or None


def draft(root: Path, operation: str, output: Path, json_output: bool = False) -> int:
    if operation != "init":
        raise WorkspaceError("draft 目前只支持 init")
    try:
        target = output.resolve()
    except (OSError, RuntimeError) as exc:
        raise WorkspaceError(f"输出路径不可解析：{output}：{exc}") from exc
    if target.exists() or target.is_symlink():
        raise WorkspaceError(f"输出文件已存在，不会覆盖：{target}")
    try:
        relative = target.relative_to(root).as_posix()
    except ValueError as exc:
        raise WorkspaceError(f"输出路径必须位于治理仓内：{target}") from exc
    if not ignored_by_root_gitignore(root, relative):
        raise WorkspaceError(f"输出路径未被根 .gitignore 忽略，拒绝写入：{relative}")

    fields: list[dict[str, object]] = [
        {
            "path": "workspace.name",
            "value": None,
            "source": "required",
            "note": "填写当前治理工作区名称。",
        }
    ]
    owner = _detected_branch_owner(root)
    fields.append(
        {
            "path": "local.branchOwner",
            "value": owner,
            "source": "detected" if owner else "required",
            "note": "来自本机 Git user.name，确认后再使用。"
            if owner
            else "未检测到 Git user.name，按需填写或留空。",
        }
    )

    repositories: list[dict[str, object]] = []
    for candidate in discover_sibling_repositories(root):
        remote = origin_remote(candidate)
        repositories.append(
            {
                "path": candidate.name,
                "remote": remote,
                "category": "repository",
            }
        )
        fields.append(
            {
                "path": "repositories[].remote",
                "value": remote,
                "source": "detected" if remote else "required",
                "note": f"{candidate.name}：来自本地 origin。"
                if remote
                else f"{candidate.name}：未检测到 origin，需要填写。",
            }
        )
    if not repositories:
        fields.append(
            {
                "path": "repositories[].remote",
                "value": None,
                "source": "required",
                "note": "父目录未发现兄弟 Git 仓，需要手工填写仓库清单。",
            }
        )

    payload = {
        "workspace": {"name": None},
        "local": {"branchOwner": owner},
        "repositories": repositories,
    }
    content = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    try:
        with open(
            os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600),
            "w",
            encoding="utf-8",
        ) as handle:
            handle.write(content)
    except FileExistsError as exc:
        raise WorkspaceError(f"输出文件已存在，不会覆盖：{target}") from exc

    result = {
        "schemaVersion": 1,
        "operation": operation,
        "output": relative,
        "fields": fields,
        "notes": [
            "draft 只写输入草稿，不读取或写入 .workspace，不执行 clone。",
            "detected 值来自本机探测，需要人工确认；required 字段必须补齐后才能 plan。",
            "补齐后运行 init plan 校验，未知或不安全字段仍会被拒绝。",
        ],
    }
    if json_output:
        print(json.dumps(result, ensure_ascii=False))
        return 0
    print(f"已生成输入草稿：{relative}")
    for field in fields:
        print(f"- {field['path']}（{field['source']}）：{field['note']}")
    print("补齐 required 字段后运行 init plan 校验。")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="治理仓目录（默认脚本所在项目目录）",
    )
    operations = parser.add_subparsers(dest="operation", required=True)
    for operation in ("init", "add-repo"):
        operation_parser = operations.add_parser(
            operation,
            help="初始化工作区" if operation == "init" else "登记新增仓库",
        )
        actions = operation_parser.add_subparsers(dest="action", required=True)
        explain_parser = actions.add_parser(
            "explain", help="显示参数含义、默认值和候选值"
        )
        explain_parser.add_argument("--json", action="store_true")
        if operation == "init":
            draft_parser = actions.add_parser(
                "draft", help="生成输入草稿，只写被忽略的输入文件"
            )
            draft_parser.add_argument("--output", type=Path, required=True)
            draft_parser.add_argument("--json", action="store_true")
        plan_parser = actions.add_parser("plan", help="只读生成候选和 clone 清单")
        plan_parser.add_argument("--config", type=Path, required=True)
        plan_parser.add_argument("--json", action="store_true")
        clone_parser = actions.add_parser("clone", help="执行 clone")
        clone_parser.add_argument("--config", type=Path, required=True)
        preview_parser = actions.add_parser("preview", help="只读预览待写入内容")
        preview_parser.add_argument("--config", type=Path, required=True)
        preview_parser.add_argument("--json", action="store_true")
        preview_parser.add_argument(
            "--diff", action="store_true", help="JSON 模式附加完整 unified diff"
        )
        apply_parser = actions.add_parser("apply", help="写入登记和上下文")
        apply_parser.add_argument("--config", type=Path, required=True)
        apply_parser.add_argument("--preview-hash", required=True)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        root = args.root.resolve()
    except (OSError, RuntimeError) as exc:
        print(f"错误：治理仓目录不可解析：{args.root}：{exc}", file=sys.stderr)
        return 1
    if not root.is_dir():
        print(f"错误：治理仓目录不存在：{root}", file=sys.stderr)
        return 1
    try:
        if args.action == "explain":
            return explain(root, args.operation, args.json)
        if args.action == "draft":
            return draft(root, args.operation, args.output, args.json)
        if args.action == "plan":
            return plan(root, args.operation, args.config, args.json)
        if args.action == "clone":
            return clone(root, args.operation, args.config)
        if args.action == "preview":
            return preview(root, args.operation, args.config, args.json, args.diff)
        return apply(root, args.operation, args.config, args.preview_hash)
    except (OSError, UnicodeError, WorkspaceError, json.JSONDecodeError) as exc:
        if getattr(args, "json", False):
            print(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "error": {
                            "code": "WORKSPACE_SETUP_INVALID",
                            "message": str(exc),
                            "field": None,
                            "hint": "运行 init explain --json 查看参数说明和默认值。",
                        },
                    },
                    ensure_ascii=False,
                ),
                file=sys.stderr,
            )
        else:
            print(f"错误：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
