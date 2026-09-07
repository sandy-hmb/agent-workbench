---
name: workspace-writing-plan
description: Turn an approved feature design into a reviewable, executable implementation plan and obtain execution approval.
---

# Workspace Writing Plan

用于已确认书面设计后的实施计划草案、计划自审和执行确认。它共享 `feature.design` 锚点，不新增 Core Stage。

## 前置条件

读取当前需求、已确认的 `design/design.md`、仓内约定和相关代码。书面设计未获用户确认时停止并回到 `workspace-feature-design`；轻量改动不使用本 Skill。

## 生成计划

在 `plans/implementation.md` 写唯一书面计划草案并从 README 链接，需求仍保持 `planning`。先检查设计是否包含可独立交付的子系统；能独立验收的子系统拆成不同需求或计划。

每个任务是独立、可验证的交付单元，只有审查者可能独立接受或拒绝时才拆分。测试、实现、配置和必要文档归入服务同一结果的任务，不按技术层横向分批。每项至少写明：

- 修改的仓、文件或符号；
- 提供或消费的接口与前置依赖；
- 可观察验收；
- 精确验证命令及预期结果；
- 非平凡行为的 Red、Green 和回归检查，或声明式变化的最小有效检查。

不要求逐任务 commit、独立 worktree、内嵌完整实现代码或机械化 2-5 分钟步骤。

## 计划自审与确认

写入后逐条检查需求覆盖：每项需求验收标准都有任务承接；任务边界可独立审查；接口名称一致；依赖顺序可执行；没有 TBD、占位描述或泛化的“补测试”步骤；每个任务有验证入口。

向用户展示实际计划文件、工作类型、基线、候选分支及执行方式，等待明确批准。仅在批准后才去除草案标记、更新 README 为 `development` 并转交 `workspace-execute-plan`。创建计划与开始实现之间必须保留此确认门禁。

## 边界

本 Skill 不创建或切换分支，不实现代码，不 commit、push、合并、提测或运行外部环境。分支候选由 `python3 scripts/workspace_registry.py branch <repo> --type <type> --slug <slug> --json` 计算，直接使用返回的 `baseBranch` 和 `branch`，不得手工替换命名模板。
