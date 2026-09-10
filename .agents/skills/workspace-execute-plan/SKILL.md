---
name: workspace-execute-plan
description: Execute an approved implementation plan task by task with risk-based TDD, root-cause debugging, diff review, and recorded progress.
---

# Workspace Execute Plan

用于已获批准的 `development` 需求。新会话先运行 status 和显式指定 feature slug 的 `brief <slug> --execution --json`；用 `brief <slug> --task T01 --json` 展开当前任务，取得任务正文、直接依赖、设计引用和 `instructionContext`，不默认全文加载历史记录；不得依赖历史对话。轻量改动不使用本 Skill。

计划和执行条件已批准且用户明确开始或继续后，只要目标仓、范围、验收、关键方案和实际影响不变，该授权持续覆盖计划内的本地实施。任务切换、进度检查、本地编译测试及范围内修复重试不是新的确认点；`confirmationRequired=false` 时不得再次询问是否执行。

## 执行

先预检计划声明的来源、实际代码、分支和范围是否一致。普通代码漂移，例如文件移动、类位于另一现有模块或等价测试入口变化，在不改变范围和方案时修正计划事实并继续；任务边界、验证强度、公共契约、关键方案或目标仓改变时，将相应文档退回待审阅。

一次一个可验证任务不是会话边界。计划获批后循环执行：

1. 读取 brief 的 `currentTask`，再用 `brief <slug> --task <currentTask.id> --json` 展开它；不得跳过 `currentTask` 自行选择 `readyTasks` 中的后续任务。
2. 新会话先按 `instructionContext` 的读取清单读取工作区入口。首次编辑某目标仓前，按返回顺序读取治理 profile、`sourceInstruction` 和 `scopedInstructions`；再依据这些入口中的明确索引与当前改动类型读取专项规范。实现扩展到新仓、新路径或新职责时，首次编辑前补读对应规范。同一会话可复用内容和作用域均未变化的已读规范，不保存“已读”状态。
3. 行为变化先写最小失败测试，再实现至通过；声明式变化执行计划中的最小有效检查；持久化变化执行计划声明的结构、迁移或集成检查。
4. 运行任务验证和 `brief <slug> --check --json`，核对目标执行数、跳过数、退出状态、交付路径和实际 diff。部分实现、编译成功、零测试或跳过测试都不算完成。
5. 运行 `verify snapshot` 取得执行时代码状态，在 `testing/verification.md` 追加当前任务的精简“任务证据”；再勾选任务并重新运行 brief。
6. 新计划只有在 `trustedProgress` 包含当前任务且 `executionDecision=RUN` 时继续；有下一项依赖满足的未完成任务时直接继续。`COMPLETE` 时进入整体复核；`BLOCKED` 时核对证据诊断和确认要求。旧计划没有可信进度时保持原完成语义。

失败时先稳定复现，沿调用关系定位根因，验证一个最小假设，只实施一个根因修复并重新验证。编译失败、测试失败、代码定位变化和修复后的再次验证都不是任务切换理由。只有计划明确批准并行执行时才能选择 `readyTasks` 中非 `currentTask` 的任务；否则单个任务受阻且证据满足停止条件时，不得跳到后续任务。

所有任务可信完成后，复核需求、计划、实际 diff 和代码质量。重要问题必须修复或以证据裁定；无未处理重要问题后转交 `workspace-verify`。

## 停止条件

发送最终答复前必须重新运行 brief。`executionDecision=RUN` 表示仍有已授权任务，不能结束或请求继续；`executionDecision=BLOCKED` 只有在 `confirmationRequired=true` 或存在可复核的阻塞证据时才允许停止。只有全部任务完成或所有剩余任务真实阻塞时，才能正常结束执行。用户要求暂停、缺少必要确认、需求/设计/计划需要重新批准、当前顺序任务需要未授权外部动作，或失败经根因诊断后仍无法在已授权范围内修复，属于真实阻塞。未完成时的结束回复必须包含当前任务、阻塞类别、实际证据、已完成的安全步骤和最小用户决策；不得只报告进度或提示“继续开发”。

默认由当前 Agent 顺序执行和自审。风险需要独立审查时，可使用用户明确要求的审查方式或现有 Extension Action；本 Skill 不强制子代理、worktree、逐任务 commit 或外部服务。

## 边界

只在已确认范围内修改目标仓。未明确要求 worktree 时，默认在目标仓当前工作目录开发；分支批准不包含 worktree 操作。创建、删除、切换到或把代码迁移至其他 worktree 前，必须展示目标仓、分支或基线、目录和用途并取得明确确认。

不得自动 commit、push、合并、提测、部署、访问真实接口或执行未获授权的外部命令。计划任务和验证记录只在对应证据存在时更新。
