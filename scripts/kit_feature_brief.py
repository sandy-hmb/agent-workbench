#!/usr/bin/env python3
"""One-shot minimal context brief for an in-progress feature (read-only)."""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Optional, Sequence


sys.dont_write_bytecode = True

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from workspace_local import load_local_settings  # noqa: E402
from context_measure import estimate_tokens  # noqa: E402
from workspace_model import (  # noqa: E402
    WorkspaceError,
    is_independent_git,
    load_workspace,
    repository_path,
    resolve_repository,
)
from workspace_paths import state_root  # noqa: E402
from workspace_status import (  # noqa: E402
    _single_feature_progress,
    plan_analysis,
    plan_progress,
    status_result,
    verification_record,
)


FEATURE_FILES = {
    "readme": "README.md",
    "requirements": "requirements/requirements.md",
    "design": "design/design.md",
    "plan": "plans/implementation.md",
    "verification": "testing/verification.md",
}
PENDING_TASK_LIMIT = 10
VERIFICATION_SUMMARY_LIMIT = 1200
EXECUTION_PROJECTION_FIELDS = (
    "featureSlug",
    "currentTask",
    "selectedTask",
    "readyTasks",
    "executionDecision",
    "confirmationRequired",
    "taskBlockers",
    "instructionContext",
    "trustedProgress",
    "documentDiagnostics",
)
MARKDOWN_LINK_RE = re.compile(r"\]\(([^)]+)\)")
FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})")
EXPLICIT_ANCHOR_RE = re.compile(r"\bid=[\"']([^\"']+)[\"']")
HEADING_RE = re.compile(r"^#{1,6}\s+(.+?)\s*#*\s*$")
DECISION_HEADING_RE = re.compile(r"^#{1,6}\s+(D\d+)\b", re.IGNORECASE)
DECISION_ANCHOR_RE = re.compile(r"\bid=[\"'](d\d+)[\"']", re.IGNORECASE)
DECISION_REFERENCE_RE = re.compile(r"\bD\d+\b", re.IGNORECASE)


def _resolve_slug(root: Path, status: dict[str, object], slug: str | None) -> str:
    by_slug = {item["featureSlug"]: item for item in status["features"]}
    if slug is not None:
        if slug not in by_slug:
            raise ValueError(f"未找到进行中的需求：{slug}（可能已完成或不存在）")
        return slug
    active = None
    if status["mode"] == "workspace":
        try:
            active = load_local_settings(root, required=False).active_feature
        except WorkspaceError:
            active = None
    if active in by_slug:
        return active
    if len(by_slug) == 1:
        return next(iter(by_slug))
    candidates = "、".join(sorted(by_slug)) if by_slug else "（没有进行中的需求）"
    raise ValueError(f"未指定 slug 且无法确定唯一需求，候选：{candidates}")


def _file_report(feature_dir: Path) -> dict[str, dict[str, object]]:
    report = {}
    for key, relative in FEATURE_FILES.items():
        path = feature_dir / relative
        exists = path.is_file() and not path.is_symlink()
        report[key] = {
            "path": relative,
            "exists": exists,
            "bytes": path.stat().st_size if exists else 0,
        }
    return report


def _verification_tail(feature_dir: Path, limit: int = 10) -> list[str]:
    path = feature_dir / "testing/verification.md"
    if not path.is_file() or path.is_symlink():
        return []
    return path.read_text(encoding="utf-8").splitlines()[-limit:]


def _markdown_lines(path: Path) -> list[tuple[int, str]]:
    visible = []
    fence: tuple[str, int] | None = None
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        match = FENCE_RE.match(line)
        if fence is not None:
            if (
                match is not None
                and match.group(1)[0] == fence[0]
                and len(match.group(1)) >= fence[1]
            ):
                fence = None
            continue
        if match is not None:
            fence = (match.group(1)[0], len(match.group(1)))
            continue
        visible.append((line_number, line))
    return visible


def _heading_anchor(text: str) -> str:
    return re.sub(r"[^\w-]+", "-", text.strip().lower()).strip("-")


def _document_diagnostic(
    code: str, severity: str, path: Path, feature_dir: Path, line: int, message: str
) -> dict[str, object]:
    return {
        "severity": severity,
        "code": code,
        "path": path.relative_to(feature_dir).as_posix(),
        "line": line,
        "message": message,
    }


