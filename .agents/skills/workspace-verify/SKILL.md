---
name: workspace-verify
description: Record actual validation and delivery facts, then complete an accepted WorkItem.
---

# 验证与完成

验证已授权范围内的实际结果。没有 WorkItem 的小改，在回复中给出命令、退出状态和结果，不创建记录目录。

## 验证

从当前需求验收、仓规则、manifest、CI 和已有测试核实检查入口。任务验证覆盖当前行为，整体验证覆盖必要回归与需求符合性。检查测试确实执行；零执行、跳过目标测试或仅编译不能证明行为完成。

运行检查后，用 `verify snapshot <slug> --json` 取得当前代码状态；逐任务加 `--task T01`。证据记录格式按需读取 [证据输入](references/evidence.md)。普通需求只需一次整体验证。

```bash
python3 scripts/kit.py verify record <slug> --input evidence.json --state-revision <stateRevision> --json
```

记录成功自动更新任务完成事实或整体验证指针，并生成摘要。失败也记录实际结论；最新失败覆盖旧成功。状态已提交而摘要未生成时执行 `verify render <slug>`，不重复执行检查。历史和日志只在需要时通过 evidence/history 展开。

## 外部验收与交付

通过 `item delivery <slug> --input delivery.json --state-revision <stateRevision>` 记录逐仓版本、提测、部署、验收及依据；未知不猜。未完成外部事项写明 R、描述、负责人和条件。关闭时提供实际证据；范围调整用 waived 并说明理由，不静默丢弃待办。

README 和 verification.md 均由脚本生成。需要发布顺序、开关、观察指标、回退条件时，交付说明放 artifacts/ 并链接已有设计，不另维护状态副本。前端指南复用原文件。

## 完成

使用 `brief <slug> --check-code` 核对当前适用性。记录通过、当前代码有效和实际部署是不同事实。所有本次目标验收关闭后，用户确认结束，再运行：

```bash
python3 scripts/kit.py item complete <slug> --state-revision <stateRevision> --json
```

脚本再次核对审批、任务、当前验证和外部验收。没有部署目标不要求伪造部署。工作台使用同一完成入口，不另维护门禁。
