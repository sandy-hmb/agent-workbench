---
name: workspace-submit-test
description: Deliver an explicitly selected repository to its configured test target in self-test, formal submission, or testing-patch mode.
---

# Workspace Submit Test

仅在用户明确指定目标仓并要求自测、正式提测或测试中补丁时使用；不把授权扩展到其他仓。

## 模式

- “自测”只保持需求原状态；“正式提测/提交测试”完成后将需求状态更新为 `testing`；“测试中补丁/重新合 test”要求需求已经是 `testing` 并保持该状态。
- 表达无法唯一判断时只询问一次模式，不执行 Git。不要把其他分支或外部流程的授权带入本 Skill。

## 前置检查

1. 用 `python3 scripts/workspace_registry.py resolve <name> --json` 解析仓路径和 `effectiveBranchPolicy.testTarget`；不要猜测测试目标。`testTarget` 为 `null` 时立即停止，不执行提测。
2. 用 `python3 scripts/feature_context.py resolve --repo <name> --branch <work-branch> --json` 定位需求；标准需求或补丁模式无法唯一匹配、状态不符时停止。
3. 在目标仓检查当前工作分支、远程、未提交文件、未完成 Git 操作和敏感文件。只把用户通过 `--path` 明确指定的业务仓文件交给脚本；feature 目录的 `artifacts/` 不属于业务仓提交路径。

## 交付顺序

在用户已授权的目标仓逐仓调用确定性提测脚本，记录结果。先运行只读 plan：

```bash
python3 scripts/workspace_submit.py plan \
  --root . --repo <repo> --branch <work-branch> --feature <feature-slug> \
  --path <business-file> --message "<message>" --json
```

确认脚本返回的整批远端动作后执行 plan 返回的 `planHash`：

```bash
python3 scripts/workspace_submit.py apply \
  --root . --repo <repo> --branch <work-branch> --feature <feature-slug> \
  --path <business-file> --message "<message>" --plan-hash <planHash> --json
```

`<test-target>` 必须来自 `effectiveBranchPolicy.testTarget`。没有未提交改动时省略 `--path` 和 `--message`，脚本仍核实工作分支已 push。正式提测在测试分支 push 成功后自动更新需求为 `testing`；自测不更新状态，补丁要求需求已经是 `testing`。没有 Skill 机制时直接读取 `scripts/workspace_submit.py --help` 和普通脚本输出。

Feature 目录中的 `artifacts/` 交付物不属于业务仓 `--path`，不会被脚本自动加入提交。

## 停止条件与边界

hooks、验证、网络、权限、push 拒绝、目标不存在或任何 Git 失败立即停止。merge 冲突时列出冲突文件并保持现场，不自动解决、abort、rebase、stash、强推或跳过 hooks。切回工作分支失败时报告当前分支。此 Skill 不创建 PR/MR、填写外部表单、操作外部系统或处理其他仓。
