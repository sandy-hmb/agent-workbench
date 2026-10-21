---
name: workspace-execute-plan
description: Execute approved work continuously with targeted validation and authoritative evidence.
---

# 实施执行

使用 `kit.py brief <slug> --json` 接手，已知任务加 `--task T01`。检查审批、实际分支和范围；普通活动没有独立计划时沿已批准 change.md 执行。

## 执行循环

1. 读取任务正文、sources 指向的 R/D 和直接依赖的必要结果。按 instructionContext.rules 的 scope 应用规范，facts 为事实；同会话按路径、版本和范围复用，新会话重读。普通工作项用 --repo/--path 定位目录规范，小改不建计划。必需入口缺失先处理诊断，目录变化补查规则。
2. 复用覆盖，按行为与独有风险选检查，不按文件或层配测试。Bug 优先聚焦回归，新功能可先实现后验证；RED 可选，无须跳过说明。最小实现，不做无关重构、预留或测试专用生产能力；数据库按 workspace-verify 选择。
   跨会话任务可用 `item task-progress` 记进度；它不代替验证。
3. 核对检查执行数、跳过数、退出码、diff 和交付路径。删减本次多余抽象、配置或分支后重验，不削减安全、兼容与必要验证。Bug/失败/回归先复现，沿 owner/数据流找根因，再最小修复并回归；无法复现时说明原因，外部等待用 block、证据不足保留 unknown，不猜测性补丁。
4. 按实际检查取得 verify snapshot 并 record；同批多任务用 taskCoverage 一次登记，不复制 checks。格式见 workspace-verify。脚本更新状态和摘要，不手工勾选。
5. 按 record.nextStep 继续，有下一任务直接 brief --task，不重复查询完整工作区或接手摘要。状态变化或新会话再刷新上下文；完成后整体复核与验证。

普通活动默认一批目标检查；有缺口或独有风险才扩大，必要最终回归集中一次。需独立解锁才逐项登记。同一内容、模块/profile/参数的可信检查核对凭据复用，不重复编译；漂移或可疑才重验。历史基线失败另列，不扩大修复或掩盖新增回归。

## 阻塞与交接

当前任务普通代码或测试失败先解决。真正等待外部条件或业务决策时，记录事实、影响和恢复条件，再推进其他独立且已授权的 readyTasks。调整顺序不等于并行。

沿 nextStep 连续推进，完成或真实阻塞才停止；未漂移不重读规则。只汇报事实、未完成项和决策。

默认当前 Agent 连续执行。子 Agent 仅用于已授权且有明确收益的独立任务或审查；使用前读取 [协作边界](references/subagent-execution.md)。同一 checkout 只允许一个写入者，主 Agent 核对实际结果后统一记证据。未经授权不引入 worktree、并行、远端 Git、部署或外部环境。

## 开发阻塞

未执行检查但在等待环境、权限或决策时，使用 `item block <slug> --reason <原因> --owner <负责方> --condition <解除条件> --state-revision <stateRevision>`；只影响某项任务时加 `--task T01`。依赖等待由脚本推导，不重复登记。解除条件满足后使用 `item unblock --blocker B01 --reason <实际依据>`，不会生成测试通过或任务完成。

仅执行 readyTasks。工作项级阻塞暂停全部实施，任务级阻塞排除该任务和依赖方；其他独立任务继续。实际检查失败仍记录 failed，不再用验证结果 blocked 表达环境等待。
