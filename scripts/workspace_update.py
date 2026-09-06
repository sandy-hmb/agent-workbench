#!/usr/bin/env python3
"""Plan and apply a compatibility-checked public Kit update."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path, PurePosixPath
from typing import Mapping, Sequence


sys.dont_write_bytecode = True


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from extension_model import ExtensionError, load_manifest  # noqa: E402
from workspace_extension import _read_lock, extension_sources  # noqa: E402
from workspace_model import WorkspaceError  # noqa: E402
from workspace_paths import extensions_root  # noqa: E402


MANIFEST_PATH = "upgrades/manifest.json"
CHANGELOG_PATH = "CHANGELOG.md"
SEMVER_RE = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
VERSION_HEADING_RE = re.compile(r"^## (\d+\.\d+\.\d+)\b")
MANIFEST_REQUIRED_FIELDS = frozenset({"schemaVersion", "kitVersion", "extensionActions"})
MANIFEST_ALLOWED_FIELDS = MANIFEST_REQUIRED_FIELDS | {"manualSteps"}
MANUAL_STEP_FIELDS = frozenset({"sinceVersion", "summary", "runbook"})
DIRTY_FILE_LIMIT = 10


class UpdateError(WorkspaceError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _error(code: str, message: str) -> UpdateError:
    return UpdateError(code, message)


def _root(root: Path) -> Path:
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise _error("UPDATE_INVALID", f"治理仓必须是普通目录：{root}")
    try:
        root = root.resolve()
    except (OSError, RuntimeError) as exc:
        raise _error("UPDATE_INVALID", f"治理仓不可解析：{root}") from exc
    top = _git(root, ["rev-parse", "--show-toplevel"]).strip()
    if Path(top).resolve() != root:
        raise _error("UPDATE_INVALID", "更新必须在公共 Kit Git 根目录执行")
    return root


def _git_result(root: Path, arguments: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(root), *arguments],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
    )


def _git(root: Path, arguments: Sequence[str]) -> str:
    result = _git_result(root, arguments)
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip() or "命令失败"
        raise _error("UPDATE_GIT_FAILED", f"git {' '.join(arguments)}：{detail}")
    return result.stdout


def _hash(value: Mapping[str, object]) -> str:
    content = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _tracking_target(root: Path) -> tuple[str, str]:
    remote = _git(root, ["symbolic-ref", "--short", "refs/remotes/origin/HEAD"]).strip()
    if not remote.startswith("origin/") or remote == "origin/":
        raise _error("UPDATE_TARGET_INVALID", "无法确定 origin 默认分支")
    upstream = _git(root, ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"]).strip()
    if upstream != remote:
        raise _error("UPDATE_TARGET_INVALID", f"当前分支上游必须是 {remote}，实际为 {upstream or '无'}")
    branch = _git(root, ["branch", "--show-current"]).strip()
    if not branch:
        raise _error("UPDATE_TARGET_INVALID", "当前处于 detached HEAD，无法安全更新")
    return branch, remote


def _validate_relative_path(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise _error("UPDATE_MANIFEST_INVALID", "目标升级清单路径字段无效")
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise _error("UPDATE_MANIFEST_INVALID", "目标升级清单路径字段无效")
    return value


def _validate_manual_steps(value: object) -> list[dict[str, str]]:
    if not isinstance(value, list):
        raise _error("UPDATE_MANIFEST_INVALID", "目标升级清单 manualSteps 必须是数组")
    steps: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict) or set(item) != MANUAL_STEP_FIELDS:
            raise _error("UPDATE_MANIFEST_INVALID", "目标升级清单 manualSteps 条目字段无效")
        since_version = item["sinceVersion"]
        summary = item["summary"]
        if (
            not isinstance(since_version, str)
            or not SEMVER_RE.fullmatch(since_version)
            or not isinstance(summary, str)
            or not summary
        ):
            raise _error("UPDATE_MANIFEST_INVALID", "目标升级清单 manualSteps 条目值无效")
        runbook = _validate_relative_path(item["runbook"])
        steps.append({"sinceVersion": since_version, "summary": summary, "runbook": runbook})
    return steps


def _upgrade_manifest(root: Path, target: str) -> dict[str, object]:
    result = _git_result(root, ["show", f"{target}:{MANIFEST_PATH}"])
    if result.returncode:
        if not any(
            marker in result.stderr
            for marker in ("does not exist in", "exists on disk, but not in")
        ):
            detail = result.stderr.strip() or result.stdout.strip() or "命令失败"
            raise _error("UPDATE_GIT_FAILED", f"无法读取目标升级清单：{detail}")
        return {
            "schemaVersion": 1,
            "kitVersion": None,
            "extensionActions": {
                "minimumApiVersion": 1,
                "requiredFields": [],
                "migration": None,
            },
        }
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise _error("UPDATE_MANIFEST_INVALID", "目标升级清单不是有效 JSON") from exc
    if (
        not isinstance(value, dict)
        or not MANIFEST_REQUIRED_FIELDS <= set(value)
        or not set(value) <= MANIFEST_ALLOWED_FIELDS
    ):
        raise _error("UPDATE_MANIFEST_INVALID", "目标升级清单字段无效")
    if value["schemaVersion"] != 1 or not isinstance(value["kitVersion"], str) or not SEMVER_RE.fullmatch(value["kitVersion"]):
        raise _error("UPDATE_MANIFEST_INVALID", "目标升级清单版本无效")
    actions = value["extensionActions"]
    if not isinstance(actions, dict) or set(actions) != {"minimumApiVersion", "requiredFields", "migration"}:
        raise _error("UPDATE_MANIFEST_INVALID", "目标升级清单 Action 规则无效")
    minimum = actions["minimumApiVersion"]
    fields = actions["requiredFields"]
    migration = actions["migration"]
    if (
        type(minimum) is not int
        or minimum < 1
        or not isinstance(fields, list)
        or not all(isinstance(item, str) and item for item in fields)
        or migration is not None and (not isinstance(migration, str) or not migration)
    ):
        raise _error("UPDATE_MANIFEST_INVALID", "目标升级清单 Action 值无效")
    if migration is not None:
        _validate_relative_path(migration)
    if "manualSteps" in value:
        value["manualSteps"] = _validate_manual_steps(value["manualSteps"])
    return value


def _active_extension_ids(root: Path) -> set[str]:
    try:
        lock = _read_lock(root)
    except Exception as exc:
        raise _error("UPDATE_EXTENSION_INVALID", str(exc)) from exc
    extensions = lock["extensions"]
    assert isinstance(extensions, list)
    return {
        item["id"]
        for item in extensions
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }


def _extension_upgrades(root: Path, manifest: Mapping[str, object]) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    action_rules = manifest["extensionActions"]
    assert isinstance(action_rules, dict)
    minimum = action_rules["minimumApiVersion"]
    fields = action_rules["requiredFields"]
    migration = action_rules["migration"]
    assert isinstance(minimum, int) and isinstance(fields, list) and isinstance(migration, (str, type(None)))
    try:
        sources = extension_sources(root)
        active = _active_extension_ids(root)
    except Exception as exc:
        raise _error("UPDATE_EXTENSION_INVALID", str(exc)) from exc
    blockers: list[dict[str, object]] = []
    for extension_id in sorted(active):
        try:
            extension = load_manifest(
                extensions_root(root) / extension_id / "workspace-extension.json"
            )
        except ExtensionError as exc:
            raise _error("UPDATE_EXTENSION_INVALID", str(exc)) from exc
        source = sources.get(extension_id, {}).get("source")
        for action in extension.actions:
            if action.api_version >= minimum:
                continue
            item = {
                "code": "EXTENSION_ACTION_UPGRADE_REQUIRED",
                "extensionId": extension_id,
                "action": action.action,
                "installedPath": str(extension.root.relative_to(root)),
                "source": source,
                "apiVersion": action.api_version,
                "requiredApiVersion": minimum,
                "missingFields": list(fields),
                "migration": migration,
            }
            blockers.append(item)
    return blockers, []


def _changelog_section_since(text: str, since_version: str | None) -> str | None:
    lines = text.splitlines()
    start = next((index for index, line in enumerate(lines) if line.startswith("## ")), None)
    if start is None:
        return None
    end = len(lines)
    for index in range(start, len(lines)):
        match = VERSION_HEADING_RE.match(lines[index])
        if match and since_version and match.group(1) == since_version:
            end = index
            break
    if not any(line.strip() for line in lines[start + 1:end]):
        return None
    return "\n".join(lines[start:end]).strip()


def _target_changelog_section(root: Path, target: str, current_version: str) -> str | None:
    result = _git_result(root, ["show", f"{target}:{CHANGELOG_PATH}"])
    if result.returncode:
        return None
    return _changelog_section_since(result.stdout, current_version)


def _semver_tuple(value: str) -> tuple[int, int, int]:
    return tuple(int(part) for part in value.split("."))  # 调用前已由 SEMVER_RE 校验格式


def _applicable_manual_steps(
    manifest: Mapping[str, object], current_version: str
) -> list[dict[str, str]]:
    steps = manifest.get("manualSteps") or []
    assert isinstance(steps, list)
    current = _semver_tuple(current_version)
    kit_version = manifest.get("kitVersion")
    upper = _semver_tuple(kit_version) if isinstance(kit_version, str) else None
    applicable = []
    for step in steps:
        since = _semver_tuple(step["sinceVersion"])
        if since > current and (upper is None or since <= upper):
            applicable.append(step)
    return applicable


def plan_result(root: Path) -> dict[str, object]:
    root = _root(root)
    status = _git(root, ["status", "--porcelain", "--untracked-files=all"])
    dirty = [line for line in status.splitlines() if line]
    if dirty:
        paths = [line[3:] for line in dirty]
        total = len(paths)
        shown = "、".join(paths[:DIRTY_FILE_LIMIT])
        suffix = (
            f"（前 {DIRTY_FILE_LIMIT} 个，共 {total} 个）"
            if total > DIRTY_FILE_LIMIT
            else f"（共 {total} 个）"
        )
        raise _error("UPDATE_DIRTY", f"公共 Kit 工作树包含未提交文件{suffix}：{shown}")
    branch, target = _tracking_target(root)
    _git(root, ["fetch", "origin", "--tags"])
    current = _git(root, ["rev-parse", "HEAD"]).strip()
    target_commit = _git(root, ["rev-parse", target]).strip()
    if current == target_commit:
        return {
            "currentCommit": current,
            "targetCommit": target_commit,
            "branch": branch,
            "upToDate": True,
            "blocked": False,
            "blockers": [],
            "notices": [],
        }
    if _git_result(root, ["merge-base", "--is-ancestor", "HEAD", target]).returncode:
        raise _error("UPDATE_NOT_FAST_FORWARD", "目标提交不能 fast-forward 到当前工作树")
    manifest = _upgrade_manifest(root, target)
    blockers, notices = _extension_upgrades(root, manifest)
    commits = [line for line in _git(root, ["log", "--oneline", f"HEAD..{target}"]).splitlines() if line]
    result: dict[str, object] = {
        "currentCommit": current,
        "targetCommit": target_commit,
        "branch": branch,
        "upToDate": False,
        "kitVersion": manifest["kitVersion"],
        "commits": commits,
        "blocked": bool(blockers),
        "blockers": blockers,
        "notices": notices,
    }
    current_version = (root / "VERSION").read_text(encoding="utf-8").strip()
    changelog = _target_changelog_section(root, target, current_version)
    if changelog:
        result["changelog"] = changelog
    manual_steps = _applicable_manual_steps(manifest, current_version)
    if manual_steps:
        result["manualSteps"] = manual_steps
    result["planHash"] = _hash(result)
    if not blockers:
        result["applyCommand"] = shlex.join(
            [
                "python3", "scripts/workspace_update.py", "apply", "--root", str(root),
                "--plan-hash", str(result["planHash"]), "--json",
            ]
        )
    return result


def apply_result(root: Path, expected_hash: str) -> dict[str, object]:
    if not isinstance(expected_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_hash):
        raise _error("UPDATE_PLAN_STALE", "plan hash 必须是 64 位小写十六进制")
    plan = plan_result(root)
    if plan.get("upToDate"):
        return {"updated": False, "commit": plan["currentCommit"], "doctor": []}
    if plan["blocked"]:
        raise _error("UPDATE_BLOCKED", "本地 Extension 需要升级后才能更新 Kit")
    if plan["planHash"] != expected_hash:
        raise _error("UPDATE_PLAN_STALE", "更新计划已变化，请重新运行 plan")
    root = _root(root)
    _git(root, ["pull", "--ff-only"])
    from workspace_doctor import REMEDIATIONS, audit

    doctor = [
        {
            "level": item.level,
            "code": item.code,
            "message": item.message,
            "remediation": REMEDIATIONS[item.code].as_dict() if item.code in REMEDIATIONS else None,
        }
        for item in audit(root, verbose=True)
    ]
    result = {"updated": True, "commit": _git(root, ["rev-parse", "HEAD"]).strip(), "doctor": doctor}
    if "manualSteps" in plan:
        result["manualSteps"] = plan["manualSteps"]
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    plan = commands.add_parser("plan", help="检查目标更新和本地 Extension 兼容性")
    plan.add_argument("--root", type=Path, default=Path.cwd())
    plan.add_argument("--json", action="store_true")
    apply = commands.add_parser("apply", help="应用已确认的 Kit 更新")
    apply.add_argument("--root", type=Path, default=Path.cwd())
    apply.add_argument("--plan-hash", required=True)
    apply.add_argument("--json", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = plan_result(args.root) if args.command == "plan" else apply_result(args.root, args.plan_hash)
        print(json.dumps(result, ensure_ascii=False) if args.json else json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except UpdateError as exc:
        error = {"error": {"code": exc.code, "message": str(exc)}}
        print(json.dumps(error, ensure_ascii=False) if args.json else f"错误：{exc.code}：{exc}", file=sys.stderr)
        return 1
    except (OSError, RuntimeError, UnicodeError, ValueError, WorkspaceError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