def _design_attachments(feature_dir: Path) -> list[Path]:
    design_dir = feature_dir / "design"
    if design_dir.is_symlink() or not design_dir.is_dir():
        return []
    return [
        path
        for path in sorted(design_dir.glob("*.md"))
        if path.name != "design.md"
    ]


def _document_sources(feature_dir: Path) -> list[Path]:
    return [
        *(feature_dir / relative for relative in FEATURE_FILES.values()),
        *_design_attachments(feature_dir),
    ]


def _decision_id(value: str) -> str:
    return f"D{int(value[1:])}"


def _main_decisions(path: Path) -> set[str]:
    decisions = set()
    for _, line in _markdown_lines(path):
        if heading := DECISION_HEADING_RE.match(line):
            decisions.add(_decision_id(heading.group(1)))
        decisions.update(_decision_id(value) for value in DECISION_ANCHOR_RE.findall(line))
    return decisions


def _link_diagnostics(feature_dir: Path) -> list[dict[str, object]]:
    diagnostics = []
    features_root = feature_dir.parent.resolve()
    linked_paths: dict[Path, set[Path]] = {}
    for source in _document_sources(feature_dir):
        if not source.exists():
            continue
        if source.is_symlink() or not source.is_file():
            diagnostics.append(
                _document_diagnostic(
                    "DOCUMENT_UNSAFE_PATH",
                    "error",
                    source,
                    feature_dir,
                    1,
                    "文档必须是 feature 目录内的普通文件",
                )
            )
            continue
        linked_paths[source.resolve()] = set()
        for line_number, line in _markdown_lines(source):
            for target in MARKDOWN_LINK_RE.findall(line):
                target = target.strip().strip("<>")
                if not target or "://" in target or target.startswith(("mailto:", "tel:")):
                    continue
                file_part, separator, anchor = target.partition("#")
                candidate = source if not file_part else source.parent / file_part
                try:
                    resolved = candidate.resolve()
                except OSError:
                    resolved = candidate
                if not resolved.is_relative_to(features_root):
                    diagnostics.append(
                        _document_diagnostic(
                            "DOCUMENT_LINK_OUTSIDE_FEATURE",
                            "error",
                            source,
                            feature_dir,
                            line_number,
                            f"本地链接不能离开需求目录集合：{target}",
                        )
                    )
                    continue
                if candidate.is_symlink() or not candidate.is_file():
                    diagnostics.append(
                        _document_diagnostic(
                            "DOCUMENT_MISSING_LINK_TARGET",
                            "error",
                            source,
                            feature_dir,
                            line_number,
                            f"本地链接目标不存在或不安全：{target}",
                        )
                    )
                    continue
                linked_paths[source.resolve()].add(resolved)
                if not separator or not anchor:
                    continue
                target_text = candidate.read_text(encoding="utf-8")
                explicit = set(EXPLICIT_ANCHOR_RE.findall(target_text))
                if anchor in explicit:
                    continue
                headings = {
                    _heading_anchor(match.group(1))
                    for candidate_line in target_text.splitlines()
                    if (match := HEADING_RE.match(candidate_line))
                }
                if anchor in headings:
                    diagnostics.append(
                        _document_diagnostic(
                            "DOCUMENT_UNVERIFIED_HEADING_ANCHOR",
                            "warning",
                            source,
                            feature_dir,
                            line_number,
                            f"链接使用历史标题锚点，未验证其渲染器兼容性：{target}",
                        )
                    )
                    continue
                diagnostics.append(
                    _document_diagnostic(
                        "DOCUMENT_MISSING_ANCHOR",
                        "error",
                        source,
                        feature_dir,
                        line_number,
                        f"本地链接锚点不存在：{target}",
                    )
                )
    design = feature_dir / "design/design.md"
    attachments = _design_attachments(feature_dir)
    if design.is_file() and not design.is_symlink():
        design_path = design.resolve()
        for attachment in attachments:
            if attachment.is_symlink() or not attachment.is_file():
                continue
            attachment_path = attachment.resolve()
            if attachment_path not in linked_paths.get(design_path, set()):
                diagnostics.append(
                    _document_diagnostic(
                        "DESIGN_ATTACHMENT_MISSING_MAIN_LINK",
                        "error",
                        design,
                        feature_dir,
                        1,
                        f"主设计必须链接设计附件：{attachment.name}",
                    )
                )
            if design_path not in linked_paths.get(attachment_path, set()):
                diagnostics.append(
                    _document_diagnostic(
                        "DESIGN_ATTACHMENT_MISSING_BACKLINK",
                        "error",
                        attachment,
                        feature_dir,
                        1,
                        "设计附件必须链接回 design.md",
                    )
                )
        decisions = _main_decisions(design)
        if attachments and not decisions:
            diagnostics.append(
                _document_diagnostic(
                    "DESIGN_MAIN_DECISION_MISSING",
                    "error",
                    design,
                    feature_dir,
                    1,
                    "存在设计附件，但主设计没有可解析的 D 决策定义",
                )
            )
        plan = feature_dir / FEATURE_FILES["plan"]
        if plan.is_file() and not plan.is_symlink():
            for line_number, line in _markdown_lines(plan):
                for reference in DECISION_REFERENCE_RE.findall(line):
                    if _decision_id(reference) not in decisions:
                        diagnostics.append(
                            _document_diagnostic(
                                "PLAN_UNKNOWN_DESIGN_DECISION",
                                "error",
                                plan,
                                feature_dir,
                                line_number,
                                f"计划引用的设计决策不在主设计中：{reference}",
                            )
                        )
    return diagnostics


