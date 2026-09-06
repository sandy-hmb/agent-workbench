---
name: workspace-feature-workflow
description: Resolve and run user-defined workflow Actions at explicit feature development stages.
---

# Workspace Feature Workflow

用于在功能开发流程的 Core Stage 前后发现和续接本地自定义 Action。它只负责流程编排，不替代 `workspace-feature-design`、`workspace-verify` 或 `workspace-submit-test` 的具体工作。

## 先判断是否启用

先运行：

```bash
python3 scripts/workspace_status.py --root . --json
```

`workflow.enabled` 为 `false` 时直接按现有 Core Skill 工作，不读取 Extension Action，不创建 Workflow Run。为 `true` 时，再读取当前需求的最小上下文，并使用：

```bash
python3 scripts/workspace_workflow.py start --root . --feature <slug> --json
```

轻量改动不创建需求目录；只传仓库和分支即可。标准需求只要求已有 README，验证记录尚未生成时也可以启动 Run；一个 Run 只服务当前一次流程续接。

## 到达 Stage

Core Stage 完成或即将开始时，只查询当前锚点：

```bash
python3 scripts/workspace_workflow.py plan \
  --root . --run <run-id> --after <core-stage> --json
```

需要前置 Action 时使用 `--before`。plan 只返回待执行 Stage、一个 Action、确认摘要、Skill 路径、effects、参数和 plan hash，不返回整个 Extension 或历史日志。

- `manual`：Action API v1 必须带 confirmation，才能进入 plan。使用 plan 的 `confirmation.title` 和 `confirmation.summary` 向用户展示将执行的操作，再展示非敏感 `with` 参数和 effects，等待用户明确执行或跳过。`planHash` 仅用于脚本防漂移校验，不要求用户复制、核对或回复该值。
- `auto`：自动选中 Action；仅当 effects 包含未授权网络、远端 Git、外部环境或其他共享副作用时暂停授权。
- Action 失败、中断或前置未完成时停止后续 Stage，不猜测重试。

## 执行

有 `command` 的 Action 使用：

```bash
python3 scripts/workspace_workflow.py run \
  --root . --run <run-id> --stage <stage-id> --plan-hash <plan-hash> --json
```

无 `command` 的 Action 只读取 plan 返回的精确 `SKILL.md`，完成后使用 `finish`；用户决定不执行时使用带理由的 `skip`。所有命令都必须使用当前 plan hash，失配就重新 plan。

## 上下文和边界

每次只读取当前 Core Skill 和当前 Action Skill；同一任务中已授权且未变化的 Action 可连续执行。Action、参数、effects 或目标改变后重新核对实际影响。详细日志留在本地路径，Agent 只接收结构化摘要。不要手工修改 `.workspace/workflow.json`、`.workspace/runs/`、Extension lock 或受管 Adapter。

Action 如需写 feature 相关文件，必须由自身 SKILL.md 说明精确输出位置、文件归属和重复运行时的追加或覆盖方式；不预设扩展专用目录。它不得改写需求、设计或实施计划正文，除非用户已在当前阶段确认该内容变更。已有 `testing/verification.md` 时，Runner 只追加 Action 状态；文件尚不存在时不代建占位记录。

本 Skill 不自动部署、不创建代码审查请求、不提交或推送业务仓；这些行为必须由使用者定义 Action 并按本轮授权执行。
