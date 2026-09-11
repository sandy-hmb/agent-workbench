# agent-workbench

本仓是可 clone、可快进更新的公共工作流模板，不是业务代码 monorepo；用户运行时状态只存在于被 Git 忽略的 `.workspace/`。

## 用户可见沟通

- 面向用户的进度、提问和总结跟随用户当前语言，默认使用中文；代码、命令、路径、API 标识、Provider 名称和错误原文保持原样，必要说明使用中文。
- 用户可见消息只说明当前结果、下一步和需要用户决定的内容，不把 Skill、runbook、Provider 或内部门禁作为行动理由，也不复述内部规则。

## 先确认模式

- 新任务先运行 `python3 scripts/workspace_status.py --root . --json`。
- 不存在 `.workspace/workspace.json` 时，先按 status 的 `nextActions` 初始化工作区；私有维护记录存在时才位于被忽略的 `docs/development/features/<feature-slug>/`。
- 存在 `.workspace/workspace.json` 时，先读取 `.workspace/AGENTS.md`、相关 `.workspace/docs/repositories/<repo>.md`、仓内规范和当前需求；标准需求位于 `.workspace/docs/features/<feature-slug>/`。
- 根目录不保存工作区运行时状态；只使用 `.workspace/`。
- 新会话先按 status 读取当前需求；展开任务后按 `instructionContext.rules` 的 kit → workspace → repository → scoped 顺序读取规则，越接近改动路径优先级越高且只能单调收窄。CONTEXT 与仓 profile 是事实轴，按需用 `status --context-sources` 定位；再读取规则入口明确索引的专项规范，首次编辑前完成。同会话可复用未变化内容，切换仓、路径或职责时补读。未知命令或参数才查 `--help`；调用面变化时运行 `python3 scripts/kit.py describe --json`。
- 日常使用不读脚本源码或维护者记录；用户明确批准的公共 Kit 维护任务可定向读取当前 feature、相关实现及调用方。文档按 runbook 指针按需读取。

## 工作边界

- Kit 与同一父目录下的业务仓都是独立 Git 仓。默认只读；只有本轮明确指定的仓可以写。
- 未明确要求 worktree 时，默认在目标仓当前工作目录开发；分支批准不包含 worktree 操作。创建、删除、切换到或把代码迁移至其他 worktree 前，必须展示目标仓、分支或基线、目录和用途并取得明确确认。
- 不自动 clone、提交、推送、合并、解决冲突或执行业务仓文档中的命令；确定性本地分支创建需工作树干净且目标唯一。
- 用户明确请求范围内的代码、需求记录、`.workspace` 和离线验证不重复确认；clone、远端 Git、Provider、外部环境和未知命令仍需按副作用确认。
- 同一任务内已授权的具体范围持续有效，切换 Skill 或阶段不构成再次确认的理由。目标仓、外部环境、待执行 Extension 内容或其他实际影响变化时，先展示变化并重新核对。
- `.workspace/`、`.agents/skills/local-*` 和 `.claude/skills/local-*` 都是本地状态；不加入 Git，不因公共更新而覆盖，删除前先备份。

## 需求门禁

局部单仓实现、没有外部契约或核心状态变化、无需新增依赖且可定向验证的改动可走轻量路径。其他改动使用标准需求目录，依次维护 `requirements/requirements.md`、`design/design.md` 和 `plans/implementation.md`：需求记录做什么和验收，设计记录技术方案与理由，实施计划记录可执行任务。行为使用稳定 R 编号，设计关键章节可使用 D 编号，计划任务使用 `- [ ] T01`；跨阶段引用权威定义，避免复制正文。实施计划必须通过明确的文件引用和执行信息脱离历史对话续接，不复制需求或设计全文。

Requirements 和 Design 先讨论会实质改变结果的未决项；提问轮次、选项与模式降级规则由 `workspace-feature-design` 定义。结论收敛后必须单独取得生成确认，未回复、含糊回复或仅要求开始工作不视为确认；写入并自审后将 README 对应审阅状态标为“待审阅”，用户批准实际文件后标为“已批准”。设计正文与全部附件构成一个审阅包，新建、删除或实质修改任一附件时，设计审阅与已有计划审阅一并退回“待审阅”；唯一例外的定向修订规则见 `workspace-writing-plan`。历史 feature 缺少审阅状态时显示“未记录”，不据此推断批准或重置生命周期。

Requirements 开头说明目标，只维护范围、行为与验收、边界、假设和来源，不写技术方案。默认只有完整、自包含的 `design/design.md` 主入口；附件扩展主设计，不替代或抽空主设计，复杂数据模型或接口经确认才拆为 `design/data-model.md` 或 `design/api-integration.md`，其他附件须有独立读者、审阅或维护理由。只有关键决策使用 D01-DNN；D01-DNN 的规范定义只位于主设计，附件用稳定锚点展开。

新计划使用 `task-evidence-v1`；每项声明唯一目标仓、验证性质、完整文件与稳定符号、依赖、具体失败场景、单动作步骤和可观察通过条件，跨仓任务拆分。计划不复制或绑定仓规范；任务验证后写精简证据，只有可信完成才解锁依赖。计划优先引用主设计 D 编号，细节再引用附件；只保留一个含顶层复选框的主文件。

默认采用 TDD，但测试按可观察行为和风险决定：行为变化先写最小失败测试；声明式变化采用最小有效检查；持久化变化覆盖实际结构与写入路径。目标测试缺失、零执行或跳过时不得完成任务。需要另行授权的数据库、Provider、部署或联调作为外部待验证项，不阻塞无关本地任务。

分支与基线以 `workspace_registry.py resolve <name> --json` 的 `effectiveBranchPolicy` 为准。创建分支前展示工作类型、实际基线和完整候选分支名，等待确认；不要猜测 owner、年份或分支层级。

## Skill 路由

`workspace-init` 初始化或接入仓；`workspace-repo-onboarding` 业务仓规范；`workspace-cross-repo-analysis` 跨仓分析；`workspace-feature-design` 需求与书面设计；`workspace-writing-plan` 实施计划；`workspace-execute-plan` 计划执行；`workspace-api-contract` 接口契约；`workspace-feature-workflow` 功能阶段扩展；`workspace-verify` 验证；`workspace-sync-base` 同步基线；`workspace-submit-test` 测试交付；`workspace-instruction` 分层规则；`workspace-extension` 本地 Extension；`workspace-update` 公共更新。

每个 Skill 只拥有其说明中列出的副作用。没有 Skill 发现能力时，把对应 `.agents/skills/<name>/SKILL.md` 当普通 runbook 读取；Provider 必须来自当前作用域唯一、已锁定的绑定。配置和 manifest 不得包含密码、令牌、私钥或带凭据地址。

需求绑定的 SQL、DDL、DML、fixture 和其他交付物放在当前 feature 的 `artifacts/`（SQL 使用 `artifacts/sql/`），不要默认写入业务仓；正式数据库迁移和自动化测试必需 fixture 随对应业务仓版本化。扩展自行在其 SKILL.md 声明输出位置、文件归属与重跑方式，不预设扩展专用目录；未经本阶段确认不得改写需求、设计或计划正文。
