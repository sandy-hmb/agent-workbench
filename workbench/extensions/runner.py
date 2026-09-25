#!/usr/bin/env python3
"""Activate, inspect, and continue local custom workflow actions."""
from __future__ import annotations
from workbench.resources import KIT_ROOT

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



from workbench.extensions.model import (  # noqa: E402
    ActionDeclaration,
    ExtensionError,
    ExtensionManifest,
    extension_digest,
)
from workbench.extensions.registry import discover_extensions  # noqa: E402
import workbench.extensions.attempts as attempts
from workbench.extensions.protocol import run_provider  # noqa: E402
from workbench.extensions.management import (  # noqa: E402
    ExtensionCommandError,
    _read_lock,
    extension_lock,
)
from workbench.workspace.model import WorkspaceError, atomic_write_many, parse_json_bytes  # noqa: E402
from workbench.workspace.paths import (
    item_document_file,  # noqa: E402
    ignored_by_root_gitignore,
    items_root,
    state_root,
    workflow_file,
    workflow_run_file,
    workflow_runs_root,
)
from workbench.extensions.providers import _command as extension_command  # noqa: E402
from workbench.extensions.workflow import (  # noqa: E402
    CustomStage,
    ID_RE,
    ResolvedWorkflow,
    WorkflowDefinition,
    WorkflowError,
    WorkflowOverlay,
    load_core_workflow,
    load_overlay,
    resolve_stages,
)


CORE_WORKFLOW = KIT_ROOT / "workflows" / "item-development.json"
RUN_FIELDS = frozenset(
    {"schemaVersion", "id", "workflow", "itemSlug", "repository", "branch", "stages"}
)
RUN_FIELDS = (RUN_FIELDS - {"stages"}) | {"iteration", "bindingRevision"}
RUN_STATUSES = frozenset({"running", "succeeded", "failed", "skipped", "unknown"})
RUN_ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
FINGERPRINT_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
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


def _overlay_dict(overlay):
    return {"schemaVersion": {"major": 1, "minor": 0}, "workflow": overlay.workflow, "stages": [
        {"id": stage.id, **({"before": stage.before} if stage.before else {"after": stage.after}),
         "uses": stage.uses, "trigger": stage.trigger, "with": dict(stage.config)} for stage in overlay.stages]}


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
    from workbench.extensions.model import lock_version_major
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
        f"python3 scripts/kit.py workflow apply --root {root} "
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


def _valid_run_id(value: object) -> bool:
    return isinstance(value, str) and bool(RUN_ID_RE.fullmatch(value))


