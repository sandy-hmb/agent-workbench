---
name: workspace-writing-plan
description: Prepare independent executable tasks when a change needs explicit dependencies or handoff.
---

# 实施计划

仅在重大需求或普通需求确有独立任务、依赖或接力需要时使用。普通需求默认沿已批准 change.md 实施，不强造任务。

读取当前需求、设计、目标仓规则及相关代码，使用 templates/item/plan.md。按可独立验证的行为拆分，同仓服务同一结果的测试、实现与配置归同一任务。跨仓任务分别声明唯一目标仓，通过真实契约连接。

## 每项任务

使用 `### T01 标题`，编号高于当前 WorkItem 已用最大编号，不使用完成复选框。任务包含：

- 可观察交付结果、R/D 依据、唯一目标仓。
- 真实依赖编号或“无”；不把阅读顺序作为依赖。
- Create/Modify/Test/Delete/Verify 的精确路径，已有逻辑标注稳定符号。
- 行为、声明式或持久化的验证性质。
- 测试工作目录、精确命令、通过条件；非平凡行为写最小失败场景。

步骤只保留本任务易遗漏或有顺序要求的动作，不复制通用 TDD 和证据教程。持久化变化需要真实结构、迁移或写入检查。声明式改动选择最小有效检查，不机械新增测试。

在整体验证区承接跨任务回归；跨仓行为明确最终验收入口，各仓局部通过不能代替整体验收。外部待验证写清 R、事项、负责方和完成条件，随后通过 item delivery 登记，不阻塞无关本地任务。

## 审阅与执行

逐项检查 R→D→任务→验证的语义覆盖、接口与依赖、路径和命令。运行 brief 确认计划可解析。计划正文不依赖历史会话，引用权威文档，不复制全文。

将实际计划、目标仓、基线、分支和执行方式一起展示。默认单 Agent；已有授权内的组织细节自行处理。重大需求计划单独审阅，普通方案可一次审阅包含计划的完整包。批准后使用 `item approval --role plan --decision approved --reason <依据> --state-revision <stateRevision>` 登记任务，再转 workspace-execute-plan 连续推进。

事实漂移且范围方案不变时修正内容；改变范围、任务边界或验证强度时沿需求设计的变更分类规则处理。
