# 自定义工作流

公共 Kit 提供稳定的功能开发 Stage：

```text
item.context
item.classify
item.analyze
item.design
item.prepare-branch
item.implement
item.verify
item.submit-test
item.complete
```

`item.analyze` 和 `item.submit-test` 可以按需求或仓配置跳过，但它们的 before/after 边界仍可承载自定义 Stage。

普通 Action 仍然通过本指南挂在 Core Stage 前后，依赖 Workflow Run、Stage、确认和验收。需要在没有 WorkItem、Run 或 Stage 时按需使用的工具，应声明 Skill-only `standalone: true` 并使用 `kit.py action list/resolve`；该入口只返回元数据和精确 Skill 路径，不执行 Action。

## 添加 Stage

先激活含有 Action 的 Extension，再在 `.workspace/config/workflow.draft.json` 写入 Overlay：

```json
{
  "schemaVersion": {"major": 1, "minor": 0},
  "workflow": "item-development",
  "stages": [
    {
      "id": "team-delivery.integration-test",
      "after": "item.implement",
      "uses": "team-delivery/integration-test",
      "trigger": "auto",
      "with": {"repository": "service"}
    },
    {
      "id": "team-delivery.deploy-test",
      "after": "team-delivery.integration-test",
      "uses": "team-delivery/deploy-test",
      "trigger": "manual",
      "with": {"environment": "test"}
    }
  ]
}
```

每个自定义 Stage 必须：

- 有全局唯一 ID；
- 恰好使用一个 Action；
- 恰好声明一个 `before` 或 `after` 锚点；
- 使用 `manual` 或 `auto` trigger；
- 只携带非敏感 `with` 参数。

同一锚点的声明顺序就是稳定执行顺序。引用未知 Stage、Action 或形成循环时，preview 会停止。

```bash
python3 scripts/kit.py workflow preview \
  --root . --config .workspace/config/workflow.draft.json --json
```

检查 Stage、Action、变化路径和 `previewHash` 后，执行 preview 返回的 apply 命令。apply 只写 `.workspace/config/workflow.json`。

## 运行和续接

未启用 Overlay 的工作区保持原有流程。新会话只需：

```bash
python3 scripts/kit.py status --root . --json
```

当 `workflow.enabled` 为 `true` 时，创建 Run：

```bash
python3 scripts/kit.py workflow start \
  --root . --item-slug <itemSlug> --json
```

轻量改动不创建需求目录，改为显式提供稳定 Run ID、仓库和分支：

```bash
python3 scripts/kit.py workflow start \
  --root . --workflow-run-id <workflowRunId> --repository-path <repositoryPath> --branch <branch> --json
```

到达某个 Core Stage 时只查询该边界：

```bash
python3 scripts/kit.py workflow plan \
  --root . --workflow-run-id <workflowRunId> --after item.implement --json
```

`manual` Action 核对已授权范围；实际影响未变时直接继续，否则展示具体内容等待选择。Action API v1 的确认提示必须展示 plan 中的 `confirmation.title`、`confirmation.summary`、非敏感 `with` 参数和 effects；`planHash` 只用于脚本校验，不能作为要求用户确认的唯一信息。`auto` 只会自动发现 Action；已授权且内容未变化的 Action 可连续执行，带新增网络、Git、文件或进程 effects 时仍须先取得授权。

`workflow plan --before/--after` 的参数只能是 Core Stage。Custom Stage 不能作为 plan 锚点；应使用 plan 返回的 `stageId` 和 `requestId` 执行 `run`，或对 Skill-only Action 使用 `finish`/`skip`。

有 command 的 Action：

```bash
python3 scripts/kit.py workflow run \
  --root . --workflow-run-id <workflowRunId> --stage-id <stageId> --plan-hash <planHash> --request-id <requestId> --json
```

Skill-only Action 由 Agent 读取 plan 返回的唯一 `skillPath` 后执行，再记录结果：

```bash
python3 scripts/kit.py workflow finish \
  --root . --workflow-run-id <workflowRunId> --stage-id <stageId> \
  --plan-hash <planHash> --status succeeded --summary "completed" --json
```

用户决定不执行时必须记录原因：

```bash
python3 scripts/kit.py workflow skip \
  --root . --workflow-run-id <workflowRunId> --stage-id <stageId> \
  --plan-hash <planHash> --reason "deferred by user" --json
```

标准需求默认以需求短名与当前迭代作为 Workflow Run ID，重复执行 `start --item-slug <itemSlug>` 会返回同一 Run。每次执行都要求当前 plan hash。Extension、Action、参数或 Stage 位置改变后，旧 hash 失效；成功或明确跳过的相同 fingerprint 默认不重复执行。运行状态需结合执行者锁与尝试记录判断；遗留 `running` 无法证明进程存活，也不能证明外部操作失败。

