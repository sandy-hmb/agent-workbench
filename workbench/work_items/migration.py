"""Plan and apply the WorkItem storage layout migration."""
from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

from workbench.work_items.commands import render
from workbench.work_items.store import (
    WorkItemError,
    atomic_write,
    canonical,
    digest,
    has_modern_storage,
    item_area,
    item_path,
    load_state,
    modern_state_root,
    read_bytes,
    read_json,
    safe_path,
    state_path,
)


KNOWN_ROOT_FILES = {
    "README.md",
    "change.md",
    "requirements.md",
    "design.md",
    "plan.md",
    "verification.md",
    "state.json",
    ".state.lock",
}
KNOWN_ROOT_DIRECTORIES = {".state", "evidence", "history", "references", "artifacts", "testing"}


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _relative(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _item_plan(item: Path) -> dict[str, object]:
    item = item.resolve()
    if has_modern_storage(item):
        return {"slug": item.name, "path": str(item), "status": "modern", "moves": [], "conflicts": []}
    legacy_state = safe_path(item, "state.json")
    if not legacy_state.is_file():
        return {"slug": item.name, "path": str(item), "status": "unsupported", "moves": [], "conflicts": ["state.json"]}
    load_state(item)
    moves: list[dict[str, str]] = []
    conflicts: list[str] = []

    def add_file(source: Path, target: Path) -> None:
        if not source.exists():
            return
        if target.exists() or target.is_symlink():
            conflicts.append(_relative(target, item))
            return
        moves.append({"source": _relative(source, item), "target": _relative(target, item)})

    add_file(item / "state.json", modern_state_root(item) / "state.json")
    add_file(item / ".state.lock", modern_state_root(item) / "lock")
    for directory, target_name in (("evidence", "evidence"), ("history", "history")):
        source = item / directory
        if source.is_dir():
            for path in sorted(source.rglob("*")):
                if path.is_file():
                    add_file(path, modern_state_root(item) / target_name / path.relative_to(source))
    if (item / "verification.md").is_file():
        add_file(item / "verification.md", modern_state_root(item) / "inputs/legacy/verification.md")
    for path in sorted(item.iterdir()):
        if path.name in KNOWN_ROOT_FILES or path.name in KNOWN_ROOT_DIRECTORIES:
            continue
        if path.is_file() and path.suffix.lower() in {".json", ".txt", ".log"}:
            add_file(path, modern_state_root(item) / "inputs/legacy" / path.name)
        elif path.is_dir() and path.name == "testing":
            for child in sorted(path.rglob("*")):
                if child.is_file():
                    add_file(child, item / "artifacts/testing" / child.relative_to(path))
    testing = item / "testing"
    if testing.is_dir():
        for child in sorted(testing.rglob("*")):
            if child.is_file():
                add_file(child, item / "artifacts/testing" / child.relative_to(testing))
    return {"slug": item.name, "path": str(item), "status": "legacy", "moves": moves, "conflicts": conflicts}


def _items(root: Path, slug: str | None, all_items: bool) -> list[Path]:
    area = item_area(root)
    if slug:
        return [item_path(root, slug)]
    if not all_items:
        raise WorkItemError("MIGRATION_TARGET_REQUIRED", "请指定 slug 或 --all")
    if not area.is_dir():
        return []
    return [path for path in sorted(area.iterdir()) if path.is_dir() and not path.is_symlink()]


def preview(root: Path, slug: str | None = None, all_items: bool = False) -> dict[str, object]:
    root = Path(root).resolve()
    plans = [_item_plan(path) for path in _items(root, slug, all_items)]
    conflicts = [
        {"slug": plan["slug"], "paths": plan["conflicts"]}
        for plan in plans
        if plan["conflicts"]
    ]
    payload = {"root": str(root), "items": plans, "conflicts": conflicts}
    plan_hash = digest(payload)
    backup_dir = root.parent / f"{root.name}-item-storage-backup-{plan_hash[7:19]}"
    target_args = "--all" if all_items else f"--slug {slug}"
    return {"schemaVersion": 1, "operation": "item-storage-migrate", "planHash": plan_hash,
            "backupDir": str(backup_dir), "items": plans, "conflicts": conflicts,
            "upToDate": all(plan["status"] == "modern" for plan in plans),
            "blocked": bool(conflicts),
            "applyCommand": f"python3 scripts/kit.py item storage-migrate apply --root {root} {target_args} --plan-hash {plan_hash} --backup-dir {backup_dir}"}


def _copy_backup(item: Path, backup_root: Path) -> None:
    target = backup_root / item.name
    if target.exists():
        raise WorkItemError("MIGRATION_BACKUP_EXISTS", f"备份目标已存在：{target}")
    shutil.copytree(item, target, symlinks=True)


def _move(source: Path, target: Path) -> None:
    if not source.exists():
        return
    if target.exists() or target.is_symlink():
        raise WorkItemError("MIGRATION_TARGET_CONFLICT", f"目标已存在：{target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    source.rename(target)


def _apply_item(root: Path, item: Path) -> None:
    if has_modern_storage(item):
        return
    plan = _item_plan(item)
    if plan["conflicts"]:
        raise WorkItemError("MIGRATION_CONFLICT", f"{item.name} 存在目标冲突：{plan['conflicts']}")
    modern = modern_state_root(item)
    modern.mkdir(parents=True, exist_ok=True)
    for move in plan["moves"]:
        _move(safe_path(item, move["source"]), safe_path(item, move["target"]))
    for name in ("evidence", "history", "testing"):
        legacy = item / name
        if legacy.is_dir() and not any(legacy.iterdir()):
            legacy.rmdir()
    old_state = read_json(safe_path(modern, "state.json"))
    history = old_state.get("history", [])
    for row in history:
        if isinstance(row, dict) and isinstance(row.get("path"), str) and row["path"].startswith("history/"):
            row["path"] = ".state/" + row["path"]
    if history:
        old_state["history"] = history
        old_state["stateRevision"] = digest({k: v for k, v in old_state.items() if k != "stateRevision"})
        atomic_write(modern / "state.json", canonical(old_state) + b"\n")
    render(root, item.name)


def apply(root: Path, expected_hash: str, backup_dir: Path, slug: str | None = None, all_items: bool = False) -> dict[str, object]:
    root = Path(root).resolve()
    plan = preview(root, slug=slug, all_items=all_items)
    if plan["planHash"] != expected_hash:
        raise WorkItemError("MIGRATION_PLAN_CHANGED", "迁移预览已变化，请重新生成 preview")
    if plan["blocked"]:
        raise WorkItemError("MIGRATION_BLOCKED", f"迁移存在冲突：{plan['conflicts']}")
    pending = [plan_item for plan_item in plan["items"] if plan_item["status"] != "modern"]
    if not pending:
        return {"migrated": [], "backupDir": None, "planHash": expected_hash, "upToDate": True}
    backup_dir = Path(backup_dir).expanduser().resolve()
    if backup_dir.exists():
        raise WorkItemError("MIGRATION_BACKUP_EXISTS", f"备份目标已存在：{backup_dir}")
    backup_dir.mkdir(parents=True, exist_ok=False)
    applied = []
    for plan_item in pending:
        item = Path(plan_item["path"])
        _copy_backup(item, backup_dir)
        _apply_item(root, item)
        applied.append(plan_item["slug"])
    return {"migrated": applied, "backupDir": str(backup_dir), "planHash": expected_hash, "upToDate": False}