def _recent_commits(
    root: Path,
    mode: str,
    workspace_model: object,
    repo: str,
    branch: str,
    limit: int = 5,
) -> dict[str, object]:
    if mode == "maintenance":
        git_root = root
    else:
        repository = resolve_repository(workspace_model.repositories, repo)
        git_root = repository_path(workspace_model, repository)
        if not is_independent_git(git_root):
            return {"repository": repo, "branch": branch, "commits": [], "note": "仓库未拉取"}
    result = subprocess.run(
        ["git", "-C", str(git_root), "log", "--oneline", f"-{limit}", branch],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        env=dict(os.environ, GIT_OPTIONAL_LOCKS="0"),
    )
    commits = result.stdout.splitlines() if result.returncode == 0 else []
    return {"repository": repo, "branch": branch, "commits": commits}


def _task_summary(task: dict[str, object]) -> dict[str, object]:
    result = {
        "id": task["id"],
        "title": task["title"],
        "path": FEATURE_FILES["plan"],
        "startLine": task["startLine"],
        "endLine": task["endLine"],
        "dependencies": task["dependencies"],
        "references": task["references"],
    }
    if task.get("repository") is not None:
        result.update(
            {
                "repository": task["repository"],
                "validationKind": task["validationKind"],
                "deliverables": task["deliverables"],
            }
        )
    return result


def _task_state(
    analysis: dict[str, object],
    task_evidence: list[dict[str, object]],
) -> tuple[dict[str, object] | None, list[dict[str, object]], list[str], bool]:
    diagnostics = analysis["diagnostics"]
    if any(item["severity"] == "error" for item in diagnostics):
        return None, [], ["计划存在结构错误，修复后才能选择可执行任务"], True
    tasks = analysis["tasks"]
    evidence_contract = analysis["completionPolicy"] == "task-evidence-v1"
    trusted = {
        result["taskId"]
        for result in task_evidence
        if result.get("trusted") is True
    }
    completed_ids = {
        task["id"]
        for task in tasks
        if task["id"] is not None
        and task["completed"]
        and (not evidence_contract or task["id"] in trusted)
    }
    untrusted = [
        task
        for task in tasks
        if evidence_contract and task["completed"] and task["id"] not in trusted
    ]
    if untrusted:
        blockers = [
            f"任务 {task['id']} 已勾选但完成证据无效"
            for task in untrusted
        ]
        return _task_summary(untrusted[0]), [], blockers, True
    ready = []
    blockers = []
    for task in tasks:
        if task["completed"]:
            continue
        if task["id"] is None:
            ready.append(_task_summary(task))
            continue
        missing = [dependency for dependency in task["dependencies"] if dependency not in completed_ids]
        if missing:
            blockers.append(f"任务 {task['id']} 等待：{', '.join(missing)}")
            continue
        ready.append(_task_summary(task))
    return (ready[0] if ready else None), ready, blockers, False


def _execution_decision(
    stage: dict[str, object],
    progress: dict[str, int],
    current_task: dict[str, object] | None,
    completion_blocked: bool = False,
) -> str:
    if completion_blocked:
        return "BLOCKED"
    current_stage = stage["currentStage"]
    if current_stage == "feature.implement":
        return "RUN" if current_task is not None else "BLOCKED"
    if (
        progress["total"] > 0
        and progress["completed"] == progress["total"]
        and current_stage in {"feature.verify", "feature.submit-test", "feature.complete"}
    ):
        return "COMPLETE"
    return "BLOCKED"


