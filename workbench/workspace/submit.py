#!/usr/bin/env python3
"""Plan and deliver one workspace work item to its configured test branch."""

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


from workbench.work_items.query import resolve_items
from workbench.work_items.commands import delivery
from workbench.work_items.store import item_path, load_state  # noqa: E402
from workbench.workspace.model import (  # noqa: E402
    WorkspaceError,
    effective_branch_policy,
    load_workspace,
    repository_path,
    resolve_repository,
    validate_repository_state,
)
from workbench.identifiers import item_slug as validate_item_slug
from workbench.workspace import submit_attempts


class SubmitError(WorkspaceError):
    def __init__(self, code: str, message: str, details: dict[str, object] | None = None):
        super().__init__(message)
        self.code = code
        self.details = details or {}


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
        lowered = detail.casefold()
        code = "SUBMIT_GIT_FAILED"
        if "ssl_error_syscall" in lowered or "ssl" in lowered and "syscall" in lowered:
            code = "SUBMIT_GIT_SSL_ERROR"
        elif "timed out" in lowered or "timeout" in lowered:
            code = "SUBMIT_GIT_TIMEOUT"
        elif "permission denied" in lowered or "access denied" in lowered:
            code = "SUBMIT_GIT_PERMISSION"
        elif "merge conflict" in lowered or "conflict" in lowered:
            code = "SUBMIT_GIT_MERGE_CONFLICT"
        raise SubmitError(code, f"git {' '.join(arguments)}：{detail}")
    return result.stdout


def _status_paths(repository: Path) -> list[str]:
    output = _git(repository, ["status", "--porcelain=v1", "-z", "--untracked-files=all"])
    fields = output.split("\0")
    paths: list[str] = []
    index = 0
    while index < len(fields):
        record = fields[index]
        index += 1
        if not record:
            continue
        value = record[3:] if len(record) >= 4 else ""
        if value:
            paths.append(value)
        if len(record) >= 2 and any(value in {"R", "C"} for value in record[:2]) and index < len(fields):
            old = fields[index]
            index += 1
            if old:
                paths.append(old)
    return sorted(set(paths))


SENSITIVE_PATH_PARTS = {
    ".env", ".env.local", ".env.production", ".env.development",
    "id_rsa", "id_ed25519", "credentials", "credential", "secret", "secrets",
    "token", "tokens", "password", "passwd", "private-key", "private_key",
}


def _sensitive_path(value: str) -> bool:
    parts = PurePosixPath(value).parts
    names = {part.casefold() for part in parts}
    return bool(names & SENSITIVE_PATH_PARTS) or any(
        part.casefold().endswith((".pem", ".key", ".p12", ".pfx")) for part in parts
    )


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


def _item_artifacts(item: Path) -> list[str]:
    root = item / "artifacts"
    if root.is_symlink():
        raise SubmitError("SUBMIT_ITEM_INVALID", f"交付物目录不安全：{root}")
    if not root.exists():
        return []
    if not root.is_dir():
        raise SubmitError("SUBMIT_ITEM_INVALID", f"交付物目录不安全：{root}")
    result = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise SubmitError("SUBMIT_ITEM_INVALID", f"交付物不允许符号链接：{path}")
        if path.is_file():
            result.append(path.relative_to(item).as_posix())
    return result


def _resolve_item(root: Path, repository: str, branch: str, slug: str) -> tuple[Path, str]:
    matches = resolve_items(root, repository, branch)
    if slug not in {match.slug for match in matches} or len(matches) != 1:
        raise SubmitError(
            "SUBMIT_ITEM_INVALID",
            f"需求与仓库/分支不唯一匹配：{slug} / {repository} / {branch}",
        )
    item = next(match for match in matches if match.slug == slug)
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


def _apply_command(root: Path, payload: dict[str, object]) -> str:
    command = [
        "python3", "scripts/kit.py", "submit", "apply", "--root", str(root),
        "--repository-path", str(payload["repository"]), "--branch", str(payload["branch"]),
        "--item-slug", str(payload["item"]), "--mode", str(payload["mode"]),
    ]
    if payload.get("message") is not None:
        command.extend(("--message", str(payload["message"])))
    for value in payload.get("paths", []):
        command.extend(("--path", str(value)))
    command.extend(("--plan-hash", str(payload["planHash"]), "--json"))
    return shlex.join(command)


