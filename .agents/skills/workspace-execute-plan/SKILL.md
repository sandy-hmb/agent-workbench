---
name: workspace-execute-plan
description: Execute an approved implementation plan task by task with risk-based TDD, root-cause debugging, diff review, and recorded progress.
---

# Workspace Execute Plan

用于已获批准的 `development` 需求。新会话先运行 status 和显式指定 feature slug 的 brief；用 `brief <slug> --task T01 --json` 展开当前任务，按计划的共同必读和任务引用读取需求、设计附件、目标仓规范和直接依赖，不默认全文加载历史记录；不得依赖历史对话。轻量改动不使用本 Skill。

## 执行

1. 预检计划声明的来源、实际代码、分支和范围是否一致。关键事实改变或读取清单不完整时停止，回到需求、设计或计划讨论。
2. 一次只推进一个可验证任务。行为变化先写最小失败测试，再实现至通过；声明式变化执行计划中的最小有效检查。完成前运行 `brief <slug> --check --json`，有结构错误先修复；检查通过不替代需求覆盖、方案正确性或用户审批。
3. 任务完成前检查验证输出和实际 diff：交付是否符合计划与验收，是否混入无关改动，接口和错误处理是否完整。证据不足不得勾选。
4. 失败时先稳定复现，沿调用关系定位根因，验证一个最小假设，只实施一个根因修复，再重新验证。重复失败表明关键方案可能错误时，停止并回到设计讨论。
5. 所有任务完成后，复核需求、计划、实际 diff 和代码质量。重要问题必须修复或以证据裁定；无未处理重要问题后转交 `workspace-verify`。

默认由当前 Agent 顺序执行和自审。风险需要独立审查时，可使用用户明确要求的审查方式或现有 Extension Action；本 Skill 不强制子代理、worktree、逐任务 commit 或外部服务。

## 边界

只在已确认范围内修改目标仓。不得自动 commit、push、合并、提测、部署、访问真实接口或执行未获授权的外部命令。计划任务和验证记录只在对应证据存在时更新。