def _selected_task(
    task_id: str | None, analysis: dict[str, object], plan: Path
) -> dict[str, object] | None:
    if task_id is None:
        return None
    matches = [task for task in analysis["tasks"] if task["id"] == task_id]
    if not matches:
        raise ValueError(f"计划中没有任务：{task_id}")
    if len(matches) != 1:
        raise ValueError(f"计划中任务编号重复，无法展开：{task_id}")
    task = matches[0]
    text = plan.read_text(encoding="utf-8").splitlines()
    return {
        **_task_summary(task),
        "body": "\n".join(text[task["startLine"] - 1 : task["endLine"]]),
    }


def _instruction_diagnostic(code: str, path: str, message: str) -> dict[str, object]:
    return {
        "severity": "error",
        "code": code,
        "path": path,
        "line": 1,
        "message": message,
    }


def _ordinary_file(base: Path, path: Path) -> bool:
    try:
        relative = path.relative_to(base)
    except ValueError:
        return False
    current = base
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            return False
    return path.is_file()


def _scoped_instruction_paths(
    root: Path, repository_root: Path, task: dict[str, object], excluded: set[Path]
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    paths = []
    diagnostics = []
    for deliverable in task.get("deliverables", []):
        target = repository_root / str(deliverable["path"])
        parent = target.parent
        try:
            relative_parent = parent.relative_to(repository_root)
        except ValueError:
            diagnostics.append(
                _instruction_diagnostic(
                    "INSTRUCTION_SCOPE_UNSAFE",
                    str(deliverable["path"]),
                    "任务交付路径离开目标仓，无法解析适用规范",
                )
            )
            continue
        current = repository_root
        candidates = [repository_root / "AGENTS.md"]
        unsafe = False
        for part in relative_parent.parts:
            current /= part
            if current.is_symlink():
                unsafe = True
                break
            candidates.append(current / "AGENTS.md")
        if unsafe:
            diagnostics.append(
                _instruction_diagnostic(
                    "INSTRUCTION_SCOPE_UNSAFE",
                    str(deliverable["path"]),
                    "任务交付路径包含符号链接，无法解析适用规范",
                )
            )
            continue
        for candidate in candidates:
            if candidate.resolve() in excluded or not _ordinary_file(repository_root, candidate):
                continue
            relative = Path(os.path.relpath(candidate, root)).as_posix()
            if relative not in {item["path"] for item in paths}:
                paths.append(
                    _instruction_rule(
                        root, 4, "scoped", relative, str(task["repository"])
                    )
                )
    return paths, diagnostics


def _instruction_rule(
    root: Path,
    level: int,
    scope: str,
    path: str,
    repository: str | None = None,
) -> dict[str, object]:
    rule: dict[str, object] = {"level": level, "scope": scope, "path": path}
    if repository is not None:
        rule["repository"] = repository
    rule["estTokens"] = estimate_tokens(
        (root / path).read_text(encoding="utf-8")
    )["estTokens"]
    return rule


def _instruction_context(
    root: Path,
    mode: str,
    workspace_model: object,
    task: dict[str, object] | None,
) -> tuple[dict[str, object] | None, list[dict[str, object]]]:
    if task is None:
        return None, []
    repository_name = task.get("repository")
    if not isinstance(repository_name, str):
        return None, []
    diagnostics = []
    kit_agents = root / "AGENTS.md"
    rules = []
    if _ordinary_file(root, kit_agents):
        rules.append(_instruction_rule(root, 1, "kit", "AGENTS.md"))
    else:
        diagnostics.append(
            _instruction_diagnostic(
                "INSTRUCTION_SOURCE_MISSING",
                "AGENTS.md",
                "Kit 根规范入口不存在或不安全",
            )
        )
    if mode == "maintenance":
        scoped, scoped_diagnostics = _scoped_instruction_paths(
            root, root, task, {kit_agents.resolve()}
        )
        diagnostics.extend(scoped_diagnostics)
        return {"policy": "monotonic-narrowing", "rules": [*rules, *scoped]}, diagnostics

    workspace_path = ".workspace/AGENTS.md"
    workspace_agents = root / workspace_path
    if _ordinary_file(root, workspace_agents):
        rules.append(_instruction_rule(root, 2, "workspace", workspace_path))
    else:
        diagnostics.append(
            _instruction_diagnostic(
                "INSTRUCTION_SOURCE_MISSING",
                workspace_path,
                f"工作区规范入口不存在或不安全：{workspace_path}",
            )
        )

    repository = resolve_repository(workspace_model.repositories, repository_name)
    repository_root = repository_path(workspace_model, repository)
    source = (
        repository_root / repository.source_instruction
        if repository.source_instruction is not None
        else None
    )
    source_path = (
        Path(os.path.relpath(source, root)).as_posix() if source is not None else None
    )
    if source is not None and not _ordinary_file(repository_root, source):
        diagnostics.append(
            _instruction_diagnostic(
                "INSTRUCTION_SOURCE_MISSING",
                source_path or repository.source_instruction,
                f"仓库规范入口不存在或不安全：{repository.source_instruction}",
            )
        )
    elif source_path is not None:
        rules.append(
            _instruction_rule(root, 3, "repository", source_path, repository_name)
        )
    excluded = {source.resolve()} if source is not None else set()
    scoped, scoped_diagnostics = _scoped_instruction_paths(
        root, repository_root, task, excluded
    )
    diagnostics.extend(scoped_diagnostics)
    return {"policy": "monotonic-narrowing", "rules": [*rules, *scoped]}, diagnostics


def brief_result(
    root: Path,
    slug: str | None = None,
    task_id: str | None = None,
    *,
    execution: bool = False,
) -> dict[str, object]:
    root = Path(root).resolve()
    status = status_result(root)
    resolved = _resolve_slug(root, status, slug)
    feature = next(item for item in status["features"] if item["featureSlug"] == resolved)
    feature_dir = root / feature["path"]
    stage = _single_feature_progress(feature, mode=str(status["mode"]))
    workspace_model = load_workspace(root) if status["mode"] == "workspace" else None
    plan = feature_dir / FEATURE_FILES["plan"]
    analysis = plan_analysis(plan, repositories=set(feature["repositories"]))
    pending = [task["line"] for task in analysis["tasks"] if not task["completed"]]
    task_evidence = list(feature.get("taskEvidence", []))
    current_task, ready_tasks, task_blockers, completion_blocked = _task_state(
        analysis, task_evidence
    )
    progress = plan_progress(plan)
    record = verification_record(feature_dir)
    verification_summary = record[:VERIFICATION_SUMMARY_LIMIT] if record is not None else None
    verification_truncated = record is not None and len(record) > VERIFICATION_SUMMARY_LIMIT
    selected_task = _selected_task(task_id, analysis, plan)
    instruction_context, instruction_diagnostics = _instruction_context(
        root, str(status["mode"]), workspace_model, selected_task
    )
    result = {
        "featureSlug": resolved,
        "status": feature["status"],
        "repositories": feature["repositories"],
        "branches": feature["branches"],
        "baseBranches": feature["baseBranches"],
        "lastUpdated": feature["lastUpdated"],
        "files": _file_report(feature_dir),
        "artifacts": feature["artifacts"],
        "documentReviews": feature["documentReviews"],
        "documentDiagnostics": [
            *feature["documentDiagnostics"],
            *_link_diagnostics(feature_dir),
            *instruction_diagnostics,
        ],
        "verificationTail": _verification_tail(feature_dir),
        "blockers": [
            blocker
            for blocker in status["blockers"]
            if slug is None or blocker not in {"MULTIPLE_ACTIVE_FEATURES", "ACTIVE_FEATURE_INVALID"}
        ],
        "progress": progress,
        "pendingTasks": pending[:PENDING_TASK_LIMIT],
        "currentTask": current_task,
        "taskBlockers": task_blockers,
        "selectedTask": selected_task,
        "verificationSummary": verification_summary,
        "summaryTruncated": {
            "pendingTasks": len(pending) > PENDING_TASK_LIMIT,
            "verificationSummary": verification_truncated,
        },
        "currentStage": stage["currentStage"],
        "nextActions": stage["nextActions"],
        "recentCommits": [
            _recent_commits(root, status["mode"], workspace_model, repo, branch)
            for repo, branch in feature["branches"]
        ],
    }
    if analysis["completionPolicy"] == "task-evidence-v1":
        result.update(
            {
                "completionPolicy": analysis["completionPolicy"],
                "trustedProgress": feature["trustedProgress"],
                "taskEvidence": task_evidence,
            }
        )
    if instruction_context is not None:
        result["instructionContext"] = instruction_context
    if execution:
        result.update(
            {
                "readyTasks": [task["id"] or task["title"] for task in ready_tasks],
                "executionDecision": _execution_decision(
                    stage, progress, current_task, completion_blocked
                ),
                "confirmationRequired": stage["confirmation"]["required"],
            }
        )
    return result


def _render_text(result: dict[str, object]) -> None:
    print(f"{result['featureSlug']}（{result['status']}）")
    print(f"当前 stage：{result['currentStage']}")
    blockers = result["blockers"]
    if blockers:
        print("阻塞：" + ", ".join(blockers))
    progress = result["progress"]
    print(f"计划：{progress['completed']}/{progress['total']}")
    trusted = result.get("trustedProgress")
    if trusted is not None and trusted["applicable"]:
        print(f"可信进度：{trusted['completed']}/{trusted['total']}")
    if "executionDecision" in result:
        print(f"执行决策：{result['executionDecision']}")
        print(f"需要确认：{'是' if result['confirmationRequired'] else '否'}")
    pending = result["pendingTasks"]
    if pending:
        print("未完成项：")
        for task in pending:
            print(f"  {task}")
        if result["summaryTruncated"]["pendingTasks"]:
            print("  （其余未完成项已省略）")
    current_task = result["currentTask"]
    if current_task is not None:
        print(
            f"当前任务：{current_task['id'] or '未编号'} {current_task['title']}"
            f"（{current_task['path']}:{current_task['startLine']}）"
        )
    ready_tasks = result.get("readyTasks", [])
    if ready_tasks:
        print("可执行队列：" + "、".join(str(task) for task in ready_tasks))
    summary = result["verificationSummary"]
    if summary is not None:
        print("最近有效验证记录：")
        for line in summary.splitlines():
            print(f"  {line}")
        if result["summaryTruncated"]["verificationSummary"]:
            print("  （记录已截断）")
    files = result["files"]
    flags = " ".join(
        f"{key}={'有' if info['exists'] else '缺'}" for key, info in files.items()
    )
    print(f"文档：{flags}")
    print(f"artifacts：{len(result['artifacts'])} 个")
    print("按阶段生成：requirements/requirements.md、design/design.md、plans/implementation.md、testing/verification.md")
    for action in result["nextActions"]:
        print(f"下一步：{action['runbook']}（{action['reason']}）")
    for entry in result["recentCommits"]:
        print(f"{entry['repository']} -> {entry['branch']}：")
        for line in entry["commits"][:3]:
            print(f"  {line}")
        if entry.get("note"):
            print(f"  ({entry['note']})")


def _project(result: dict[str, object], projection: str) -> dict[str, object]:
    """按投影裁剪输出；full 返回原结果，不改变 brief_result 的形状。"""
    if projection != "execution":
        return result
    projected = {
        field: result[field] for field in EXECUTION_PROJECTION_FIELDS if field in result
    }
    selected = projected.get("selectedTask")
    current = projected.get("currentTask")
    if (
        isinstance(selected, dict)
        and isinstance(current, dict)
        and selected.get("id") is not None
        and selected.get("id") == current.get("id")
    ):
        # 展开的就是当前任务时，currentTask 是 selectedTask 的摘要子集，省略无信息损失。
        projected.pop("currentTask")
    diagnostics = projected.get("documentDiagnostics")
    if isinstance(diagnostics, list):
        projected["documentDiagnostics"] = [
            item for item in diagnostics if item.get("severity") == "error"
        ]
    return projected


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("slug", nargs="?", help="不传时取活跃指针或唯一的进行中需求")
    parser.add_argument("--task", dest="task_id", help="展开指定编号任务的正文与引用")
    parser.add_argument("--check", action="store_true", help="检查已有文档的本地结构与引用")
    parser.add_argument("--execution", action="store_true", help="补充连续执行决策与可执行队列")
    parser.add_argument(
        "--projection",
        choices=("full", "execution"),
        default="full",
        help="execution 只保留执行循环消费的字段",
    )
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = brief_result(args.root, args.slug, args.task_id, execution=args.execution)
    except (OSError, RuntimeError, UnicodeError, ValueError, WorkspaceError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    projected = _project(result, args.projection)
    if args.json:
        print(json.dumps(projected, ensure_ascii=False))
    else:
        _render_text(result)
    if args.check and any(item["severity"] == "error" for item in result["documentDiagnostics"]):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