def _repair_command(root: Path, repository: str, branch: str, item: str, mode: str,
                    message: str | None, paths: Sequence[str]) -> str:
    payload: dict[str, object] = {
        "repository": repository, "branch": branch, "item": item,
        "mode": mode, "message": message, "paths": list(paths),
        "planHash": "<planHash-from-plan>",
    }
    return _apply_command(root, payload).replace(" --plan-hash '<planHash-from-plan>'", "")


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
    item_slug: str,
    paths: Sequence[str],
    message: str | None,
    mode: str,
) -> dict[str, object]:
    root = root.resolve()
    try:
        validate_item_slug(item_slug, field="itemSlug")
    except ValueError as exc:
        raise SubmitError("SUBMIT_ITEM_INVALID", str(exc)) from exc
    workspace = load_workspace(root)
    repository = resolve_repository(workspace.repositories, repository_name)
    if repository_name != repository.path:
        raise SubmitError(
            "SUBMIT_REPOSITORY_INVALID",
            f"repositoryPath 必须填写已登记业务仓路径 {repository.path}，不能填写别名或仓库名称：{repository_name}",
        )
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
    item, item_status = _resolve_item(root, repository.path, branch, item_slug)
    if mode == "patch" and load_state(item_path(root, item_slug))["delivery"].get(repository.path, {}).get("submission") != "submitted":
        raise SubmitError("SUBMIT_STATUS_INVALID", "测试中补丁要求该仓已有提测记录")
    changed = _status_paths(path)
    sensitive = sorted(value for value in changed if _sensitive_path(value))
    if sensitive:
        raise SubmitError("SUBMIT_SENSITIVE_PATH", "检测到不允许提交的敏感路径：" + ", ".join(sensitive))
    if changed and not message:
        raise SubmitError("SUBMIT_MESSAGE_REQUIRED", "有业务仓改动时必须提供 commit message")
    selected = sorted({_validate_relative_path(path, item) for item in paths})
    if not selected and changed:
        repair = _repair_command(root, repository.path, branch, item_slug, mode, message, changed)
        raise SubmitError(
            "SUBMIT_PATH_REQUIRED",
            "工作树有改动，必须显式提供业务仓 --path；检测到未提交路径："
            + ", ".join(changed) + f"；修复命令：{repair}",
            {"detectedDirtyPaths": changed, "repairCommand": repair},
        )
    outside = sorted(set(changed) - set(selected))
    if outside:
        raise SubmitError("SUBMIT_DIRTY_OUTSIDE_PATHS", "存在未选择的业务仓改动：" + ", ".join(outside))
    if selected and not set(selected).issubset(set(changed)):
        missing = sorted(set(selected) - set(changed))
        raise SubmitError("SUBMIT_PATH_UNCHANGED", "指定路径没有未提交改动：" + ", ".join(missing))
    if message is not None and (not message.strip() or any(char in message for char in "\r\n")):
        raise SubmitError("SUBMIT_MESSAGE_INVALID", "commit message 必须是单行非空文本")
    target = policy.test_target
    assert target is not None
    payload: dict[str, object] = {
        "repository": repository.path,
        "branch": branch,
        "item": item_slug,
        "target": target,
        "paths": selected,
        "message": message,
        "mode": mode,
        "changedFiles": changed,
        "itemArtifacts": _item_artifacts(item),
        "contractVersion": 1,
    }
    payload["planHash"] = _hash(payload)
    payload["attemptId"] = "submit-" + str(payload["planHash"])[:32]
    payload["applyCommand"] = _apply_command(root, payload)
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
    item_slug: str,
    paths: Sequence[str],
    message: str | None,
    mode: str,
    expected_hash: str,
) -> dict[str, object]:
    payload = plan_result(root, repository_name, branch, item_slug, paths, message, mode)
    if payload["planHash"] != expected_hash:
        raise SubmitError("SUBMIT_PLAN_STALE", "提测 plan hash 已变化，请重新 plan")
    workspace = load_workspace(root.resolve())
    repository = resolve_repository(workspace.repositories, repository_name)
    if repository_name != repository.path:
        raise SubmitError(
            "SUBMIT_REPOSITORY_INVALID",
            f"repositoryPath 必须填写已登记业务仓路径 {repository.path}，不能填写别名或仓库名称：{repository_name}",
        )
    path = repository_path(workspace, repository)
    selected = payload["paths"]
    assert isinstance(selected, list)
    attempt_id = str(payload["attemptId"])
    attempt = submit_attempts.create(root, attempt_id, payload)
    phases = attempt["phases"]
    assert isinstance(phases, dict)
    if any(isinstance(row, dict) and row.get("status") != "pending" for row in phases.values()):
        raise SubmitError(
            "SUBMIT_CONTINUE_REQUIRED",
            "该 plan 已开始执行；必须先 reconcile，再使用 continue，不能重复 apply",
            {
                "attemptId": attempt_id,
                "reconcileCommand": f"python3 scripts/kit.py submit reconcile --root {shlex.quote(str(root))} --attempt-id {shlex.quote(attempt_id)} --json",
                "continueCommand": f"python3 scripts/kit.py submit continue --root {shlex.quote(str(root))} --attempt-id {shlex.quote(attempt_id)} --json",
            },
        )

    def phase(name: str, arguments: Sequence[str]) -> str:
        submit_attempts.update(root, attempt_id, name, "running", command=shlex.join(["git", *arguments]))
        try:
            output = _git(path, arguments)
        except SubmitError as exc:
            submit_attempts.update(root, attempt_id, name, "failed", errorCode=exc.code, error=str(exc))
            exc.details.update({
                "attemptId": attempt_id,
                "reconcileCommand": f"python3 scripts/kit.py submit reconcile --root {shlex.quote(str(root))} --attempt-id {shlex.quote(attempt_id)} --json",
                "continueCommand": f"python3 scripts/kit.py submit continue --root {shlex.quote(str(root))} --attempt-id {shlex.quote(attempt_id)} --json",
            })
            raise
        submit_attempts.update(
            root, attempt_id, name, "succeeded",
            branch=_git(path, ["branch", "--show-current"], check=False).strip(),
            head=_git(path, ["rev-parse", "HEAD"], check=False).strip(),
        )
        return output

    if selected:
        submit_attempts.update(root, attempt_id, "commit", "running", paths=selected, message=message)
        try:
            _git(path, ["add", "--", *selected])
            _git(path, ["diff", "--cached", "--check"])
            assert message is not None
            _git(path, ["commit", "-m", message])
        except SubmitError as exc:
            submit_attempts.update(root, attempt_id, "commit", "failed", errorCode=exc.code, error=str(exc))
            exc.details.update({"attemptId": attempt_id})
            raise
        submit_attempts.update(root, attempt_id, "commit", "succeeded", head=_git(path, ["rev-parse", "HEAD"]).strip())
    else:
        submit_attempts.update(root, attempt_id, "commit", "succeeded", skipped=True)
    phase("featurePush", ["push", "-u", "origin", branch])
    target = payload["target"]
    assert isinstance(target, str)
    phase("fetch", ["fetch", "origin", "--prune"])
    _git(path, ["show-ref", "--verify", f"refs/remotes/origin/{target}"])
    local_target = _git(path, ["branch", "--list", target], check=False).strip()
    if local_target:
        _git(path, ["switch", target])
    else:
        _git(path, ["switch", "-c", target, f"origin/{target}"])
    # Bring an untouched local target forward without reset/force operations.
    # A divergent local branch is left intact and must be reconciled by the user.
    if _git(path, ["rev-parse", target], check=False).strip() != _git(path, ["rev-parse", f"origin/{target}"], check=False).strip():
        try:
            _git(path, ["merge", "--ff-only", f"origin/{target}"])
        except SubmitError as exc:
            raise SubmitError("SUBMIT_TARGET_DIVERGED", "本地测试分支与 origin/testTarget 分叉，未覆盖本地提交；请先人工核对") from exc
    phase("testMerge", ["merge", "--no-edit", branch])
    try:
        phase("testPush", ["push", "origin", target])
    except SubmitError:
        if _git(path, ["branch", "--show-current"], check=False).strip() != branch:
            try:
                phase("restoreBranch", ["switch", branch])
            except SubmitError:
                submit_attempts.update(root, attempt_id, "restoreBranch", "unknown", recovery="当前分支仍需人工核对")
        raise
    phase("restoreBranch", ["switch", branch])
    if mode == "formal":
        try:
            state = load_state(item_path(root, item_slug))
            delivery(root, item_slug, {"repositories": {repository.path: {"submission": "submitted", "version": _git(path, ["rev-parse", "HEAD"]).strip(), "evidence": f"push origin {target} succeeded"}}}, expected_revision=state["stateRevision"])
            submit_attempts.update(root, attempt_id, "delivery", "succeeded")
        except (SubmitError, OSError, RuntimeError, ValueError) as exc:
            submit_attempts.update(root, attempt_id, "delivery", "failed", error=str(exc))
            raise SubmitError(
                "SUBMIT_DELIVERY_UNKNOWN",
                f"Git 提测已成功，但 WorkItem 交付事实尚未写入：{exc}",
                {
                    "attemptId": attempt_id,
                    "reconcileCommand": f"python3 scripts/kit.py submit reconcile --root {shlex.quote(str(root))} --attempt-id {shlex.quote(attempt_id)} --json",
                    "continueCommand": f"python3 scripts/kit.py submit continue --root {shlex.quote(str(root))} --attempt-id {shlex.quote(attempt_id)} --json",
                },
            ) from exc
    else:
        submit_attempts.update(root, attempt_id, "delivery", "succeeded", skipped=True)
    return {
        "repository": repository.path,
        "branch": branch,
        "target": target,
        "item": item_slug,
        "status": "submitted" if mode == "formal" else "self-tested",
        "itemArtifacts": payload["itemArtifacts"],
        "attemptId": attempt_id,
    }


