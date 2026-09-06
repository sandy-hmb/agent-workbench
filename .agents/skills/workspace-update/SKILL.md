---
name: workspace-update
description: Safely update the public agent-workbench files while preserving ignored local workspace state.
---

# Workspace Update

用于更新当前 clone 的公共 Kit 文件。`.workspace/`、本地 Extension 和 `local-*` Adapter 都是本地状态，不属于这次更新的目标。

## 检查更新

1. 先检查公共工作树是否干净：

   ```bash
   git status --short --branch
   ```

   输出包含未提交公共文件时停止。不要 stash、丢弃或替用户处理这些修改。

2. 查看 remote：

   ```bash
   git remote -v
   ```

   将 `origin` 的 fetch 地址展示给用户，并与用户确认的公共 Kit 来源核对。没有 `origin`、地址不匹配或来源无法确认时停止。

3. 说明更新计划会访问网络，取得用户明确确认后才执行：

```bash
python3 scripts/workspace_update.py plan --root . --json
```

4. 展示 plan 返回的当前/目标提交、提交摘要、Extension 升级项和阻塞项；返回值含 `manualSteps` 时，把每条 `summary` 和 `runbook` 一并转告使用者——这些是跨过目标版本必须手动完成的一次性步骤。存在阻塞时不要拉取公共 Kit；先让使用者升级其 Extension 源码，再按返回的来源路径执行 Extension 的 validate、install preview/apply 和 activation preview/apply，随后重新运行 update plan。

   来源未知的 Extension 只能由使用者定位源码并显式重新安装；不要扫描工作区外目录猜测来源。

   公共工作树包含未提交文件时，plan 会报 `UPDATE_DIRTY` 并列出具体文件（超过 10 个只列前 10 个并注明总数）；把这份清单转告使用者自行判断如何处理，不要替使用者删除或 stash 任何文件。

## 应用更新

用户确认无阻塞的计划后，只执行 plan 返回的 `applyCommand`，等价形式为：

```bash
python3 scripts/workspace_update.py apply --root . --plan-hash <planHash> --json
```

apply 会重新检查目标提交和 plan hash，内部仅执行 `git pull --ff-only` 与 doctor；无法快进时停止并保留现场。不得执行 `git merge`、`git rebase`、`git stash`、`git reset` 或 `git push`，也不得自动解决冲突。返回值若含 `manualSteps`，原样转告使用者——这是 plan 阶段已经展示过的同一批步骤。

doctor 报告需要迁移时，只展示以下只读命令和预览结果；迁移 apply 必须取得另一轮明确确认：

```bash
python3 scripts/workspace_migrate.py preview --root . --json
```

doctor findings 若带 `remediation` 字段，直接把 `detail` 转告使用者作为可执行的下一步命令；没有 `remediation` 的 finding 仍按原有方式人工判断。

## 边界

- 更新前不要删除 `.workspace/`。它被 Git 忽略，公共更新不会同步或覆盖它。
- 不把本地 Extension、Adapter 或缓存加入 Git。
- `workspace_update.py plan` 是网络动作；`apply` 会改变公共工作树，二者都需要本轮用户授权。
