---
name: workspace-sync-base
description: Synchronize an explicitly selected repository branch with its recorded feature base or effective workBase using fetch and merge only.
---

# Workspace Sync Base

仅在用户明确指定目标仓并说明同步/合并语义时使用。

## 前置

1. 用 `python3 scripts/workspace_registry.py resolve <name> --json` 解析唯一仓路径和 `effectiveBranchPolicy`。
2. 在目标仓读取当前分支和状态；不要在治理根执行 Git。
3. 优先用 `python3 scripts/feature_context.py resolve --repo <name> --branch <current-branch> --json` 从 `.workspace/docs/features/` 得到需求记录的 `baseBranch`。没有唯一匹配时，才使用该仓有效策略的 `workBase`，并把选择记录给用户。
4. 确认远程和目标 ref 存在，工作区没有未完成的 merge/rebase/cherry-pick/revert。

## 执行

按确认的目标执行：

```bash
git fetch origin --prune
git merge --no-edit origin/<base-branch>
```

实际命令中的 `<base-branch>` 必须来自需求记录或有效策略。已经最新时直接报告，不制造提交。

## 停止条件与边界

fetch、ref、权限或其他 Git 错误立即停止并报告实际状态。出现冲突时列出冲突文件，保持现场等待用户决定；不自动解决、abort、push、rebase、stash、创建或切换分支，也不修改其他仓。
