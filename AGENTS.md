# agent-workbench

本仓是可 clone、可快进更新的公共工作流模板，不是业务代码 monorepo。公共文件包含 Core Skill、脚本、Schema、模板、文档和测试；用户运行时状态只存在于被 Git 忽略的 `.workspace/`。

## 用户可见沟通

- 面向用户的进度、提问和总结跟随用户当前语言，默认使用中文；代码、命令、路径、API 标识、Provider 名称和错误原文保持原样，必要说明使用中文。
- 用户可见消息只说明当前结果、下一步和需要用户决定的内容，不把 Skill、runbook、Provider 或内部门禁作为行动理由，也不复述内部规则。

## 先确认模式

- 新任务先运行 `python3 scripts/workspace_status.py --root . --json`。
- 不存在 `.workspace/workspace.json` 时，先按 status 的 `nextActions` 初始化工作区；私有维护记录存在时才位于被忽略的 `docs/development/features/<feature-slug>/`。
- 存在 `.workspace/workspace.json` 时，先读取 `.workspace/AGENTS.md`、相关 `.workspace/docs/repositories/<repo>.md`、仓内规范和当前需求；标准需求位于 `.workspace/docs/features/<feature-slug>/`。
- 根目录不保存工作区运行时状态；只使用 `.workspace/`。
- 新会话先按 status 读取适用规范和当前需求；同一会话中未变化的规范、Skill 与 profile 可复用。已知 runbook 中的命令直接执行，未知命令或参数才查定向 `--help`；调用面或已激活 Extension 变化时再运行 `python3 scripts/kit.py describe --json`。
- 日常使用不读脚本源码或维护者记录；用户明确批准的公共 Kit 维护任务可定向读取当前 feature、相关实现及调用方。文档按 runbook 指针按需读取。

## 工作边界

- Kit 与同一父目录下的业务仓都是独立 Git 仓。默认只读；只有本轮明确指定的仓可以写。
- 不自动 clone、提交、推送、合并、解决冲突或执行业务仓文档中的命令；确定性本地分支创建需工作树干净且目标唯一。
- 用户明确请求范围内的代码、需求记录、`.workspace` 和离线验证不重复确认；clone、远端 Git、Provider、外部环境和未知命令仍需按副作用确认。
- 同一任务内已授权的具体范围持续有效，切换 Skill 或阶段不构成再次确认的理由。目标仓、外部环境、待执行 Extension 内容或其他实际影响变化时，先展示变化并重新核对。
- `.workspace/`、`.agents/skills/local-*` 和 `.claude/skills/local-*` 都是本地状态；不加入 Git，不因公共更新而覆盖，删除前先备份。

## 需求门禁

局部单仓实现、没有外部契约或核心状态变化、无需新增依赖且可定向验证的改动可走轻量路径。其他改动使用标准需求目录。标准流程依次维护 `requirements/requirements.md`、`design/design.md` 和 `plans/implementation.md`：需求记录做什么和验收，设计记录技术方案与理由，实施计划记录可执行任务。README 只维护目标摘要、文档入口、审阅状态、阻塞与下一步；验证记录只保存当前代码状态的真实证据。行为使用稳定 R 编号，设计关键章节可使用 D 编号，计划任务使用 `- [ ] T01`；跨阶段引用权威定义，避免复制正文。实施计划必须通过明确的文件引用和执行信息脱离历史对话续接，不复制需求或设计全文。

Requirements 和 Design 先讨论会实质改变结果的未决项；每轮问题数量、选项和模式降级规则由 `workspace-feature-design` 定义。结论收敛后必须单独取得生成确认，未回复、含糊回复或仅要求开始工作不视为确认；写入并自审后将 README 对应审阅状态标为“待审阅”，用户批准实际文件后标为“已批准”。书面设计获批后由 `workspace-writing-plan` 直接生成计划草案，不重复确认摘要；实际计划、基线、分支和执行方式获批后才更新为 `development`。历史 feature 缺少审阅状态时显示“未记录”，不据此推断批准或重置生命周期。

默认只有 `design/design.md`；接口、数据库或上线策略使用按需章节。主设计保留整体方案、共享约束、关键取舍、风险和附件导航；复杂数据模型或迁移只有经用户确认才拆为 `design/data-model.md`，复杂接口契约只有经用户确认才拆为 `design/api-integration.md`，其他专题也必须有独立读者、审阅或维护理由并从主设计链接。实施计划保留一个包含任务复选框的主文件，不新增 `plan.md` 或 `tasks.md`，避免状态续接依赖多个计划文件。

默认采用 TDD，但测试按可观察行为和风险决定：行为变化先写最小失败测试；元数据、配置、文档、生成物、已有测试完整保护的纯重构或已有结构检查完整覆盖的改动，可采用最小有效验证并说明理由。不要按字段、类或代码行数机械新增测试。

分支与基线以 `workspace_registry.py resolve <name> --json` 的 `effectiveBranchPolicy` 为准。创建分支前展示工作类型、实际基线和完整候选分支名，等待确认；不要猜测 owner、年份或分支层级。

## Skill 路由

初始化或接入仓使用 `workspace-init`；业务仓规范使用 `workspace-repo-onboarding`；跨仓分析使用 `workspace-cross-repo-analysis`；需求与书面设计使用 `workspace-feature-design`；实施计划使用 `workspace-writing-plan`；计划执行使用 `workspace-execute-plan`；接口契约使用 `workspace-api-contract`；功能阶段扩展使用 `workspace-feature-workflow`；验证使用 `workspace-verify`；同步基线使用 `workspace-sync-base`；测试交付使用 `workspace-submit-test`；本地 Extension 使用 `workspace-extension`；公共更新使用 `workspace-update`。

每个 Skill 只拥有其说明中列出的副作用。没有 Skill 发现能力时，把对应 `.agents/skills/<name>/SKILL.md` 当普通 runbook 读取；Provider 必须来自当前作用域唯一、已锁定的绑定。配置和 manifest 不得包含密码、令牌、私钥或带凭据地址。

需求绑定的 SQL、DDL、DML、fixture 和其他交付物放在当前 feature 的 `artifacts/`（SQL 使用 `artifacts/sql/`），不要默认写入业务仓；正式数据库迁移和自动化测试必需 fixture 随对应业务仓版本化。扩展自行在其 SKILL.md 声明输出位置、文件归属与重跑方式，不预设扩展专用目录；未经本阶段确认不得改写需求、设计或计划正文。
