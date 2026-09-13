#!/usr/bin/env python3
"""Activate, inspect, and continue local custom workflow actions."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Optional, Sequence


sys.dont_write_bytecode = True


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from extension_model import (  # noqa: E402
    ActionDeclaration,
    ExtensionError,
    ExtensionManifest,
    extension_digest,
)
from extension_registry import discover_extensions  # noqa: E402
from provider_protocol import run_provider  # noqa: E402
from workspace_extension import (  # noqa: E402
    ExtensionCommandError,
    _read_lock,
    extension_lock,
)
from workspace_model import WorkspaceError, atomic_write_many, parse_json_bytes  # noqa: E402
from workspace_paths import (  # noqa: E402
    ignored_by_root_gitignore,
    features_root,
    state_root,
    workflow_file,
    workflow_run_file,
    workflow_runs_root,
)
from workspace_provider import _command as extension_command  # noqa: E402
from workflow_model import (  # noqa: E402
    CustomStage,
    ID_RE,
    ResolvedWorkflow,
    WorkflowDefinition,
    WorkflowError,
    WorkflowOverlay,
    evaluate_skip_hints,
    load_core_workflow,
    load_overlay,
    resolve_stages,
)


CORE_WORKFLOW = SCRIPT_DIR.parent / "workflows" / "feature-development.json"
RUN_FIELDS = frozenset(
    {"schemaVersion", "id", "workflow", "featureSlug", "repository", "branch", "stages"}
)
RUN_V2_FIELDS = RUN_FIELDS | {"events"}
RUN_STATUSES = frozenset({"running", "succeeded", "failed", "skipped"})
RUN_ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
FINGERPRINT_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
STAGE_RECORD_FIELDS = frozenset({"fingerprint", "status", "updatedAt", "summary"})
MAX_SUMMARY_BYTES = 8 * 1024


class WorkflowCommandError(WorkflowError):
    """Raised by the local Workflow command surface."""


@dataclass(frozen=True)
class ActionBinding:
    manifest: ExtensionManifest
    action: ActionDeclaration
    digest: str

    @property
    def ref(self) -> str:
        return self.action.ref


def _command(code: str, message: str) -> WorkflowCommandError:
    return WorkflowCommandError(f"{code}: {message}")


def _root(root: Path) -> Path:
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise _command("WORKFLOW_INVALID", f"治理仓必须是普通目录：{root}")
    try:
        return root.resolve()
    except (OSError, RuntimeError) as exc:
        raise _command("WORKFLOW_INVALID", f"治理仓不可解析：{root}") from exc


def _safe_path(root: Path, path: Path) -> None:
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise _command("WORKFLOW_INVALID", f"路径越出治理仓：{path}") from exc
    current = root
    try:
        for part in relative.parts:
            current /= part
            if current.is_symlink():
                raise _command("WORKFLOW_INVALID", f"路径包含符号链接：{current}")
        path.resolve(strict=False).relative_to(root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise _command("WORKFLOW_INVALID", f"路径不可安全解析：{path}") from exc


def _state(root: Path) -> Path:
    state = state_root(root)
    _safe_path(root, state)
    if state.is_symlink() or not state.is_dir():
        raise _command("WORKFLOW_INVALID", "缺少本地工作区状态")
    return state


def _json_text(value: Mapping[str, object]) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"


def _read_json(path: Path, code: str) -> dict[str, object]:
    try:
        if path.is_symlink() or not path.is_file():
            raise _command(code, f"缺少或不安全的 JSON 文件：{path}")
        return parse_json_bytes(path.read_bytes(), str(path))
    except WorkflowCommandError:
        raise
    except (OSError, RuntimeError, UnicodeError, WorkspaceError) as exc:
        raise _command(code, f"无法读取 JSON：{path}：{exc}") from exc


def _core() -> WorkflowDefinition:
    try:
        return load_core_workflow(CORE_WORKFLOW)
    except WorkflowError as exc:
        raise _command("WORKFLOW_INVALID", str(exc)) from exc


def _active_overlay(root: Path) -> WorkflowOverlay | None:
    path = workflow_file(root)
    _safe_path(root, path)
    if not path.exists() and not path.is_symlink():
        return None
    try:
        return load_overlay(path)
    except WorkflowError as exc:
        raise _command("WORKFLOW_INVALID", str(exc)) from exc


def _overlay_dict(overlay: WorkflowOverlay) -> dict[str, object]:
    return {
        "schemaVersion": {"major": 1, "minor": 0},
        "workflow": overlay.workflow,
        "stages": [
            {
                **{"id": stage.id},
                **({"before": stage.before} if stage.before is not None else {"after": stage.after}),
                "uses": stage.uses,
                **({"trigger": stage.trigger} if stage.trigger != "manual" else {}),
                **({"with": dict(stage.config)} if stage.config else {}),
            }
            for stage in overlay.stages
        ],
        **(
            {
                "skipHints": [
                    {
                        "stage": hint.stage,
                        "when": {"repositoryCount": {"max": hint.max_repository_count}},
                        "reason": hint.reason,
                    }
                    for hint in overlay.skip_hints
                ]
            }
            if overlay.skip_hints
            else {}
        ),
    }


def _action_rows(manifest: ExtensionManifest) -> list[dict[str, object]]:
    rows = []
    for action in sorted(manifest.actions, key=lambda item: item.action):
        row: dict[str, object] = {
            "id": action.action,
            "apiVersion": action.api_version,
            "skill": action.skill,
        }
        if action.confirmation_title is not None:
            row["confirmation"] = {
                "title": action.confirmation_title,
                "summary": action.confirmation_summary,
        }
        rows.append(row)
    return rows


def _active_actions(root: Path) -> dict[str, ActionBinding]:
    try:
        lock = _read_lock(root)
        discovered = discover_extensions(root)
    except (ExtensionCommandError, ExtensionError) as exc:
        raise _command("ACTION_MISSING", str(exc)) from exc
    from extension_model import lock_version_major
    if lock_version_major(lock["lockVersion"]) != 1:
        raise _command("ACTION_MISSING", "Extension lock 版本不受当前 Kit 支持")
    result: dict[str, ActionBinding] = {}
    for item in lock["extensions"]:
        assert isinstance(item, dict)
        extension_id = item["id"]
        assert isinstance(extension_id, str)
        manifest = discovered.get(extension_id)
        if manifest is None:
            raise _command("ACTION_MISSING", f"已锁定 Extension 不存在：{extension_id}")
        try:
            drifted = (
                manifest.version != item["version"]
                or item["path"] != f"extensions/{extension_id}"
                or extension_digest(manifest.root) != item["digest"]
                or _action_rows(manifest) != item.get("actions")
            )
        except ExtensionError as exc:
            raise _command("ACTION_DRIFT", str(exc)) from exc
        if drifted:
            raise _command("ACTION_DRIFT", f"已激活 Extension 已漂移：{extension_id}")
        digest = item["digest"]
        assert isinstance(digest, str)
        for action in manifest.actions:
            if action.ref in result:
                raise _command("ACTION_MISSING", f"Action 引用重复：{action.ref}")
            result[action.ref] = ActionBinding(manifest, action, digest)
    return result


def _validate_actions(root: Path, overlay: WorkflowOverlay) -> dict[str, ActionBinding]:
    actions = _active_actions(root)
    for stage in overlay.stages:
        if stage.uses not in actions:
            raise _command("ACTION_MISSING", f"Action 未安装或未锁定：{stage.uses}")
    return actions


def _resolve(root: Path, overlay: WorkflowOverlay | None = None) -> tuple[WorkflowDefinition, WorkflowOverlay, ResolvedWorkflow, dict[str, ActionBinding]]:
    core = _core()
    active = _active_overlay(root) if overlay is None else overlay
    if active is None:
        raise _command("WORKFLOW_DISABLED", "当前工作区没有激活 Workflow Overlay")
    try:
        resolved = resolve_stages(core, active)
    except WorkflowError as exc:
        code, _, message = str(exc).partition(": ")
        raise _command(code or "WORKFLOW_INVALID", message or str(exc)) from exc
    return core, active, resolved, _validate_actions(root, active)


def _hash(value: Mapping[str, object]) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _core_digest() -> str:
    try:
        return "sha256:" + hashlib.sha256(CORE_WORKFLOW.read_bytes()).hexdigest()
    except OSError as exc:
        raise _command("WORKFLOW_INVALID", f"无法读取 Core Workflow：{exc}") from exc


def _preview_payload(root: Path, overlay: WorkflowOverlay) -> dict[str, object]:
    core, _, resolved, actions = _resolve(root, overlay)
    normalized = _overlay_dict(overlay)
    return {
        "workflow": core.id,
        "overlay": normalized,
        "stages": list(resolved.stage_ids),
        "actions": sorted(actions),
        "paths": [".workspace/workflow.json"],
        "previewHash": _hash(
            {
                "core": _core_digest(),
                "overlay": normalized,
                "actions": {ref: binding.digest for ref, binding in sorted(actions.items())},
            }
        ),
    }


def preview_result(root: Path, config: Path) -> dict[str, object]:
    root = _root(root)
    _state(root)
    try:
        overlay = load_overlay(Path(config))
    except WorkflowError as exc:
        raise _command("WORKFLOW_INVALID", str(exc)) from exc
    result = _preview_payload(root, overlay)
    result["applyCommand"] = (
        f"python3 scripts/workspace_workflow.py apply --root {root} "
        f"--config {Path(config).resolve()} --preview-hash {result['previewHash']}"
    )
    return result


def _require_ignored(root: Path, relative: str) -> None:
    if not ignored_by_root_gitignore(root, relative):
        raise _command("WORKFLOW_INVALID", f"本地产物未被根 .gitignore 忽略：{relative}")
    result = subprocess.run(
        ["git", "-C", str(root), "ls-files", "--error-unmatch", "--", relative],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode == 0:
        raise _command("WORKFLOW_TRACKED", f"Git 正在跟踪 Workflow 本地状态：{relative}")
    if result.returncode != 1:
        raise _command("WORKFLOW_TRACKED", f"无法检查 Git 跟踪状态：{relative}")


def apply(root: Path, config: Path, expected_hash: str) -> int:
    root = _root(root)
    _state(root)
    if not isinstance(expected_hash, str) or not re_full_hash(expected_hash):
        raise _command("WORKFLOW_PLAN_STALE", "preview hash 必须是 64 位小写十六进制")
    with extension_lock(root, fcntl.LOCK_EX, create_cache=True):
        result = preview_result(root, config)
        if result["previewHash"] != expected_hash:
            raise _command("WORKFLOW_PLAN_STALE", "Workflow preview hash 已变化")
        target = workflow_file(root)
        _safe_path(root, target)
        _require_ignored(root, ".workspace/workflow.json")
        overlay = result["overlay"]
        assert isinstance(overlay, dict)
        try:
            atomic_write_many(((target, _json_text(overlay)),))
        except WorkspaceError as exc:
            raise _command("WORKFLOW_INVALID", str(exc)) from exc
    return 0


def re_full_hash(value: str) -> bool:
    return bool(re.fullmatch(r"[0-9a-f]{64}", value))


def _ensure_runs_root(root: Path) -> Path:
    path = workflow_runs_root(root)
    _safe_path(root, path)
    if not path.exists():
        try:
            path.mkdir(mode=0o700)
        except OSError as exc:
            raise _command("WORKFLOW_INVALID", f"无法创建 Workflow Run 目录：{exc}") from exc
    if path.is_symlink() or not path.is_dir():
        raise _command("WORKFLOW_INVALID", "Workflow Run 目录不安全")
    return path


def _verification_file(root: Path, feature_slug: str | None) -> Path | None:
    if feature_slug is None:
        return None
    feature = features_root(root) / feature_slug
    readme = feature / "README.md"
    _safe_path(root, readme)
    if not readme.is_file():
        raise _command("WORKFLOW_FEATURE_MISSING", f"标准需求缺少 README：{readme}")
    path = feature / "testing" / "verification.md"
    _safe_path(root, path)
    if not path.exists():
        return None
    if not path.is_file():
        raise _command("WORKFLOW_FEATURE_MISSING", f"验证记录不是普通文件：{path}")
    return path


def _feature_uses_v2(root: Path, feature_slug: str | None) -> bool:
    if feature_slug is None:
        return False
    plan = features_root(root) / feature_slug / "plans" / "implementation.md"
    if plan.is_symlink() or not plan.is_file():
        return False
    return bool(re.search(r"(?:completion-policy:\s*|完成门禁：\s*`?)task-evidence-v2\b", plan.read_text(encoding="utf-8")))


def _valid_run_id(value: object) -> bool:
    return isinstance(value, str) and bool(RUN_ID_RE.fullmatch(value))


def start_run(
    root: Path,
    *,
    run_id: str | None = None,
    feature_slug: str | None = None,
    repository: str | None = None,
    branch: str | None = None,
) -> dict[str, object]:
    root = _root(root)
    _state(root)
    with extension_lock(root, fcntl.LOCK_EX, create_cache=True):
        core, overlay, _, _ = _resolve(root)
        if run_id is None:
            if feature_slug is None:
                raise _command("WORKFLOW_INVALID", "轻量改动必须显式提供 run id")
            run_id = feature_slug
        if not _valid_run_id(run_id):
            raise _command("WORKFLOW_INVALID", "run id 必须是小写 kebab-case")
        if feature_slug is not None and not _valid_run_id(feature_slug):
            raise _command("WORKFLOW_INVALID", "feature slug 必须是小写 kebab-case")
        for label, value in (("repository", repository), ("branch", branch)):
            if value is not None and (
                not isinstance(value, str) or not value or any(char in value for char in "\0\r\n")
            ):
                raise _command("WORKFLOW_INVALID", f"{label} 无效")
        _ensure_runs_root(root)
        _verification_file(root, feature_slug)
        is_v2 = _feature_uses_v2(root, feature_slug)
        target = workflow_run_file(root, run_id)
        _safe_path(root, target)
        if target.exists() or target.is_symlink():
            existing = _load_run(root, run_id)
            candidate = {
                "workflow": core.id,
                "featureSlug": feature_slug,
                "repository": repository,
                "branch": branch,
            }
            if all(existing[field] == value for field, value in candidate.items()):
                return existing
            raise _command("WORKFLOW_RUN_EXISTS", f"Workflow Run 已存在且上下文不同：{run_id}")
        _require_ignored(root, ".workspace/runs")
        run = {
            "schemaVersion": 2 if is_v2 else 1,
            "id": run_id,
            "workflow": core.id,
            "featureSlug": feature_slug,
            "repository": repository,
            "branch": branch,
            "stages": {},
        }
        if is_v2:
            run["events"] = []
        try:
            atomic_write_many(((target, _json_text(run)),))
        except WorkspaceError as exc:
            raise _command("WORKFLOW_INVALID", str(exc)) from exc
        return run


def _load_run(root: Path, run_id: str) -> dict[str, object]:
    if not _valid_run_id(run_id):
        raise _command("WORKFLOW_RUN_MISSING", "run id 无效")
    path = workflow_run_file(root, run_id)
    _safe_path(root, path)
    raw = _read_json(path, "WORKFLOW_RUN_MISSING")
    version = raw.get("schemaVersion")
    if set(raw) != (RUN_V2_FIELDS if version == 2 else RUN_FIELDS) or version not in {1, 2} or raw.get("id") != run_id:
        raise _command("WORKFLOW_RUN_MISSING", "Workflow Run 结构无效")
    if not isinstance(raw["workflow"], str) or not isinstance(raw["stages"], dict):
        raise _command("WORKFLOW_RUN_MISSING", "Workflow Run 字段无效")
    for field in ("featureSlug", "repository", "branch"):
        if raw[field] is not None and not isinstance(raw[field], str):
            raise _command("WORKFLOW_RUN_MISSING", f"Workflow Run {field} 无效")
    stages = raw["stages"]
    assert isinstance(stages, dict)
    for stage_id, record in stages.items():
        if not isinstance(stage_id, str) or not ID_RE.fullmatch(stage_id):
            raise _command("WORKFLOW_RUN_MISSING", "Workflow Run Stage id 无效")
        if not isinstance(record, dict) or set(record) != STAGE_RECORD_FIELDS:
            raise _command("WORKFLOW_RUN_MISSING", f"Workflow Run Stage 结构无效：{stage_id}")
        fingerprint = record["fingerprint"]
        status = record["status"]
        updated_at = record["updatedAt"]
        summary = record["summary"]
        if (
            not isinstance(fingerprint, str)
            or not FINGERPRINT_RE.fullmatch(fingerprint)
            or not isinstance(status, str)
            or status not in RUN_STATUSES
            or not isinstance(updated_at, str)
            or not updated_at.endswith("Z")
        ):
            raise _command("WORKFLOW_RUN_MISSING", f"Workflow Run Stage 字段无效：{stage_id}")
        _summary(summary, "Workflow Run summary")
    if raw.get("schemaVersion") == 2:
        events = raw.get("events")
        if not isinstance(events, list):
            raise _command("WORKFLOW_RUN_MISSING", "Workflow Run events 无效")
        for sequence, event in enumerate(events, 1):
            if (
                not isinstance(event, dict)
                or set(event) != {"sequence", "stage", "fingerprint", "status", "updatedAt", "summary"}
                or event.get("sequence") != sequence
                or not isinstance(event.get("stage"), str) or not ID_RE.fullmatch(event["stage"])
                or not isinstance(event.get("fingerprint"), str) or not FINGERPRINT_RE.fullmatch(event["fingerprint"])
                or event.get("status") not in RUN_STATUSES
                or not isinstance(event.get("updatedAt"), str) or not event["updatedAt"].endswith("Z")
                or not isinstance(event.get("summary"), str)
            ):
                raise _command("WORKFLOW_RUN_MISSING", "Workflow Run event 结构无效")
            _summary(event["summary"], "Workflow Run event summary")
    return raw


def _region_stages(resolved: ResolvedWorkflow, anchor: str, *, before: bool) -> tuple[CustomStage, ...]:
    core_ids = {stage.id for stage in resolved.stages if stage.core}
    if anchor not in core_ids:
        raise _command("WORKFLOW_ANCHOR_MISSING", f"plan 锚点不是 Core Stage：{anchor}")
    index = resolved.stage_ids.index(anchor)
    values = list(resolved.stages)
    if before:
        start = index - 1
        while start >= 0 and not values[start].core:
            start -= 1
        selected = values[start + 1 : index]
    else:
        end = index + 1
        while end < len(values) and not values[end].core:
            end += 1
        selected = values[index + 1 : end]
    return tuple(
        CustomStage(
            item.id,
            None,
            None,
            item.action or "",
            item.trigger or "manual",
            item.config or {},
            position,
        )
        for position, item in enumerate(selected)
        if not item.core
    )


def _stage_fingerprint(
    stage: CustomStage, binding: ActionBinding, run: Mapping[str, object]
) -> str:
    return "sha256:" + _hash(
        {
            "run": run["id"],
            "workflow": run["workflow"],
            "coreDigest": _core_digest(),
            "stage": stage.id,
            "before": stage.before,
            "after": stage.after,
            "trigger": stage.trigger,
            "action": binding.action.as_dict(),
            "extensionDigest": binding.digest,
            "with": dict(stage.config),
        }
    )


def _plan_item(
    stage: CustomStage, binding: ActionBinding, run: Mapping[str, object]
) -> dict[str, object]:
    skill_path = binding.manifest.root / "skills" / binding.action.skill / "SKILL.md"
    assert binding.action.confirmation_title is not None
    assert binding.action.confirmation_summary is not None
    item: dict[str, object] = {
        "stage": stage.id,
        "action": binding.ref,
        "trigger": stage.trigger,
        "confirmation": {
            "title": binding.action.confirmation_title,
            "summary": binding.action.confirmation_summary,
        },
        "effects": list(binding.action.effects),
        "skillPath": str(skill_path),
        "with": dict(stage.config),
        "fingerprint": _stage_fingerprint(stage, binding, run),
    }
    item["planHash"] = _hash(item)
    return item


def plan_result(
    root: Path,
    run_id: str,
    *,
    before: str | None = None,
    after: str | None = None,
) -> dict[str, object]:
    root = _root(root)
    if (before is None) == (after is None):
        raise _command("WORKFLOW_INVALID", "plan 必须恰好声明 before 或 after")
    _state(root)
    core, overlay, resolved, actions = _resolve(root)
    run = _load_run(root, run_id)
    if run["workflow"] != core.id:
        raise _command("WORKFLOW_RUN_MISSING", "Workflow Run 与当前 Workflow 不匹配")
    anchor = before if before is not None else after
    assert anchor is not None
    stages = _region_stages(resolved, anchor, before=before is not None)
    source_stages = {stage.id: stage for stage in overlay.stages}
    stage_records = run["stages"]
    assert isinstance(stage_records, dict)
    pending: list[dict[str, object]] = []
    blocked = False
    for stage in stages:
        stage = source_stages[stage.id]
        binding = actions.get(stage.uses)
        if binding is None:
            raise _command("ACTION_MISSING", f"Action 未安装或未锁定：{stage.uses}")
        item = _plan_item(stage, binding, run)
        fingerprint = item["fingerprint"]
        assert isinstance(fingerprint, str)
        record = stage_records.get(stage.id)
        if isinstance(record, dict) and record.get("fingerprint") == fingerprint:
            status = record.get("status")
            if status in {"succeeded", "skipped"}:
                continue
            if status in {"failed", "running"}:
                blocked = True
        pending.append(item)
        if blocked:
            break
    return {
        "run": run_id,
        "anchor": anchor,
        "event": "before" if before is not None else "after",
        "blocked": blocked,
        "pending": pending,
    }


def _stage_context(
    root: Path, run_id: str, stage_id: str
) -> tuple[dict[str, object], CustomStage, ActionBinding, dict[str, object], tuple[str, ...]]:
    core, overlay, resolved, actions = _resolve(root)
    run = _load_run(root, run_id)
    if run["workflow"] != core.id:
        raise _command("WORKFLOW_RUN_MISSING", "Workflow Run 与当前 Workflow 不匹配")
    source = {stage.id: stage for stage in overlay.stages}
    stage = source.get(stage_id)
    if stage is None:
        raise _command("ACTION_MISSING", f"Custom Stage 不存在：{stage_id}")
    binding = actions.get(stage.uses)
    if binding is None:
        raise _command("ACTION_MISSING", f"Action 未安装或未锁定：{stage.uses}")
    item = _plan_item(stage, binding, run)
    ordered = list(resolved.stages)
    index = resolved.stage_ids.index(stage_id)
    previous = [item_before.id for item_before in ordered[:index] if not item_before.core]
    return run, stage, binding, item, tuple(previous)


def _summary(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or any(char in value for char in "\r\n"):
        raise _command("WORKFLOW_INVALID", f"{label} 必须是单行非空文本")
    encoded = value.strip().encode("utf-8")
    if len(encoded) > MAX_SUMMARY_BYTES:
        raise _command("WORKFLOW_INVALID", f"{label} 超过 8 KiB")
    return value.strip()


def _assert_plan_hash(item: Mapping[str, object], expected_hash: str) -> None:
    if not isinstance(expected_hash, str) or not re_full_hash(expected_hash):
        raise _command("ACTION_PLAN_REQUIRED", "必须提供当前 plan hash")
    if item["planHash"] != expected_hash:
        raise _command("ACTION_PLAN_STALE", "Action plan hash 已变化")


def _assert_predecessors(
    run: Mapping[str, object], previous: Sequence[str]
) -> None:
    records = run["stages"]
    assert isinstance(records, dict)
    for stage_id in previous:
        record = records.get(stage_id)
        if not isinstance(record, dict) or record.get("status") not in {"succeeded", "skipped"}:
            raise _command("ACTION_BLOCKED", f"前置 Custom Stage 尚未完成：{stage_id}")


def _write_run(root: Path, run: Mapping[str, object]) -> None:
    run_id = run["id"]
    assert isinstance(run_id, str)
    path = workflow_run_file(root, run_id)
    _safe_path(root, path)
    try:
        atomic_write_many(((path, _json_text(dict(run))),))
    except WorkspaceError as exc:
        raise _command("WORKFLOW_INVALID", str(exc)) from exc


def _record_stage(
    root: Path,
    run: dict[str, object],
    item: Mapping[str, object],
    *,
    status: str,
    summary: str,
) -> dict[str, object]:
    if status not in RUN_STATUSES:
        raise _command("WORKFLOW_INVALID", f"Stage 状态无效：{status}")
    feature_slug = run["featureSlug"]
    assert feature_slug is None or isinstance(feature_slug, str)
    if _feature_uses_v2(root, feature_slug) and run.get("schemaVersion") == 1:
        run["schemaVersion"] = 2
        run["events"] = []
    stages = run["stages"]
    assert isinstance(stages, dict)
    stage_id = item["stage"]
    fingerprint = item["fingerprint"]
    assert isinstance(stage_id, str) and isinstance(fingerprint, str)
    timestamp = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    stages[stage_id] = {
        "fingerprint": fingerprint,
        "status": status,
        "updatedAt": timestamp,
        "summary": summary,
    }
    if run.get("schemaVersion") == 2:
        events = run.setdefault("events", [])
        assert isinstance(events, list)
        events.append({
            "sequence": len(events) + 1,
            "stage": stage_id,
            "fingerprint": fingerprint,
            "status": status,
            "updatedAt": timestamp,
            "summary": summary,
        })
    outputs: list[tuple[Path, str]] = [
        (workflow_run_file(root, str(run["id"])), _json_text(run))
    ]
    verification = _verification_file(root, feature_slug) if run.get("schemaVersion") != 2 else None
    if run.get("schemaVersion") == 2 and feature_slug is not None:
        evidence_index = features_root(root) / feature_slug / "testing" / "evidence" / "index.json"
        if evidence_index.is_file():
            try:
                from workspace_verification import summary_text

                human_summary = summary_text(
                    root,
                    feature_slug,
                    features_root(root) / feature_slug,
                    action_summaries=[item for item in run.get("events", []) if isinstance(item, dict)],
                )
                outputs.append((features_root(root) / feature_slug / "testing" / "verification.md", human_summary))
            except (OSError, ValueError) as exc:
                raise _command("WORKFLOW_INVALID", f"无法生成 v2 验证摘要：{exc}") from exc
    if verification is not None:
        try:
            existing = verification.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise _command("WORKFLOW_FEATURE_MISSING", f"无法读取验证记录：{verification}") from exc
        action = item["action"]
        assert isinstance(action, str)
        outputs.append(
            (
                verification,
                existing.rstrip()
                + f"\n\n- {timestamp} Workflow Action `{stage_id}` / `{action}`：{status}\n",
            )
        )
    try:
        atomic_write_many(outputs)
    except WorkspaceError as exc:
        raise _command("WORKFLOW_INVALID", str(exc)) from exc
    return stages[stage_id]


def finish(
    root: Path,
    run_id: str,
    stage_id: str,
    plan_hash: str,
    *,
    status: str,
    summary: str,
) -> dict[str, object]:
    if status not in {"succeeded", "failed"}:
        raise _command("WORKFLOW_INVALID", "finish 只接受 succeeded 或 failed")
    root = _root(root)
    with extension_lock(root, fcntl.LOCK_EX, create_cache=True):
        run, _, binding, item, previous = _stage_context(root, run_id, stage_id)
        _assert_plan_hash(item, plan_hash)
        _assert_predecessors(run, previous)
        if binding.action.command is not None:
            raise _command("ACTION_COMMAND_REQUIRED", f"Action 必须使用 run：{binding.ref}")
        record = _record_stage(root, run, item, status=status, summary=_summary(summary, "summary"))
        return {"run": run_id, "stage": stage_id, **record}


def skip(
    root: Path, run_id: str, stage_id: str, plan_hash: str, *, reason: str
) -> dict[str, object]:
    root = _root(root)
    with extension_lock(root, fcntl.LOCK_EX, create_cache=True):
        run, _, _, item, previous = _stage_context(root, run_id, stage_id)
        _assert_plan_hash(item, plan_hash)
        _assert_predecessors(run, previous)
        record = _record_stage(root, run, item, status="skipped", summary=_summary(reason, "skip reason"))
        return {"run": run_id, "stage": stage_id, **record}


def _result_summary(result: Mapping[str, object]) -> str:
    diagnostics = result.get("diagnostics")
    if isinstance(diagnostics, list) and diagnostics:
        first = diagnostics[0]
        if isinstance(first, Mapping) and isinstance(first.get("code"), str):
            return f"Action failed: {first['code']}"
    return "Action completed" if result.get("status") == "ok" else "Action failed"


def run_action(
    root: Path, run_id: str, stage_id: str, plan_hash: str
) -> dict[str, object]:
    root = _root(root)
    with extension_lock(root, fcntl.LOCK_EX, create_cache=True):
        run, stage, binding, item, previous = _stage_context(root, run_id, stage_id)
        _assert_plan_hash(item, plan_hash)
        _assert_predecessors(run, previous)
        if binding.action.command is None:
            raise _command("ACTION_SKILL_ONLY", f"Action 没有 command：{binding.ref}")
        command = extension_command(binding.manifest, binding.action)
        if command is None:
            raise _command("ACTION_DRIFT", f"Action command 不安全或不可执行：{binding.ref}")
        _record_stage(root, run, item, status="running", summary="Action command started")
        request = {
            "workflow": run["workflow"],
            "run": run_id,
            "stage": stage.id,
            "featureSlug": run["featureSlug"],
            "repository": run["repository"],
            "branch": run["branch"],
            "with": dict(stage.config),
        }
        result = run_provider(
            command,
            cwd=binding.manifest.root,
            provider=binding.ref,
            request=request,
            environment=binding.action.environment or (),
        )
        status = "succeeded" if result.get("status") == "ok" else "failed"
        record = _record_stage(
            root,
            run,
            item,
            status=status,
            summary=_result_summary(result),
        )
        diagnostics = result.get("diagnostics")
        codes = [
            item.get("code")
            for item in diagnostics
            if isinstance(item, Mapping) and isinstance(item.get("code"), str)
        ] if isinstance(diagnostics, list) else []
        return {
            "run": run_id,
            "stage": stage_id,
            "action": binding.ref,
            "status": record["status"],
            "summary": record["summary"],
            "diagnosticCodes": codes[:20],
            "effects": list(binding.action.effects),
        }


def status_result(root: Path) -> dict[str, object]:
    root = _root(root)
    state = state_root(root)
    if not state.exists() and not state.is_symlink():
        return {"enabled": False}
    _state(root)
    overlay = _active_overlay(root)
    if overlay is None:
        return {"enabled": False}
    try:
        core, active, resolved, actions = _resolve(root, overlay)
    except WorkflowCommandError as exc:
        code, _, _ = str(exc).partition(": ")
        return {"enabled": True, "blockedCodes": [code]}
    runs = workflow_runs_root(root)
    if not runs.exists():
        return {
            "enabled": True,
            "runs": 0,
            "actions": len(actions),
            "pending": 0,
            "failed": 0,
            "interrupted": 0,
            "nextStage": None,
        }
    _safe_path(root, runs)
    if runs.is_symlink() or not runs.is_dir():
        return {"enabled": True, "blockedCodes": ["WORKFLOW_INVALID"]}
    pending = failed = interrupted = 0
    next_stage: str | None = None
    source = {stage.id: stage for stage in active.stages}
    custom_ids = [stage.id for stage in resolved.stages if not stage.core]
    count = 0
    for path in sorted(runs.glob("*.json")):
        count += 1
        try:
            run = _load_run(root, path.stem)
            if run["workflow"] != core.id:
                raise _command("WORKFLOW_RUN_MISSING", "Workflow Run 与当前 Workflow 不匹配")
            records = run["stages"]
            assert isinstance(records, dict)
            for stage_id in custom_ids:
                stage = source[stage_id]
                binding = actions[stage.uses]
                fingerprint = _stage_fingerprint(stage, binding, run)
                record = records.get(stage_id)
                if isinstance(record, dict) and record.get("fingerprint") == fingerprint:
                    status = record.get("status")
                    if status in {"succeeded", "skipped"}:
                        continue
                    if status == "failed":
                        failed += 1
                        continue
                    if status == "running":
                        interrupted += 1
                        continue
                pending += 1
                if next_stage is None:
                    next_stage = stage_id
        except WorkflowCommandError as exc:
            code, _, _ = str(exc).partition(": ")
            return {"enabled": True, "blockedCodes": [code]}
    return {
        "enabled": True,
        "runs": count,
        "actions": len(actions),
        "pending": pending,
        "failed": failed,
        "interrupted": interrupted,
        "nextStage": next_stage,
    }


def skip_suggestions(root: Path, *, repository_count: int) -> dict[str, object]:
    """按仓库数量返回当前激活 Overlay 声明的跳过建议；只读，不修改任何状态。"""
    root = _root(root)
    state = state_root(root)
    if not state.exists() and not state.is_symlink():
        return {"enabled": False, "suggestions": []}
    _state(root)
    overlay = _active_overlay(root)
    if overlay is None:
        return {"enabled": False, "suggestions": []}
    try:
        core, active, _, _ = _resolve(root, overlay)
    except WorkflowCommandError as exc:
        code, _, _ = str(exc).partition(": ")
        return {"enabled": True, "blockedCodes": [code]}
    suggestions = evaluate_skip_hints(core, active, repository_count=repository_count)
    return {"enabled": True, "suggestions": list(suggestions)}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("status",):
        command = commands.add_parser(name)
        command.add_argument("--root", type=Path, default=Path.cwd())
        command.add_argument("--json", action="store_true")
    preview = commands.add_parser("preview")
    preview.add_argument("--root", type=Path, default=Path.cwd())
    preview.add_argument("--config", type=Path, required=True)
    preview.add_argument("--json", action="store_true")
    apply_parser = commands.add_parser("apply")
    apply_parser.add_argument("--root", type=Path, default=Path.cwd())
    apply_parser.add_argument("--config", type=Path, required=True)
    apply_parser.add_argument("--preview-hash", required=True)
    start = commands.add_parser("start")
    start.add_argument("--root", type=Path, default=Path.cwd())
    start.add_argument("--run-id")
    start.add_argument("--feature")
    start.add_argument("--repo")
    start.add_argument("--branch")
    start.add_argument("--json", action="store_true")
    plan = commands.add_parser("plan")
    plan.add_argument("--root", type=Path, default=Path.cwd())
    plan.add_argument("--run", required=True)
    event = plan.add_mutually_exclusive_group(required=True)
    event.add_argument("--before")
    event.add_argument("--after")
    plan.add_argument("--json", action="store_true")
    run = commands.add_parser("run")
    run.add_argument("--root", type=Path, default=Path.cwd())
    run.add_argument("--run", required=True)
    run.add_argument("--stage", required=True)
    run.add_argument("--plan-hash", required=True)
    run.add_argument("--json", action="store_true")
    finish_parser = commands.add_parser("finish")
    finish_parser.add_argument("--root", type=Path, default=Path.cwd())
    finish_parser.add_argument("--run", required=True)
    finish_parser.add_argument("--stage", required=True)
    finish_parser.add_argument("--plan-hash", required=True)
    finish_parser.add_argument("--status", choices=("succeeded", "failed"), required=True)
    finish_parser.add_argument("--summary", required=True)
    finish_parser.add_argument("--json", action="store_true")
    skip_parser = commands.add_parser("skip")
    skip_parser.add_argument("--root", type=Path, default=Path.cwd())
    skip_parser.add_argument("--run", required=True)
    skip_parser.add_argument("--stage", required=True)
    skip_parser.add_argument("--plan-hash", required=True)
    skip_parser.add_argument("--reason", required=True)
    skip_parser.add_argument("--json", action="store_true")
    skip_suggestions_parser = commands.add_parser("skip-suggestions")
    skip_suggestions_parser.add_argument("--root", type=Path, default=Path.cwd())
    skip_suggestions_parser.add_argument("--repositories", type=int, required=True)
    skip_suggestions_parser.add_argument("--json", action="store_true")
    return parser


def _print(value: Mapping[str, object], json_output: bool) -> None:
    print(json.dumps(value, ensure_ascii=False) if json_output else json.dumps(value, ensure_ascii=False, indent=2))


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "status":
            _print(status_result(args.root), args.json)
        elif args.command == "preview":
            _print(preview_result(args.root, args.config), args.json)
        elif args.command == "apply":
            apply(args.root, args.config, args.preview_hash)
        elif args.command == "start":
            _print(
                start_run(
                    args.root,
                    run_id=args.run_id,
                    feature_slug=args.feature,
                    repository=args.repo,
                    branch=args.branch,
                ),
                args.json,
            )
        elif args.command == "plan":
            _print(plan_result(args.root, args.run, before=args.before, after=args.after), args.json)
        elif args.command == "run":
            _print(run_action(args.root, args.run, args.stage, args.plan_hash), args.json)
        elif args.command == "finish":
            _print(
                finish(
                    args.root,
                    args.run,
                    args.stage,
                    args.plan_hash,
                    status=args.status,
                    summary=args.summary,
                ),
                args.json,
            )
        elif args.command == "skip":
            _print(skip(args.root, args.run, args.stage, args.plan_hash, reason=args.reason), args.json)
        else:
            if args.repositories < 0:
                raise _command("WORKFLOW_INVALID", "repositories 不能为负数")
            _print(
                skip_suggestions(args.root, repository_count=args.repositories),
                args.json,
            )
        return 0
    except (OSError, RuntimeError, UnicodeError, WorkspaceError, ExtensionError, WorkflowError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
