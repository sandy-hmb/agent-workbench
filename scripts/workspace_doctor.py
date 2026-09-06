#!/usr/bin/env python3
"""Read-only health checks for an agent-workbench instance."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional, Sequence
from urllib.parse import unquote


sys.dont_write_bytecode = True


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from workspace_model import (  # noqa: E402
    VERSION,
    Repository,
    Workspace,
    WorkspaceError,
    clone_command,
    is_independent_git,
    load_workspace,
    render_context,
    render_repository_profile,
    repository_path,
    resolve_repository,
    validate_repository_state,
    workspace_schema_version,
)
from feature_context import feature_summary  # noqa: E402
from workspace_local import load_local_settings  # noqa: E402
from workspace_paths import (  # noqa: E402
    cache_root,
    context_file,
    extensions_root,
    features_root,
    ignored_by_root_gitignore,
    local_file,
    lock_file,
    migration_marker_file,
    profiles_root,
    state_root,
    workflow_file,
    workflow_runs_root,
    workspace_file,
)


AGENTS_MAX_BYTES = 8 * 1024
INLINE_LINK_RE = re.compile(r"!?\[[^]\n]*\]\(")
REFERENCE_LINK_RE = re.compile(r"^\s{0,3}\[([^]]+)\]:\s*(.*)$")
REFERENCE_USE_RE = re.compile(r"!?\[([^]\n]+)\]\[([^]\n]*)\]")
SKILL_NAME_RE = re.compile(r"^name:\s*([a-z0-9][a-z0-9-]*)\s*$", re.MULTILINE)
REQUIRED_SKILLS = frozenset(
    {
        "workspace-init",
        "workspace-repo-onboarding",
        "workspace-cross-repo-analysis",
        "workspace-feature-design",
        "workspace-api-contract",
        "workspace-feature-workflow",
        "workspace-verify",
        "workspace-sync-base",
        "workspace-submit-test",
        "workspace-extension",
        "workspace-update",
    }
)


@dataclass(frozen=True)
class Finding:
    level: str
    code: str
    message: str


def finding(level: str, code: str, message: str) -> Finding:
    return Finding(level, code, message)


@dataclass(frozen=True)
class Remediation:
    kind: str  # "command" | "manual"
    detail: str

    def as_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "detail": self.detail}


REMEDIATIONS: dict[str, Remediation] = {
    "WORKSPACE_TRACKED": Remediation("command", "git rm -r --cached .workspace"),
    "WORKSPACE_NOT_IGNORED": Remediation(
        "manual",
        "检查根 .gitignore 是否包含 /.workspace/、/.agents/skills/local-*、"
        "/.claude/skills/local-* 三条规则，缺失时手工补全。",
    ),
    "AGENTS_TOO_LARGE": Remediation(
        "manual",
        "把 AGENTS.md 中的非核心内容迁移到 docs/ 下的其他文档，保持在 8KB 以内。",
    ),
    "AGENTS_INVALID": Remediation(
        "manual", "确认 AGENTS.md 是普通文件、未被替换为符号链接，且可用 UTF-8 正常读取。"
    ),
    "AGENTS_READ_FAILED": Remediation(
        "manual", "确认 AGENTS.md 是普通文件、未被替换为符号链接，且可用 UTF-8 正常读取。"
    ),
    "CLIENT_ADAPTER_INVALID": Remediation(
        "manual",
        "确认 CLAUDE.md 内容为 @AGENTS.md、GEMINI.md 内容为 @./AGENTS.md，且都不是"
        "符号链接；这两个文件由公共 Kit 维护，不要手工新增其他内容。",
    ),
    "SKILL_MISSING": Remediation(
        "manual",
        "确认公共 Kit 是否被完整拉取（.agents/skills 下对应目录是否被稀疏 checkout 或"
        "误删），必要时重新从公共源同步该目录。",
    ),
    "SKILL_INVALID": Remediation(
        "manual",
        "核对该 Skill 的 SKILL.md frontmatter name 是否与目录名一致，内容是否被手工"
        "改动。",
    ),
    "CLIENT_SKILL_ADAPTER_INVALID": Remediation(
        "command",
        "cd .claude/skills && for n in $(ls ../../.agents/skills | grep -v ^local-); "
        "do ln -sfn ../../.agents/skills/$n $n; done",
    ),
    "SKILL_UNEXPECTED": Remediation(
        "manual",
        "确认该目录是否应改为以 local- 前缀声明的本地 Extension Skill，或是否为误留"
        "的公共目录，人工判断后处理。",
    ),
    "CLIENT_SKILL_ADAPTER_UNEXPECTED": Remediation(
        "manual",
        "确认该目录是否应改为以 local- 前缀声明的本地 Extension Skill，或是否为误留"
        "的公共目录，人工判断后处理。",
    ),
    "EXTENSION_DRIFT": Remediation(
        "command",
        "python3 scripts/workspace_extension.py preview --root . "
        "--config .workspace/extensions/.state/input.json --json，核对后执行返回的 applyCommand",
    ),
    "MARKDOWN_LINK_BROKEN": Remediation(
        "manual", "修正 message 中标出的相对链接目标路径，或补齐缺失的目标文件。"
    ),
    "FEATURE_METADATA_INVALID": Remediation(
        "manual",
        "对照 templates/feature/README.md 核对该需求 README 的状态字段格式（状态、"
        "需求短名、工作分支、基线分支、最后更新）。",
    ),
    "ROOT_RESIDUAL_DIRECTORY": Remediation(
        "manual",
        "确认是否为历史方案残留后自行删除；Kit 不自动删除任何未跟踪的本地目录。",
    ),
}


def _safe_path_chain(root: Path, path: Path) -> bool:
    try:
        relative = path.relative_to(root)
        current = root
        for part in relative.parts:
            current /= part
            if current.is_symlink():
                return False
        current.resolve(strict=False).relative_to(root)
        return True
    except (OSError, RuntimeError, ValueError):
        return False


def _safe_directory(root: Path, path: Path, *, required: bool) -> bool:
    if not _safe_path_chain(root, path):
        return False
    if not path.exists():
        return not required
    return path.is_dir()


def _check_generated_file(
    path: Path, expected: str | Sequence[str], code: str, findings: list[Finding]
) -> None:
    if path.is_symlink() or not path.is_file():
        findings.append(finding("ERROR", code, f"缺少或不安全：{path}"))
        return
    try:
        expected_values = (expected,) if isinstance(expected, str) else expected
        if path.read_text(encoding="utf-8") not in expected_values:
            findings.append(finding("ERROR", code, f"生成内容已失配：{path}"))
    except (OSError, UnicodeError) as exc:
        findings.append(finding("ERROR", code, f"无法读取 {path}：{exc}"))


def _check_repository(
    workspace: Workspace,
    repository: Repository,
    findings: list[Finding],
    *,
    target: bool,
) -> None:
    try:
        path = repository_path(workspace, repository)
    except WorkspaceError as exc:
        findings.append(finding("ERROR", "REPO_PATH_UNSAFE", str(exc)))
        return
    root_instruction = state_root(workspace.root) / repository.instruction
    if (
        not _safe_path_chain(workspace.root, root_instruction)
        or not root_instruction.is_file()
    ):
        code = "INSTRUCTION_UNSAFE" if root_instruction.is_symlink() else "INSTRUCTION_MISSING"
        findings.append(
            finding(
                "ERROR",
                code,
                f"{repository.path} 的治理根 instruction 无效：{repository.instruction}",
            )
        )
    if not path.exists():
        command = clone_command(workspace, repository)
        if target:
            detail = f"；可执行：{command}" if command else "；无 clone remote"
            findings.append(
                finding(
                    "ERROR",
                    "TARGET_REPO_MISSING",
                    f"目标仓库尚未拉取：{repository.path}{detail}",
                )
            )
        else:
            detail = "；无 clone remote" if repository.remote is None else ""
            findings.append(
                finding(
                    "INFO",
                    "REPO_NOT_INSTALLED",
                    f"可选仓库尚未拉取：{repository.path}{detail}",
                )
            )
        return
    try:
        validate_repository_state(workspace, repository, allow_missing_remote=False)
    except WorkspaceError as exc:
        findings.append(finding("ERROR", "REPO_INVALID", str(exc)))
        return
    if repository.source_instruction is not None:
        source = path / repository.source_instruction
        if not _safe_path_chain(path, source) or not source.is_file():
            code = "INSTRUCTION_UNSAFE" if source.is_symlink() else "INSTRUCTION_MISSING"
            findings.append(
                finding(
                    "ERROR",
                    code,
                    f"{repository.path} 的 sourceInstruction 无效：{repository.source_instruction}",
                )
            )


def _check_generated_workspace(workspace: Workspace, findings: list[Finding]) -> None:
    _check_generated_file(
        context_file(workspace.root),
        render_context(workspace),
        "CONTEXT_INVALID",
        findings,
    )
    docs = state_root(workspace.root) / "docs"
    features = features_root(workspace.root)
    repositories = profiles_root(workspace.root)
    directories = (
        (docs, "DOCS_DIRECTORY_INVALID", True),
        (features, "FEATURES_DIRECTORY_INVALID", True),
        (
            repositories,
            "REPOSITORIES_DIRECTORY_INVALID",
            bool(workspace.repositories),
        ),
    )
    directories_safe = True
    for path, code, required in directories:
        if not _safe_directory(workspace.root, path, required=required):
            findings.append(finding("ERROR", code, f"缺少或不安全：{path}"))
            directories_safe = False
    if not directories_safe:
        return
    for repository in workspace.repositories:
        _check_generated_file(
            profiles_root(workspace.root) / f"{repository.path}.md",
            render_repository_profile(workspace, repository),
            "REPOSITORY_PROFILE_INVALID",
            findings,
        )


def _check_feature_metadata(workspace: Workspace, findings: list[Finding]) -> None:
    features = features_root(workspace.root)
    if not _safe_directory(workspace.root, features, required=True):
        return
    try:
        children = sorted(features.iterdir())
    except OSError as exc:
        findings.append(
            finding("ERROR", "FEATURE_METADATA_INVALID", f"{features}：{exc}")
        )
        return
    for feature in children:
        try:
            if feature.is_symlink():
                raise ValueError("需求目录不允许符号链接")
            if not feature.is_dir():
                continue
            if not _safe_path_chain(features, feature):
                raise ValueError("需求目录越出治理根")
            readme = feature / "README.md"
            if readme.is_symlink() or not readme.is_file():
                raise ValueError("README.md 必须是普通文件且不能是符号链接")
            feature_summary(workspace, feature)
        except (OSError, RuntimeError, UnicodeError, ValueError) as exc:
            findings.append(
                finding("ERROR", "FEATURE_METADATA_INVALID", f"{feature}：{exc}")
            )


RESIDUAL_ROOT_DIRECTORY_NAMES = frozenset({"artifacts", "design", "plans", "requirements", "testing"})


def _check_residual_root_directories(root: Path, findings: list[Finding]) -> None:
    residual = []
    for name in sorted(RESIDUAL_ROOT_DIRECTORY_NAMES):
        path = root / name
        if not path.is_dir() or path.is_symlink():
            continue
        if not any(item.is_file() for item in path.rglob("*")):
            residual.append(name)
    if residual:
        findings.append(
            finding(
                "WARN",
                "ROOT_RESIDUAL_DIRECTORY",
                f"根目录存在空的历史残留目录：{'、'.join(residual)}",
            )
        )


def _check_local_state_artifacts(root: Path, findings: list[Finding]) -> None:
    files = (state_root(root) / "AGENTS.md", lock_file(root))
    directories = (extensions_root(root),)
    for path in (*files, *directories):
        if not _safe_path_chain(root, path):
            findings.append(
                finding("ERROR", "WORKSPACE_PATH_UNSAFE", f"生成路径不安全：{path}")
            )
            continue
        expected = path.is_file() if path in files else path.is_dir()
        if not expected:
            findings.append(
                finding("ERROR", "WORKSPACE_ARTIFACT_MISSING", f"缺少初始化产物：{path}")
            )
    cache = cache_root(root)
    if (cache.exists() or cache.is_symlink()) and (
        not _safe_path_chain(root, cache) or not cache.is_dir()
    ):
        findings.append(
            finding("ERROR", "WORKSPACE_PATH_UNSAFE", f"生成路径不安全：{cache}")
        )
    marker = migration_marker_file(root)
    if (marker.exists() or marker.is_symlink()) and (
        not _safe_path_chain(root, marker) or marker.is_symlink() or not marker.is_file()
    ):
        findings.append(
            finding("ERROR", "WORKSPACE_PATH_UNSAFE", f"迁移标记路径不安全：{marker}")
        )


def _check_workflow_artifacts(root: Path, findings: list[Finding]) -> None:
    overlay = workflow_file(root)
    runs = workflow_runs_root(root)
    for path, expect_file in ((overlay, True), (runs, False)):
        if not (path.exists() or path.is_symlink()):
            continue
        if not _safe_path_chain(root, path):
            findings.append(finding("ERROR", "WORKFLOW_INVALID", f"Workflow 路径不安全：{path}"))
            continue
        expected = path.is_file() if expect_file else path.is_dir()
        if path.is_symlink() or not expected:
            findings.append(finding("ERROR", "WORKFLOW_INVALID", f"Workflow 产物无效：{path}"))
    if not overlay.is_file() or overlay.is_symlink():
        return
    try:
        from workspace_workflow import status_result

        status = status_result(root)
        blocked = status.get("blockedCodes")
        if isinstance(blocked, list):
            for code in blocked:
                if isinstance(code, str):
                    findings.append(finding("ERROR", code, "Workflow 状态不可用"))
    except (OSError, RuntimeError, UnicodeError, WorkspaceError) as exc:
        findings.append(finding("ERROR", "WORKFLOW_INVALID", str(exc)))


def _check_clients_and_skills(root: Path, findings: list[Finding]) -> None:
    agents = root / "AGENTS.md"
    try:
        if agents.is_symlink() or not agents.is_file():
            findings.append(
                finding("ERROR", "AGENTS_INVALID", "AGENTS.md 必须是普通文件且不能是符号链接")
            )
        elif agents.stat().st_size >= AGENTS_MAX_BYTES:
            findings.append(
                finding(
                    "ERROR",
                    "AGENTS_TOO_LARGE",
                    f"AGENTS.md 必须小于 {AGENTS_MAX_BYTES} 字节：{agents.stat().st_size}",
                )
            )
    except OSError as exc:
        findings.append(finding("ERROR", "AGENTS_READ_FAILED", str(exc)))

    expected_clients = {"CLAUDE.md": "@AGENTS.md\n", "GEMINI.md": "@./AGENTS.md\n"}
    for relative, expected in expected_clients.items():
        path = root / relative
        try:
            if path.is_symlink() or not path.is_file() or path.read_text(encoding="utf-8") != expected:
                findings.append(
                    finding("ERROR", "CLIENT_ADAPTER_INVALID", f"客户端适配无效：{relative}")
                )
        except (OSError, UnicodeError) as exc:
            findings.append(finding("ERROR", "CLIENT_ADAPTER_INVALID", str(exc)))

    skills_parent = root / ".agents"
    skills_root = skills_parent / "skills"
    if (
        skills_parent.is_symlink()
        or skills_root.is_symlink()
        or (skills_parent.exists() and not skills_parent.is_dir())
        or (skills_root.exists() and not skills_root.is_dir())
    ):
        findings.append(finding("ERROR", "SKILLS_INVALID", f"Skill 目录不安全：{skills_root}"))
        skills_available = False
    else:
        skills_available = skills_root.is_dir()
        if not skills_available:
            findings.append(finding("ERROR", "SKILLS_INVALID", f"缺少 Skill 目录：{skills_root}"))

    claude_root = root / ".claude" / "skills"
    claude_parent = root / ".claude"
    if (
        claude_parent.is_symlink()
        or claude_root.is_symlink()
        or (claude_parent.exists() and not claude_parent.is_dir())
        or (claude_root.exists() and not claude_root.is_dir())
    ):
        findings.append(
            finding(
                "ERROR",
                "CLIENT_SKILL_ROOT_INVALID",
                f"Claude Skill 目录不安全：{claude_root}",
            )
        )
        claude_available = False
    else:
        claude_available = claude_root.is_dir()
        if not claude_available:
            findings.append(
                finding(
                    "ERROR",
                    "CLIENT_SKILL_ROOT_INVALID",
                    f"缺少 Claude Skill 目录：{claude_root}",
                )
            )

    if claude_available:
        for name in sorted(
            path.name
            for path in claude_root.iterdir()
            if path.name not in REQUIRED_SKILLS and not path.name.startswith("local-")
        ):
            findings.append(
                finding(
                    "ERROR",
                    "CLIENT_SKILL_ADAPTER_UNEXPECTED",
                    f"存在未声明 Claude Skill 适配：{name}",
                )
            )
        for name in sorted(REQUIRED_SKILLS):
            skill = skills_root / name
            adapter = claude_root / name
            try:
                target = os.readlink(adapter) if adapter.is_symlink() else ""
                expected_target = Path("../../.agents/skills") / name
                valid = (
                    adapter.is_symlink()
                    and adapter.exists()
                    and not Path(target).is_absolute()
                    and Path(target) == expected_target
                    and adapter.resolve() == skill.resolve()
                )
            except (OSError, RuntimeError):
                valid = False
            if not valid:
                findings.append(
                    finding(
                        "ERROR",
                        "CLIENT_SKILL_ADAPTER_INVALID",
                        f"Claude Skill 适配无效：{name}",
                    )
                )

    if not skills_available:
        return
    skills = {
        skill.name: skill
        for skill in skills_root.iterdir()
        if skill.name not in {".", ".."} and not skill.name.startswith("local-")
    }
    for name in sorted(REQUIRED_SKILLS - set(skills)):
        findings.append(finding("ERROR", "SKILL_MISSING", f"缺少 Skill：{name}"))
    for name in sorted(set(skills) - REQUIRED_SKILLS):
        findings.append(finding("ERROR", "SKILL_UNEXPECTED", f"存在未声明 Skill：{name}"))
    for skill in sorted(skills.values(), key=lambda path: path.name):
        source = skill / "SKILL.md"
        if (
            skill.is_symlink()
            or not _safe_path_chain(skills_root, source)
            or not source.is_file()
        ):
            findings.append(finding("ERROR", "SKILL_INVALID", f"Skill 无效：{skill.name}"))
            continue
        try:
            match = SKILL_NAME_RE.search(source.read_text(encoding="utf-8"))
        except (OSError, UnicodeError) as exc:
            findings.append(finding("ERROR", "SKILL_INVALID", f"{skill.name}：{exc}"))
            continue
        if match is None or match.group(1) != skill.name:
            findings.append(
                finding("ERROR", "SKILL_INVALID", f"Skill name 与目录不一致：{skill.name}")
            )


def markdown_files(root: Path) -> Iterable[Path]:
    for path in sorted(root.glob("*.md")):
        if path.is_file() and not path.is_symlink():
            yield path
    for directory in (root / "docs", root / ".agents", state_root(root)):
        if not _safe_directory(root, directory, required=False) or not directory.is_dir():
            continue
        for current, directories, files in os.walk(directory, followlinks=False):
            current_path = Path(current)
            directories[:] = [
                name
                for name in directories
                if _safe_directory(root, current_path / name, required=True)
            ]
            for name in sorted(files):
                path = current_path / name
                if (
                    path.suffix == ".md"
                    and _safe_path_chain(root, path)
                    and path.is_file()
                    and not path.is_symlink()
                ):
                    yield path


def _balanced_destination(line: str, start: int) -> tuple[str | None, int]:
    depth = 1
    escaped = False
    for index in range(start, len(line)):
        character = line[index]
        if escaped:
            escaped = False
        elif character == "\\":
            escaped = True
        elif character == "(":
            depth += 1
        elif character == ")":
            depth -= 1
            if depth == 0:
                return line[start:index], index + 1
    return None, len(line)


def _first_destination(raw: str) -> str:
    raw = raw.lstrip()
    if raw.startswith("<"):
        end = raw.find(">", 1)
        return raw[1:end] if end >= 0 else ""
    depth = 0
    escaped = False
    for index, character in enumerate(raw):
        if escaped:
            escaped = False
        elif character == "\\":
            escaped = True
        elif character == "(":
            depth += 1
        elif character == ")" and depth:
            depth -= 1
        elif character.isspace() and depth == 0:
            return raw[:index]
    return raw


def _normalize_local_target(raw: str) -> str | None:
    target = _first_destination(raw)
    if not target or target.startswith(("#", "//")) or re.match(
        r"^[A-Za-z][A-Za-z0-9+.-]*:", target
    ):
        return None
    target = re.sub(r"\\([\\()<> ])", r"\1", target)
    target = unquote(target.split("#", 1)[0].split("?", 1)[0])
    return target or None


def _without_inline_code(line: str) -> str:
    position = 0
    while True:
        start = line.find("`", position)
        if start < 0:
            return line
        end_of_marker = start
        while end_of_marker < len(line) and line[end_of_marker] == "`":
            end_of_marker += 1
        marker = line[start:end_of_marker]
        end = line.find(marker, end_of_marker)
        if end < 0:
            return line
        end += len(marker)
        line = line[:start] + " " * (end - start) + line[end:]
        position = end


def _reference_label(value: str) -> str:
    return " ".join(value.split()).casefold()


def local_link_targets(path: Path) -> Iterable[str]:
    fenced = False
    lines = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.lstrip().startswith(("```", "~~~")):
            fenced = not fenced
            continue
        if not fenced:
            lines.append(_without_inline_code(line))
    definitions: dict[str, str] = {}
    for line in lines:
        match = REFERENCE_LINK_RE.match(line)
        if match:
            definitions[_reference_label(match.group(1))] = match.group(2)
    for line in lines:
        position = 0
        while match := INLINE_LINK_RE.search(line, position):
            raw, position = _balanced_destination(line, match.end())
            if raw is not None:
                target = _normalize_local_target(raw)
                if target is not None:
                    yield target
        for match in REFERENCE_USE_RE.finditer(line):
            label = match.group(2) or match.group(1)
            raw = definitions.get(_reference_label(label))
            if raw is not None:
                target = _normalize_local_target(raw)
                if target is not None:
                    yield target


def _check_markdown_links(
    root: Path, findings: list[Finding], workspace: Workspace | None = None
) -> None:
    root = root.resolve()
    markdown_roots = (
        root / "docs",
        features_root(root),
        profiles_root(root),
        root / ".agents",
    )
    for path in markdown_roots:
        if (path.exists() or path.is_symlink()) and not _safe_directory(
            root, path, required=True
        ):
            findings.append(
                finding(
                    "ERROR",
                    "MARKDOWN_ROOT_INVALID",
                    f"Markdown 根目录不安全：{path}",
                )
            )
    repositories = (
        {
            repository.path: repository_path(workspace, repository)
            for repository in workspace.repositories
        }
        if workspace is not None
        else {}
    )
    for markdown in markdown_files(root):
        try:
            targets = list(local_link_targets(markdown))
        except (OSError, UnicodeError) as exc:
            findings.append(
                finding("ERROR", "MARKDOWN_READ_FAILED", f"{markdown}：{exc}")
            )
            continue
        for target in targets:
            if "\0" in target:
                findings.append(
                    finding("ERROR", "MARKDOWN_LINK_INVALID", f"{markdown} -> {target!r}")
                )
                continue
            if Path(target).is_absolute():
                findings.append(
                    finding("ERROR", "MARKDOWN_LINK_UNSAFE", f"{markdown} -> {target}")
                )
                continue
            candidate = markdown.parent / target
            try:
                resolved = candidate.resolve(strict=False)
            except (OSError, RuntimeError):
                findings.append(
                    finding("ERROR", "MARKDOWN_LINK_INVALID", f"{markdown} -> {target}")
                )
                continue
            try:
                resolved.relative_to(root)
                inside_root = True
            except ValueError:
                inside_root = False
            if inside_root:
                if not candidate.exists():
                    findings.append(
                        finding("ERROR", "MARKDOWN_LINK_BROKEN", f"{markdown} -> {target}")
                    )
                continue
            matched_repository = next(
                (
                    path
                    for path in repositories.values()
                    if resolved == path or resolved.is_relative_to(path)
                ),
                None,
            )
            if matched_repository is None:
                findings.append(
                    finding("ERROR", "MARKDOWN_LINK_UNSAFE", f"{markdown} -> {target}")
                )
            elif matched_repository.exists() and not candidate.exists():
                findings.append(
                    finding("ERROR", "MARKDOWN_LINK_BROKEN", f"{markdown} -> {target}")
                )


def _unregistered_siblings(workspace: Workspace, findings: list[Finding]) -> None:
    registered = {repository.path for repository in workspace.repositories}
    for child in sorted(workspace.business_root.iterdir(), key=lambda path: path.name):
        if (
            child.is_symlink()
            or child.resolve() == workspace.root
            or child.name in registered
        ):
            continue
        if is_independent_git(child):
            findings.append(
                finding(
                    "INFO",
                    "UNREGISTERED_SIBLING",
                    f"发现未登记兄弟 Git 仓（未纳管）：{child}",
                )
            )


def _check_extensions(root: Path, findings: list[Finding]) -> None:
    try:
        from workspace_extension import extension_findings

        for item in extension_findings(root):
            findings.append(finding(item.level, item.code, item.message))
    except (OSError, RuntimeError, UnicodeError, WorkspaceError) as exc:
        findings.append(finding("ERROR", "EXTENSION_DRIFT", str(exc)))


def audit(
    root: Path, repository: str | None = None, verbose: bool = False
) -> list[Finding]:
    try:
        root = root.resolve()
    except (OSError, RuntimeError) as exc:
        return [
            finding(
                "ERROR",
                "WORKSPACE_ROOT_INVALID",
                f"治理仓目录不可解析：{root}：{exc}",
            )
        ]
    findings: list[Finding] = []
    if not root.is_dir():
        return [finding("ERROR", "WORKSPACE_ROOT_INVALID", f"治理仓目录不存在：{root}")]
    _check_residual_root_directories(root, findings)
    registry = workspace_file(root)
    if not registry.exists() and not registry.is_symlink():
        state = state_root(root)
        state_present = state.exists() or state.is_symlink()
        if state_present and (
            state.is_symlink() or not state.is_dir() or not _safe_path_chain(root, state)
        ):
            findings.append(
                finding("ERROR", "WORKSPACE_PATH_UNSAFE", f"本地状态目录不安全：{state}")
            )
        else:
            findings.append(
                finding(
                    "INFO",
                    "WORKSPACE_UNINITIALIZED",
                    "工作区尚未初始化；先运行 scripts/workspace_setup.py init plan "
                    "--config ./workspace-input.json，再按 clone -> preview --json（读取"
                    "摘要、previewHash 和 applyCommand，需要全文时加 --diff）-> 执行 applyCommand",
                )
            )
        _check_clients_and_skills(root, findings)
        _check_markdown_links(root, findings)
        return findings
    state = state_root(root)
    if state.is_symlink() or not state.is_dir() or not _safe_path_chain(root, state):
        return [finding("ERROR", "WORKSPACE_PATH_UNSAFE", f"本地状态目录不安全：{state}")]
    if registry.is_symlink():
        return [
            finding("ERROR", "WORKSPACE_PATH_UNSAFE", f"生成路径不安全：{registry}")
        ]
    if not registry.is_file():
        return [
            finding("ERROR", "REGISTRY_INVALID", "workspace.json 必须是普通文件且不能是符号链接")
        ]
    try:
        version = workspace_schema_version(root)
    except WorkspaceError as exc:
        return [finding("ERROR", "REGISTRY_INVALID", str(exc))]
    if version < VERSION:
        return [
            finding(
                "ERROR",
                "WORKSPACE_MIGRATION_REQUIRED",
                f"检测到 workspace schema v{version}；需要迁移，先运行 "
                "python3 scripts/workspace_migrate.py preview --root . --json",
            )
        ]
    try:
        workspace = load_workspace(root)
    except WorkspaceError as exc:
        findings.append(finding("ERROR", "REGISTRY_INVALID", str(exc)))
        _check_extensions(root, findings)
        return findings
    try:
        load_local_settings(root, required=True)
    except WorkspaceError as exc:
        findings.append(finding("ERROR", "LOCAL_CONFIG_INVALID", str(exc)))
    try:
        managed_paths = (
            state_root(root),
            workspace_file(root),
            local_file(root),
            context_file(root),
            state_root(root) / "AGENTS.md",
            lock_file(root),
            workflow_file(root),
            profiles_root(root),
            features_root(root),
            extensions_root(root),
            cache_root(root),
            workflow_runs_root(root),
        )
        ignored = all(
            ignored_by_root_gitignore(
                root,
                path.relative_to(root).as_posix()
                + ("/" if path == state_root(root) else ""),
            )
            for path in managed_paths
        )
        if not ignored:
            findings.append(
                finding("ERROR", "WORKSPACE_NOT_IGNORED", ".workspace 状态树未被完整 Git ignore")
            )
        tracked = subprocess.run(
            ["git", "-C", str(root), "ls-files", "--", ".workspace"],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if tracked.returncode or tracked.stdout.strip():
            findings.append(
                finding("ERROR", "WORKSPACE_TRACKED", "Git 正在跟踪 .workspace 文件")
            )
    except OSError as exc:
        findings.append(finding("ERROR", "WORKSPACE_NOT_IGNORED", str(exc)))
    for path in (
        registry,
        local_file(root),
        context_file(root),
        profiles_root(root),
        features_root(root),
        state_root(root) / "AGENTS.md",
        lock_file(root),
        workflow_file(root),
        extensions_root(root),
        cache_root(root),
        workflow_runs_root(root),
    ):
        if not _safe_path_chain(root, path):
            findings.append(
                finding("ERROR", "WORKSPACE_PATH_UNSAFE", f"生成路径不安全：{path}")
            )
    _check_local_state_artifacts(root, findings)
    _check_workflow_artifacts(root, findings)
    target: Repository | None = None
    if repository:
        try:
            target = resolve_repository(workspace.repositories, repository)
        except WorkspaceError as exc:
            findings.append(finding("ERROR", "REPOSITORY_UNKNOWN", str(exc)))
            return findings
    try:
        _check_generated_workspace(workspace, findings)
    except WorkspaceError as exc:
        findings.append(finding("ERROR", "GENERATED_CONTENT_INVALID", str(exc)))
    _check_feature_metadata(workspace, findings)
    for item in workspace.repositories:
        if target is not None and item != target:
            continue
        _check_repository(workspace, item, findings, target=target is not None)
    _check_clients_and_skills(root, findings)
    _check_extensions(root, findings)
    _check_markdown_links(root, findings, workspace)
    if verbose:
        _unregistered_siblings(workspace, findings)
    return findings


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="治理仓目录（默认脚本所在项目目录）",
    )
    parser.add_argument("--repo", help="严格检查指定登记仓库")
    parser.add_argument("--verbose", action="store_true", help="显示未登记兄弟 Git 仓")
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON（含修复建议）")
    return parser


def _print_remediation(remediation: Remediation | None) -> None:
    if remediation is None:
        return
    label = "可执行命令" if remediation.kind == "command" else "需人工判断"
    print(f"  建议（{label}）：{remediation.detail}")


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    findings = audit(args.root, args.repo, args.verbose)
    counts = Counter(item.level for item in findings)
    if args.json:
        payload = {
            "findings": [
                {
                    "level": item.level,
                    "code": item.code,
                    "message": item.message,
                    "remediation": (
                        REMEDIATIONS[item.code].as_dict()
                        if item.code in REMEDIATIONS
                        else None
                    ),
                }
                for item in findings
            ],
            "summary": {
                "errors": counts["ERROR"],
                "warnings": counts["WARN"],
                "info": counts["INFO"],
            },
        }
        print(json.dumps(payload, ensure_ascii=False))
    else:
        for item in findings:
            print(f"[{item.level}] {item.code} {item.message}")
            _print_remediation(REMEDIATIONS.get(item.code))
        print(
            f"SUMMARY ERROR={counts['ERROR']} WARN={counts['WARN']} INFO={counts['INFO']}"
        )
    return 1 if counts["ERROR"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