def start_run(
    root: Path,
    *,
    run_id: str | None = None,
    item_slug: str | None = None,
    repository: str | None = None,
    branch: str | None = None,
) -> dict[str, object]:
    root = _root(root)
    _state(root)
    with extension_lock(root, fcntl.LOCK_EX, create_cache=True):
        core, overlay, _, _ = _resolve(root)
        if run_id is None:
            if item_slug is None:
                raise _command("WORKFLOW_INVALID", "轻量改动必须显式提供 run id")
            from workbench.work_items.store import item_path, load_state
            run_id = item_slug + "-" + load_state(item_path(root, item_slug))["iteration"]
        if not _valid_run_id(run_id):
            raise _command("WORKFLOW_INVALID", "run id 必须是小写 kebab-case")
        if item_slug is not None and not _valid_run_id(item_slug):
            raise _command("WORKFLOW_INVALID", "item slug 必须是小写 kebab-case")
        for label, value in (("repository", repository), ("branch", branch)):
            if value is not None and (
                not isinstance(value, str) or not value or any(char in value for char in "\0\r\n")
            ):
                raise _command("WORKFLOW_INVALID", f"{label} 无效")
        _ensure_runs_root(root)
        iteration = None
        binding_revision = None
        if item_slug is not None:
            from workbench.work_items.store import item_path, load_state, digest
            item_state = load_state(item_path(root, item_slug))
            iteration = item_state['iteration']
            binding_revision = digest(item_state['bindings'])
            if item_state['lifecycle'] != 'active':
                raise _command('WORKFLOW_ITEM_INACTIVE', '只能为进行中的工作项创建 Run')
            if repository is not None and not any(row['repository'] == repository and (branch is None or row['workBranch'] == branch) for row in item_state['bindings']):
                raise _command('WORKFLOW_BINDING_CHANGED', 'Run 仓库分支与工作项不匹配')
        target = workflow_run_file(root, run_id)
        _safe_path(root, target)
        if target.exists() or target.is_symlink():
            existing = _load_run(root, run_id)
            candidate = {
                "workflow": core.id,
                "itemSlug": item_slug,
                "iteration": iteration,
                "bindingRevision": binding_revision,
                "repository": repository,
                "branch": branch,
            }
            if all(existing[field] == value for field, value in candidate.items()):
                return existing
            raise _command("WORKFLOW_RUN_EXISTS", f"Workflow Run 已存在且上下文不同：{run_id}")
        _require_ignored(root, ".workspace/runs")
        run = {
            "schemaVersion": 3,
            "id": run_id,
            "workflow": core.id,
            "itemSlug": item_slug,
            "iteration": iteration,
            "bindingRevision": binding_revision,
            "repository": repository,
            "branch": branch,
        }
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
    if set(raw) != RUN_FIELDS or raw.get("schemaVersion") != 3 or raw.get("id") != run_id:
        raise _command("WORKFLOW_RUN_MISSING", "仅支持新版 Workflow Run")
    return attempts.project(root, {**raw, "stages": {}})


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
    if binding.action.command is not None:
        item["requestId"] = attempts.default_request_id(str(run["id"]), stage.id, item["planHash"])
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
        last_attempt = attempts.latest(attempts.read(root, run_id), stage.id)
        if last_attempt is not None:
            item["attempt"] = attempts.observed(root, last_attempt)
            item["requestId"] = last_attempt["requestId"]
        fingerprint = item["fingerprint"]
        assert isinstance(fingerprint, str)
        record = stage_records.get(stage.id)
        if isinstance(record, dict) and record.get("fingerprint") == fingerprint:
            status = record.get("status")
            if status in {"succeeded", "skipped"}:
                continue
            if status in {"failed", "running", "unknown"}:
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
    if run['itemSlug'] is not None:
        from workbench.work_items.store import item_path, load_state, digest
        from workbench.work_items.query import WorkItemQuery
        current = load_state(item_path(root, run['itemSlug']))
        if current['iteration'] != run['iteration']:
            raise _command('WORKFLOW_ITERATION_CHANGED', '当前 Run 属于历史迭代，不能继续执行')
        if current['lifecycle'] != 'active':
            raise _command('WORKFLOW_ITEM_INACTIVE', '工作项已暂停或结束，不能派发新动作')
        if digest(current['bindings']) != run['bindingRevision']:
            raise _command('WORKFLOW_BINDING_CHANGED', '工作项仓库绑定已变化，请创建当前绑定的新 Run')
        blocks = WorkItemQuery(root, run['itemSlug'], state=current).execution_blocks()
        if blocks['itemBlocked'] or (run['repository'] is None and blocks['records']):
            raise _command('WORKFLOW_ITEM_BLOCKED', '先解除适用的开发阻塞')
        if run['repository'] is not None and any(t['repository'] == run['repository'] and t['id'] in blocks['tasks'] for t in WorkItemQuery(root, run['itemSlug'], state=current).tasks()):
            raise _command('WORKFLOW_ITEM_BLOCKED', '当前仓库任务存在开发阻塞')
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
    root: Path, run: Mapping[str, object], previous: Sequence[str]
) -> None:
    _, overlay, _, actions = _resolve(root)
    current = {stage.id: stage for stage in overlay.stages}
    records = run["stages"]
    assert isinstance(records, dict)
    for stage_id in previous:
        record = records.get(stage_id)
        stage = current[stage_id]
        expected = _stage_fingerprint(stage, actions[stage.uses], run)
        if not isinstance(record, dict) or record.get("status") not in {"succeeded", "skipped"} or record.get("fingerprint") != expected:
            raise _command("ACTION_BLOCKED", f"前置 Custom Stage 尚未完成或配置已变化：{stage_id}")




def _record_stage(root, run, item, *, status, summary):
    data = attempts.read(root, run['id'])
    identifier = attempts.default_request_id(run['id'], item['stage'], item['planHash'])
    previous = data['requests'].get(identifier)
    if previous and previous['status'] == status and previous['summary'] == summary:
        return previous
    if previous and attempts.alive(root, run['id'], identifier):
        raise _command('ACTION_BUSY', '请求仍在执行')
    if previous:
        identifier += '-' + str(max(v['sequence'] for v in data['requests'].values()) + 1)
    row = {'requestId': identifier, 'run': run['id'], 'stage': item['stage'],
           'planHash': item['planHash'], 'fingerprint': item['fingerprint'],
           'sequence': max((v['sequence'] for v in data['requests'].values()), default=0) + 1,
           'status': status, 'origin': 'reconciled', 'summary': summary, 'updatedAt': attempts.stamp()}
    data['requests'][identifier] = row
    attempts.save(root, run['id'], data)
    return row


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
        _assert_predecessors(root, run, previous)
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
        _assert_predecessors(root, run, previous)
        summary = _summary(reason, "skip reason")
        data = attempts.read(root, run_id)
        last = attempts.latest(data, stage_id)
        if last is not None:
            if attempts.observed(root, last)['status'] in {'unknown', 'running'}:
                raise _command('ACTION_RECONCILIATION_REQUIRED', '前次结果尚未确认，先核对再决定跳过')
        record = _record_stage(root, run, item, status="skipped", summary=summary)
        return {"run": run_id, "stage": stage_id, **record}