def submit_status(root: Path, attempt_id: str) -> dict[str, object]:
    record = submit_attempts.load(Path(root).resolve(), attempt_id)
    return {
        "attemptId": attempt_id,
        "planHash": record["planHash"],
        "phases": record["phases"],
        "updatedAt": record["updatedAt"],
    }


def reconcile_submit(root: Path, attempt_id: str) -> dict[str, object]:
    root = Path(root).resolve()
    record = submit_attempts.load(root, attempt_id)
    plan = record["plan"]
    assert isinstance(plan, dict)
    workspace = load_workspace(root)
    repository = resolve_repository(workspace.repositories, str(plan["repository"]))
    path = repository_path(workspace, repository)
    branch = str(plan["branch"])
    target = str(plan["target"])
    local_head = _git(path, ["rev-parse", "HEAD"], check=False).strip()
    remote_branch = _git(path, ["ls-remote", "--heads", "origin", branch], check=False).split()[0:1]
    remote_target = _git(path, ["ls-remote", "--heads", "origin", target], check=False).split()[0:1]
    observations = {
        "currentBranch": _git(path, ["branch", "--show-current"], check=False).strip() or None,
        "localHead": local_head,
        "remoteFeatureHead": remote_branch[0] if remote_branch else None,
        "remoteTestHead": remote_target[0] if remote_target else None,
        "featurePushConfirmed": bool(remote_branch and remote_branch[0] == local_head),
    }
    phases = record["phases"]
    assert isinstance(phases, dict)
    test_row = phases.get("testPush")
    if isinstance(test_row, dict) and test_row.get("status") in {"failed", "unknown"}:
        merge_head = _git(path, ["rev-parse", target], check=False).strip()
        if remote_target and remote_target[0] == merge_head:
            submit_attempts.update(root, attempt_id, "testPush", "succeeded", reconciled=True, head=merge_head)
            observations["testPushConfirmed"] = True
        else:
            observations["testPushConfirmed"] = False
    return {"attemptId": attempt_id, "observations": observations, **submit_status(root, attempt_id)}


