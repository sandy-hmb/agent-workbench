#!/usr/bin/env python3
"""Shared schema, validation, and rendering for workspace.json version 1."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import stat
import string
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Mapping, Sequence
import re


VERSION = 1
VERSION_VALUE = {"major": VERSION, "minor": 0}
JSON_MAX_NESTING = 256
BRANCH_TYPES = frozenset({"feature", "fix", "hotfix", "refactor", "docs", "chore"})
BRANCH_FIELDS = frozenset({"workBase", "testTarget", "hotfixBase", "namePattern"})
BRANCH_TOKENS = frozenset({"owner", "type", "slug"})
DEFAULT_BRANCH_POLICY = {
    "workBase": "develop",
    "testTarget": "test",
    "hotfixBase": "main",
    "namePattern": "{owner}/{type}/{slug}",
}
TOP_LEVEL_FIELDS = frozenset(
    {"version", "workspace", "context", "branchPolicy", "extensions", "repositories"}
)
WORKSPACE_FIELDS = frozenset({"name"})
EXTENSION_FIELDS = frozenset({"providers", "config"})
TERM_ROUTER_FIELDS = frozenset({"products", "capabilities", "actions", "repositories"})
TERM_ROUTER_TARGETS = {
    "products": "repository",
    "capabilities": "capability",
    "actions": "action",
    "repositories": "repository",
}
CATEGORY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
REPOSITORY_FIELDS = frozenset(
    {
        "path",
        "aliases",
        "remote",
        "category",
        "description",
        "instruction",
        "branchPolicy",
        "sourceInstruction",
        "stack",
        "validation",
    }
)
REQUIRED_REPOSITORY_FIELDS = frozenset(
    {"path", "aliases", "remote", "category", "description", "instruction"}
)
SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
SCP_REMOTE_PATTERN = r"[A-Za-z0-9._-]+@[A-Za-z0-9.-]+:[^\s]+"
REMOTE_PORT_PATTERN = (
    r"(?:[1-9][0-9]{0,3}|[1-5][0-9]{4}|6[0-4][0-9]{3}|65[0-4][0-9]{2}"
    r"|655[0-2][0-9]|6553[0-5])"
)
REMOTE_PATTERN = (
    rf"^(?!.*[\r\n])(?:{SCP_REMOTE_PATTERN}"
    rf"|ssh://(?:[A-Za-z0-9._-]+@)?[A-Za-z0-9.-]+(?::{REMOTE_PORT_PATTERN})?/[^\s?#]+"
    rf"|https://[A-Za-z0-9.-]+(?::{REMOTE_PORT_PATTERN})?/[^\s?#]+)$"
)
REMOTE_RE = re.compile(REMOTE_PATTERN)
BRANCH_NAME_PATTERN = (
    r"^(?!.*[\r\n])(?!-)(?!/)(?!.*//)(?!.*\.\.)(?!.*(?:^|/)\.)"
    r"(?!.*(?:^|/)[^/]*\.lock(?:/|$))(?!.*\/$)(?!.*\.$)"
    r"(?!.*[~^:?*\[\\])(?!.*@\{)[^\s\x00-\x1f\x7f]+$"
)
BRANCH_NAME_RE = re.compile(BRANCH_NAME_PATTERN)
SENSITIVE_FIELD_PARTS = (
    "accesskey",
    "apikey",
    "cookie",
    "password",
    "privatekey",
    "secret",
    "token",
)


class WorkspaceError(ValueError):
    """Raised when workspace configuration or repository state is invalid."""


@dataclass(frozen=True)
class BranchPolicy:
    work_base: str
    test_target: str | None
    hotfix_base: str | None
    name_pattern: str

    def as_dict(self) -> dict[str, str | None]:
        return {
            "workBase": self.work_base,
            "testTarget": self.test_target,
            "hotfixBase": self.hotfix_base,
            "namePattern": self.name_pattern,
        }


@dataclass(frozen=True)
class WorkspaceIdentity:
    name: str

    def as_dict(self) -> dict[str, str]:
        return {"name": self.name}


@dataclass(frozen=True)
class Repository:
    path: str
    aliases: tuple[str, ...]
    remote: str | None
    category: str
    description: str
    instruction: str
    branch_policy: Mapping[str, object] = field(default_factory=dict)
    source_instruction: str | None = None
    stack: tuple[str, ...] = ()
    validation: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "path": self.path,
            "aliases": list(self.aliases),
            "remote": self.remote,
            "category": self.category,
            "description": self.description,
            "instruction": self.instruction,
        }
        if self.branch_policy:
            result["branchPolicy"] = dict(self.branch_policy)
        if self.source_instruction is not None:
            result["sourceInstruction"] = self.source_instruction
        if self.stack:
            result["stack"] = list(self.stack)
        if self.validation:
            result["validation"] = list(self.validation)
        return result


@dataclass(frozen=True)
class Workspace:
    root: Path
    identity: WorkspaceIdentity
    context: Mapping[str, object]
    branch_policy: BranchPolicy
    extensions: Mapping[str, object]
    repositories: tuple[Repository, ...]

    @property
    def business_root(self) -> Path:
        return self.root.resolve().parent

    def as_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "version": dict(VERSION_VALUE),
            "workspace": self.identity.as_dict(),
            "context": dict(self.context),
            "branchPolicy": self.branch_policy.as_dict(),
            "extensions": dict(self.extensions),
            "repositories": [repository.as_dict() for repository in self.repositories],
        }
        return result


def _json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise WorkspaceError(f"JSON 字段重复：{key}")
        result[key] = value
    return result


def _reject_excessive_json_nesting(value: str, label: str) -> None:
    """Apply a stable nesting limit before the interpreter JSON decoder recurses."""
    depth = 0
    in_string = False
    escaped = False
    for character in value:
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character in "[{":
            depth += 1
            if depth > JSON_MAX_NESTING:
                raise WorkspaceError(
                    f"无法读取有效 JSON：{label}：JSON 嵌套超过 {JSON_MAX_NESTING} 层"
                )
        elif character in "]}":
            depth -= 1


def parse_json_bytes(data: bytes, label: str) -> dict[str, object]:
    try:
        text = data.decode("utf-8")
        _reject_excessive_json_nesting(text, label)
        value = json.loads(
            text,
            object_pairs_hook=_json_object,
            parse_constant=lambda value: (_ for _ in ()).throw(
                WorkspaceError(f"JSON 不允许常量：{value}")
            ),
        )
    except WorkspaceError:
        raise
    except (RecursionError, UnicodeError, json.JSONDecodeError) as exc:
        raise WorkspaceError(f"无法读取有效 JSON：{label}：{exc}") from exc
    if not isinstance(value, dict):
        raise WorkspaceError("配置顶层必须是对象")
    return value


def read_json(path: Path) -> dict[str, object]:
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise WorkspaceError(f"无法读取有效 JSON：{path}：{exc}") from exc
    return parse_json_bytes(data, str(path))


def workspace_schema_version(root: Path) -> int:
    from workspace_paths import state_root, workspace_file

    state = state_root(root)
    config = workspace_file(root)
    if state.is_symlink() or not state.is_dir() or config.is_symlink() or not config.is_file():
        raise WorkspaceError(".workspace/workspace.json 必须是普通文件且不能是符号链接")
    version = read_json(config).get("version")
    if type(version) is int and version >= 1:
        return version
    if (
        isinstance(version, dict)
        and type(version.get("major")) is int
        and version["major"] >= 1
        and type(version.get("minor")) is int
        and version["minor"] >= 0
    ):
        return version["major"]
    raise WorkspaceError("workspace.json version 必须是正整数或 {major, minor}")


def reject_sensitive_fields(value: object, label: str) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = re.sub(r"[^a-z0-9]", "", str(key).casefold())
            field = f"{label}.{key}"
            if any(part in normalized for part in SENSITIVE_FIELD_PARTS):
                raise WorkspaceError(f"{field} 是不允许的敏感字段")
            reject_sensitive_fields(child, field)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            reject_sensitive_fields(child, f"{label}[{index}]")


def _safe_name(value: object) -> bool:
    return isinstance(value, str) and bool(SAFE_NAME_RE.fullmatch(value))


def _safe_relative(value: object) -> bool:
    if not isinstance(value, str) or not value or "\\" in value or "\0" in value:
        return False
    path = PurePosixPath(value)
    return not path.is_absolute() and all(part not in {"", ".", ".."} for part in path.parts)


def valid_remote(value: object) -> bool:
    if value is None:
        return True
    return isinstance(value, str) and bool(REMOTE_RE.fullmatch(value))


def _git_branch_valid(value: str) -> bool:
    if not BRANCH_NAME_RE.fullmatch(value):
        return False
    try:
        result = subprocess.run(
            ["git", "check-ref-format", "--branch", value],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except OSError as exc:
        raise WorkspaceError(f"无法执行 git check-ref-format：{exc}") from exc
    return result.returncode == 0


def _workspace_identity(raw: object) -> WorkspaceIdentity:
    if not isinstance(raw, dict) or set(raw) != WORKSPACE_FIELDS:
        raise WorkspaceError("workspace 必须且只能包含 name")
    name = raw["name"]
    if not isinstance(name, str) or not name.strip():
        raise WorkspaceError("workspace.name 必须是非空字符串")
    return WorkspaceIdentity(name)


def _validate_pattern(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise WorkspaceError(f"{label}.namePattern 必须是非空字符串")
    try:
        parsed = list(string.Formatter().parse(value))
    except ValueError as exc:
        raise WorkspaceError(f"{label}.namePattern 无效：{exc}") from exc
    for _, field_name, format_spec, conversion in parsed:
        if field_name is None:
            continue
        if field_name not in BRANCH_TOKENS or format_spec or conversion:
            raise WorkspaceError(
                f"{label}.namePattern 仅允许 owner、type、slug 模板变量"
            )
    try:
        probe = value.format(owner="owner", type="feature", slug="slug")
    except (KeyError, ValueError) as exc:
        raise WorkspaceError(f"{label}.namePattern 无效：{exc}") from exc
    if not _git_branch_valid(probe):
        raise WorkspaceError(f"{label}.namePattern 探测结果不是有效 Git 分支名")
    return value


def _branch_override(raw: object, label: str) -> dict[str, object]:
    if raw is None:
        return {}
    if not isinstance(raw, dict) or not set(raw) <= BRANCH_FIELDS:
        raise WorkspaceError(f"{label} 字段仅允许：{', '.join(sorted(BRANCH_FIELDS))}")
    result = dict(raw)
    for field_name in ("workBase",):
        if field_name in result:
            value = result[field_name]
            if not isinstance(value, str) or not value or not _git_branch_valid(value):
                raise WorkspaceError(f"{label}.{field_name} 不是有效分支名")
    if "testTarget" in result:
        value = result["testTarget"]
        if value is not None and (
            not isinstance(value, str) or not value or not _git_branch_valid(value)
        ):
            raise WorkspaceError(f"{label}.testTarget 不是有效分支名或 null")
    if "hotfixBase" in result:
        value = result["hotfixBase"]
        if value is not None and (
            not isinstance(value, str) or not value or not _git_branch_valid(value)
        ):
            raise WorkspaceError(f"{label}.hotfixBase 不是有效分支名或 null")
    if "namePattern" in result:
        result["namePattern"] = _validate_pattern(result["namePattern"], label)
    return result


def branch_policy(raw: object = None, label: str = "branchPolicy") -> BranchPolicy:
    values = dict(DEFAULT_BRANCH_POLICY)
    values.update(_branch_override(raw, label))
    return BranchPolicy(
        str(values["workBase"]),
        values["testTarget"] if values["testTarget"] is None else str(values["testTarget"]),
        values["hotfixBase"] if values["hotfixBase"] is None else str(values["hotfixBase"]),
        str(values["namePattern"]),
    )


def effective_branch_policy(workspace: Workspace, repository: Repository) -> BranchPolicy:
    values = workspace.branch_policy.as_dict()
    values.update(repository.branch_policy)
    return branch_policy(values, f"repositories[{repository.path}].branchPolicy")


def render_branch_name(
    policy: BranchPolicy, *, owner: str, branch_type: str, slug: str
) -> str:
    if branch_type not in BRANCH_TYPES:
        raise WorkspaceError(f"不支持的分支类型：{branch_type}")
    try:
        rendered = policy.name_pattern.format(owner=owner, type=branch_type, slug=slug)
    except (KeyError, ValueError) as exc:
        raise WorkspaceError(f"无法渲染分支名：{exc}") from exc
    if not _git_branch_valid(rendered):
        raise WorkspaceError(f"渲染结果不是有效 Git 分支名：{rendered}")
    return rendered


def _string_list(value: object, label: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise WorkspaceError(f"{label} 必须是非空字符串数组")
    return tuple(value)


def _repository(raw: object, index: int, root: Path) -> Repository:
    label = f"repositories[{index}]"
    if not isinstance(raw, dict):
        raise WorkspaceError(f"{label} 必须是对象")
    missing = REQUIRED_REPOSITORY_FIELDS - set(raw)
    if missing:
        detail = []
        if missing:
            detail.append("缺少 " + ", ".join(sorted(missing)))
        raise WorkspaceError(f"{label} 字段无效：{'；'.join(detail)}")
    if not _safe_name(raw["path"]):
        raise WorkspaceError(f"{label}.path 必须是安全的一级目录名")
    candidate = root.resolve().parent / str(raw["path"])
    if candidate == root.resolve():
        raise WorkspaceError(f"{label}.path 不能指向治理仓自身")
    if candidate.is_symlink():
        raise WorkspaceError(f"{label}.path 不允许符号链接：{raw['path']}")
    aliases = raw["aliases"]
    if not isinstance(aliases, list) or any(not _safe_name(alias) for alias in aliases):
        raise WorkspaceError(f"{label}.aliases 必须是安全的一级名称数组")
    if not valid_remote(raw["remote"]):
        raise WorkspaceError(f"{label}.remote 仅支持无凭据的 SSH、HTTPS 或 null")
    for field_name in ("category", "description"):
        if not isinstance(raw[field_name], str):
            raise WorkspaceError(f"{label}.{field_name} 必须是字符串")
    if not raw["category"]:
        raise WorkspaceError(f"{label}.category 不能为空")
    expected_instruction = f"docs/repositories/{raw['path']}.md"
    if raw["instruction"] != expected_instruction:
        raise WorkspaceError(
            f"{label}.instruction 必须精确为 {expected_instruction}"
        )
    source_instruction = raw.get("sourceInstruction")
    if source_instruction is not None and not _safe_relative(source_instruction):
        raise WorkspaceError(f"{label}.sourceInstruction 必须是仓内安全相对路径")
    return Repository(
        path=str(raw["path"]),
        aliases=tuple(str(alias) for alias in aliases),
        remote=raw["remote"] if raw["remote"] is None else str(raw["remote"]),
        category=str(raw["category"]),
        description=str(raw["description"]),
        instruction=str(raw["instruction"]),
        branch_policy=_branch_override(raw.get("branchPolicy"), f"{label}.branchPolicy"),
        source_instruction=(
            None if source_instruction is None else str(source_instruction)
        ),
        stack=_string_list(raw.get("stack"), f"{label}.stack"),
        validation=_string_list(raw.get("validation"), f"{label}.validation"),
    )


def _term_router(context: Mapping[str, object], repositories: Sequence[Repository]) -> dict[str, object]:
    router = context.get("termRouter")
    if router is None:
        return dict(context)
    if not isinstance(router, dict) or set(router) != TERM_ROUTER_FIELDS:
        raise WorkspaceError("context.termRouter 必须且只能包含 products、capabilities、actions、repositories")
    names = {repository.path for repository in repositories}
    normalized: dict[str, object] = {}
    for kind, target in TERM_ROUTER_TARGETS.items():
        entries = router[kind]
        if not isinstance(entries, list):
            raise WorkspaceError(f"context.termRouter.{kind} 必须是数组")
        items: list[dict[str, str]] = []
        for entry in entries:
            if not isinstance(entry, dict) or set(entry) != {"term", target}:
                raise WorkspaceError(f"context.termRouter.{kind} 条目必须包含 term、{target}")
            term = entry["term"]
            value = entry[target]
            if (
                not isinstance(term, str)
                or not term.strip()
                or "\r" in term
                or "\n" in term
                or not isinstance(value, str)
                or not value.strip()
                or "\r" in value
                or "\n" in value
            ):
                raise WorkspaceError(f"context.termRouter.{kind} 条目必须使用非空单行字符串")
            if target == "repository" and value not in names:
                raise WorkspaceError(f"context.termRouter.{kind} 引用未登记仓库：{value}")
            items.append({"term": term, target: value})
        normalized[kind] = items
    return {**context, "termRouter": normalized}


def parse_workspace(
    raw: Mapping[str, object],
    root: Path,
    *,
    validate_extension_refs: bool = True,
) -> Workspace:
    missing = TOP_LEVEL_FIELDS - set(raw)
    if missing:
        detail = []
        if missing:
            detail.append("缺少 " + ", ".join(sorted(missing)))
        raise WorkspaceError("配置顶层字段无效：" + "；".join(detail))
    version = raw["version"]
    if (
        not isinstance(version, dict)
        or type(version.get("major")) is not int
        or version["major"] != VERSION
        or type(version.get("minor")) is not int
        or version["minor"] < 0
    ):
        raise WorkspaceError(f"workspace.json version.major 必须为 {VERSION}")
    context = raw.get("context", {})
    if not isinstance(context, dict):
        raise WorkspaceError("context 必须是 JSON 对象")
    extensions = raw.get("extensions")
    from extension_registry import discover_extensions, normalize_workspace_extensions

    extensions = normalize_workspace_extensions(
        extensions,
        discover_extensions(root) if validate_extension_refs else None,
    )
    repositories_raw = raw["repositories"]
    if not isinstance(repositories_raw, list):
        raise WorkspaceError("repositories 必须是数组")
    repositories = tuple(
        _repository(value, index, root) for index, value in enumerate(repositories_raw)
    )
    names: dict[str, str] = {}
    for repository in repositories:
        identifiers = (repository.path, *repository.aliases)
        local_names: dict[str, str] = {}
        duplicate = None
        for name in identifiers:
            key = name.casefold()
            if key in names or key in local_names:
                duplicate = name
                break
            local_names[key] = name
        if duplicate is not None:
            raise WorkspaceError(f"仓 path/alias 全局重复（不区分大小写）：{duplicate}")
        for name in identifiers:
            names[name.casefold()] = name
    return Workspace(
        root=root.resolve(),
        identity=_workspace_identity(raw["workspace"]),
        context=_term_router(context, repositories),
        branch_policy=branch_policy(raw.get("branchPolicy")),
        extensions={
            "providers": dict(extensions["providers"]),
            "config": dict(extensions["config"]),
        },
        repositories=repositories,
    )


def load_workspace(
    root: Path, config: Path | None = None, *, validate_extension_refs: bool = True
) -> Workspace:
    root = root.resolve()
    if config is None:
        from workspace_paths import state_root, workspace_file

        state = state_root(root)
        if state.is_symlink() or not state.is_dir():
            raise WorkspaceError(f".workspace 必须是非符号链接目录：{state}")
        config = workspace_file(root)
        if config.is_symlink() or not config.is_file():
            raise WorkspaceError(
                f".workspace/workspace.json 必须是普通文件且不能是符号链接：{config}"
            )
    return parse_workspace(
        read_json(config), root, validate_extension_refs=validate_extension_refs
    )


def resolve_repository(repositories: Sequence[Repository], name: str) -> Repository:
    matches = [
        repository
        for repository in repositories
        if name == repository.path or name in repository.aliases
    ]
    if len(matches) != 1:
        raise WorkspaceError(f"workspace.json 未登记仓库或别名：{name}")
    return matches[0]


def repository_path(workspace: Workspace, repository: Repository) -> Path:
    candidate = workspace.business_root / repository.path
    if candidate.is_symlink():
        raise WorkspaceError(f"仓库路径不允许符号链接：{repository.path}")
    try:
        candidate.resolve(strict=False).relative_to(workspace.business_root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise WorkspaceError(f"仓库路径越出业务工作区：{repository.path}") from exc
    return candidate


def clone_command(workspace: Workspace, repository: Repository) -> str | None:
    if repository.remote is None:
        return None
    return shlex.join(
        [
            "git",
            "clone",
            "--quiet",
            "--",
            repository.remote,
            str(repository_path(workspace, repository)),
        ]
    )


def git_toplevel(path: Path) -> Path | None:
    if path.is_symlink() or not path.is_dir():
        return None
    try:
        result = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            env=dict(os.environ, GIT_OPTIONAL_LOCKS="0"),
        )
    except OSError:
        return None
    if result.returncode or not result.stdout.strip():
        return None
    try:
        return Path(result.stdout.strip()).resolve()
    except (OSError, RuntimeError):
        return None


def is_independent_git(path: Path) -> bool:
    top = git_toplevel(path)
    return top is not None and top == path.resolve()


def origin_remote(path: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(path), "config", "--get", "remote.origin.url"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            env=dict(os.environ, GIT_OPTIONAL_LOCKS="0"),
        )
    except OSError as exc:
        raise WorkspaceError(f"无法读取 origin：{path}：{exc}") from exc
    if result.returncode == 1:
        return None
    if result.returncode:
        raise WorkspaceError(f"无法读取 origin：{path}")
    return result.stdout.rstrip("\n")


def validate_repository_state(
    workspace: Workspace, repository: Repository, *, allow_missing_remote: bool = True
) -> bool:
    path = repository_path(workspace, repository)
    if not path.exists():
        if repository.remote is None and not allow_missing_remote:
            raise WorkspaceError(f"仓库缺失且 remote 为 null：{repository.path}")
        return False
    if path.is_symlink() or not path.is_dir():
        raise WorkspaceError(f"已存在路径不是非符号链接目录：{path}")
    if not is_independent_git(path):
        raise WorkspaceError(f"已存在目录不是独立 Git 仓库：{path}")
    actual = origin_remote(path)
    if actual != repository.remote:
        raise WorkspaceError(f"仓库 {repository.path} 的 remote 不匹配")
    return True


def canonical_json(workspace: Workspace) -> str:
    return json.dumps(workspace.as_dict(), ensure_ascii=False, indent=2) + "\n"


def render_context(workspace: Workspace) -> str:
    description = workspace.context.get("description")
    lines = [f"# {workspace.identity.name} 业务上下文", ""]
    if isinstance(description, str) and description:
        lines.extend((description, ""))
    lines.extend(("## 仓库", ""))
    if workspace.repositories:
        lines.extend(
            f"- `{repository.path}`（{repository.category}）：{repository.description}"
            for repository in workspace.repositories
        )
    else:
        lines.append("暂无登记仓库。")
    extra_context = {
        key: value
        for key, value in workspace.context.items()
        if key != "description"
    }
    if extra_context:
        lines.extend(
            (
                "",
                "## 结构化事实",
                "",
                "```json",
                json.dumps(extra_context, ensure_ascii=False, indent=2, sort_keys=True),
                "```",
            )
        )
    return "\n".join(lines).rstrip() + "\n"


def _render_repository_profile(
    workspace: Workspace, repository: Repository, template_name: str
) -> str:
    policy = effective_branch_policy(workspace, repository)
    template_path = Path(__file__).resolve().parents[1] / "templates" / template_name
    try:
        template = template_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise WorkspaceError(f"无法读取仓 profile 模板：{template_path}：{exc}") from exc
    source = repository.source_instruction or "未配置"
    stack = "、".join(f"`{item}`" for item in repository.stack) or "未配置"
    validation = "\n".join(f"- `{item}`" for item in repository.validation) or "- 未配置"
    return template.format(
        path=repository.path,
        category=repository.category,
        description=repository.description,
        instruction=repository.instruction,
        source_instruction=source,
        stack=stack,
        validation=validation,
        work_base=policy.work_base,
        test_target=policy.test_target if policy.test_target is not None else "null",
        hotfix_base=policy.hotfix_base if policy.hotfix_base is not None else "null",
        name_pattern=policy.name_pattern,
    )


def render_repository_profile(workspace: Workspace, repository: Repository) -> str:
    return _render_repository_profile(workspace, repository, "repository.md")


def atomic_write_many(outputs: Sequence[tuple[Path, str | bytes]]) -> None:
    """Replace several files together, restoring every original on failure."""
    items = list(outputs)
    if len({path for path, _ in items}) != len(items):
        raise WorkspaceError("批量写入目标重复")
    for path, _ in items:
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise WorkspaceError(f"批量写入目标不是普通文件：{path}")
        if path.parent.is_symlink() or not path.parent.is_dir():
            raise WorkspaceError(f"批量写入父路径不是普通目录：{path.parent}")

    staged: list[tuple[Path, Path]] = []
    backups: list[tuple[Path, Path, bool]] = []
    replaced: set[Path] = set()
    try:
        # Stage every new file before touching any destination.
        for path, content in items:
            mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o644
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{path.name}.tmp-", dir=path.parent
            )
            temporary = Path(temporary_name)
            staged.append((temporary, path))
            try:
                os.fchmod(descriptor, mode)
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(content.encode("utf-8") if isinstance(content, str) else content)
                    handle.flush()
                    os.fsync(handle.fileno())
            except BaseException:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
                raise

        # Keep independent copies so rollback remains possible after replace.
        for _, path in staged:
            if not path.exists():
                backups.append((path, Path(), False))
                continue
            descriptor, backup_name = tempfile.mkstemp(
                prefix=f".{path.name}.backup-", dir=path.parent
            )
            os.close(descriptor)
            backup = Path(backup_name)
            try:
                shutil.copy2(path, backup)
                os.chmod(backup, stat.S_IMODE(path.stat().st_mode))
                with backup.open("rb") as handle:
                    os.fsync(handle.fileno())
            except BaseException as copy_error:
                try:
                    backup.unlink()
                except OSError as cleanup_error:
                    raise WorkspaceError("未完成 backup 清理失败") from cleanup_error
                raise copy_error
            backups.append((path, backup, True))

        for temporary, path in staged:
            os.replace(temporary, path)
            replaced.add(path)
    except BaseException as original_error:
        rollback_error: BaseException | None = None
        for path, backup, existed in reversed(backups):
            if path not in replaced:
                continue
            try:
                if existed:
                    try:
                        os.replace(backup, path)
                    except BaseException:
                        # A mocked or transient replace failure must not prevent restoration.
                        os.rename(backup, path)
                elif path.exists() or path.is_symlink():
                    path.unlink()
            except BaseException as error:
                rollback_error = rollback_error or error
        if rollback_error is not None:
            raise WorkspaceError("批量写入失败且回滚失败") from rollback_error
        raise original_error
    finally:
        for temporary, _ in staged:
            if temporary.exists():
                temporary.unlink()
        for _, backup, existed in backups:
            if existed and backup.exists():
                backup.unlink()
