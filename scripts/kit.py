#!/usr/bin/env python3
"""统一入口：转发到 scripts/ 下各具备独立 CLI 的脚本，不重新实现任何业务逻辑。"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from typing import Optional, Sequence

sys.dont_write_bytecode = True

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))


# 子命令名 -> (被转发的脚本模块名, 一句话场景说明)。
# 只注册具备独立 CLI（含 `if __name__ == "__main__"`）的脚本；纯库模块不出现在此表。
COMMANDS = {
    "status": ("workspace_status", "只读展示工作区/需求当前阶段与下一步"),
    "doctor": ("workspace_doctor", "校验配置与环境，输出可诊断的 ERROR/WARN"),
    "setup": ("workspace_setup", "初始化输入探测、draft、plan/preview/apply"),
    "registry": ("workspace_registry", "解析 workspace.json 中注册的仓库"),
    "provider": ("workspace_provider", "探测并运行绑定的本地 Extension Provider"),
    "extension": ("workspace_extension", "预览、激活、校验本地 Extension"),
    "workflow": ("workspace_workflow", "解析并执行自定义工作流 Action"),
    "context": ("workspace_context", "按显式上下文条件路由请求"),
    "feature": ("feature_context", "创建、列出、更新需求目录元数据"),
    "submit": ("workspace_submit", "把需求交付到配置的测试分支"),
    "update": ("workspace_update", "预检并应用公共 Kit 兼容性更新"),
    "migrate": ("workspace_migrate", "预览/应用版本化 .workspace 状态迁移"),
    "context-measure": ("context_measure", "测量典型路径的上下文消耗基线"),
    "describe": ("kit_describe", "生成脚本→子命令→runbook 的机器可读能力清单"),
    "brief": ("kit_feature_brief", "接手进行中需求的最小上下文包（--json/文本）"),
    "verify": ("workspace_verification", "读取需求当前 Git 代码状态指纹"),
}


def _help_text() -> str:
    lines = [
        "用法：python3 scripts/kit.py <子命令> [脚本自身参数...]",
        "",
        "kit.py 是薄分发器：只转发到对应脚本的 main()，参数与行为完全由被转发脚本决定。",
        "各子命令的 --help 与直接运行对应脚本的 --help 输出一致，例如：",
        "  python3 scripts/kit.py status --help",
        "",
        "可用子命令：",
    ]
    width = max(len(name) for name in COMMANDS)
    for name in sorted(COMMANDS):
        module_name, description = COMMANDS[name]
        lines.append(f"  {name.ljust(width)}  {description}（scripts/{module_name}.py）")
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    if not argv or argv[0] in ("-h", "--help"):
        print(_help_text())
        return 0

    name, rest = argv[0], argv[1:]
    if name not in COMMANDS:
        print(
            f"未知子命令：{name}\n可用子命令：{', '.join(sorted(COMMANDS))}",
            file=sys.stderr,
        )
        return 2

    module_name, _ = COMMANDS[name]
    module = importlib.import_module(module_name)
    return module.main(rest)


if __name__ == "__main__":
    raise SystemExit(main())
