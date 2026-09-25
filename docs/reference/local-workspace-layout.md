# 本地工作区布局

`.workspace/` 属于使用者，公共更新不会覆盖，整个目录应被 Git 忽略。当前布局只接受 workspace v4 和 WorkItem；旧目录不会被读取、转换或覆盖。

```text
.workspace/
  AGENTS.md                    # 本工作区新增或收紧的规则
  CONTEXT.md                   # Kit 生成的跨仓索引（路径与类别）
  config/
    workspace.json             # 已生效的共享仓库登记与分支策略
    local.json                 # 本机 owner、角色与本地 Extension 设置
    workflow.draft.json        # 可编辑的 Workflow 草稿（按需存在）
    workflow.json              # Kit 校验并写入的生效 Workflow（按需存在）
    extensions.draft.json      # 可编辑的 Extension 期望状态（按需存在）
  repositories/
    <repository>.md            # Kit 生成的仓详细事实（按需存在）
  items/<slug>/
    state.json
    README.md
    change.md
    requirements.md
    design.md
    plan.md
    verification.md
    evidence/
    references/
    artifacts/
    history/
  extensions/
    <extension-id>/
    .state/                    # lock、输入和缓存，Kit 管理
  runs/                        # Workflow Run 与尝试记录，Kit 管理
```

日常使用者只需要关注 `AGENTS.md`、`items/`，以及需要自定义流程或 Extension 时的 `config/*.draft.json`。`workspace.json`、`local.json`、`workflow.json`、仓 profile、Extension `.state` 和 `runs/` 都是受管状态，应通过 `setup`、`extension`、`workflow`、`item` 或 `verify` 命令更新，不手工修改。

`workspace-input.json` 是初始化前的临时输入，不是工作区事实来源。初始化完成后可以删除它；下一次重新初始化时再生成新的输入。

Markdown 只保存内容；`state.json` 是生命周期、审批、任务和交付的唯一可变事实源。`verify record` 记录实际结果并自动更新 README 与验证摘要。详细历史、日志和附件只在需要追溯时读取。

工作区配置主版本为 4。维护 Kit 时工作项根为 `docs/development/items/`，共用同一状态与验证逻辑。备份或恢复时保留整套 `.workspace/`，再核对登记仓库和版本。

升级后可用 `setup refresh preview/apply` 重新生成 CONTEXT.md 和仓 profile；它不会修改 WorkItem、Extension 或 Run。
