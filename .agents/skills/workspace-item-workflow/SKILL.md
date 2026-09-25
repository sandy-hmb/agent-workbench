---
name: workspace-item-workflow
description: Resolve and run user-defined workflow Actions at explicit work item development stages.
---

# Workspace WorkItem Workflow

用于在功能开发流程的 Core Stage 前后发现和续接本地自定义 Action。它只负责流程编排，不替代 `workspace-item-design`、`workspace-verify` 或 `workspace-submit-test` 的具体工作。

## 先判断是否启用

先运行：

```bash
python3 scripts/kit.py status --root . --json
```

`workflow.enabled` 为 `false` 时直接按现有 Core Skill 工作，不读取 Extension Action，不创建 Workflow Run。为 `true` 时，再读取当前需求的最小上下文，并使用：

```bash
python3 scripts/kit.py workflow start --root . --item-slug <itemSlug> --json
```

轻量改动不创建需求目录；只传仓库和分支即可。标准需求要求有效 state.json，验证记录尚未生成时也可以启动 Run；一个 Run 只服务当前一次流程续接。

## 到达 Stage

Core Stage 完成或即将开始时，只查询当前锚点：

```bash
python3 scripts/kit.py workflow plan \
  --root . --workflow-run-id <workflowRunId> --after <core-stage> --json
```

需要前置 Action 时使用 `--before`。`--before/--after` 只接受 Core Stage；传入 Custom Stage 会列出可用 Core Stage，并提示使用 `run`、`finish` 或 `skip`。plan 只返回待执行 Stage、一个 Action、确认摘要、Skill 路径、effects、参数和 plan hash，不返回整个 Extension 或历史日志。

- `manual`：Action API v1 必须带 confirmation，才能进入 plan。使用 plan 的 `confirmation.title`、`confirmation.summary`、非敏感 `with` 参数和 effects 核对实际影响；已有授权明确覆盖同一目标、环境、参数和 effects 时直接继续，否则等待用户明确执行或跳过。`planHash` 只用于防漂移，不是新的确认对象。
- `auto`：自动选中 Action；仅当 effects 包含未授权网络、远端 Git、外部环境或其他共享副作用时暂停授权。
- Action 失败、中断或前置未完成时停止依赖它的 Stage；结果未知先核对，不猜测重试。

## 执行

有 `command` 的 Action 使用：

```bash
python3 scripts/kit.py workflow run \
  --root . --workflow-run-id <workflowRunId> --stage-id <stageId> --plan-hash <planHash> --request-id <requestId> --json
```

无 `command` 的 Action 只读取 plan 返回的精确 `SKILL.md`，完成后使用 `finish`；用户决定不执行时使用带理由的 `skip`。所有命令都必须使用当前 plan hash，失配就重新 plan。

```bash
python3 scripts/kit.py workflow finish \
  --root . --workflow-run-id <workflowRunId> --stage-id <stageId> \
  --plan-hash <planHash> --status succeeded --summary "completed" --json

python3 scripts/kit.py workflow skip \
  --root . --workflow-run-id <workflowRunId> --stage-id <stageId> \
  --plan-hash <planHash> --reason "deferred by user" --json
```

## 请求结果与中断恢复

命令型 Action 使用 plan 返回的 requestId。重复 run 同一请求只返回原结果或运行状态，不启动第二次命令；省略编号沿稳定初次请求兼容。结果查询不依赖当前扩展仍可用：

```bash
python3 scripts/kit.py workflow result --root . --workflow-run-id <workflowRunId> --request-id <requestId> --json
```

明确失败后的重新执行使用 `workflow retry --workflow-run-id <workflowRunId> --stage-id <stageId> --plan-hash <planHash> --previous-request-id <oldRequestId> --request-id <newRequestId> --reason "已核对失败原因" --json`。已有授权覆盖重试范围时不重复确认。

结果 unknown 表示执行者退出、超时或协议失败后外部结果尚未确认；查询命令不写状态。先核对真实目标，用 `workflow reconcile --workflow-run-id <workflowRunId> --request-id <requestId> --status succeeded|failed|skipped --summary "实际结论" --evidence "证据位置或外部操作编号" --json` 记录核对来源，再按结果继续或 retry。不能把进程消失推断为动作没有发生。结果未知时不得用 skip 绕过核对。

保持前台执行与工作区互斥，不启动后台守护进程。说明书型 Action 仍由 Agent 按授权执行并 finish，不承诺工具外操作去重。新旧 Runner 不混用。

## 上下文和边界

每次只读取当前 Core Skill 和当前 Action Skill；同一计划或迭代中已授权且实际影响未变化的 Action 可连续执行和重试，阶段或 Skill 切换不触发重复确认。Action、参数、effects、目标或环境改变后重新核对实际影响。详细日志留在本地路径，Agent 只接收结构化摘要。不要手工修改 `.workspace/config/workflow.json`、`.workspace/runs/`、Extension lock 或受管 Adapter。

Action 如需写 work item 相关文件，必须由自身 SKILL.md 说明精确输出位置、文件归属和重复运行时的追加或覆盖方式；不预设扩展专用目录。它不得改写需求、设计或实施计划正文，除非用户已在当前阶段确认该内容变更。Action 结果只保存到执行尝试记录，阶段状态按需生成，不追加验证 Markdown。

本 Skill 不自动部署、不创建代码审查请求、不提交或推送业务仓；这些行为必须由使用者定义 Action 并按本轮授权执行。
