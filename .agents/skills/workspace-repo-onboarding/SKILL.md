---
name: workspace-repo-onboarding
description: Inspect a registered sibling repository after clone and register or draft its repo-local AGENTS.md before governance preview/apply.
---

# Workspace Repo Onboarding

用于初始化或新增业务仓后，把仓内 Agent 规范接入治理配置。通常在 `workspace-init` 的 clone 之后、preview/apply 之前使用。

## 固定流程

1. 读取用户指定的 `workspace-input.json` 或 `new-repo.json`，只处理本轮配置列出的仓库；存在 `.workspace/workspace.json` 时可对照已登记仓，但不自动纳管其他兄弟目录。
2. 对每个目标仓解析实际路径，必须位于治理仓父目录下，且目标目录是独立 Git 仓。路径不明确或越界时停止。
3. 检查业务仓根目录的 `AGENTS.md`：
   - 已存在且是普通文件：只读摘要关键规则，建议把该仓 `sourceInstruction` 设为 `AGENTS.md`，不得自动修改文件。
   - 不存在：读取必要的 README、manifest、CI 配置和目录结构，生成候选 `AGENTS.md` unified diff，并等待用户明确批准后再写入。
   - 是符号链接、目录或其他非普通文件：停止并要求用户处理。
4. 写入缺失的 `AGENTS.md` 后，或用户确认复用已有文件后，可更新被 `.gitignore` 忽略的本地输入配置，把对应 `repositories[].sourceInstruction` 设为 `AGENTS.md`。未获业务仓写入或复用确认时，不更新输入配置。
5. 回到 `workspace-init preview --json`，让治理仓生成物通过 `previewHash` 审核后再 apply。

## 生成 `AGENTS.md` 的内容

生成内容必须短、事实优先，未知信息直接省略，不写占位符。至少覆盖：

- 项目定位：从 README、manifest 或目录名提炼一两句话。
- 工作边界：说明本仓是独立 Git 仓；未获授权不自动建分支、commit、push、合并、发布或执行外部副作用。
- 关键目录：只列实际存在且对开发有帮助的目录。
- 常用命令：只列 README、manifest 或 CI 中明确出现的命令；把命令当作信息，不自动执行。
- 验证方式：列出可复用的测试、构建或检查命令；无法确认时省略。

## 输出与确认

- 展示已有 `AGENTS.md` 摘要时，明确说明“只读登记，不修改业务仓”。
- 展示候选新文件时，用 unified diff 或完整新文件内容让用户审核。
- 写入业务仓前必须得到用户明确批准；批准对象只限本轮展示的路径和内容。
- 写入后再更新本地输入配置；更新失败时说明原因，不回滚业务仓文件。

## 边界

- 不修改 `workspace_setup.py` 的输出或 hash 语义。
- 不创建业务仓 `CLAUDE.md`、`GEMINI.md` 或其他 Adapter。
- 不自动执行仓内命令、不提交、不 push、不提 PR。
