# 指令分层参考

工作区把 Agent 需要读取的内容分成规则轴和事实轴。规则从宽到窄加载，事实按当前阶段按需读取。

## 四层规则轴

```text
根 AGENTS.md
  -> .workspace/AGENTS.md
    -> <repo>/AGENTS.md
      -> <repo>/<dir>/AGENTS.md
```

`brief --task` 的 `instructionContext.rules` 以 `level` 表达这条顺序：1 是 Kit 根层，2 是工作区层，3 是目标仓层，4 是目标目录层。越靠近改动路径优先级越高，下层只能单调收窄上层规则；冲突时保留就近且更严格的约定。

| 层 | 文件 | 应写什么 |
| --- | --- | --- |
| 1 | 根 `AGENTS.md` | 所有工作区都适用的边界、模式和安全约定 |
| 2 | `.workspace/AGENTS.md` | 当前工作区新增或收紧的流程、类别规则 |
| 3 | `<repo>/AGENTS.md` | 一个业务仓的工程、测试和目录约定 |
| 4 | `<repo>/<dir>/AGENTS.md` | 一个目录或组件的局部规则 |

`workspace.json` 的 `repositories[].category` 是仓库分类标签，例如 `backend`、`frontend` 或 `tool`。Kit 用它展示和组织仓库，不会按 category 自动过滤 `.workspace/AGENTS.md`；任务仍会加载完整的工作区规则。需要声明同类仓规则时，在当前工作区的 `.workspace/AGENTS.md` 明确写出作用域：

```markdown
## backend 类别通用规范

适用于 `category=backend` 的业务仓。
```

公共模板不预设具体 category。只有当前工作区确有多仓共享规则时才增加这类小节；单仓规则写入该仓的 `AGENTS.md`。

## 事实轴

`.workspace/CONTEXT.md` 保存跨仓业务事实，`.workspace/docs/repositories/<repo>.md` 保存仓 profile。它们是 `workspace.json` 的生成视图，不参与规则冲突判断；需要定位路径和存在性时使用：

```bash
python3 scripts/workspace_status.py --root . --context-sources --json
```

不要把 guardrails、操作命令或权限规则混入 CONTEXT.md，也不要把业务事实复制进 AGENTS.md。

## 入口判据

规则入口应是仓内普通、可读的 `AGENTS.md`。已有工作区中存在 `sourceInstruction` 指向 `README.md` 的真实案例：doctor 会给出 INFO，迁移或 onboarding 不会擅自改写业务仓。`CLAUDE.md`、生成的 profile 和 README 可以作为补充事实或线索，但不能替代规范入口。

常见错放包括：把“金额使用定点类型”这类工程 guardrail 塞进事实轴，或把某一仓的业务状态写进根层；应按适用范围下移或上移，并让更具体层只收紧、不放宽。

## v1 迁移

存量工作区先预览再应用，迁移会备份 `.workspace`，替换旧模板段，保留首个 `##` 之后的用户内容，并重建 CONTEXT/profile：

```bash
python3 scripts/workspace_migrate.py preview --root . --json --diff
python3 scripts/workspace_migrate.py apply --root . --preview-hash <previewHash> --backup-dir <backup-dir>
```

无法判断的旧模板会原样保留并标记 `manualReview`；先人工处理，再重新运行 doctor。
