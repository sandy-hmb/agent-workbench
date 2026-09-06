# 用户治理工作区

- 先运行 `python3 scripts/workspace_status.py --root . --json`。
- 同一会话中未变化的规范、Skill 与 profile 可复用；已知 runbook 命令直接执行，未知命令或参数才查定向 `--help`，调用面或已激活 Extension 变化时再运行 `python3 scripts/kit.py describe --json`。
- 没有 Skill 发现能力时，按 status 返回的 `runbook` 路径读取普通 Markdown；不需要安装 Skill。
- 读取 `.workspace/workspace.json`、`.workspace/CONTEXT.md`、相关 `.workspace/docs/repositories/<repo>.md` 和当前需求记录。
- 业务仓位于 Kit 父目录，均为独立 Git 仓库；未明确授权时只读。
- 同一任务内已授权的具体范围持续有效，切换 Skill 或阶段不重复确认；目标仓、外部环境、待执行 Extension 内容或实际影响变化时重新核对。
- 用户需求记录位于 `.workspace/docs/features/<feature-slug>/`。
- 需求绑定的 SQL、DDL、DML、fixture 和其他交付物放在当前需求的 `artifacts/`，SQL 放在 `artifacts/sql/`，不要默认写入业务仓。
- 标准需求按根 `AGENTS.md` 的讨论门禁逐阶段生成文档；扩展输出位置和重跑方式由其 SKILL.md 声明，不预设扩展目录。
- `workflow.enabled` 为 `true` 时，使用 `workspace-feature-workflow` 在当前 Core Stage 前后处理本地 Action；为 `false` 时不加载 Action。
- 需求门禁与验证策略以根 AGENTS.md 为准。
