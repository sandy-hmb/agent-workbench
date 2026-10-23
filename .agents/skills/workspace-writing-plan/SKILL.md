---
name: workspace-writing-plan
description: Prepare independent executable tasks when a change needs explicit dependencies or handoff.
---

# 实施计划

仅在重大需求或普通需求确有独立任务、依赖或接力需要时使用。普通需求默认沿已批准 change.md 实施，不强造任务。

读取当前规格、目标仓规则和相关代码，用 prepare 选定模板。按可独立验证的结果拆分；同仓同一结果的实现、测试和配置归同一任务。跨仓分别声明唯一目标仓，通过真实契约连接。

同 checkout 统一验证的步骤归同一任务，不以半成品解锁依赖；跨仓仅在契约、数据和发布顺序独立时并行。

## 每项任务

使用 `### T01 标题`，编号高于当前 WorkItem 已用最大编号，不使用完成复选框。任务包含：

- 可观察交付结果、R/D 依据、唯一目标仓。
- 真实依赖编号或“无”；不把阅读顺序作为依赖。
- 关键 Create/Modify/Test/Delete/Verify 路径和稳定入口；删除/契约/迁移须精确，不穷举普通类，不预设新测试文件。
- 行为、声明式或持久化的验证性质。
- 主要验证入口、工作目录、命令、通过条件和必要反例；不强制 RED。

步骤只写易遗漏或有顺序的动作；复用覆盖，不跨层重复场景，数据库按独有风险验证。取舍见 workspace-verify。新增 fallback、adapter、provider、兼容或删旧路径时补 Old path、Disposition、Reason、Verification。

同批检查映射多任务。focused-v1 用 R/D 列表、连续范围或子节，全局约束始终适用；check-docs 后审阅。legacy 不改原依据。

整体验证只承接必要跨任务回归；跨仓局部通过不代表整体验收。外部待验收用 item delivery 登记 R、事项、负责人和完成条件，不阻塞无关本地任务。

## 审阅与执行

自审 R→D→任务→验证、接口和依赖，合并过细任务，不加通用化或未来预留。brief 核对可解析，引用权威正文而非会话历史。

展示实际计划、目标仓、基线、分支和执行方式。默认单 Agent；多 Agent 只记职责，运行关联由接入层管理。重大需求 Design 批准后用 `item prepare --role plan` 准备并单独审阅；普通方案可与计划一次审阅。批准后用 `item approval --role plan --decision approved --reason <依据> --state-revision <stateRevision>` 登记任务，转 workspace-execute-plan 连续推进。

事实漂移修正文案；范围、任务或验证强度变化沿设计变更规则。
