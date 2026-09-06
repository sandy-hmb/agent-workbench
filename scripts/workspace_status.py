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
from pathlib import Path
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
    workspace_schema_version,
)
from workspace_setup import discover_sibling_repositories  # noqa: E402
from workspace_paths import state_root, workspace_file  # noqa: E402
from workspace_extension import extension_status  # noqa: E402
from workspace_local import load_local_settings  # noqa: E402
from workspace_workflow import status_result as workflow_status  # noqa: E402


CHECKBOX_RE = re.compile(r"^\s*-\s*\[([ xX])\]")
EXECUTION_RECORD_RE = re.compile(r"^## 执行记录 \d{4}-\d{2}-\d{2}\s*$", re.MULTILINE)
SECTION_RE = re.compile(r"^##\s", re.MULTILINE)
VERIFICATION_FIELDS = ("工作目录：", "命令：", "退出状态：", "结果：")
STATUS_SCHEMA_VERSION = 1


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


def plan_tasks(path: Path) -> list[tuple[bool, str]]:
    if path.is_symlink():
        raise ValueError(f"实施计划不允许符号链接：{path}")
    if not path.exists():
        return []
    if not path.is_file():
        raise ValueError(f"实施计划必须是普通文件：{path}")
    return [
        (match.group(1).lower() == "x", line.strip())
        for line in path.read_text(encoding="utf-8").splitlines()
        if (match := CHECKBOX_RE.match(line))
    ]


def plan_progress(path: Path) -> dict[str, int]:
    checked = [completed for completed, _ in plan_tasks(path)]
    return {"completed": sum(checked), "total": len(checked)}


def verification_record(feature: Path) -> str | None:
    """Read the latest execution record without falling back to an older success."""
    path = feature / "testing/verification.md"
    if not path.is_file() or path.is_symlink():
        return None
    text = path.read_text(encoding="utf-8")
    records = list(EXECUTION_RECORD_RE.finditer(text))
    if not records:
        return None
    latest = records[-1]
    next_section = SECTION_RE.search(text, latest.end())
    end = next_section.start() if next_section is not None else len(text)
    record = text[latest.start():end].strip()
    lines = record.splitlines()
    if not all(
        any(line.startswith(f"- {field}") and line[len(field) + 2:].strip() for line in lines)
        for field in VERIFICATION_FIELDS
    ):
        return None
    return record


def verification_passed(record: str | None) -> bool:
    if record is None:
        return False
    for line in record.splitlines():
        if line.startswith("- 退出状态："):
            return line.partition("：")[2].strip() == "0"
    return False


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


def _tracking(feature: Path) -> dict[str, object]:
    for directory in (feature / "design", feature / "plans", feature / "testing"):
        if directory.is_symlink():
            raise ValueError(f"需求记录目录不允许符号链接：{directory}")
    design = feature / "design" / "design.md"
    if design.is_symlink():
        raise ValueError(f"设计文档不允许符号链接：{design}")
    verification = feature / "testing" / "verification.md"
    if verification.is_symlink():
        raise ValueError(f"验证记录不允许符号链接：{verification}")
    record = verification_record(feature)
    return {
        "designExists": design.is_file(),
        "progress": plan_progress(feature / "plans" / "implementation.md"),
        "verificationExists": verification.is_file(),
        "verificationPassed": verification_passed(record),
        "artifacts": artifact_summary(feature),
    }


STAGE_RUNBOOKS = {
    "workspace.init": ".agents/skills/workspace-init/SKILL.md",
    "feature.context": ".agents/skills/workspace-feature-design/SKILL.md",
    "feature.design": ".agents/skills/workspace-feature-design/SKILL.md",
    "feature.implement": "AGENTS.md",
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
    if feature["status"] == "planning":
        if not feature["designExists"]:
            reason = f"需求 {feature['featureSlug']} 已确认，讨论方案设计后再写入设计文档"
        elif progress["total"] == 0:
            reason = f"方案已确认，讨论实施计划并写入可执行任务"
        else:
            reason = f"实施计划已记录；确认进入实现后将需求 {feature['featureSlug']} 更新为 development"
        stage = "feature.design"
    elif progress["total"] == 0:
        stage = "feature.design"
        reason = f"需求 {feature['featureSlug']} 缺少已确认的实施计划"
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
        item.update(_tracking(feature))
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
        item.update(_tracking(feature.path))
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


def status_result(root: Path) -> dict[str, object]:
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
            if workspace_schema_version(root) < VERSION:
                return _migration_required_status(findings)
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="治理仓目录（默认脚本所在项目目录）",
    )
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = status_result(args.root)
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
