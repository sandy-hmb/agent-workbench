#!/usr/bin/env python3
"""统一入口：转发到 scripts/ 下各具备独立 CLI 的脚本，不重新实现任何业务逻辑。"""

from __future__ import annotations

import importlib
import json
from workbench.errors import WorkbenchError
import sys
from pathlib import Path
from typing import Optional, Sequence

sys.dont_write_bytecode = True



# 子命令名 -> (被转发的脚本模块名, 一句话场景说明)。
# 只注册具备独立 CLI（含 `if __name__ == "__main__"`）的脚本；纯库模块不出现在此表。
COMMANDS = {
    "status": ("workbench.cli.status", "只读展示工作区/需求当前阶段与下一步"),
    "doctor": ("workbench.workspace.doctor", "校验配置与环境，输出可诊断的 ERROR/WARN"),
    "setup": ("workbench.workspace.setup", "初始化输入探测、draft、plan/preview/apply"),
    "registry": ("workbench.workspace.registry", "解析 workspace.json 中注册的仓库"),
    "provider": ("workbench.extensions.providers", "探测并运行绑定的本地 Extension Provider"),
    "extension": ("workbench.extensions.management", "预览、激活、校验本地 Extension"),
    "workflow": ("workbench.extensions.runner", "解析并执行自定义工作流 Action"),
    "context": ("workbench.workspace.context", "按显式上下文条件路由请求"),
    "item": ("workbench.cli.items", "创建、调整、审批与结束工作项"),
    "submit": ("workbench.workspace.submit", "把需求交付到配置的测试分支"),
    "update": ("workbench.workspace.update", "预检并应用公共 Kit 兼容性更新"),
    "context-measure": ("workbench.context_measure", "测量典型路径的上下文消耗基线"),
    "describe": ("workbench.cli.describe", "生成脚本→子命令→runbook 的机器可读能力清单"),
    "brief": ("workbench.cli.brief", "接手进行中需求的最小上下文包（--json/文本）"),
    "verify": ("workbench.cli.verify", "采集、记录和查询实际验证结果"),
    "inspect": ("workbench.inspection.api", "供工作台按需读取工作区、需求与工作流记录"),
}

ROOT_BEFORE_SUBCOMMAND = frozenset(
    {"setup", "registry", "describe", "context-measure", "inspect"}
)


def _help_text() -> str:
    lines = [
        "用法：python3 scripts/kit.py <子命令> [脚本自身参数...]",
        "",
        "Kit 使用本地 Python 包，无需安装。",
        "查询具体命令参数，例如：",
        "  python3 scripts/kit.py status --help",
        "",
        "可用子命令：",
    ]
    width = max(len(name) for name in COMMANDS)
    for name in sorted(COMMANDS):
        module_name, description = COMMANDS[name]
        lines.append(f"  {name.ljust(width)}  {description}")
    return "\n".join(lines)


def _root_value(arguments: list[str], index: int) -> tuple[str, int]:
    value = arguments[index]
    if value == "--root":
        if index + 1 >= len(arguments) or arguments[index + 1].startswith("-"):
            raise ValueError("--root 需要路径参数")
        return arguments[index + 1], 2
    return value.partition("=")[2], 1


def normalize_root_arguments(name: str, arguments: Sequence[str]) -> list[str]:
    """Accept --root before or after nested subcommands and canonicalize it."""
    values = list(arguments)
    matches: list[tuple[int, str, int]] = []
    index = 0
    while index < len(values):
        value = values[index]
        if value == "--root" or value.startswith("--root="):
            root, consumed = _root_value(values, index)
            if not root:
                raise ValueError("--root 需要非空路径")
            matches.append((index, root, consumed))
            index += consumed
        else:
            index += 1
    if not matches:
        return values
    if len(matches) > 1:
        raise ValueError("--root 只能指定一次")
    position, root, consumed = matches[0]
    remaining = values[:position] + values[position + consumed:]
    return (["--root", root, *remaining] if name in ROOT_BEFORE_SUBCOMMAND
            else [*remaining, "--root", root])


def main(argv: Optional[Sequence[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    if not argv or argv[0] in ("-h", "--help"):
        print(_help_text())
        return 0

    if argv[0] == "--root" or argv[0].startswith("--root="):
        try:
            root, consumed = _root_value(argv, 0)
        except ValueError as exc:
            print(f"参数错误：{exc}", file=sys.stderr)
            return 2
        if len(argv) <= consumed:
            print("参数错误：--root 后缺少子命令", file=sys.stderr)
            return 2
        name = argv[consumed]
        tail = argv[consumed + 1:]
        rest = (["--root", root, *tail] if name in ROOT_BEFORE_SUBCOMMAND else [*tail, "--root", root])
    else:
        name, rest = argv[0], argv[1:]
    if name not in COMMANDS:
        print(
            f"未知子命令：{name}\n可用子命令：{', '.join(sorted(COMMANDS))}",
            file=sys.stderr,
        )
        return 2

    try:
        rest = normalize_root_arguments(name, rest)
    except ValueError as exc:
        print(f"参数错误：{exc}", file=sys.stderr)
        return 2
    module_name, _ = COMMANDS[name]
    module = importlib.import_module(module_name)
    try:
        return module.main(rest)
    except WorkbenchError as exc:
        print(json.dumps({'error': exc.as_dict()}, ensure_ascii=False))
        return 2 if exc.code == 'ARGUMENT_INVALID' else 1


if __name__ == "__main__":
    raise SystemExit(main())