def _result_summary(result: Mapping[str, object]) -> str:
    diagnostics = result.get("diagnostics")
    if isinstance(diagnostics, list) and diagnostics:
        first = diagnostics[0]
        if isinstance(first, Mapping) and isinstance(first.get("code"), str):
            return f"Action failed: {first['code']}"
    return "Action completed" if result.get("status") == "ok" else "Action failed"


def action_result(root: Path, run_id: str, request_id: str) -> dict[str, object]:
    root = _root(root)
    attempts.identifier(request_id)
    item = attempts.read(root, run_id)["requests"].get(request_id)
    if item is None:
        raise _command("ACTION_REQUEST_NOT_FOUND", "请求不存在")
    return attempts.observed(root, item)


def _replay(root: Path, run_id: str, stage_id: str, plan_hash: str, request_id: str) -> dict | None:
    item = attempts.read(root, run_id)["requests"].get(request_id)
    if item is None:
        return None
    if item["stage"] != stage_id or item["planHash"] != plan_hash:
        raise _command("ACTION_REQUEST_CONFLICT", "请求编号已绑定其他输入")
    return {**attempts.observed(root, item), "replayed": True}


def run_action(
    root: Path, run_id: str, stage_id: str, plan_hash: str, *,
    request_id: str | None = None, previous_request_id: str | None = None,
    reason: str | None = None,
) -> dict[str, object]:
    root = _root(root)
    request_id = attempts.identifier(request_id or attempts.default_request_id(run_id, stage_id, plan_hash))
    replay = _replay(root, run_id, stage_id, plan_hash, request_id)
    if replay is not None:
        return replay
    with extension_lock(root, fcntl.LOCK_EX, create_cache=True):
        replay = _replay(root, run_id, stage_id, plan_hash, request_id)
        if replay is not None:
            return replay
        run, stage, binding, item, previous = _stage_context(root, run_id, stage_id)
        _assert_plan_hash(item, plan_hash)
        _assert_predecessors(root, run, previous)
        if binding.action.command is None:
            raise _command("ACTION_SKILL_ONLY", f"Action 没有 command：{binding.ref}")
        command = extension_command(binding.manifest, binding.action)
        if command is None:
            raise _command("ACTION_DRIFT", f"Action command 不安全或不可执行：{binding.ref}")
        data = attempts.read(root, run_id)
        last = attempts.latest(data, stage_id)
        if last is not None:
            last = attempts.observed(root, last)
            if last["status"] in {"unknown", "running"}:
                raise _command("ACTION_RECONCILIATION_REQUIRED", "前次结果未确认，先查询并核对")
            if previous_request_id != last["requestId"]:
                raise _command("ACTION_RETRY_REQUIRED", "重新执行必须引用最新请求并说明原因")
            _summary(reason, "retry reason")
        elif previous_request_id is not None:
            raise _command("ACTION_REQUEST_NOT_FOUND", "没有可重试的请求")
        record = {
            "requestId": request_id, "run": run_id, "stage": stage_id,
            "planHash": plan_hash, "fingerprint": item["fingerprint"],
            "sequence": max((x["sequence"] for x in data["requests"].values()), default=0) + 1,
            "action": binding.ref, "status": "running", "origin": "runner",
            "updatedAt": attempts.stamp(), "summary": "Action command started",
            "diagnosticCodes": [], "effects": list(binding.action.effects),
            "previousRequestId": previous_request_id, "reason": reason,
            "context": {key: run[key] for key in ("itemSlug", "repository", "branch")},
        }
        with attempts.lease(root, run_id, request_id):
            data["requests"][request_id] = record
            attempts.save(root, run_id, data)  # Publish intent before any process can start.
            request = {
                "workflow": run["workflow"], "run": run_id, "stage": stage.id,
                "itemSlug": run["itemSlug"], "repository": run["repository"],
                "branch": run["branch"], "with": dict(stage.config),
            }
            try:
                result = run_provider(command, cwd=binding.manifest.root, provider=binding.ref,
                                      request=request, environment=binding.action.environment or ())
            except BaseException:
                record.update(status="unknown", summary="执行中断，需核对外部结果", updatedAt=attempts.stamp())
                attempts.save(root, run_id, data)
                raise
            codes = [x.get("code") for x in result.get("diagnostics", []) if isinstance(x, Mapping) and isinstance(x.get("code"), str)][:20]
            did_not_start = any(d.get("code") == "PROVIDER_PROCESS_ERROR" and d.get("message") == "Provider 进程无法启动"
                                for d in result.get("diagnostics", []) if isinstance(d, Mapping))
            unknown = not did_not_start and any(code in {"PROVIDER_TIMEOUT", "PROVIDER_PROCESS_ERROR", "PROVIDER_PROTOCOL_ERROR", "PROVIDER_OUTPUT_LIMIT"} for code in codes)
            state = "succeeded" if result.get("status") == "ok" else "unknown" if unknown else "failed"
            record.update(status=state, summary="执行结果未知，先核对再重试" if unknown else _result_summary(result),
                          diagnosticCodes=codes, updatedAt=attempts.stamp())
            attempts.save(root, run_id, data)  # Result is authoritative even if its projection fails.
            return {**record, "replayed": False}


