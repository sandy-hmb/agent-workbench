"""Canonical identifiers shared by workflow, work-item and submit governance."""
from __future__ import annotations

import re
from pathlib import PurePosixPath


ITEM_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
WORKFLOW_RUN_ID_RE = ITEM_SLUG_RE
STAGE_ID_RE = re.compile(
    r"^[a-z0-9]+(?:-[a-z0-9]+)*(?:\.[a-z0-9]+(?:-[a-z0-9]+)*)+$"
)
REQUEST_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
PLAN_HASH_RE = re.compile(r"^[0-9a-f]{64}$")


def _required(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"ARGUMENT_INVALID: {label} 必须是非空字符串")
    return value


def item_slug(value: object, *, field: str = "itemSlug") -> str:
    value = _required(value, field)
    if not ITEM_SLUG_RE.fullmatch(value):
        raise ValueError(f"ARGUMENT_INVALID: {field} 必须是 WorkItem slug（小写 kebab-case），收到：{value}")
    return value


def workflow_run_id(value: object, *, field: str = "workflowRunId") -> str:
    value = _required(value, field)
    if not WORKFLOW_RUN_ID_RE.fullmatch(value):
        raise ValueError(f"ARGUMENT_INVALID: {field} 必须是 Workflow Run ID（小写 kebab-case），收到：{value}")
    return value


def stage_id(value: object, *, field: str = "stageId") -> str:
    value = _required(value, field)
    if not STAGE_ID_RE.fullmatch(value):
        raise ValueError(f"ARGUMENT_INVALID: {field} 必须是 Workflow Stage ID（含点号的 kebab-case），收到：{value}")
    return value


def request_id(value: object, *, field: str = "requestId") -> str:
    value = _required(value, field)
    if not REQUEST_ID_RE.fullmatch(value) or ".." in value:
        raise ValueError(f"ARGUMENT_INVALID: {field} 必须是 Action 请求 ID，不能包含路径片段，收到：{value}")
    return value


def plan_hash(value: object, *, field: str = "planHash") -> str:
    value = _required(value, field)
    if not PLAN_HASH_RE.fullmatch(value):
        raise ValueError(f"ARGUMENT_INVALID: {field} 必须是 64 位小写 SHA-256 哈希，收到：{value}")
    return value


def repository_path(value: object, *, field: str = "repositoryPath") -> str:
    value = _required(value, field)
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or not path.parts
        or any(part in {"", ".", ".."} for part in path.parts)
        or "\\" in value
        or "\0" in value
        or any(char in value for char in "\r\n")
    ):
        raise ValueError(f"ARGUMENT_INVALID: {field} 必须是已登记业务仓的安全相对路径，收到：{value}")
    return path.as_posix()


def describe_types() -> dict[str, str]:
    return {
        "itemSlug": "WorkItem 唯一短名",
        "workflowRunId": "Workflow Run 唯一 ID",
        "stageId": "Workflow Stage ID",
        "requestId": "Action 请求 ID",
        "planHash": "当前 Action 计划哈希",
        "repositoryPath": "Workspace Registry 中的业务仓相对路径",
    }