Run 位于 `.workspace/runs/`，命令执行尝试保存在 `attempts/<workflowRunId>.json`。查询 `workflow result --workflow-run-id <workflowRunId> --request-id <requestId> --json` 可在断线后取回结果；同一请求重复 run 不重复执行。明确失败后使用 `workflow retry`，带当前 plan hash、新 request id、previous-request-id 和 reason；结果未知必须先核对，并通过 `workflow reconcile` 附 summary 与 evidence 记录实际结论。说明书型 Action 保留 finish 语义。

执行尝试是事实来源，Run 的 Stage 和验证 Markdown 是摘要。摘要写入失败不触发命令重跑；Inspect 的 attempts 可查看权威结果。保持前台执行，不提供脱离终端运行的后台任务。详细日志仍由 Action 保存在已授权位置，不在记录中保存凭据。新旧 Runner 不应混用。

```bash
python3 scripts/kit.py workflow result --workflow-run-id <workflowRunId> --request-id <requestId> --json
python3 scripts/kit.py workflow reconcile --workflow-run-id <workflowRunId> --request-id <requestId> \
  --status failed --summary "已确认操作未完成" --evidence "外部操作编号或证据路径" --json
python3 scripts/kit.py workflow retry --workflow-run-id <workflowRunId> --stage-id <stageId> \
  --plan-hash <planHash> --previous-request-id <requestId> \
  --request-id <newRequestId> --reason "已核对，可以重试" --json
```

重新执行仍需已有授权覆盖目标与实际影响。去重只保证 Kit 对同一请求不重复调度，不证明任意外部系统恰好执行一次。

## 必要 Action 与验收

必要 Action 在执行前通过 `item delivery` 登记为 `externalChecks` 的 `pending` 项。下例假定只有这一项；已有检查时，两个输入都须合并当前完整清单，不能丢弃尚未关闭或引用已失效的项。

`delivery.pending.json`：

```json
{
  "externalChecks": [{
    "id": "integration", "requirement": "R1",
    "description": "联调符合接口契约", "owner": "测试负责人",
    "status": "pending", "evidence": ""
  }]
}
```

```bash
python3 scripts/kit.py item delivery <itemSlug> --root . \
  --input delivery.pending.json --state-revision <stateRevision> --json
python3 scripts/kit.py workflow start --root . --item-slug <itemSlug> --json
python3 scripts/kit.py workflow plan --root . --workflow-run-id <workflowRunId> \
  --after item.implement --json
```

本例沿用[本地 Extension](local-extensions.md#manifest) 中没有 `command` 的 `team-delivery/integration-test`。读取 plan 返回的 `skillPath` 并实际执行联调；成功后通过 `finish` 记录实际结果：

```bash
python3 scripts/kit.py workflow finish --root . --workflow-run-id <workflowRunId> \
  --stage-id team-delivery.integration-test --plan-hash <planHash> \
  --status succeeded --summary "联调已执行，报告已生成" --json
```

取回 `finish` 返回的实际 `requestId`，核对报告后显式提交 `passed`、验收依据和 `evidenceRefs`。以下 `<requestId>` 使用该返回值。`delivery.accepted.json`：

```json
{
  "externalChecks": [{
    "id": "integration", "requirement": "R1",
    "description": "联调符合接口契约", "owner": "测试负责人",
    "status": "passed", "evidence": "已人工核对联调报告，接口契约符合 R1",
    "evidenceRefs": [{
      "kind": "workflow", "runId": "<workflowRunId>",
      "requestId": "<requestId>", "stage": "team-delivery.integration-test"
    }]
  }]
}
```

```bash
python3 scripts/kit.py brief <itemSlug> --root . --json
python3 scripts/kit.py item delivery <itemSlug> --root . \
  --input delivery.accepted.json --state-revision <stateRevision> --json
```

每次写入使用最新 `stateRevision`；引用必须指向当前轮次、仓绑定和 Action 配置下该 Stage 的最新成功尝试，前置依赖链也须有效。重试后替换为最新 `requestId`。`evidenceRefs.status` 可省略，只是显示快照，系统读取真实尝试；省略全部或部分引用时，按 Stage 与仓库保留旧关联。同一 Stage/仓库可换为新 Run 的结果，但旧 Run 的最新尝试为 running/unknown 时必须先处理，不能用新 Run 绕过。

请求仍为 `running` 时等待或查询 `workflow result`，不能覆盖执行中的结果。只有执行者退出且查询为 `unknown` 时，才核对目标并通过上述 `workflow reconcile` 记录依据；明确结果后才能重试或验收。已审阅的范围调整可记为 `waived`，附理由并引用最新已结束尝试；不能豁免未知结果。Action 成功不会自动关闭验收或完成 WorkItem。未登记为必要检查的附加 Action，以及没有 Workflow 引用的人工验收，保持原有行为。