def reconcile_action(root: Path, run_id: str, request_id: str, *, status: str, summary: str, evidence: str) -> dict[str, object]:
    root = _root(root)
    if status not in {"succeeded", "failed", "skipped"}:
        raise _command("WORKFLOW_INVALID", "核对结果必须是 succeeded、failed 或 skipped")
    summary = _summary(summary, "reconcile summary")
    evidence = _summary(evidence, "reconcile evidence")
    with extension_lock(root, fcntl.LOCK_EX, create_cache=True):
        data = attempts.read(root, run_id)
        record = data["requests"].get(attempts.identifier(request_id))
        if record is None:
            raise _command("ACTION_REQUEST_NOT_FOUND", "请求不存在")
        if attempts.alive(root, run_id, request_id):
            raise _command("ACTION_BUSY", "请求仍在执行，不能覆盖结果")
        if attempts.latest(data, record["stage"])["requestId"] != request_id:
            raise _command("ACTION_REQUEST_STALE", "只能核对当前最新尝试")
        observed = attempts.observed(root, record)
        if record["origin"] == "reconciled" and record["status"] == status and record["summary"] == summary and record.get("evidence") == evidence:
            return observed
        if observed["status"] != "unknown":
            raise _command("ACTION_RESULT_FINAL", "已有明确结果，不能改写历史")
        record.update(status=status, summary=summary, evidence=evidence, origin="reconciled", updatedAt=attempts.stamp())
        attempts.save(root, run_id, data)
        return record


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
    pending = failed = interrupted = running = 0
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
                    last = attempts.latest(attempts.read(root, str(run["id"])), stage_id)
                    actual = attempts.observed(root, last)["status"] if last else None
                    if actual == "unknown":
                        interrupted += 1
                        continue
                    if status == "failed":
                        failed += 1
                        continue
                    if status == "running":
                        if actual == "running":
                            running += 1
                        else:
                            interrupted += 1
                        continue
                pending += 1
                if next_stage is None:
                    next_stage = stage_id
        except (WorkflowCommandError, ValueError) as exc:
            code, _, _ = str(exc).partition(": ")
            return {"enabled": True, "blockedCodes": [code]}
    return {
        "enabled": True,
        "runs": count,
        "actions": len(actions),
        "pending": pending,
        "failed": failed,
        "interrupted": interrupted,
        **({"running": running} if running else {}),
        "nextStage": next_stage,
    }


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
    start.add_argument("--item")
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
    run.add_argument("--request-id")
    retry = commands.add_parser("retry")
    retry.add_argument("--root", type=Path, default=Path.cwd())
    for flag in ("run", "stage", "plan-hash", "request-id", "previous-request-id", "reason"):
        retry.add_argument("--" + flag, required=True)
    retry.add_argument("--json", action="store_true")
    for name in ("result", "reconcile"):
        operation = commands.add_parser(name)
        operation.add_argument("--root", type=Path, default=Path.cwd())
        operation.add_argument("--run", required=True)
        operation.add_argument("--request-id", required=True)
        operation.add_argument("--json", action="store_true")
        if name == "reconcile":
            operation.add_argument("--status", choices=("succeeded", "failed", "skipped"), required=True)
            operation.add_argument("--summary", required=True)
            operation.add_argument("--evidence", required=True)
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
                    item_slug=args.item,
                    repository=args.repo,
                    branch=args.branch,
                ),
                args.json,
            )
        elif args.command == "plan":
            _print(plan_result(args.root, args.run, before=args.before, after=args.after), args.json)
        elif args.command in {"run", "retry"}:
            _print(run_action(args.root, args.run, args.stage, args.plan_hash,
                              request_id=args.request_id,
                              previous_request_id=getattr(args, "previous_request_id", None),
                              reason=getattr(args, "reason", None)), args.json)
        elif args.command == "result":
            _print(action_result(args.root, args.run, args.request_id), args.json)
        elif args.command == "reconcile":
            _print(reconcile_action(args.root, args.run, args.request_id, status=args.status,
                                    summary=args.summary, evidence=args.evidence), args.json)
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
        return 0
    except (OSError, RuntimeError, UnicodeError, ValueError, WorkspaceError, ExtensionError, WorkflowError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
