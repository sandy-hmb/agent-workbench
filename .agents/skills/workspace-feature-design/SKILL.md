---
name: workspace-feature-design
description: Classify a workspace change and create or review its scoped requirements and written design before plan drafting.
---

# Workspace Feature Design

用于需求澄清、技术方案和书面设计门禁。书面设计获批后转交 `workspace-writing-plan`，不在本 Skill 中生成实施计划。

## 验证策略

默认采用 TDD，但以可观察行为或关键不变量为单位，而不是以文件、类、字段或代码行数为单位。对 Bug、业务逻辑、权限、安全和数据计算等行为变化，先写最小失败测试，再实现并重构。

声明式元数据、配置、文档、生成物、由现有测试完整保护的纯重构，以及已有 Schema、Migration、契约或持久层测试完整覆盖的结构变化，可以不新增测试；但必须选择并记录最小有效验证。数据库字段映射的简单调整不要求机械创建 Entity、Repository、Service 测试类，约束、默认值、精度、迁移和兼容性变化优先使用 Migration、Schema 或集成验证。

## 先分级

轻量改动必须只影响一个仓的局部实现，不改变外部契约、持久化结构、权限或核心状态，不新增依赖，并可定向验证；轻量流程不创建需求目录，完成定向验证后直接汇报命令和结果。其他情况按标准需求处理，涉及多个仓、契约、数据、权限、迁移或难以回滚时提高风险级别。

纯咨询、评审或只需解释现状时不创建目录、不建分支。

## 交互与文档协议

Requirements 和 Design 只询问会改变范围、验收标准或关键方案的问题。每轮最多提出三个问题；每题给出二至三个互斥选项，推荐项排在第一并说明影响，同时允许用户输入自定义方案。阶段不限制总轮数；连续三轮仍未收敛时，汇总剩余分歧和推荐方案，让用户决定接受推荐或继续讨论。

Plan mode 且原生交互工具可用时使用交互选择。其他模式一次只提出一个简短问题，结束当前轮次并等待用户回复，不静默采用推荐项。没有关键未决项时也要汇总将写入的结论并单独请求确认生成；未回复、含糊回复、仅陈述偏好或仅要求开始工作都不算确认。

Requirements 和 Design 的对应文件必须用户确认后才创建；写入并自审后还要由用户审阅实际文件。

阶段提问、确认和停顿的用户可见表达只包含当前结果、下一步和需要用户决定的内容，跟随用户当前语言并默认使用中文。不引用 Skill 名称，不解释内部门禁或复述本文件；命令、路径、API 标识和错误原文可以保留，周围说明保持用户语言一致。

Requirements 按需包含背景与目标、用户场景与验收、范围、功能需求、边界情况、关键实体、非目标、假设与依赖和成功标准。Design 按需包含现状与约束、候选方案与取舍、已选方案、组件与数据流、数据模型与迁移、接口契约、错误处理、安全与兼容、回退和验证策略。无关章节省略，不保留空标题或占位内容；需要跨文档精确引用时才增加稳定编号。

`design/design.md` 是唯一必需的设计文档。多实体复杂关系、DDL、迁移、回填、双写、兼容、数据校验、回滚或独立数据库审阅需要较大篇幅时，先提议并取得用户确认，再创建 `design/data-model.md`。跨仓或跨团队接口、多个端点或事件、版本兼容、鉴权及错误契约需要独立维护时，按 `workspace-api-contract` 的规则确认后创建 `design/api-integration.md`。其他专题也必须有独立负责人、审批或长期维护理由；附件从主设计链接且不复制正文。

## 标准需求流程

1. 先判断模式：没有 `.workspace/workspace.json` 时为公共 Kit 维护模式，直接检查 `docs/development/features/` 判断是新需求还是继续已有需求，不调用依赖 workspace registry 的脚本；存在 `.workspace/workspace.json` 时为用户治理模式，用 `python3 scripts/feature_context.py list --json` 和 `.workspace/docs/features/` 判断。选择顺序是用户本轮明确指定、有效活跃指针、唯一未完成需求；明确值无效或仍不唯一时停止询问，不静默改选。
2. 先讨论需求澄清，按交互协议收敛目标、场景、范围、验收、边界和假设。纯咨询、评审或尚未确认范围时不创建目录、不建分支，也不预写后续文档。向用户汇总需求结论、稳定的小写 `kebab-case` `<slug>` 和目标仓；用户明确确认生成后才继续。
3. 公共 Kit 维护模式在 `docs/development/features/<slug>/` 创建 README 和需求文件。用户治理模式使用：

   ```bash
   python3 scripts/feature_context.py create <slug> \
     --repo <repo> --title "<title>" --summary "<summary>" --json
   ```

   每个涉及仓重复一次 `--repo`。命令使用 `templates/feature/README.md`，只创建 `.workspace/docs/features/<slug>/README.md` 和 `requirements/requirements.md`，并写入 registry 计算的分支与基线。按文档协议填入实际内容，自审需求可验证、无歧义且没有占位内容，再请用户审阅实际文件；未获批准不得进入 Design。需求绑定的 SQL、DDL、DML、fixture 和其他交付物按需放入 `artifacts/`，SQL 使用 `artifacts/sql/`；填完需求内容后运行 `python3 scripts/feature_context.py list --json` 校验元数据。
4. 再讨论方案设计，明确复用能力、关键取舍、风险和待确认项。简单设计一次汇总确认，复杂设计按有实质取舍的章节逐段确认；用户明确确认生成后才创建 `design/design.md` 及已批准的按需附件，并从 README 链接。写入后自审需求覆盖、矛盾、占位内容和范围，再请用户审阅实际文件。书面设计未获批准，不得生成实施计划或开始实现。
5. 书面设计获批准后，读取 `workspace-writing-plan`，由它直接生成和自审 `plans/implementation.md` 草案，不再要求用户确认计划摘要。实际计划、基线、分支和执行方式的批准属于计划 Skill；计划获批前需求保持 `planning`。
6. 实现中发现影响范围、验收标准或关键方案的新事实时，回到需求或设计讨论。`testing/verification.md` 只在首次实际验证时创建，记录真实命令、退出状态和结果。

## 边界

本 Skill 不在确认前 clone、创建或切换分支，也不自动 commit、push、合并或解决冲突。跨仓职责先用 `workspace-cross-repo-analysis`；需要同步或测试交付时转交对应 Skill。
