#!/usr/bin/env python3
"""Validated Core Workflow and local Workflow Overlay models."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Mapping

from schema_validation import SchemaValidationError, validate
from workspace_model import WorkspaceError, parse_json_bytes, reject_sensitive_fields


ID_RE = re.compile(
    r"^[a-z0-9]+(?:-[a-z0-9]+)*(?:\.[a-z0-9]+(?:-[a-z0-9]+)*)+$"
)
WORKFLOW_ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
ACTION_REF_RE = re.compile(
    r"^[a-z0-9]+(?:-[a-z0-9]+)*/[a-z0-9]+(?:-[a-z0-9]+)*$"
)
TRIGGERS = frozenset({"manual", "auto"})
SCHEMA_PATH = Path(__file__).resolve().parents[1] / "schemas/workspace-workflow.schema.json"


def schema_version_major(value: object) -> int:
    if (
        isinstance(value, dict)
        and type(value.get("major")) is int
        and type(value.get("minor")) is int
        and value["minor"] >= 0
    ):
        return value["major"]
    raise WorkflowError("schemaVersion 必须为 {major, minor}")


class WorkflowError(WorkspaceError):
    """Raised when a Core Workflow or local Overlay is invalid."""


def _error(code: str, message: str) -> None:
    raise WorkflowError(f"{code}: {message}")


@dataclass(frozen=True)
class CoreStage:
    id: str
    after: str | None
    optional: bool = False


@dataclass(frozen=True)
class CustomStage:
    id: str
    before: str | None
    after: str | None
    uses: str
    trigger: str
    config: Mapping[str, object]
    index: int

    @property
    def anchor(self) -> str:
        return self.before if self.before is not None else self.after  # type: ignore[return-value]


@dataclass(frozen=True)
class SkipHint:
    stage: str
    max_repository_count: int
    reason: str


@dataclass(frozen=True)
class WorkflowDefinition:
    id: str
    stages: tuple[CoreStage, ...]

    @property
    def stage_ids(self) -> tuple[str, ...]:
        return tuple(stage.id for stage in self.stages)


@dataclass(frozen=True)
class WorkflowOverlay:
    workflow: str
    stages: tuple[CustomStage, ...]
    skip_hints: tuple[SkipHint, ...] = ()


@dataclass(frozen=True)
class ResolvedStage:
    id: str
    core: bool
    optional: bool = False
    action: str | None = None
    trigger: str | None = None
    config: Mapping[str, object] | None = None


@dataclass(frozen=True)
class ResolvedWorkflow:
    workflow: str
    stages: tuple[ResolvedStage, ...]

    @property
    def stage_ids(self) -> tuple[str, ...]:
        return tuple(stage.id for stage in self.stages)


def _read_json(path: Path, label: str) -> dict[str, object]:
    try:
        if path.is_symlink() or not path.is_file():
            _error("WORKFLOW_INVALID", f"{label} 必须是普通文件：{path}")
        return parse_json_bytes(path.read_bytes(), str(path))
    except WorkflowError:
        raise
    except (OSError, RuntimeError, UnicodeError, WorkspaceError) as exc:
        _error("WORKFLOW_INVALID", f"无法读取 {label}：{exc}")


@lru_cache(maxsize=1)
def _overlay_schema() -> dict[str, object]:
    return _read_json(SCHEMA_PATH, "Workflow Schema")


def _valid_stage_id(value: object) -> bool:
    return isinstance(value, str) and bool(ID_RE.fullmatch(value))


def load_core_workflow(path: Path) -> WorkflowDefinition:
    raw = _read_json(Path(path), "Core Workflow")
    if set(raw) != {"schemaVersion", "id", "stages"}:
        _error("WORKFLOW_INVALID", "Core Workflow 字段无效")
    if raw["schemaVersion"] != 1 or type(raw["schemaVersion"]) is not int:
        _error("WORKFLOW_INVALID", "Core Workflow schemaVersion 必须为 1")
    workflow_id = raw["id"]
    if not isinstance(workflow_id, str) or not WORKFLOW_ID_RE.fullmatch(workflow_id):
        _error("WORKFLOW_INVALID", "Core Workflow id 无效")
    values = raw["stages"]
    if not isinstance(values, list) or not values:
        _error("WORKFLOW_INVALID", "Core Workflow stages 必须是非空数组")

    stages: list[CoreStage] = []
    seen: set[str] = set()
    previous: str | None = None
    for value in values:
        if not isinstance(value, dict) or set(value) - {"id", "after", "optional"}:
            _error("WORKFLOW_INVALID", "Core Stage 字段无效")
        if "id" not in value or not _valid_stage_id(value["id"]):
            _error("WORKFLOW_INVALID", "Core Stage id 无效")
        stage_id = value["id"]
        assert isinstance(stage_id, str)
        if stage_id in seen:
            _error("WORKFLOW_INVALID", f"Core Stage 重复：{stage_id}")
        after = value.get("after")
        if after is not None and not _valid_stage_id(after):
            _error("WORKFLOW_INVALID", f"Core Stage after 无效：{stage_id}")
        if after != previous:
            _error("WORKFLOW_INVALID", f"Core Stage 必须按顺序声明：{stage_id}")
        optional = value.get("optional", False)
        if type(optional) is not bool:
            _error("WORKFLOW_INVALID", f"Core Stage optional 必须为布尔值：{stage_id}")
        stages.append(CoreStage(stage_id, after, optional))
        seen.add(stage_id)
        previous = stage_id
    return WorkflowDefinition(workflow_id, tuple(stages))


def load_overlay(path: Path) -> WorkflowOverlay:
    raw = _read_json(Path(path), "Workflow Overlay")
    if schema_version_major(raw.get("schemaVersion")) != 1:
        _error("WORKFLOW_INVALID", "Workflow Overlay schemaVersion.major 必须为 1")
    try:
        validate(raw, _overlay_schema())
    except SchemaValidationError as exc:
        _error("WORKFLOW_INVALID", str(exc))
    workflow = raw["workflow"]
    assert isinstance(workflow, str)
    values = raw["stages"]
    assert isinstance(values, list)
    stages: list[CustomStage] = []
    seen: set[str] = set()
    for index, value in enumerate(values):
        if not isinstance(value, dict):
            _error("WORKFLOW_INVALID", "Custom Stage 必须是对象")
        stage_id = value["id"]
        uses = value["uses"]
        assert isinstance(stage_id, str) and isinstance(uses, str)
        if stage_id in seen:
            _error("WORKFLOW_INVALID", f"Custom Stage 重复：{stage_id}")
        before = value.get("before")
        after = value.get("after")
        if (before is None) == (after is None):
            _error("WORKFLOW_INVALID", f"Custom Stage 必须恰好声明 before 或 after：{stage_id}")
        if before is not None and not isinstance(before, str):
            _error("WORKFLOW_INVALID", f"Custom Stage before 无效：{stage_id}")
        if after is not None and not isinstance(after, str):
            _error("WORKFLOW_INVALID", f"Custom Stage after 无效：{stage_id}")
        if not ACTION_REF_RE.fullmatch(uses):
            _error("WORKFLOW_INVALID", f"Custom Stage uses 无效：{stage_id}")
        trigger = value.get("trigger", "manual")
        if not isinstance(trigger, str) or trigger not in TRIGGERS:
            _error("WORKFLOW_INVALID", f"Custom Stage trigger 无效：{stage_id}")
        config = value.get("with", {})
        if not isinstance(config, dict):
            _error("WORKFLOW_INVALID", f"Custom Stage with 必须是对象：{stage_id}")
        try:
            reject_sensitive_fields(config, f"workflow.stages[{index}].with")
        except WorkspaceError as exc:
            _error("WORKFLOW_INVALID", str(exc))
        stages.append(
            CustomStage(stage_id, before, after, uses, trigger, dict(config), index)
        )
        seen.add(stage_id)
    skip_hints: list[SkipHint] = []
    for value in raw.get("skipHints", []):
        stage_id = value["stage"]
        reason = value["reason"]
        max_count = value["when"]["repositoryCount"]["max"]
        assert isinstance(stage_id, str) and isinstance(reason, str)
        if type(max_count) is not int or max_count < 0:
            _error(
                "WORKFLOW_SKIP_HINT_INVALID",
                f"repositoryCount.max 必须是非负整数：{stage_id}",
            )
        skip_hints.append(SkipHint(stage_id, max_count, reason))
    return WorkflowOverlay(workflow, tuple(stages), tuple(skip_hints))


def _stable_topological_order(
    stages: tuple[CustomStage, ...], edges: dict[str, set[str]]
) -> tuple[CustomStage, ...]:
    values = {stage.id: stage for stage in stages}
    incoming = {stage.id: 0 for stage in stages}
    for source, targets in edges.items():
        if source not in values:
            _error("WORKFLOW_INVALID", f"未知 Custom Stage：{source}")
        for target in targets:
            if target not in values:
                _error("WORKFLOW_INVALID", f"未知 Custom Stage：{target}")
            incoming[target] += 1
    ready = sorted(
        (stage for stage in stages if incoming[stage.id] == 0), key=lambda stage: stage.index
    )
    result: list[CustomStage] = []
    while ready:
        stage = ready.pop(0)
        result.append(stage)
        for target in sorted(edges.get(stage.id, ()), key=lambda item: values[item].index):
            incoming[target] -= 1
            if incoming[target] == 0:
                ready.append(values[target])
                ready.sort(key=lambda item: item.index)
    if len(result) != len(stages):
        _error("WORKFLOW_CYCLE", "Custom Stage 存在循环依赖")
    return tuple(result)


def resolve_stages(core: WorkflowDefinition, overlay: WorkflowOverlay) -> ResolvedWorkflow:
    if overlay.workflow != core.id:
        _error("WORKFLOW_INVALID", f"Overlay 不能用于 Core Workflow：{overlay.workflow}")
    core_ids = set(core.stage_ids)
    custom = {stage.id: stage for stage in overlay.stages}
    if len(custom) != len(overlay.stages):
        _error("WORKFLOW_INVALID", "Custom Stage 重复")
    duplicate = core_ids & set(custom)
    if duplicate:
        _error("WORKFLOW_INVALID", f"Custom Stage 覆盖 Core Stage：{sorted(duplicate)[0]}")
    known = core_ids | set(custom)
    for stage in overlay.stages:
        if stage.anchor not in known:
            _error("WORKFLOW_ANCHOR_MISSING", f"未知 Stage 锚点：{stage.anchor}")
    _validate_skip_hints(core, overlay)

    core_order = core.stage_ids
    previous = {stage_id: core_order[index - 1] if index else None for index, stage_id in enumerate(core_order)}
    following = {
        stage_id: core_order[index + 1] if index + 1 < len(core_order) else None
        for index, stage_id in enumerate(core_order)
    }
    regions: dict[str, tuple[str | None, str | None]] = {}

    def region_for(stage_id: str, trail: tuple[str, ...] = ()) -> tuple[str | None, str | None]:
        if stage_id in regions:
            return regions[stage_id]
        if stage_id in trail:
            _error("WORKFLOW_CYCLE", f"Custom Stage 锚点循环：{stage_id}")
        stage = custom[stage_id]
        anchor = stage.anchor
        if anchor in core_ids:
            region = (previous[anchor], anchor) if stage.before else (anchor, following[anchor])
        else:
            region = region_for(anchor, (*trail, stage_id))
        regions[stage_id] = region
        return region

    groups: dict[tuple[str | None, str | None], list[CustomStage]] = {}
    for stage in overlay.stages:
        groups.setdefault(region_for(stage.id), []).append(stage)

    ordered_groups: dict[tuple[str | None, str | None], tuple[CustomStage, ...]] = {}
    for region, stages in groups.items():
        edges: dict[str, set[str]] = {}
        members = {stage.id for stage in stages}
        for stage in stages:
            if stage.anchor in custom:
                if stage.anchor not in members:
                    _error("WORKFLOW_INVALID", f"Custom Stage 跨区引用：{stage.id}")
                source, target = (stage.id, stage.anchor) if stage.before else (stage.anchor, stage.id)
                edges.setdefault(source, set()).add(target)
        ordered_groups[region] = _stable_topological_order(tuple(stages), edges)

    resolved: list[ResolvedStage] = []
    for index, stage in enumerate(core.stages):
        resolved.extend(
            ResolvedStage(
                item.id,
                core=False,
                action=item.uses,
                trigger=item.trigger,
                config=item.config,
            )
            for item in ordered_groups.get((previous[stage.id], stage.id), ())
        )
        resolved.append(ResolvedStage(stage.id, core=True, optional=stage.optional))
        if index + 1 == len(core.stages):
            resolved.extend(
                ResolvedStage(
                    item.id,
                    core=False,
                    action=item.uses,
                    trigger=item.trigger,
                    config=item.config,
                )
                for item in ordered_groups.get((stage.id, None), ())
            )
    return ResolvedWorkflow(core.id, tuple(resolved))


def _validate_skip_hints(core: WorkflowDefinition, overlay: WorkflowOverlay) -> None:
    optional_ids = {stage.id for stage in core.stages if stage.optional}
    for hint in overlay.skip_hints:
        if hint.stage not in optional_ids:
            _error(
                "WORKFLOW_SKIP_HINT_INVALID",
                f"跳过建议只能用于可选 Core Stage：{hint.stage}",
            )


def evaluate_skip_hints(
    core: WorkflowDefinition, overlay: WorkflowOverlay, *, repository_count: int
) -> tuple[dict[str, object], ...]:
    """按仓库数量返回匹配的跳过建议；纯只读，不修改任何状态。"""
    _validate_skip_hints(core, overlay)
    return tuple(
        {
            "stage": hint.stage,
            "reason": hint.reason,
            "rule": f"repositoryCount<={hint.max_repository_count}",
        }
        for hint in overlay.skip_hints
        if repository_count <= hint.max_repository_count
    )
