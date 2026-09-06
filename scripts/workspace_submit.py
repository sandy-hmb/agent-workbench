#!/usr/bin/env python3
"""Plan and deliver one workspace feature to its configured test branch."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import subprocess
import sys
from datetime import date
from pathlib import Path, PurePosixPath
from typing import Sequence


sys.dont_write_bytecode = True

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from feature_context import resolve_features, update_status  # noqa: E402
from workspace_model import (  # noqa: E402
    WorkspaceError,
    effective_branch_policy,
    load_workspace,
    repository_path,
    resolve_repository,
    validate_repository_state,
)


class SubmitError(WorkspaceError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _git(path: Path, arguments: Sequence[str], *, check: bool = True) -> str:
    result = subprocess.run(
        ["git", "-C", str(path), *arguments],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        env=dict(os.environ, GIT_OPTIONAL_LOCKS="0"),
    )
    if check and result.returncode:
        detail = result.stderr.strip() or result.stdout.strip() or "命令失败"
        raise SubmitError("SUBMIT_GIT_FAILED", f"git {' '.join(arguments)}：{detail}")
    return result.stdout


def _status_paths(repository: Path) -> list[str]:
    output = _git(repository, ["status", "--porcelain", "--untracked-files=all"])
    paths = []
    for line in output.splitlines():
        if not line:
            continue
        value = line[3:] if len(line) >= 4 else ""
        if " -> " in value:
            value = value.split(" -> ", 1)[1]
        if value:
            paths.append(value)
    return sorted(set(paths))


def _validate_relative_path(repository: Path, value: str) -> str:
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise SubmitError("SUBMIT_PATH_INVALID", f"路径必须是业务仓内安全相对路径：{value}")
    candidate = repository.joinpath(*path.parts)
    if candidate.is_symlink():
        raise SubmitError("SUBMIT_PATH_INVALID", f"提交路径不允许符号链接：{value}")
    try:
        candidate.resolve(strict=False).relative_to(repository.resolve())
    except (OSError, RuntimeError, ValueError) as exc:
        raise SubmitError("SUBMIT_PATH_INVALID", f"路径越出业务仓：{value}") from exc
    return path.as_posix()


def _feature_artifacts(feature: Path) -> list[str]:
    root = feature / "artifacts"
    if root.is_symlink():
        raise SubmitError("SUBMIT_FEATURE_INVALID", f"交付物目录不安全：{root}")
    if not root.exists():
        return []
    if not root.is_dir():
        raise SubmitError("SUBMIT_FEATURE_INVALID", f"交付物目录不安全：{root}")
    result = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise SubmitError("SUBMIT_FEATURE_INVALID", f"交付物不允许符号链接：{path}")
        if path.is_file():
            result.append(path.relative_to(feature).as_posix())
    return result


def _resolve_feature(root: Path, repository: str, branch: str, feature: str) -> tuple[Path, str]:
    matches = resolve_features(root, repository, branch)
    if feature not in {item.slug for item in matches} or len(matches) != 1:
        raise SubmitError(
            "SUBMIT_FEATURE_INVALID",
            f"需求与仓库/分支不唯一匹配：{feature} / {repository} / {branch}",
        )
    item = next(item for item in matches if item.slug == feature)
    return item.path, item.status


def _operation_in_progress(repository: Path) -> bool:
    git_dir = Path(_git(repository, ["rev-parse", "--git-dir"]).strip())
    if not git_dir.is_absolute():
        git_dir = repository / git_dir
    return any(
        (git_dir / marker).exists()
        for marker in ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-merge", "rebase-apply")
    )


def _hash(payload: dict[str, object]) -> str:
    data = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def _current_branch(root: Path, repository_name: str) -> str | None:
    try:
        workspace = load_workspace(root.resolve())
        repository = resolve_repository(workspace.repositories, repository_name)
        path = repository_path(workspace, repository)
        return _git(path, ["branch", "--show-current"], check=False).strip() or None
    except (OSError, RuntimeError, WorkspaceError):
        return None


def plan_result(
    root: Path,
    repository_name: str,
    branch: str,
    feature_slug: str,
    paths: Sequence[str],
    message: str | None,
    mode: str,
) -> dict[str, object]:
    root = root.resolve()
    workspace = load_workspace(root)
    repository = resolve_repository(workspace.repositories, repository_name)
    if not validate_repository_state(workspace, repository, allow_missing_remote=False):
        raise SubmitError("SUBMIT_REPOSITORY_MISSING", f"业务仓不存在：{repository.path}")
    path = repository_path(workspace, repository)
    current = _git(path, ["branch", "--show-current"]).strip()
    if current != branch:
        raise SubmitError("SUBMIT_BRANCH_MISMATCH", f"当前分支为 {current or 'detached'}，不是 {branch}")
    if _operation_in_progress(path):
        raise SubmitError("SUBMIT_GIT_IN_PROGRESS", "业务仓存在未完成的 Git 操作")
    policy = effective_branch_policy(workspace, repository)
    if policy.test_target is None:
        raise SubmitError("SUBMIT_TARGET_MISSING", f"仓库未配置 testTarget：{repository.path}")
    feature, feature_status = _resolve_feature(root, repository.path, branch, feature_slug)
    if mode == "patch" and feature_status != "testing":
        raise SubmitError("SUBMIT_STATUS_INVALID", "测试中补丁要求需求状态为 testing")
    changed = _status_paths(path)
    selected = sorted({_validate_relative_path(path, item) for item in paths})
    if not selected and changed:
        raise SubmitError("SUBMIT_PATH_REQUIRED", "工作树有改动，必须显式提供业务仓 --path")
    outside = sorted(set(changed) - set(selected))
    if outside:
        raise SubmitError("SUBMIT_DIRTY_OUTSIDE_PATHS", "存在未选择的业务仓改动：" + ", ".join(outside))
    if selected and not set(selected).issubset(set(changed)):
        missing = sorted(set(selected) - set(changed))
        raise SubmitError("SUBMIT_PATH_UNCHANGED", "指定路径没有未提交改动：" + ", ".join(missing))
    if message is not None and (not message.strip() or any(char in message for char in "\r\n")):
        raise SubmitError("SUBMIT_MESSAGE_INVALID", "commit message 必须是单行非空文本")
    if selected and not message:
        raise SubmitError("SUBMIT_MESSAGE_REQUIRED", "有业务仓改动时必须提供 commit message")
    target = policy.test_target
    assert target is not None
    payload: dict[str, object] = {
        "repository": repository.path,
        "branch": branch,
        "feature": feature_slug,
        "target": target,
        "paths": selected,
        "message": message,
        "mode": mode,
        "changedFiles": changed,
        "featureArtifacts": _feature_artifacts(feature),
    }
    payload["planHash"] = _hash(payload)
    payload["confirmation"] = {"category": "remote", "required": True}
    payload["actions"] = [
        *([shlex.join(["git", "add", "--", *selected])] if selected else []),
        *( [shlex.join(["git", "commit", "-m", message])] if selected and message else []),
        shlex.join(["git", "push", "-u", "origin", branch]),
        shlex.join(["git", "fetch", "origin", "--prune"]),
        shlex.join(["git", "switch", "-C", target, f"origin/{target}"]),
        shlex.join(["git", "merge", "--no-edit", branch]),
        shlex.join(["git", "push", "origin", target]),
        shlex.join(["git", "switch", branch]),
    ]
    return payload


def apply_result(
    root: Path,
    repository_name: str,
    branch: str,
    feature_slug: str,
    paths: Sequence[str],
    message: str | None,
    mode: str,
    expected_hash: str,
) -> dict[str, object]:
    payload = plan_result(root, repository_name, branch, feature_slug, paths, message, mode)
    if payload["planHash"] != expected_hash:
        raise SubmitError("SUBMIT_PLAN_STALE", "提测 plan hash 已变化，请重新 plan")
    workspace = load_workspace(root.resolve())
    repository = resolve_repository(workspace.repositories, repository_name)
    path = repository_path(workspace, repository)
    selected = payload["paths"]
    assert isinstance(selected, list)
    if selected:
        _git(path, ["add", "--", *selected])
        _git(path, ["diff", "--cached", "--check"])
        assert message is not None
        _git(path, ["commit", "-m", message])
    _git(path, ["push", "-u", "origin", branch])
    target = payload["target"]
    assert isinstance(target, str)
    _git(path, ["fetch", "origin", "--prune"])
    _git(path, ["show-ref", "--verify", f"refs/remotes/origin/{target}"])
    _git(path, ["switch", "-C", target, f"origin/{target}"])
    merged = False
    try:
        _git(path, ["merge", "--no-edit", branch])
        merged = True
        _git(path, ["push", "origin", target])
    finally:
        if merged and _git(path, ["branch", "--show-current"]).strip() != branch:
            _git(path, ["switch", branch])
    if mode == "formal":
        update_status(root.resolve(), feature_slug, "testing", date.today().isoformat())
    return {
        "repository": repository.path,
        "branch": branch,
        "target": target,
        "feature": feature_slug,
        "status": "testing" if mode == "formal" else "development",
        "featureArtifacts": payload["featureArtifacts"],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("plan", "apply"):
        item = commands.add_parser(command, help="预览或执行提测")
        item.add_argument("--root", type=Path, default=Path.cwd())
        item.add_argument("--repo", required=True)
        item.add_argument("--branch", required=True)
        item.add_argument("--feature", required=True)
        item.add_argument("--path", action="append", default=[])
        item.add_argument("--message")
        item.add_argument("--mode", choices=("formal", "self-test", "patch"), default="formal")
        item.add_argument("--json", action="store_true")
        if command == "apply":
            item.add_argument("--plan-hash", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "plan":
            result = plan_result(
                args.root,
                args.repo,
                args.branch,
                args.feature,
                args.path,
                args.message,
                args.mode,
            )
        else:
            result = apply_result(
                args.root,
                args.repo,
                args.branch,
                args.feature,
                args.path,
                args.message,
                args.mode,
                args.plan_hash,
            )
        print(json.dumps(result, ensure_ascii=False) if args.json else result)
        return 0
    except SubmitError as exc:
        error = {
            "error": {
                "code": exc.code,
                "message": str(exc),
                "field": None,
                "hint": "修复 plan 指出的状态后重新运行 workspace_submit.py plan。",
                "failedAction": exc.code,
                "currentBranch": _current_branch(args.root, args.repo),
            }
        }
        print(json.dumps(error, ensure_ascii=False) if args.json else f"错误：{exc.code}：{exc}", file=sys.stderr)
        return 1
    except (OSError, RuntimeError, UnicodeError, ValueError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
