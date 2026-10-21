---
name: workspace-writing-plan
description: Prepare independent executable tasks when a change needs explicit dependencies or handoff.
---

# 实施计划

仅在重大需求或普通需求确有独立任务、依赖或接力需要时使用。普通需求默认沿已批准 change.md 实施，不强造任务。

读取当前需求、设计、目标仓规则及相关代码，使用 templates/item/plan.md。按可独立验证的行为拆分，同仓服务同一结果的测试、实现与配置归同一任务。跨仓任务分别声明唯一目标仓，通过真实契约连接。

同 checkout 需统一编译/测试的步骤归同一任务，不以半成品解锁依赖。跨仓仅在接口、数据和发布顺序独立时并行，明确写入者 checkout。

## 每项任务

使用 `### T01 标题`，编号高于当前 WorkItem 已用最大编号，不使用完成复选框。任务包含：

- 可观察交付结果、R/D 依据、唯一目标仓。
- 真实依赖编号或“无”；不把阅读顺序作为依赖。
- 实际 Create/Modify/Test/Delete/Verify 路径及稳定符号，不预设新测试文件。
- 行为、声明式或持久化的验证性质。
- 主要验证入口、工作目录、命令、通过条件和必要反例；不强制 RED。

步骤只写易遗漏或有顺序的动作；复用覆盖，不跨层重复场景，数据库按独有风险验证。取舍见 workspace-verify。新增 fallback、adapter、provider、兼容或删旧路径时补 Old path、Disposition、Reason、Verification。

允许一批检查显式映射多个任务，不为证据登记拆测试。

在整体验证区承接跨任务回归；跨仓行为明确最终验收入口，各仓局部通过不能代替整体验收。外部待验证写清 R、事项、负责方和完成条件，随后通过 item delivery 登记，不阻塞无关本地任务。

## 审阅与执行

自审 R→D→任务→验证覆盖、接口、依赖、路径和命令；删掉不服务当前验收或约束的任务，合并过细拆分，不借计划增加通用化或未来预留。运行 brief 确认可解析；正文引用权威文档，不复制全文或依赖历史会话。

展示实际计划、目标仓、基线、分支和执行方式。默认单 Agent；多 Agent 只记职责，运行关联由接入层管理。重大需求 Design 批准后用 `item prepare --role plan` 准备并单独审阅；普通方案可与计划一次审阅。批准后用 `item approval --role plan --decision approved --reason <依据> --state-revision <stateRevision>` 登记任务，转 workspace-execute-plan 连续推进。

事实漂移且范围方案不变时修正内容；改变范围、任务边界或验证强度时沿需求设计的变更分类规则处理。
