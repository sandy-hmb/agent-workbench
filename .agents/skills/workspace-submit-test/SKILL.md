---
name: workspace-submit-test
description: Deliver an explicitly selected repository to its configured test target in self-test, formal submission, or testing-patch mode.
---

# Workspace Submit Test

仅在用户明确指定目标仓并要求自测、正式提测或测试中补丁时使用；不把授权扩展到其他仓。

## 模式

- “自测”只保持需求原状态；“正式提测/提交测试”完成后将对应仓交付状态记录为 submitted；“测试中补丁/重新合 test”要求对应仓已有 submitted 提测事实 并保持该状态。
- 表达无法唯一判断时只询问一次模式，不执行 Git。不要把其他分支或外部流程的授权带入本 Skill。

## 前置检查

1. 用 `python3 scripts/kit.py registry resolve <name> --json` 解析仓路径和 `effectiveBranchPolicy.testTarget`；不要猜测测试目标。`testTarget` 为 `null` 时立即停止，不执行提测。
2. 用 `python3 scripts/kit.py item resolve --repo <name> --branch <work-branch> --json` 定位需求；命令返回的 `itemSlug` 是 WorkItem slug，不是 Workflow Run ID。标准需求或补丁模式无法唯一匹配、状态不符时停止。
3. 在目标仓检查当前工作分支、远程、未提交文件、未完成 Git 操作和敏感文件。只把用户通过 `--path` 明确指定的业务仓文件交给脚本；work item 目录的 `artifacts/` 不属于业务仓提交路径。

## 交付顺序

在用户已授权的目标仓逐仓调用确定性提测脚本，记录结果。先运行只读 plan：

```bash
python3 scripts/kit.py submit plan \
  --root . --repository-path <repositoryPath> --branch <work-branch> --item-slug <itemSlug> \
  --path <business-file> --message "<message>" --json
```

确认脚本返回的整批远端动作后，直接执行 plan JSON 中完整的 `applyCommand`；不得手工猜测参数。也可以核对命令中的 `planHash` 后执行：

```bash
python3 scripts/kit.py submit apply \
  --root . --repository-path <repositoryPath> --branch <work-branch> --item-slug <itemSlug> \
  --path <business-file> --message "<message>" --plan-hash <planHash> --json
```

`<repositoryPath>` 是 Workspace Registry 中的业务仓路径，`<itemSlug>` 是 WorkItem 唯一短名；两者都不能用 Workflow Run ID 代替。`<test-target>` 必须来自 `effectiveBranchPolicy.testTarget`。没有未提交改动时省略 `--path` 和 `--message`，脚本仍核实工作分支已 push。正式提测在测试分支 push 成功后自动记录对应仓提交版本与 submitted 事实；自测不更新状态，补丁要求对应仓已有 submitted 提测事实。没有 Skill 机制时直接读取 `scripts/kit.py submit --help` 和普通脚本输出。

WorkItem 目录中的 `artifacts/` 交付物不属于业务仓 `--path`，不会被脚本自动加入提交。

提测脚本记录版本和实际提测结果，README 自动生成。部署与外部验收需要另外的真实依据，通过 item delivery 更新。失败也如实记录；不从 push、PR 或提测成功推断已经部署。前端指南复用原文档，状态不写入内容正文。

## 停止条件与边界

hooks、验证、网络、权限、push 拒绝、目标不存在或任何 Git 失败立即停止。merge 冲突时列出冲突文件并保持现场，不自动解决、abort、reset、rebase、stash、强推或覆盖用户改动。失败后先使用 plan 返回的 `attemptId` 调用 `submit reconcile --attempt-id`，确认阶段事实后再用 `submit continue --attempt-id`；不得重复 commit、push 或 merge。切回工作分支失败时报告当前分支。此 Skill 不创建 PR/MR、填写外部表单、操作外部系统或处理其他仓。