def continue_submit(root: Path, attempt_id: str) -> dict[str, object]:
    root = Path(root).resolve()
    record = submit_attempts.load(root, attempt_id)
    plan = record["plan"]
    assert isinstance(plan, dict)
    phases = record["phases"]
    assert isinstance(phases, dict)
    workspace = load_workspace(root)
    repository = resolve_repository(workspace.repositories, str(plan["repository"]))
    path = repository_path(workspace, repository)
    branch = str(plan["branch"])
    target = str(plan["target"])

    def phase(name: str, arguments: Sequence[str]) -> None:
        row = phases.get(name)
        if isinstance(row, dict) and row.get("status") == "succeeded":
            return
        submit_attempts.update(root, attempt_id, name, "running", command=shlex.join(["git", *arguments]))
        try:
            _git(path, arguments)
        except SubmitError as exc:
            submit_attempts.update(root, attempt_id, name, "failed", errorCode=exc.code, error=str(exc))
            raise
        submit_attempts.update(root, attempt_id, name, "succeeded", head=_git(path, ["rev-parse", "HEAD"], check=False).strip())

    if phases.get("commit", {}).get("status") != "succeeded":
        raise SubmitError("SUBMIT_CONTINUE_UNSAFE", "commit 阶段未确认成功，不能在 continue 中猜测或重复提交")
    phase("featurePush", ["push", "-u", "origin", branch])
    phase("fetch", ["fetch", "origin", "--prune"])
    if _git(path, ["branch", "--show-current"], check=False).strip() != target:
        local_target = _git(path, ["branch", "--list", target], check=False).strip()
        _git(path, ["switch", target] if local_target else ["switch", "-c", target, f"origin/{target}"])
    if _git(path, ["rev-parse", target], check=False).strip() != _git(path, ["rev-parse", f"origin/{target}"], check=False).strip():
        try:
            _git(path, ["merge", "--ff-only", f"origin/{target}"])
        except SubmitError as exc:
            raise SubmitError("SUBMIT_TARGET_DIVERGED", "本地测试分支与远端分叉，未覆盖本地提交；请先人工核对") from exc
    phase("testMerge", ["merge", "--no-edit", branch])
    phase("testPush", ["push", "origin", target])
    phase("restoreBranch", ["switch", branch])
    mode = str(plan["mode"])
    delivery_row = submit_attempts.load(root, attempt_id)["phases"].get("delivery", {})
    if mode == "formal" and delivery_row.get("status") != "succeeded":
        item_slug = str(plan["item"])
        state = load_state(item_path(root, item_slug))
        delivery(
            root,
            item_slug,
            {"repositories": {repository.path: {
                "submission": "submitted",
                "version": _git(path, ["rev-parse", branch]).strip(),
                "evidence": f"push origin {target} succeeded",
            }}},
            expected_revision=state["stateRevision"],
        )
        submit_attempts.update(root, attempt_id, "delivery", "succeeded", reconciled=True)
    elif mode != "formal" and delivery_row.get("status") != "succeeded":
        submit_attempts.update(root, attempt_id, "delivery", "succeeded", skipped=True)
    return submit_status(root, attempt_id)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("plan", "apply"):
        item = commands.add_parser(command, help="预览或执行提测")
        item.add_argument("--root", type=Path, default=Path.cwd())
        item.add_argument("--repository-path", dest="repo", required=True,
                          help="已登记业务仓路径")
        item.add_argument("--branch", required=True)
        item.add_argument("--item-slug", dest="item", required=True,
                          help="WorkItem slug")
        item.add_argument("--path", action="append", default=[])
        item.add_argument("--message")
        item.add_argument("--mode", choices=("formal", "self-test", "patch"), default="formal")
        item.add_argument("--json", action="store_true")
        if command == "apply":
            item.add_argument("--plan-hash", required=True)
    for name in ("status", "reconcile", "continue"):
        command = commands.add_parser(name, help="查看或安全续接分阶段提测")
        command.add_argument("--root", type=Path, default=Path.cwd())
        command.add_argument("--attempt-id", required=True)
        command.add_argument("--json", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "plan":
            result = plan_result(
                args.root,
                args.repo,
                args.branch,
                args.item,
                args.path,
                args.message,
                args.mode,
            )
        elif args.command == "apply":
            result = apply_result(
                args.root,
                args.repo,
                args.branch,
                args.item,
                args.path,
                args.message,
                args.mode,
                args.plan_hash,
            )
        elif args.command == "status":
            result = submit_status(args.root, args.attempt_id)
        elif args.command == "reconcile":
            result = reconcile_submit(args.root, args.attempt_id)
        else:
            result = continue_submit(args.root, args.attempt_id)
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
                **({"legacyCode": "SUBMIT_GIT_FAILED"} if exc.code.startswith("SUBMIT_GIT_") and exc.code != "SUBMIT_GIT_FAILED" else {}),
                "currentBranch": _current_branch(args.root, getattr(args, "repo", "")) if hasattr(args, "repo") else None,
                **exc.details,
            }
        }
        print(json.dumps(error, ensure_ascii=False) if args.json else f"错误：{exc.code}：{exc}", file=sys.stderr)
        return 1
    except (OSError, RuntimeError, UnicodeError, ValueError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
