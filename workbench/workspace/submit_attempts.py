"""Durable, phase-oriented facts for workspace submit recovery."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from workbench.workspace.model import atomic_write_many


PHASES = (
    "commit",
    "featurePush",
    "fetch",
    "testMerge",
    "testPush",
    "restoreBranch",
    "delivery",
)


def stamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def path(root: Path, attempt_id: str) -> Path:
    if not attempt_id or "/" in attempt_id or "\\" in attempt_id or ".." in attempt_id:
        raise ValueError("SUBMIT_ATTEMPT_INVALID: attemptId 无效")
    return Path(root) / ".workspace" / "submit-attempts" / f"{attempt_id}.json"


def create(root: Path, attempt_id: str, plan: dict[str, object]) -> dict[str, object]:
    target = path(root, attempt_id)
    if target.exists():
        existing = load(root, attempt_id)
        if existing.get("planHash") != plan.get("planHash"):
            raise ValueError("SUBMIT_ATTEMPT_CONFLICT: attemptId 已绑定其他计划")
        return existing
    value = {
        "schemaVersion": 1,
        "attemptId": attempt_id,
        "planHash": plan["planHash"],
        "plan": plan,
        "createdAt": stamp(),
        "updatedAt": stamp(),
        "phases": {name: {"status": "pending"} for name in PHASES},
    }
    save(root, attempt_id, value)
    return value


def load(root: Path, attempt_id: str) -> dict[str, object]:
    target = path(root, attempt_id)
    if not target.is_file() or target.is_symlink():
        raise ValueError(f"SUBMIT_ATTEMPT_NOT_FOUND: attemptId 不存在：{attempt_id}")
    value = json.loads(target.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("schemaVersion") != 1 or value.get("attemptId") != attempt_id:
        raise ValueError("SUBMIT_ATTEMPT_INVALID: 提交尝试记录结构无效")
    if not isinstance(value.get("phases"), dict) or not isinstance(value.get("plan"), dict):
        raise ValueError("SUBMIT_ATTEMPT_INVALID: 提交尝试缺少计划或阶段")
    return value


def save(root: Path, attempt_id: str, value: dict[str, object]) -> None:
    target = path(root, attempt_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    value["updatedAt"] = stamp()
    atomic_write_many(((target, json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"),))


def update(root: Path, attempt_id: str, phase: str, status: str, **facts: object) -> dict[str, object]:
    if phase not in PHASES:
        raise ValueError(f"SUBMIT_ATTEMPT_INVALID: 未知提交阶段：{phase}")
    if status not in {"pending", "running", "succeeded", "failed", "unknown"}:
        raise ValueError(f"SUBMIT_ATTEMPT_INVALID: 未知提交阶段状态：{status}")
    value = load(root, attempt_id)
    phases = value["phases"]
    assert isinstance(phases, dict)
    row = dict(phases.get(phase) or {})
    row.update(status=status, **facts)
    phases[phase] = row
    save(root, attempt_id, value)
    return value
