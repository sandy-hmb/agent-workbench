---
name: workspace-execute-plan
description: Execute approved work continuously with targeted validation and authoritative evidence.
---

# 实施执行

已知 WorkItem 使用 `kit.py brief <slug> --json` 接手。检查审批、当前实际分支和范围。普通活动没有独立计划时直接执行已批准 change.md；有任务时使用 `brief <slug> --task T01 --json`。

## 执行循环

1. 读取当前任务、直接依赖与 R/D 引用，按 instructionContext 加载适用规则；同会话复用未变内容。
2. 行为变化先写最小失败测试，确认失败源于目标行为缺失；编译失败、环境异常和未执行不能作为有效 RED。实现最小修改并定向验证。声明式变化使用最小有效检查，持久化变化覆盖真实结构或写入。
3. 核对检查执行数、跳过数、退出码、实际 diff 和交付路径。失败先复现、定位一个根因并验证最小修复，不弱化断言规避失败。
4. 取得 `verify snapshot <slug> --task T01 --json`，按实际结果调用 `verify record`。格式见 workspace-verify 的证据参考。脚本更新完成状态和摘要，不手工勾选计划。
5. 再读 brief。有依赖满足且已授权任务时继续，完成后做整体复核与整体验证。

普通活动默认只记录收尾的一批整体验证。需要独立解锁的任务才逐项记录。记录 RED→GREEN 过程有价值时，在实际通过检查的 result 中简述或引用日志，不新增红绿状态。

## 阻塞与交接

当前任务普通代码或测试失败先解决。真正等待外部条件或业务决策时，记录事实、影响和恢复条件，再推进其他独立且已授权的 readyTasks。调整顺序不等于并行。

一次一个任务不是会话边界。结束前重读 brief；仍有已授权可执行工作就继续，全部完成或真实阻塞才停止。只汇报已证实结果、未完成项和必要决策。

按需子 Agent 只用于有明确收益的独立任务或审查；获授权后读取 references/subagent-execution.md。未经授权不引入 worktree、并行、远端 Git、部署或外部环境。

## 开发阻塞

未执行检查但在等待环境、权限或决策时，使用 `item block <slug> --reason <原因> --owner <负责方> --condition <解除条件> --state-revision <stateRevision>`；只影响某项任务时加 `--task T01`。依赖等待由脚本推导，不重复登记。解除条件满足后使用 `item unblock --blocker B01 --reason <实际依据>`，不会生成测试通过或任务完成。

仅执行 readyTasks。工作项级阻塞暂停全部实施，任务级阻塞排除该任务和依赖方；其他独立任务继续。实际检查失败仍记录 failed，不再用验证结果 blocked 表达环境等待。
