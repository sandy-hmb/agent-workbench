#!/usr/bin/env python3
"""One-shot minimal context brief for an in-progress feature (read-only)."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional, Sequence


sys.dont_write_bytecode = True

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from workspace_local import load_local_settings  # noqa: E402
from workspace_model import (  # noqa: E402
    WorkspaceError,
    is_independent_git,
    load_workspace,
    repository_path,
    resolve_repository,
)
from workspace_status import (  # noqa: E402
    _single_feature_progress,
    plan_progress,
    plan_tasks,
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


def brief_result(root: Path, slug: str | None = None) -> dict[str, object]:
    root = Path(root).resolve()
    status = status_result(root)
    resolved = _resolve_slug(root, status, slug)
    feature = next(item for item in status["features"] if item["featureSlug"] == resolved)
    feature_dir = root / feature["path"]
    stage = _single_feature_progress(feature, mode=str(status["mode"]))
    workspace_model = load_workspace(root) if status["mode"] == "workspace" else None
    pending = [task for completed, task in plan_tasks(feature_dir / FEATURE_FILES["plan"]) if not completed]
    record = verification_record(feature_dir)
    verification_summary = record[:VERIFICATION_SUMMARY_LIMIT] if record is not None else None
    verification_truncated = record is not None and len(record) > VERIFICATION_SUMMARY_LIMIT
    return {
        "featureSlug": resolved,
        "status": feature["status"],
        "repositories": feature["repositories"],
        "branches": feature["branches"],
        "baseBranches": feature["baseBranches"],
        "lastUpdated": feature["lastUpdated"],
        "files": _file_report(feature_dir),
        "artifacts": feature["artifacts"],
        "verificationTail": _verification_tail(feature_dir),
        "blockers": list(status["blockers"]),
        "progress": plan_progress(feature_dir / FEATURE_FILES["plan"]),
        "pendingTasks": pending[:PENDING_TASK_LIMIT],
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


def _render_text(result: dict[str, object]) -> None:
    print(f"{result['featureSlug']}（{result['status']}）")
    print(f"当前 stage：{result['currentStage']}")
    blockers = result["blockers"]
    if blockers:
        print("阻塞：" + ", ".join(blockers))
    progress = result["progress"]
    print(f"计划：{progress['completed']}/{progress['total']}")
    pending = result["pendingTasks"]
    if pending:
        print("未完成项：")
        for task in pending:
            print(f"  {task}")
        if result["summaryTruncated"]["pendingTasks"]:
            print("  （其余未完成项已省略）")
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("slug", nargs="?", help="不传时取活跃指针或唯一的进行中需求")
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = brief_result(args.root, args.slug)
    except (OSError, RuntimeError, UnicodeError, ValueError, WorkspaceError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(result, ensure_ascii=False))
    else:
        _render_text(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
