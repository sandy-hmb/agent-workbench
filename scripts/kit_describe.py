#!/usr/bin/env python3
"""Machine-readable capability surface: command -> parameters -> runbook."""
from __future__ import annotations

import argparse
import importlib
import json
import sys
from pathlib import Path
from typing import Optional, Sequence


sys.dont_write_bytecode = True

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from kit import COMMANDS  # noqa: E402


# 哪份文档负责讲某个顶层命令是编辑决策，不是 argparse 能自省出来的信息；
# 这张表和 workspace_status.py 的 STAGE_RUNBOOKS 性质相同，是唯一数据源。
COMMAND_RUNBOOKS = {
    "status": "docs/foundation/README.md",
    "doctor": "docs/foundation/README.md",
    "setup": "docs/getting-started.md",
    "registry": "docs/guides/first-feature.md",
    "provider": "docs/guides/local-extensions.md",
    "extension": ".agents/skills/workspace-extension/SKILL.md",
    "workflow": "docs/guides/custom-workflows.md",
    "context": "docs/guides/local-extensions.md",
    "feature": "docs/guides/first-feature.md",
    "submit": ".agents/skills/workspace-submit-test/SKILL.md",
    "update": ".agents/skills/workspace-update/SKILL.md",
    "migrate": "docs/reference/local-workspace-layout.md",
    "context-measure": "",
    "describe": "AGENTS.md",
    "brief": "docs/guides/first-feature.md",
    "verify": ".agents/skills/workspace-verify/SKILL.md",
    "inspect": "docs/reference/workbench-inspect.md",
}

EXEMPT = {
    "context-measure": "面向 Kit 维护者的上下文用量测量工具，不面向日常使用者，不要求出现在使用者文档中。",
}


def _describe_parser(parser: argparse.ArgumentParser) -> dict[str, object]:
    # parser._actions / argparse._SubParsersAction / action.choices 是 argparse
    # 的内部属性，但这是对已构造 parser 做只读自省的唯一标准库途径，形状长期稳定；
    # 项目零第三方依赖，不为此引入专门的自省库。
    options = sorted(
        max(action.option_strings, key=len)
        for action in parser._actions  # noqa: SLF001
        if action.option_strings
    )
    subcommands: dict[str, object] = {}
    for action in parser._actions:  # noqa: SLF001
        if isinstance(action, argparse._SubParsersAction):  # noqa: SLF001
            for name, subparser in sorted(action.choices.items()):
                subcommands[name] = _describe_parser(subparser)
    return {"options": options, "subcommands": subcommands}


def _active_extension_actions(root: Path) -> list[dict[str, str]]:
    from workspace_paths import lock_file

    lock_path = lock_file(Path(root))
    if not lock_path.is_file() or lock_path.is_symlink():
        return []
    try:
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return []
    if not isinstance(lock, dict):
        return []
    active_ids = {
        item["id"]
        for item in lock.get("extensions", [])
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    if not active_ids:
        return []
    from workspace_extension import discover_extensions

    try:
        manifests = discover_extensions(root)
    except Exception:
        return []
    result: list[dict[str, str]] = []
    for extension_id in sorted(active_ids):
        manifest = manifests.get(extension_id)
        if manifest is None:
            continue
        for action in manifest.actions:
            skill_path = manifest.root / "skills" / action.skill / "SKILL.md"
            result.append(
                {
                    "id": f"{extension_id}/{action.action}",
                    "summary": action.confirmation_summary or "",
                    "skill": str(skill_path.relative_to(Path(root).resolve())),
                }
            )
    return result


def describe_result(root: Path) -> dict[str, object]:
    commands = []
    for name in sorted(COMMANDS):
        module_name, summary = COMMANDS[name]
        module = importlib.import_module(module_name)
        commands.append(
            {
                "name": name,
                "module": module_name,
                "summary": summary,
                "runbook": COMMAND_RUNBOOKS.get(name, ""),
                "parameters": _describe_parser(module.build_parser()),
            }
        )
    return {
        "commands": commands,
        "extensionActions": _active_extension_actions(Path(root).resolve()),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    result = describe_result(args.root)
    print(json.dumps(result, ensure_ascii=False) if args.json else json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
