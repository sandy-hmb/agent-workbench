---
name: workspace-execute-plan
description: Execute approved work continuously with targeted validation and authoritative evidence.
---

# 实施执行

已知任务直接使用 `kit.py brief <slug> --task T01 --json`；只知道 WorkItem 时使用 `kit.py brief <slug> --json` 接手。检查审批、当前实际分支和范围。普通活动没有独立计划时直接执行已批准 change.md；有任务时使用 `brief <slug> --task T01 --json`。

## 执行循环

1. 读取当前任务正文和 sources 指向的 R/D，直接依赖只展开必要结果。按 instructionContext.rules 的 scope 应用规范，facts 是事实来源；同会话按路径、版本和作用范围复用未变内容，新会话重新读取。普通工作项可加 --repo/--path 定位目录规范，小改无须建立计划。必需入口缺失时先处理诊断；目录变化后补查对应作用域。
2. 行为变化先写最小失败测试，确认失败源于目标行为缺失；编译失败、环境异常和未执行不能作为有效 RED。实现最小修改并定向验证。声明式变化使用最小有效检查，持久化变化覆盖真实结构或写入。
3. 核对检查执行数、跳过数、退出码、实际 diff 和交付路径。Bug/失败/回归先复现，沿 owner/数据流找根因，再最小修复并回归；无法复现时说明原因，外部等待用 block、证据不足保留 unknown，不猜测性补丁。
4. 取得 `verify snapshot <slug> --task T01 --json`，按实际结果调用 `verify record`。格式见 workspace-verify 的证据参考。脚本更新完成状态和摘要，不手工勾选计划。
5. record 的 nextStep 返回阶段、下一任务和阻塞摘要；有下一任务时直接 brief --task 展开它。无需先查询完整工作区和同一份接手摘要。状态变化或新会话才重新获取所需上下文；完成后做整体复核与整体验证。

普通活动默认只记录收尾的一批整体验证。需要独立解锁的任务才逐项记录。记录 RED→GREEN 过程有价值时，在实际通过检查的 result 中简述或引用日志，不新增红绿状态。

## 阻塞与交接

当前任务普通代码或测试失败先解决。真正等待外部条件或业务决策时，记录事实、影响和恢复条件，再推进其他独立且已授权的 readyTasks。调整顺序不等于并行。

一次一个任务不是会话边界。结束前重读 brief；仍有已授权可执行工作就继续，全部完成或真实阻塞才停止。只汇报已证实结果、未完成项和必要决策。

按需子 Agent 只用于有明确收益的独立任务或审查；获授权后读取 references/subagent-execution.md。未经授权不引入 worktree、并行、远端 Git、部署或外部环境。

## 多 Agent 宿主接入

默认当前 Agent 连续执行。宿主提供子 Agent 时，主 Agent 仍是 WorkItem 的唯一协调者：负责计划、授权范围、依赖、结果接纳和 `verify record`；实现 worker 只处理一个已准备好的任务。只读 advisor 或 committee 只能给出意见，不能编辑、批准或记录完成；用户明确要求转交整项责任时才使用 handoff。

派发前核对当前 `brief --task`、实际分支、直接依赖和写入范围。`readyTasks` 只表示 Kit 的依赖和阻塞条件已满足，不表示该任务尚未被宿主 Agent 派发。Paseo 等宿主的运行关联、模型和 Agent ID 由可选接入层维护，不进入 WorkItem 状态；接入层必须先排除仍在运行或待验收的同一任务和同一实际 checkout 写入者。

同一实际 checkout 在实现、回收、必要修复和验证完成前只允许一个写入者，主 Agent 也算写入者。不同仓的独立任务可按授权并行；同仓任务默认串行。worker 返回、Agent idle、commit 或宿主完成通知都不是 Kit 完成事实：主 Agent 先核对实际 diff 与检查，再按本 Skill 的证据流程记录。

宿主不可用或运行关系无法核实时，先确认没有仍会写入的 worker，再退回单 Agent 继续。不要猜测性重新派发、停止、reset、stash 或覆盖用户改动。

## 开发阻塞

未执行检查但在等待环境、权限或决策时，使用 `item block <slug> --reason <原因> --owner <负责方> --condition <解除条件> --state-revision <stateRevision>`；只影响某项任务时加 `--task T01`。依赖等待由脚本推导，不重复登记。解除条件满足后使用 `item unblock --blocker B01 --reason <实际依据>`，不会生成测试通过或任务完成。

仅执行 readyTasks。工作项级阻塞暂停全部实施，任务级阻塞排除该任务和依赖方；其他独立任务继续。实际检查失败仍记录 failed，不再用验证结果 blocked 表达环境等待。
