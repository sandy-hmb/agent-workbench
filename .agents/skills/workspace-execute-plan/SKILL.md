---
name: workspace-execute-plan
description: Execute an approved implementation plan task by task with risk-based TDD, root-cause debugging, diff review, and recorded progress.
---

# Workspace Execute Plan

用于已获批准的 `development` 需求。新会话先运行 status，并在多需求时显式指定 feature slug；再用 `brief <slug> --task <id> --execution --check --json --projection execution` 取得任务正文、直接依赖、设计引用、`instructionContext` 和执行决策。不得依赖历史对话，默认不加载历史迭代或完整验证记录。轻量改动不使用本 Skill。

计划和执行条件已批准且用户明确开始或继续后，只要目标仓、环境、操作类型、验收和实际影响不变，该授权持续覆盖计划内实施、验证和修复重试。阶段、Skill、任务切换和技术哈希刷新不是新的确认点；新增环境、部署、数据范围或其他实际影响时再确认。`confirmationRequired=false` 只表示当前 Core Stage 无文档确认要求，不代表所有外部操作均已授权。

## 执行

先预检计划声明的来源、实际代码、分支和范围是否一致。普通代码漂移，例如文件移动、类位于另一现有模块或等价测试入口变化，在不改变范围和方案时修正计划事实并继续；任务边界、验证强度、公共契约、关键方案或目标仓改变时，将相应文档退回待审阅。

一次一个可验证任务不是会话边界。计划获批后循环执行：

1. `currentTask` 是默认候选；用 `brief <slug> --task <currentTask.id> --execution --check --json --projection execution` 展开并校验规范入口。退出码非 0 时报告具体诊断，不开始编辑。
2. 新会话按 `instructionContext.rules` 的规则读取清单顺序读取；该顺序即 kit → workspace → repository → scoped 的单调收窄顺序。CONTEXT 与仓 profile 属于事实轴，按阶段需要用 `status --context-sources` 定位；再依据规则入口中的明确索引与当前改动类型读取专项规范。首次编辑目标仓前完成；扩展到新仓、新路径或新职责时补读，同一会话可复用内容和作用域均未变化的已读规范，不保存“已读”状态。
3. 行为变化先写最小失败测试，再实现至通过；声明式变化执行计划中的最小有效检查；持久化变化执行计划声明的结构、迁移或集成检查。
4. 运行任务验证，核对目标执行数、跳过数、退出状态、交付路径和实际 diff。部分实现、编译成功、零测试或跳过测试都不算完成。
5. 运行 `verify snapshot` 取得执行时代码状态，在 `testing/verification.md` 追加当前任务的精简“任务证据”；再勾选任务并重新运行 `brief <slug> --execution --check --json --projection execution`。
6. 新计划只有在 `trustedProgress` 包含当前任务且 `executionDecision=RUN` 时继续；有下一项依赖满足的未完成任务时直接继续。`readyTasks` 是依赖满足的候选集合，仍需核对实际环境和授权；`COMPLETE` 时进入整体复核，`BLOCKED` 时核对证据诊断和确认要求。旧计划保持原完成语义。

失败时先稳定复现，沿调用关系定位根因，验证一个最小假设，只实施一个根因修复并重新验证。编译失败、测试失败、缺少上下文和普通代码漂移均先在当前任务解决，不能借调整顺序逃避修复。

单个任务受阻时，记录当前任务、阻塞类别、实际证据、影响范围和恢复条件。若阻塞仅等待用户决策、外部条件或超出授权范围，使用 `brief --task` 展开其他 `readyTasks` 中独立且已授权的任务并顺序推进；调整执行顺序不等于并行。依赖当前阻塞结果的任务仍不得开始。

所有任务可信完成后，复核需求、计划、实际 diff 和代码质量。重要问题必须修复或以证据裁定；无未处理重要问题后转交 `workspace-verify`。

## 停止条件

发送最终答复前必须重新运行 brief。`executionDecision=RUN` 表示仍有已授权任务，不能结束或请求继续；`executionDecision=BLOCKED` 只有在 `confirmationRequired=true` 或存在可复核的阻塞证据时才允许停止。只有全部任务完成或所有剩余任务真实阻塞时，才能正常结束执行。用户要求暂停、缺少必要确认、需求/设计/计划需要重新批准、当前顺序任务需要未授权外部动作，或失败经根因诊断后仍无法在已授权范围内修复，属于真实阻塞。未完成时的结束回复必须包含当前任务、阻塞类别、实际证据、已完成的安全步骤和最小用户决策；不得只报告进度或提示“继续开发”。

执行方式随计划一起批准：默认“单 Agent”顺序实现、验证和自审；“按需子 Agent”只为独立任务或有明确收益的关键审查分派；“每任务子 Agent”由独立实现者完成并检查需求符合性和代码质量。选择后两种方式时读取[子 Agent 执行](references/subagent-execution.md)，默认单 Agent 不读取。均不自动并行、创建 worktree、commit 或 push。宿主缺少子 Agent 时，按需模式可由主 Agent 完成并说明，完整独立审查模式不得静默降级。

## 边界

只在已确认范围内修改目标仓。未明确要求 worktree 时，默认在目标仓当前工作目录开发；分支批准不包含 worktree 操作。创建、删除、切换到或把代码迁移至其他 worktree 前，必须展示目标仓、分支或基线、目录和用途并取得明确确认。

不得自动 commit、push、合并、提测、部署、访问真实接口或执行未获授权的外部命令。计划任务和验证记录只在对应证据存在时更新。
