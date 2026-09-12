# 用户治理工作区

- Kit 通用约定见根 `AGENTS.md`，本文件只写本工作区新增或收紧的规则。
- 没有 Skill 发现能力时，按 status 返回的 `runbook` 路径读取普通 Markdown；不需要安装 Skill。
- 读取 `.workspace/workspace.json`、`.workspace/CONTEXT.md`、相关 `.workspace/docs/repositories/<repo>.md` 和当前需求记录。
- 未明确要求 worktree 时，默认在目标仓当前工作目录开发；分支批准不包含 worktree 操作。创建、删除、切换到或把代码迁移至其他 worktree 前，必须展示目标仓、分支或基线、目录和用途并取得明确确认。
- 用户需求记录位于 `.workspace/docs/features/<feature-slug>/`。
- 同一业务 Feature 的后续变更保持原 slug；顶层文档维护当前规格和本轮任务，已结束迭代按 `workspace-feature-design` 指引归档。
- 标准需求按根 `AGENTS.md` 的讨论门禁逐阶段生成文档；扩展输出位置和重跑方式由其 SKILL.md 声明，不预设扩展目录。
- 已批准的同一仓、环境、操作与重试范围在计划内持续有效；只有实际影响扩大时重新确认。
- `workflow.enabled` 为 `true` 时，使用 `workspace-feature-workflow` 在当前 Core Stage 前后处理本地 Action；为 `false` 时不加载 Action。
