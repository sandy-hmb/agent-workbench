# 自定义工作流

公共 Kit 提供稳定的功能开发 Stage：

```text
feature.context
feature.classify
feature.analyze
feature.design
feature.prepare-branch
feature.implement
feature.verify
feature.submit-test
feature.complete
```

`feature.analyze` 和 `feature.submit-test` 可以按需求或仓配置跳过，但它们的 before/after 边界仍可承载自定义 Stage。

## 添加 Stage

先激活含有 Action 的 Extension，再在 `.workspace/workflow-input.json` 写入 Overlay：

```json
{
  "schemaVersion": 1,
  "workflow": "feature-development",
  "stages": [
    {
      "id": "team-delivery.integration-test",
      "after": "feature.implement",
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
python3 scripts/workspace_workflow.py preview \
  --root . --config .workspace/workflow-input.json --json
```

检查 Stage、Action、变化路径和 `previewHash` 后，执行 preview 返回的 apply 命令。apply 只写 `.workspace/workflow.json`。

## 运行和续接

未启用 Overlay 的工作区保持原有流程。新会话只需：

```bash
python3 scripts/workspace_status.py --root . --json
```

当 `workflow.enabled` 为 `true` 时，创建 Run：

```bash
python3 scripts/workspace_workflow.py start \
  --root . --feature <feature-slug> --json
```

轻量改动不创建需求目录，改为显式提供稳定 Run ID、仓库和分支：

```bash
python3 scripts/workspace_workflow.py start \
  --root . --run-id <run-id> --repo <repository> --branch <branch> --json
```

到达某个 Core Stage 时只查询该边界：

```bash
python3 scripts/workspace_workflow.py plan \
  --root . --run <run-id> --after feature.implement --json
```

`manual` Action 需要用户明确选择执行或跳过。Action API v1 的确认提示必须展示 plan 中的 `confirmation.title`、`confirmation.summary`、非敏感 `with` 参数和 effects；`planHash` 只用于脚本校验，不能作为要求用户确认的唯一信息。`auto` 只会自动发现 Action；已授权且内容未变化的 Action 可连续执行，带新增网络、Git、文件或进程 effects 时仍须先取得授权。

有 command 的 Action：

```bash
python3 scripts/workspace_workflow.py run \
  --root . --run <run-id> --stage <stage-id> --plan-hash <plan-hash> --json
```

Skill-only Action 由 Agent 读取 plan 返回的唯一 `skillPath` 后执行，再记录结果：

```bash
python3 scripts/workspace_workflow.py finish \
  --root . --run <run-id> --stage <stage-id> \
  --plan-hash <plan-hash> --status succeeded --summary "completed" --json
```

用户决定不执行时必须记录原因：

```bash
python3 scripts/workspace_workflow.py skip \
  --root . --run <run-id> --stage <stage-id> \
  --plan-hash <plan-hash> --reason "deferred by user" --json
```

标准需求默认以需求短名作为 Run ID，重复执行 `start --feature <feature-slug>` 会返回同一 Run。每次执行都要求当前 plan hash。Extension、Action、参数或 Stage 位置改变后，旧 hash 失效；成功或明确跳过的相同 fingerprint 默认不重复执行。`running` 表示上次中断，重新 run 或 skip 是一次明确的续接决定。

Run 位于 `.workspace/runs/`，只保存状态与短摘要，不复制 Skill、manifest、日志或凭据。详细日志由 Action 保存到自己的已授权路径。

## 跳过建议

`feature.analyze`、`feature.submit-test` 这两个 Core Stage 声明为 `optional`（见 `workflows/feature-development.json`）。团队可以在同一份 `.workspace/workflow-input.json` 里追加 `skipHints`，为可选 Stage 声明数据驱动的跳过建议：

```json
{
  "schemaVersion": 1,
  "workflow": "feature-development",
  "stages": [],
  "skipHints": [
    {
      "stage": "feature.analyze",
      "when": {"repositoryCount": {"max": 1}},
      "reason": "单仓、无外部契约变化的需求通常不需要跨仓分析"
    }
  ]
}
```

`stage` 必须是 Core Workflow 中声明为 `optional: true` 的 Stage，否则 `preview`/`apply` 会以 `WORKFLOW_SKIP_HINT_INVALID` 拒绝。`when` 目前只支持 `repositoryCount.max`：当调用方提供的仓库数量小于等于该值时命中建议。

查询当前建议（只读，不修改任何状态，不自动执行跳过）：

```bash
python3 scripts/workspace_workflow.py skip-suggestions \
  --root . --repositories 1 --json
```

命中时返回 `stage`、`reason`、`rule` 三个字段，说明"为什么可以跳过"；没有激活 Overlay 或没有声明 `skipHints` 时返回空建议列表。是否真正跳过仍由使用者按当前需求的实际情况判断——这只是建议，不是自动决策，也不会调用 `skip`/`finish` 或改变任何 Run 状态。
