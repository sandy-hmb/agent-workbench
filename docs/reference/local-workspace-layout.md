# 本地状态布局

`.workspace/` 属于使用者，公共更新不会覆盖，整个目录应被 Git 忽略。只接受当前工作项格式，不迁移或覆盖旧目录。

```text
.workspace/
  workspace.json
  workspace.local.json
  CONTEXT.md
  docs/repositories/
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
  runs/
```

上述内容文件按需生成，不预建空文件。普通需求主要使用 change.md，复杂需求才使用 requirements/design/plan。state.json 通过 Kit 命令修改；README 和 verification.md 自动生成。证据不可改写，历史按需查询。

工作区配置主版本仍为 3；工作项采用唯一当前 state 格式。维护 Kit 时工作项根为 docs/development/items/，共用同一状态与验证逻辑。

旧的 docs/features、testing/evidence 和维护 records 保持原状但不会被新查询当作当前数据加载。备份或恢复时保留整套状态及证据，再核对登记仓库和版本。
