#!/usr/bin/env python3
"""Show a compact, read-only agent-workbench status."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from collections import Counter
from datetime import date
from pathlib import Path, PurePosixPath
from typing import Optional, Sequence


sys.dont_write_bytecode = True


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from feature_context import (  # noqa: E402
    FEATURE_STATUSES,
    SLUG_RE,
    feature_metadata,
    list_features_lenient,
    summary_payload,
)
from workspace_doctor import audit  # noqa: E402
from workspace_model import (  # noqa: E402
    VERSION,
    WorkspaceError,
    is_independent_git,
    load_workspace,
    read_json,
    repository_path,
    resolve_repository,
    workspace_schema_version,
)
from workspace_setup import discover_sibling_repositories  # noqa: E402
from workspace_paths import state_root, workspace_file  # noqa: E402
from workspace_extension import extension_status  # noqa: E402
from workspace_local import load_local_settings  # noqa: E402
from workspace_workflow import status_result as workflow_status  # noqa: E402
from workspace_verification import (  # noqa: E402
    describe_task_evidence_document,
    evaluate_task_evidence,
    feature_code_state,
    verification_passed as batch_verification_passed,
)


CHECKBOX_RE = re.compile(r"^-\s*\[([ xX])\]\s*(.*)$")
HEADING_CHECKBOX_RE = re.compile(r"^(#{1,6})\s*\[([ xX])\]\s*(.*)$")
NESTED_CHECKBOX_RE = re.compile(r"^\s+-\s*\[([ xX])\]")
FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})")
TASK_ID_RE = re.compile(r"^(T\d{2,})(?:\s+(.+))?$")
DEPENDENCY_RE = re.compile(r"^\s*依赖：\s*(.*?)\s*$")
TASK_ID_IN_TEXT_RE = re.compile(r"(?<![A-Za-z0-9_])T\d{2,}(?![A-Za-z0-9_])")
TASK_RANGE_RE = re.compile(r"\b(T\d{2,})\s*(?:-|–|—|~|至)\s*(T\d{2,})\b")
MARKDOWN_LINK_RE = re.compile(r"\]\(([^)]+)\)")
COMPLETION_POLICY_RE = re.compile(r"^\s*-\s*完成门禁：\s*`?([^`\s]+)`?\s*$")
TASK_REPOSITORY_RE = re.compile(r"^\s*目标仓：\s*`?([^`\s]+)`?\s*$")
VALIDATION_KIND_RE = re.compile(r"^\s*验证性质：\s*(.*?)\s*$")
DELIVERABLE_RE = re.compile(
    r"^\s*-\s*(Create|Modify|Test|Delete|Verify)：\s*(.*?)\s*$"
)
INLINE_CODE_RE = re.compile(r"`([^`]+)`")
COMPLETION_POLICIES = {"task-evidence-v1"}
VALIDATION_KINDS = {"行为", "声明式", "持久化"}
VERIFICATION_RECORD_RE = re.compile(
    r"^## (?:执行记录 \d{4}-\d{2}-\d{2}|验证批次 \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:Z|[+-]\d{2}:\d{2}))\s*$",
    re.MULTILINE,
)
SECTION_RE = re.compile(r"^##\s", re.MULTILINE)
STATUS_SCHEMA_VERSION = 1
DOCUMENT_REVIEW_FIELDS = {
    "requirements": "需求审阅",
    "design": "设计审阅",
    "plan": "计划审阅",
}
DOCUMENT_REVIEW_VALUES = {"未生成", "待审阅", "已批准"}
DOCUMENT_PATHS = {
    "requirements": "requirements/requirements.md",
    "design": "design/design.md",
    "plan": "plans/implementation.md",
}


def artifact_summary(feature: Path) -> list[dict[str, str]]:
    root = feature / "artifacts"
    if root.is_symlink():
        raise ValueError(f"需求交付物目录不安全：{root}")
    if not root.exists():
        return []
    if not root.is_dir():
        raise ValueError(f"需求交付物目录不安全：{root}")
    result = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError(f"需求交付物不允许符号链接：{path}")
        if not path.is_file():
            continue
        relative = path.relative_to(feature).as_posix()
        parts = path.relative_to(root).parts
        kind = parts[0] if len(parts) > 1 else path.suffix.removeprefix(".") or "other"
        result.append({"path": relative, "type": kind})
    return result


def _diagnostic(
    code: str, severity: str, path: Path, line: int, message: str
) -> dict[str, object]:
    return {
        "severity": severity,
        "code": code,
        "path": path.name,
        "line": line,
        "message": message,
    }


def _fenced_lines(lines: list[str], path: Path) -> tuple[set[int], list[dict[str, object]]]:
    fenced = set()
    diagnostics = []
    opening: tuple[str, int, int] | None = None
    for line_number, line in enumerate(lines, start=1):
        match = FENCE_RE.match(line)
        if opening is not None:
            fenced.add(line_number)
            if (
                match is not None
                and match.group(1)[0] == opening[0]
                and len(match.group(1)) >= opening[1]
            ):
                opening = None
            continue
        if match is not None:
            marker = match.group(1)
            opening = (marker[0], len(marker), line_number)
            fenced.add(line_number)
    if opening is not None:
        diagnostics.append(
            _diagnostic(
                "PLAN_UNCLOSED_FENCE",
                "error",
                path,
                opening[2],
                "代码围栏未闭合，围栏内内容不会作为计划任务解析",
            )
        )
    return fenced, diagnostics


def _task_range(
    lines: list[str], fenced: set[int], tasks: list[dict[str, object]], index: int
) -> tuple[int, int]:
    task = tasks[index]
    start = int(task["startLine"])
    next_start = int(tasks[index + 1]["startLine"]) - 1 if index + 1 < len(tasks) else len(lines)
    if task["style"] == "heading":
        level = int(task["headingLevel"])
        boundary = re.compile(r"^(#{1," + str(level) + r"})\s")
    else:
        boundary = re.compile(r"^#{1,6}\s")
    for line_number in range(start + 1, next_start + 1):
        if line_number in fenced:
            continue
        if boundary.match(lines[line_number - 1]):
            return start, line_number - 1
    return start, next_start


def _dependency_diagnostics(
    tasks: list[dict[str, object]], path: Path
) -> list[dict[str, object]]:
    diagnostics = []
    by_id: dict[str, dict[str, object]] = {}
    for task in tasks:
        task_id = task["id"]
        if task_id is None:
            continue
        if task_id in by_id:
            diagnostics.append(
                _diagnostic(
                    "PLAN_DUPLICATE_TASK_ID",
                    "error",
                    path,
                    int(task["startLine"]),
                    f"任务编号重复：{task_id}",
                )
            )
            continue
        by_id[str(task_id)] = task

    edges: dict[str, list[str]] = {task_id: [] for task_id in by_id}
    for task in tasks:
        task_id = task["id"]
        if task_id is None:
            continue
        task_id = str(task_id)
        dependencies = list(task["dependencies"])
        for dependency in dependencies:
            if dependency == task_id:
                diagnostics.append(
                    _diagnostic(
                        "PLAN_SELF_DEPENDENCY",
                        "error",
                        path,
                        int(task["startLine"]),
                        f"任务 {task_id} 不能依赖自身",
                    )
                )
            elif dependency not in by_id:
                diagnostics.append(
                    _diagnostic(
                        "PLAN_UNKNOWN_DEPENDENCY",
                        "error",
                        path,
                        int(task["startLine"]),
                        f"任务 {task_id} 依赖不存在的任务：{dependency}",
                    )
                )
            elif by_id[task_id] is task:
                edges[task_id].append(dependency)

    visiting: set[str] = set()
    visited: set[str] = set()
    reported: set[tuple[str, ...]] = set()

    def visit(task_id: str, stack: list[str]) -> None:
        if task_id in visiting:
            cycle = tuple(stack[stack.index(task_id) :] + [task_id])
            if cycle not in reported:
                reported.add(cycle)
                diagnostics.append(
                    _diagnostic(
                        "PLAN_DEPENDENCY_CYCLE",
                        "error",
                        path,
                        int(by_id[task_id]["startLine"]),
                        "任务依赖形成环：" + " -> ".join(cycle),
                    )
                )
            return
        if task_id in visited:
            return
        visiting.add(task_id)
        for dependency in edges[task_id]:
            visit(dependency, [*stack, task_id])
        visiting.remove(task_id)
        visited.add(task_id)

    for task_id in edges:
        visit(task_id, [])
    return diagnostics


def _safe_deliverable_path(value: str) -> bool:
    if not value or value.endswith("/") or "\\" in value:
        return False
    if any(marker in value for marker in ("*", "?", "[", "]")):
        return False
    pure = PurePosixPath(value)
    return not pure.is_absolute() and pure != PurePosixPath(".") and ".." not in pure.parts


def plan_analysis(
    path: Path,
    text: str | None = None,
    repositories: set[str] | None = None,
) -> dict[str, object]:
    if path.is_symlink():
        raise ValueError(f"实施计划不允许符号链接：{path}")
    if not path.exists():
        return {
            "exists": False,
            "completionPolicy": None,
            "tasks": [],
            "diagnostics": [],
        }
    if not path.is_file():
        raise ValueError(f"实施计划必须是普通文件：{path}")
    lines = (path.read_text(encoding="utf-8") if text is None else text).splitlines()
    fenced, diagnostics = _fenced_lines(lines, path)
    policy_matches = [
        (line_number, match.group(1))
        for line_number, line in enumerate(lines, start=1)
        if line_number not in fenced and (match := COMPLETION_POLICY_RE.match(line))
    ]
    completion_policy = (
        "legacy-checkbox" if not policy_matches else policy_matches[0][1]
    )
    evidence_contract = bool(policy_matches)
    if len(policy_matches) > 1 or (
        policy_matches and completion_policy not in COMPLETION_POLICIES
    ):
        diagnostics.append(
            _diagnostic(
                "PLAN_COMPLETION_POLICY_UNKNOWN",
                "error",
                path,
                policy_matches[1][0] if len(policy_matches) > 1 else policy_matches[0][0],
                "完成门禁必须唯一且值为 task-evidence-v1",
            )
        )
    tasks: list[dict[str, object]] = []
    for line_number, line in enumerate(lines, start=1):
        if line_number in fenced:
            continue
        list_match = CHECKBOX_RE.match(line)
        heading_match = HEADING_CHECKBOX_RE.match(line)
        if list_match is not None:
            completed = list_match.group(1).lower() == "x"
            title = list_match.group(2).strip()
            style = "list"
            heading_level = None
        elif heading_match is not None:
            completed = heading_match.group(2).lower() == "x"
            title = heading_match.group(3).strip()
            style = "heading"
            heading_level = len(heading_match.group(1))
            diagnostics.append(
                _diagnostic(
                    "PLAN_LEGACY_HEADING_TASK",
                    "warning",
                    path,
                    line_number,
                    "标题式任务仍会统计；新计划请使用 - [ ] T01 任务标题",
                )
            )
        else:
            if NESTED_CHECKBOX_RE.match(line):
                diagnostics.append(
                    _diagnostic(
                        "PLAN_NESTED_TASK",
                        "warning",
                        path,
                        line_number,
                        "嵌套复选框不作为计划任务统计",
                    )
                )
            continue
        identifier = TASK_ID_RE.match(title)
        task_id = identifier.group(1) if identifier is not None else None
        task_title = identifier.group(2) if identifier is not None else title
        if style == "list" and task_id is None:
            diagnostics.append(
                _diagnostic(
                    "PLAN_UNNUMBERED_TASK",
                    "warning",
                    path,
                    line_number,
                    "未编号任务仍会统计；新计划请使用 T01 等稳定编号",
                )
            )
        tasks.append(
            {
                "id": task_id,
                "title": task_title,
                "completed": completed,
                "line": line.strip(),
                "startLine": line_number,
                "endLine": line_number,
                "style": style,
                "headingLevel": heading_level,
                "dependencies": [],
                "references": [],
                "repository": None,
                "validationKind": None,
                "deliverables": [],
            }
        )

    for index, task in enumerate(tasks):
        start, end = _task_range(lines, fenced, tasks, index)
        task["endLine"] = end
        body = lines[start - 1 : end]
        dependencies = []
        for line_number, line in enumerate(body[1:], start=start + 1):
            match = DEPENDENCY_RE.match(line)
            if match is None or match.group(1) in {"", "无"}:
                continue
            value = match.group(1)
            for range_match in TASK_RANGE_RE.finditer(value):
                diagnostics.append(
                    _diagnostic(
                        "PLAN_DEPENDENCY_RANGE",
                        "error",
                        path,
                        line_number,
                        f"依赖范围 {range_match.group(0)} 可能遗漏任务；请显式列出每个任务编号",
                    )
                )
            dependencies.extend(TASK_ID_IN_TEXT_RE.findall(value))
        task["dependencies"] = list(dict.fromkeys(dependencies))
        task["references"] = [
            target
            for line in body
            for target in MARKDOWN_LINK_RE.findall(line)
        ]
        if not evidence_contract:
            continue

        repositories_in_task = [
            (line_number, match.group(1))
            for line_number, line in enumerate(body[1:], start=start + 1)
            if (match := TASK_REPOSITORY_RE.match(line))
        ]
        repository = (
            repositories_in_task[0][1] if len(repositories_in_task) == 1 else None
        )
        repository_valid = (
            repository is not None
            and SLUG_RE.fullmatch(repository) is not None
            and (repositories is None or repository in repositories)
        )
        if not repository_valid:
            diagnostics.append(
                _diagnostic(
                    "PLAN_TASK_REPOSITORY_INVALID",
                    "error",
                    path,
                    repositories_in_task[0][0] if repositories_in_task else start,
                    "新计划任务必须声明 feature 内唯一目标仓",
                )
            )
        task["repository"] = repository

        validation_fields = [
            (line_number, match.group(1))
            for line_number, line in enumerate(body[1:], start=start + 1)
            if (match := VALIDATION_KIND_RE.match(line))
        ]
        validation_kind = (
            validation_fields[0][1] if len(validation_fields) == 1 else None
        )
        if validation_kind not in VALIDATION_KINDS:
            diagnostics.append(
                _diagnostic(
                    "PLAN_TASK_VALIDATION_KIND_MISSING",
                    "error",
                    path,
                    validation_fields[0][0] if validation_fields else start,
                    "新计划任务必须声明唯一验证性质：行为、声明式或持久化",
                )
            )
        task["validationKind"] = validation_kind

        deliverables = []
        deliverable_lines = [
            (line_number, match.group(1), match.group(2))
            for line_number, line in enumerate(body[1:], start=start + 1)
            if (match := DELIVERABLE_RE.match(line))
        ]
        for line_number, kind, detail in deliverable_lines:
            code_values = INLINE_CODE_RE.findall(detail)
            value = code_values[0] if code_values else ""
            if not _safe_deliverable_path(value):
                diagnostics.append(
                    _diagnostic(
                        "PLAN_TASK_DELIVERABLE_INVALID",
                        "error",
                        path,
                        line_number,
                        f"{kind} 必须使用完整、安全且不含 glob 的仓内相对文件路径",
                    )
                )
                continue
            deliverables.append(
                {
                    "repository": repository,
                    "kind": kind,
                    "path": value,
                    "symbol": "、".join(code_values[1:]) or None,
                    "line": line_number,
                }
            )
        if not deliverable_lines:
            diagnostics.append(
                _diagnostic(
                    "PLAN_TASK_DELIVERABLE_INVALID",
                    "error",
                    path,
                    start,
                    "新计划任务必须声明至少一个精确交付文件",
                )
            )
        task["deliverables"] = deliverables

    if not tasks:
        diagnostics.append(
            _diagnostic(
                "PLAN_EMPTY",
                "error",
                path,
                1,
                "实施计划存在但没有可识别的顶层任务",
            )
        )
    diagnostics.extend(_dependency_diagnostics(tasks, path))
    return {
        "exists": True,
        "completionPolicy": completion_policy,
        "tasks": tasks,
        "diagnostics": diagnostics,
    }


def plan_tasks(path: Path) -> list[tuple[bool, str]]:
    return [
        (bool(task["completed"]), str(task["line"]))
        for task in plan_analysis(path)["tasks"]
    ]


def plan_progress(path: Path) -> dict[str, int]:
    checked = [bool(task["completed"]) for task in plan_analysis(path)["tasks"]]
    return {"completed": sum(checked), "total": len(checked)}


def verification_record(feature: Path, text: str | None = None) -> str | None:
    """Read the latest legacy record or verification batch without fallback."""
    path = feature / "testing/verification.md"
    if text is None and (not path.is_file() or path.is_symlink()):
        return None
    text = path.read_text(encoding="utf-8") if text is None else text
    records = list(VERIFICATION_RECORD_RE.finditer(text))
    if not records:
        return None
    latest = records[-1]
    next_section = SECTION_RE.search(text, latest.end())
    end = next_section.start() if next_section is not None else len(text)
    record = text[latest.start():end].strip()
    return record


def document_reviews(feature: Path, text: str | None = None) -> tuple[dict[str, str], list[dict[str, object]], bool]:
    readme = feature / "README.md"
    text = readme.read_text(encoding="utf-8") if text is None else text
    metadata = feature_metadata(readme, text)
    lines = text.splitlines()
    reviews: dict[str, str] = {}
    diagnostics: list[dict[str, object]] = []
    recorded = False
    for key, field in DOCUMENT_REVIEW_FIELDS.items():
        value = metadata.get(field)
        if value is None:
            reviews[key] = "未记录"
            continue
        recorded = True
        if value not in DOCUMENT_REVIEW_VALUES:
            reviews[key] = "未记录"
            diagnostics.append(
                _diagnostic(
                    "DOCUMENT_REVIEW_INVALID",
                    "error",
                    readme,
                    next(
                        index
                        for index, line in enumerate(
                            lines, start=1
                        )
                        if line.startswith(f"- {field}：")
                    ),
                    f"{field} 必须为：{'、'.join(sorted(DOCUMENT_REVIEW_VALUES))}",
                )
            )
            continue
        reviews[key] = value
        path = feature / DOCUMENT_PATHS[key]
        if value in {"待审阅", "已批准"} and (path.is_symlink() or not path.is_file()):
            diagnostics.append(
                _diagnostic(
                    "DOCUMENT_REVIEW_MISSING_FILE",
                    "error",
                    readme,
                    next(
                        index
                        for index, line in enumerate(
                            lines, start=1
                        )
                        if line.startswith(f"- {field}：")
                    ),
                    f"{field} 为 {value}，但缺少 {DOCUMENT_PATHS[key]}",
                )
            )
        elif value == "未生成" and path.is_file():
            diagnostics.append(
                _diagnostic(
                    "DOCUMENT_REVIEW_UNEXPECTED_FILE",
                    "warning",
                    readme,
                    next(
                        index
                        for index, line in enumerate(
                            lines, start=1
                        )
                        if line.startswith(f"- {field}：")
                    ),
                    f"{field} 为未生成，但 {DOCUMENT_PATHS[key]} 已存在",
                )
            )
    if reviews["design"] == "待审阅" and reviews["plan"] == "已批准":
        diagnostics.append(
            _diagnostic(
                "DOCUMENT_REVIEW_PLAN_STALE",
                "error",
                readme,
                next(
                    index
                    for index, line in enumerate(lines, start=1)
                    if line.startswith("- 计划审阅：")
                ),
                "设计审阅为待审阅时，已有实施计划也必须改为待审阅",
            )
        )
    return reviews, diagnostics, recorded


def current_branch(path: Path) -> str | None:
    result = subprocess.run(
        ["git", "-C", str(path), "branch", "--show-current"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        env=dict(os.environ, GIT_OPTIONAL_LOCKS="0"),
    )
    return result.stdout.strip() or None if result.returncode == 0 else None


def _task_repository_roots(
    root: Path, mode: str, item: dict[str, object]
) -> dict[str, Path]:
    repositories = [str(name) for name in item["repositories"]]
    if mode == "maintenance":
        return {root.name: root}
    workspace = load_workspace(root)
    return {
        name: repository_path(
            workspace, resolve_repository(workspace.repositories, name)
        )
        for name in repositories
    }


def _task_evidence_state(
    root: Path,
    mode: str,
    feature: Path,
    item: dict[str, object],
    analysis: dict[str, object],
    verification: Path,
) -> tuple[dict[str, object], list[dict[str, object]], list[dict[str, object]]]:
    tasks = analysis["tasks"]
    if analysis["completionPolicy"] != "task-evidence-v1":
        return (
            {"applicable": False, "completed": None, "total": len(tasks)},
            [],
            [],
        )
    text = verification.read_text(encoding="utf-8") if verification.is_file() else ""
    evidence = describe_task_evidence_document(text)["latestByTask"]
    roots = _task_repository_roots(root, mode, item)
    results = []
    diagnostics = []
    for task in tasks:
        if not task["completed"]:
            continue
        result = evaluate_task_evidence(
            task,
            evidence.get(task["id"]),
            roots,
            workspace_root=root,
            feature_root=feature,
        )
        results.append(result)
        diagnostics.extend(result["diagnostics"])
    return (
        {
            "applicable": True,
            "completed": sum(bool(result["trusted"]) for result in results),
            "total": len(tasks),
        },
        results,
        diagnostics,
    )


def _tracking(root: Path, mode: str, feature: Path, item: dict[str, object]) -> dict[str, object]:
    for directory in (feature / "design", feature / "plans", feature / "testing"):
        if directory.is_symlink():
            raise ValueError(f"需求记录目录不允许符号链接：{directory}")
    design = feature / "design" / "design.md"
    if design.is_symlink():
        raise ValueError(f"设计文档不允许符号链接：{design}")
    verification = feature / "testing" / "verification.md"
    if verification.is_symlink():
        raise ValueError(f"验证记录不允许符号链接：{verification}")
    plan = feature / "plans" / "implementation.md"
    analysis = plan_analysis(
        plan,
        repositories={str(repository) for repository in item["repositories"]},
    )
    reviews, review_diagnostics, reviews_recorded = document_reviews(feature)
    trusted_progress, task_evidence, evidence_diagnostics = _task_evidence_state(
        root, mode, feature, item, analysis, verification
    )
    record = verification_record(feature)
    current_states = None
    if record is not None and record.startswith("## 验证批次 "):
        try:
            current_states = feature_code_state(root, mode, item)
        except (OSError, RuntimeError, UnicodeError, ValueError, WorkspaceError):
            current_states = None
    result = {
        "designExists": design.is_file(),
        "planExists": analysis["exists"],
        "progress": {
            "completed": sum(bool(task["completed"]) for task in analysis["tasks"]),
            "total": len(analysis["tasks"]),
        },
        "documentReviews": reviews,
        "documentReviewsRecorded": reviews_recorded,
        "documentDiagnostics": [
            *review_diagnostics,
            *analysis["diagnostics"],
            *evidence_diagnostics,
        ],
        "verificationExists": verification.is_file(),
        "verificationPassed": batch_verification_passed(record, current_states),
        "artifacts": artifact_summary(feature),
    }
    if analysis["completionPolicy"] == "task-evidence-v1":
        result.update(
            {
                "completionPolicy": analysis["completionPolicy"],
                "trustedProgress": trusted_progress,
                "taskEvidence": task_evidence,
            }
        )
    return result


STAGE_RUNBOOKS = {
    "workspace.init": ".agents/skills/workspace-init/SKILL.md",
    "feature.context": ".agents/skills/workspace-feature-design/SKILL.md",
    "feature.design": ".agents/skills/workspace-feature-design/SKILL.md",
    "feature.implement": ".agents/skills/workspace-execute-plan/SKILL.md",
    "feature.verify": ".agents/skills/workspace-verify/SKILL.md",
    "feature.submit-test": ".agents/skills/workspace-submit-test/SKILL.md",
    "feature.complete": "docs/foundation/README.md",
    "extension.blocked": ".agents/skills/workspace-extension/SKILL.md",
}

STAGE_CONFIRMATIONS = {
    "workspace.init": "network",
    "feature.design": "semantic",
    "feature.submit-test": "remote",
    "feature.complete": "semantic",
}


def _stage_action(
    stage: str, reason: str, confirmation: str | None = None
) -> dict[str, object]:
    return {
        "stage": stage,
        "runbook": STAGE_RUNBOOKS[stage],
        "reason": reason,
        "confirmation": confirmation or STAGE_CONFIRMATIONS.get(stage, "local"),
    }


def _confirmation(category: str) -> dict[str, object]:
    return {"category": category, "required": category in {"network", "remote", "semantic"}}


def _single_feature_progress(
    feature: dict[str, object], *, mode: str = "workspace"
) -> dict[str, object]:
    if feature["status"] == "paused":
        return {
            "currentStage": None,
            "nextActions": [
                _stage_action(
                    "feature.context",
                    f"需求 {feature['featureSlug']} 已暂停，恢复它或改选其他需求后继续",
                    confirmation="semantic",
                )
            ],
            "blockers": ["FEATURE_PAUSED"],
            "confirmation": _confirmation("semantic"),
        }
    progress = feature["progress"]
    assert isinstance(progress, dict)
    trusted_progress = feature.get("trustedProgress")
    if (
        isinstance(trusted_progress, dict)
        and trusted_progress.get("applicable") is True
    ):
        progress = trusted_progress
    reviews = feature.get("documentReviews")
    reviews_recorded = feature.get("documentReviewsRecorded") is True
    assert isinstance(reviews, dict)
    plan_exists = feature.get("planExists") is True
    if reviews_recorded and feature["status"] != "paused":
        requirements_review = reviews["requirements"]
        design_review = reviews["design"]
        plan_review = reviews["plan"]
        if feature["status"] == "planning":
            if requirements_review == "未生成":
                reason = f"需求 {feature['featureSlug']} 的需求记录尚未生成；讨论并确认范围后生成需求文档"
            elif requirements_review != "已批准":
                reason = f"需求 {feature['featureSlug']} 的需求文件已存在；审阅实际需求文件后讨论方案"
            elif design_review == "未生成":
                reason = f"需求 {feature['featureSlug']} 的需求文件已批准；讨论并确认方案后生成设计文档"
            elif design_review != "已批准":
                reason = f"需求 {feature['featureSlug']} 的设计文档已存在；审阅实际设计文件后生成实施计划"
            elif plan_review == "未生成":
                reason = f"需求 {feature['featureSlug']} 的设计文件已批准；生成实施计划草案"
            elif plan_review != "已批准":
                reason = f"需求 {feature['featureSlug']} 的实施计划已存在；审阅并批准实际计划、基线、分支和执行方式后更新为 development"
            elif not plan_exists or progress["total"] == 0:
                reason = f"需求 {feature['featureSlug']} 的已批准计划缺少可执行任务；先修复计划格式或内容"
            else:
                reason = f"需求 {feature['featureSlug']} 的计划已批准；更新状态为 development 后执行"
            return {
                "currentStage": "feature.design",
                "nextActions": [_stage_action("feature.design", reason)],
                "blockers": [],
                "confirmation": _confirmation("semantic"),
            }
        if design_review not in {"已批准", "未记录"}:
            return {
                "currentStage": "feature.design",
                "nextActions": [
                    _stage_action(
                        "feature.design",
                        f"需求 {feature['featureSlug']} 的 Design 审阅包尚未获批准；"
                        "审阅主设计和附件，并将已有实施计划改为待审阅后继续",
                    )
                ],
                "blockers": [],
                "confirmation": _confirmation("semantic"),
            }
        if plan_review != "已批准":
            return {
                "currentStage": "feature.design",
                "nextActions": [
                    _stage_action(
                        "feature.design",
                        f"需求 {feature['featureSlug']} 的实施计划尚未获批准；审阅实际计划和执行条件后继续",
                    )
                ],
                "blockers": [],
                "confirmation": _confirmation("semantic"),
            }
    if feature["status"] == "planning":
        if not feature["designExists"]:
            reason = f"需求 {feature['featureSlug']} 的需求记录已存在；讨论并确认方案后生成设计文档"
        elif progress["total"] == 0:
            reason = f"需求 {feature['featureSlug']} 的设计文档已存在；审阅确认后生成实施计划"
        else:
            reason = f"需求 {feature['featureSlug']} 的实施计划已存在；审阅并批准计划、基线、分支和执行方式后更新为 development"
        stage = "feature.design"
    elif progress["total"] == 0:
        stage = "feature.design"
        reason = f"需求 {feature['featureSlug']} 缺少可执行的实施计划"
    elif progress["completed"] < progress["total"]:
        stage = "feature.implement"
        reason = f"继续需求 {feature['featureSlug']}"
    elif not feature["verificationPassed"]:
        stage = "feature.verify"
        reason = f"继续需求 {feature['featureSlug']}"
    elif mode == "maintenance" or feature["status"] == "testing":
        stage = "feature.complete"
        reason = f"继续需求 {feature['featureSlug']}"
    else:
        stage = "feature.submit-test"
        reason = f"继续需求 {feature['featureSlug']}"
    return {
        "currentStage": stage,
        "nextActions": [_stage_action(stage, reason)],
        "blockers": [],
        "confirmation": _confirmation(str(_stage_action(stage, "")["confirmation"])),
    }


def _progress_state(
    mode: str,
    registry_exists: bool,
    features: list[dict[str, object]],
    active_feature: str | None = None,
) -> dict[str, object]:
    if len(features) > 1:
        by_slug = {str(item["featureSlug"]): item for item in features}
        if mode == "workspace" and active_feature is not None:
            if active_feature in by_slug:
                result = _single_feature_progress(by_slug[active_feature], mode=mode)
                result["otherActiveFeatures"] = sorted(
                    slug for slug in by_slug if slug != active_feature
                )
                return result
            slugs = ", ".join(sorted(by_slug))
            return {
                "currentStage": None,
                "nextActions": [
                    _stage_action(
                        "feature.context",
                        f"活跃指针指向的需求不存在或已完成：{active_feature}；"
                        f"从以下需求中选择：{slugs}",
                        confirmation="semantic",
                    )
                ],
                "blockers": ["ACTIVE_FEATURE_INVALID"],
                "confirmation": _confirmation("none"),
            }
        slugs = ", ".join(str(item["featureSlug"]) for item in features)
        return {
            "currentStage": None,
            "nextActions": [
                _stage_action(
                    "feature.context",
                    f"存在多个未完成需求，选定唯一需求后继续：{slugs}",
                    confirmation="semantic",
                )
            ],
            "blockers": ["MULTIPLE_ACTIVE_FEATURES"],
            "confirmation": _confirmation("none"),
        }
    if not registry_exists:
        if len(features) == 1:
            return _single_feature_progress(features[0], mode=mode)
        return {
            "currentStage": "workspace.init",
            "nextActions": [_stage_action("workspace.init", "工作区尚未初始化")],
            "blockers": [],
            "confirmation": _confirmation("network"),
        }
    if not features:
        return {
            "currentStage": "feature.context",
            "nextActions": [_stage_action("feature.context", "没有未完成需求")],
            "blockers": [],
            "confirmation": _confirmation("local"),
        }
    return _single_feature_progress(features[0], mode=mode)


def _maintenance_features(root: Path) -> tuple[list[dict[str, object]], list[dict[str, str]]]:
    features = root / "docs" / "development" / "features"
    if not features.exists():
        return [], []
    if (
        features.is_symlink()
        or not features.is_dir()
        or not features.resolve().is_relative_to(root)
    ):
        raise ValueError(f"维护需求目录不存在或不安全：{features}")
    result = []
    degraded: list[dict[str, str]] = []
    for feature in sorted(features.iterdir()):
        if feature.is_symlink():
            raise ValueError(f"维护需求目录不允许符号链接：{feature}")
        if not feature.is_dir():
            continue
        relative = feature.relative_to(root).as_posix()
        try:
            if not SLUG_RE.fullmatch(feature.name):
                raise ValueError(f"维护需求目录名必须使用小写 kebab-case：{feature.name}")
            readme = feature / "README.md"
            if readme.is_symlink() or not readme.is_file():
                raise ValueError(f"维护需求 README 不存在或不安全：{readme}")
            metadata = feature_metadata(readme)
            status = metadata.get("状态", "")
            slug = metadata.get("需求短名", "")
            branch = metadata.get("工作分支", "")
            base = metadata.get("基线分支", "")
            updated = metadata.get("最后更新", "")
            if status not in FEATURE_STATUSES:
                raise ValueError(f"{readme} 的状态无效：{status}")
            if slug != feature.name:
                raise ValueError(f"{readme} 的需求短名必须为 {feature.name}")
            if not branch or not base:
                raise ValueError(f"{readme} 的工作分支和基线分支不能为空")
            try:
                if date.fromisoformat(updated).isoformat() != updated:
                    raise ValueError
            except ValueError as exc:
                raise ValueError(f"{readme} 的最后更新日期必须为 YYYY-MM-DD") from exc
        except (ValueError, OSError, RuntimeError) as exc:
            degraded.append({"path": relative, "error": str(exc)})
            continue
        if status == "done":
            continue
        item = {
            "featureSlug": feature.name,
            "path": relative,
            "status": status,
            "repositories": [root.name],
            "branches": [[root.name, branch]],
            "baseBranches": [[root.name, base]],
            "lastUpdated": updated,
        }
        item.update(_tracking(root, "maintenance", feature, item))
        result.append(item)
    return result, degraded


def _workspace_features(root: Path) -> tuple[list[dict[str, object]], list[dict[str, str]]]:
    summaries, degraded = list_features_lenient(root)
    result = []
    for feature in summaries:
        if feature.status == "done":
            continue
        item = summary_payload(feature)
        item["path"] = feature.path.relative_to(root).as_posix()
        item.update(_tracking(root, "workspace", feature.path, item))
        result.append(item)
    return result, degraded


def _blocked_workspace_summary(root: Path, extensions: dict[str, object]) -> dict[str, object]:
    try:
        identity = read_json(workspace_file(root)).get("workspace")
    except WorkspaceError:
        identity = None
    workspace = (
        {"name": identity["name"]}
        if isinstance(identity, dict) and isinstance(identity.get("name"), str)
        else None
    )
    return {
        "mode": "workspace",
        "workspace": workspace,
        "repositories": [],
        "candidateSiblingRepositories": [],
        "extensions": extensions,
        "workflow": workflow_status(root),
        "features": [],
    }


def _migration_required_status(findings: list[object]) -> dict[str, object]:
    counts = Counter(getattr(item, "level") for item in findings)
    return {
        "schemaVersion": STATUS_SCHEMA_VERSION,
        "mode": "workspace",
        "workspace": None,
        "repositories": [],
        "candidateSiblingRepositories": [],
        "extensions": {"activeIds": [], "providers": {}, "repositoryOverrides": {}, "blockedCodes": []},
        "workflow": {"enabled": False},
        "features": [],
        "currentStage": None,
        "nextActions": [_stage_action("feature.context", "workspace schema 需要迁移，先运行 python3 scripts/workspace_migrate.py preview --root . --json", confirmation="semantic")],
        "blockers": ["WORKSPACE_MIGRATION_REQUIRED"],
        "confirmation": _confirmation("semantic"),
        "doctor": {"errors": counts["ERROR"], "warnings": counts["WARN"], "info": counts["INFO"]},
    }


def _context_sources(root: Path) -> dict[str, object]:
    state = state_root(root)
    context = state / "CONTEXT.md"
    result: dict[str, object] = {
        "workspace": {
            "path": ".workspace/CONTEXT.md",
            "exists": context.is_file() and not context.is_symlink(),
        },
        "repositories": [],
    }
    try:
        repositories = read_json(workspace_file(root)).get("repositories", [])
    except (OSError, UnicodeError, WorkspaceError):
        repositories = []
    for repository in repositories if isinstance(repositories, list) else []:
        if not isinstance(repository, dict):
            continue
        name = repository.get("path")
        instruction = repository.get("instruction")
        if not isinstance(name, str) or not isinstance(instruction, str):
            continue
        relative = PurePosixPath(instruction)
        if relative.is_absolute() or ".." in relative.parts:
            continue
        path = state / relative
        result["repositories"].append(
            {
                "repository": name,
                "path": f".workspace/{relative.as_posix()}",
                "exists": path.is_file() and not path.is_symlink(),
            }
        )
    return result


def status_result(root: Path, *, context_sources: bool = False) -> dict[str, object]:
    root = root.resolve()
    if not root.is_dir():
        raise ValueError(f"治理仓目录不存在：{root}")
    registry = workspace_file(root)
    state = state_root(root)
    findings = audit(root, verbose=True)
    counts = Counter(item.level for item in findings)
    if not registry.exists() and not registry.is_symlink():
        workspace = None
        repositories: list[dict[str, object]] = []
        candidates: list[str] = []
        features, degraded_features = _maintenance_features(root)
        extensions = extension_status(root)
        mode = "maintenance"
    else:
        try:
            if (
                workspace_schema_version(root) < VERSION
                and (state_root(root) / "AGENTS.md").is_file()
            ):
                result = _migration_required_status(findings)
                if context_sources:
                    result["contextSources"] = _context_sources(root)
                return result
        except WorkspaceError:
            pass
        try:
            workspace_model = load_workspace(root)
        except WorkspaceError:
            extensions = extension_status(root)
            if extensions["blockedCodes"]:
                result = _blocked_workspace_summary(root, extensions)
                result["doctor"] = {
                    "errors": counts["ERROR"],
                    "warnings": counts["WARN"],
                    "info": counts["INFO"],
                }
                codes = extensions["blockedCodes"]
                result["currentStage"] = None
                result["nextActions"] = [
                    _stage_action(
                        "extension.blocked",
                        f"Extension/Provider 配置阻塞：{', '.join(codes)}",
                        confirmation="semantic",
                    )
                ]
                result["blockers"] = list(codes)
                result["confirmation"] = _confirmation("semantic")
                result["schemaVersion"] = STATUS_SCHEMA_VERSION
                if context_sources:
                    result["contextSources"] = _context_sources(root)
                return result
            raise
        registered = {repository.path for repository in workspace_model.repositories}
        repositories = []
        for repository in workspace_model.repositories:
            path = repository_path(workspace_model, repository)
            installed = is_independent_git(path)
            repositories.append(
                {
                    "path": repository.path,
                    "installed": installed,
                    "currentBranch": current_branch(path) if installed else None,
                }
            )
        candidates = [
            str(path)
            for path in discover_sibling_repositories(root)
            if path.name not in registered
        ]
        features, degraded_features = _workspace_features(root)
        workspace = workspace_model.identity.as_dict()
        extensions = extension_status(root)
        mode = "workspace"
    active_feature = None
    if mode == "workspace":
        try:
            active_feature = load_local_settings(root, required=False).active_feature
        except WorkspaceError:
            active_feature = None
    progress_state = _progress_state(mode, registry.exists(), features, active_feature)
    result: dict[str, object] = {
        "schemaVersion": STATUS_SCHEMA_VERSION,
        "mode": mode,
        "workspace": workspace,
        "repositories": repositories,
        "candidateSiblingRepositories": candidates,
        "extensions": extensions,
        "workflow": workflow_status(root),
        "features": features,
        **progress_state,
        "doctor": {
            "errors": counts["ERROR"],
            "warnings": counts["WARN"],
            "info": counts["INFO"],
        },
    }
    if degraded_features:
        result["degradedFeatures"] = degraded_features
    if context_sources:
        result["contextSources"] = _context_sources(root)
    return result


def _render_text(result: dict[str, object]) -> None:
    print(f"模式：{result['mode']}")
    print(f"当前阶段：{result['currentStage'] or '需选择需求'}")
    actions = result["nextActions"]
    if actions:
        print(f"下一步：{actions[0]['runbook']}")
    if result["blockers"]:
        print("阻塞：" + ", ".join(result["blockers"]))
    other_active = result.get("otherActiveFeatures")
    if other_active:
        print("其他进行中的需求：" + ", ".join(other_active))
    degraded = result.get("degradedFeatures")
    if degraded:
        paths = "、".join(item["path"] for item in degraded)
        print(f"警示：{len(degraded)} 个需求目录解析失败，已跳过：{paths}")
    workspace = result["workspace"]
    if isinstance(workspace, dict):
        print(f"工作区：{workspace['name']}")
    print("仓库：")
    repositories = result["repositories"]
    assert isinstance(repositories, list)
    for repository in repositories:
        print(
            f"- {repository['path']}："
            f"{'已安装' if repository['installed'] else '未安装'}，"
            f"分支 {repository['currentBranch'] or '-'}"
        )
    if not repositories:
        print("- （无）")
    extensions = result["extensions"]
    assert isinstance(extensions, dict)
    active = extensions["activeIds"]
    print(f"Extensions：{', '.join(active) if active else '（无）'}")
    workflow = result["workflow"]
    assert isinstance(workflow, dict)
    if workflow.get("enabled"):
        print(
            "Workflow："
            f"Run={workflow.get('runs', 0)}，待执行 {workflow.get('pending', 0)}，"
            f"失败 {workflow.get('failed', 0)}，中断 {workflow.get('interrupted', 0)}"
        )
    else:
        print("Workflow：（未启用）")
    print("需求：")
    features = result["features"]
    assert isinstance(features, list)
    for feature in features:
        progress = feature["progress"]
        print(
            f"- {feature['featureSlug']} [{feature['status']}] "
            f"{progress['completed']}/{progress['total']}，"
            f"验证 {'通过' if feature['verificationPassed'] else '未通过或未执行'}"
        )
    if not features:
        print("- （无）")
    doctor = result["doctor"]
    assert isinstance(doctor, dict)
    print(
        f"Doctor：ERROR={doctor['errors']} WARN={doctor['warnings']} "
        f"INFO={doctor['info']}"
    )
    sources = result.get("contextSources")
    if isinstance(sources, dict):
        print("事实入口：")
        workspace = sources["workspace"]
        print(f"- {workspace['path']}：{'存在' if workspace['exists'] else '缺失'}")
        for repository in sources["repositories"]:
            print(
                f"- {repository['repository']} {repository['path']}："
                f"{'存在' if repository['exists'] else '缺失'}"
            )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="治理仓目录（默认脚本所在项目目录）",
    )
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--context-sources",
        action="store_true",
        help="显示 CONTEXT.md 与仓库 profile 的事实入口",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = status_result(args.root, context_sources=args.context_sources)
        if args.json:
            print(json.dumps(result, ensure_ascii=False))
        else:
            _render_text(result)
        doctor = result["doctor"]
        assert isinstance(doctor, dict)
        return 1 if doctor["errors"] else 0
    except (OSError, RuntimeError, UnicodeError, ValueError, WorkspaceError) as exc:
        if args.json:
            print(
                json.dumps(
                    {
                        "schemaVersion": STATUS_SCHEMA_VERSION,
                        "error": {
                            "code": "WORKSPACE_STATUS_INVALID",
                            "message": str(exc),
                            "field": None,
                            "hint": "处理迁移或修复列出的状态文件后重新运行 status。",
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
