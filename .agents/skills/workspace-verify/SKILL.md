---
name: workspace-verify
description: Record actual validation and delivery facts, then complete an accepted WorkItem.
---

# 验证与完成

无 Item 的小改只报实际命令、退出码和结果，不建目录。

## 验证

从验收、仓规则、CI 和已有测试选入口。规则选主要验证层，其他层只补独有风险，不重复已有覆盖。范围或数据库需取舍时读[验证策略](references/strategy.md)。Mock 不替代真实数据库/鉴权/事务验证，缺环境保留待验收。零执行、跳过目标或仅编译不证明行为完成。新旧路径检查主路径、残留引用和兼容边界，其他任务不加检查。

同一次实际检查只记一份证据。用 `verify snapshot <slug> --json` 取代码状态，逐任务加 --task；同批覆盖多任务按 [证据输入](references/evidence.md) 的 taskCoverage 映射 checks。scope=item 可同时登记任务和整体，scope=batch 仅更新所覆盖任务；不要复制同一 checks 逐项重登。

默认 `verify record <slug> --input - --state-revision <revision> --json` 从 stdin 输入；临时文件不进 artifacts。record 自动更新状态和 README；失败也如实记录。摘要失败只 verify render，不重复检查。原始报告、失败日志按需保留，不默认生成 results.json、validation.md、review.md 等同义报告；普通预检成功不另写 preflight。历史用 evidence/history 按需读取，不清理被引用文件。

## 外部验收与交付

`item delivery <slug> --input - --state-revision <revision>` 记录实际版本、提测、部署与验收；未知不猜。externalChecks 提交完整清单，未关闭事项保留负责人和完成条件；关闭附依据或明确 waived 理由。机器 attempt/receipt 是外部执行事实，不复制成人工报告。

README 可重建。独立审查、前端指南、SQL、发布/回退说明按需保留链接，不另维护状态；指南复用原文件。

focused-v1 只登记稳定交付入口和最小可校验证据，不复制完整报告/逐批 Markdown；说明临时证据失效风险。legacy 不搬迁。

## 完成

记录通过、当前代码有效、部署和业务验收分别说明。目标验收关闭且用户确认结束后直接 `item complete <slug> --state-revision <revision> --json`；它即时核对审批、代码、任务和外部验收，无须先重复 brief --check-code。仅诊断时使用后者。无部署目标不伪造部署事实。
