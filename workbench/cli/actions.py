"""Discover explicitly enabled standalone Extension Actions."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from workbench.errors import WorkbenchError
from workbench.extensions.management import ExtensionCommandError, validate_manifest_config
from workbench.extensions.runner import _active_actions
from workbench.workspace.local import load_local_settings
from workbench.workspace.model import load_workspace
from workbench.workspace.paths import workspace_file


def _relative(root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except (OSError, RuntimeError, ValueError) as exc:
        raise ExtensionCommandError(f"ACTION_PATH_INVALID: 路径越出治理仓：{path}") from exc


def _metadata(root: Path, binding) -> dict[str, object]:
    action = binding.action
    skill_root = binding.manifest.root / "skills" / action.skill
    skill_path = skill_root / "SKILL.md"
    return {
        "id": binding.ref,
        "extension": binding.manifest.id,
        "action": action.action,
        "standalone": action.standalone,
        "skill": action.skill,
        "skillPath": _relative(root, skill_path),
        "resourceRoot": _relative(root, skill_root),
        "summary": action.confirmation_summary or "",
        "confirmation": (
            {"title": action.confirmation_title, "summary": action.confirmation_summary}
            if action.confirmation_title is not None
            else None
        ),
        "effects": list(action.effects),
        "environment": list(action.environment or ()),
        "digest": binding.digest,
    }


def _actions(root: Path) -> dict[str, object]:
    if not workspace_file(root).is_file():
        return {}
    return _active_actions(root)


def list_result(root: Path) -> dict[str, object]:
    return {"actions": sorted((_metadata(root, binding) for binding in _actions(root).values() if binding.action.standalone), key=lambda row: row["id"])}


def resolve_result(root: Path, action_ref: str) -> dict[str, object]:
    binding = _actions(root).get(action_ref)
    if binding is None or not binding.action.standalone:
        raise ExtensionCommandError(f"ACTION_NOT_STANDALONE: 未找到已启用的独立 Skill-only Action：{action_ref}")
    workspace = load_workspace(root)
    local = load_local_settings(root, required=True)
    shared = workspace.extensions["config"].get(binding.manifest.id, {})
    local_config = local.extensions.get(binding.manifest.id, {})
    if not isinstance(shared, dict) or not isinstance(local_config, dict):
        raise ExtensionCommandError("ACTION_CONFIG_INVALID: Extension 配置必须是对象")
    config = validate_manifest_config(root, binding.manifest, shared, local_config)
    return {"action": _metadata(root, binding), "config": config}


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    listing = commands.add_parser("list"); listing.add_argument("--root", type=Path, default=Path.cwd()); listing.add_argument("--json", action="store_true")
    resolve = commands.add_parser("resolve"); resolve.add_argument("action_ref"); resolve.add_argument("--root", type=Path, default=Path.cwd()); resolve.add_argument("--json", action="store_true")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        root = args.root.resolve()
        result = list_result(root) if args.command == "list" else resolve_result(root, args.action_ref)
        if args.json:
            print(json.dumps(result, ensure_ascii=False))
        else:
            for action in result.get("actions", [result.get("action", {})]):
                print(f"{action['id']}：{action.get('summary', '')}")
                print(f"  Skill：{action.get('skillPath', '')}")
        return 0
    except (OSError, ValueError, WorkbenchError) as exc:
        if isinstance(exc, WorkbenchError):
            error = exc.as_dict()
        else:
            code, _, message = str(exc).partition(": ")
            error = {"code": code if code.isupper() else "ACTION_ERROR", "message": str(exc)}
        print(json.dumps({"error": error}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
