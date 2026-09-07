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

## 标准需求流程

1. 先判断模式：没有 `.workspace/workspace.json` 时为公共 Kit 维护模式，直接检查 `docs/development/features/` 判断是新需求还是继续已有需求，不调用依赖 workspace registry 的脚本；存在 `.workspace/workspace.json` 时为用户治理模式，用 `python3 scripts/feature_context.py list --json` 和 `.workspace/docs/features/` 判断。选择顺序是用户本轮明确指定、有效活跃指针、唯一未完成需求；明确值无效或仍不唯一时停止询问，不静默改选。
2. 先讨论需求澄清：目标、典型场景、范围、非目标、验收标准和仍需确认的假设。纯咨询、评审或尚未确认范围时不创建目录、不建分支，也不预写后续文档。用户明确确认需求结论后，确认稳定的小写 `kebab-case` `<slug>` 和目标仓。
3. 公共 Kit 维护模式在 `docs/development/features/<slug>/` 创建 README 和需求文件。用户治理模式使用：

   ```bash
   python3 scripts/feature_context.py create <slug> \
     --repo <repo> --title "<title>" --summary "<summary>" --json
   ```

   每个涉及仓重复一次 `--repo`。命令使用 `templates/feature/README.md`，只创建 `.workspace/docs/features/<slug>/README.md` 和 `requirements/requirements.md`，并写入 registry 计算的分支与基线。需求绑定的 SQL、DDL、DML、fixture 和其他交付物按需放入 `artifacts/`，SQL 使用 `artifacts/sql/`；填完需求内容后运行 `python3 scripts/feature_context.py list --json` 校验元数据。
4. 再讨论方案设计，明确复用能力、关键取舍、风险和待确认项。用户确认后才创建 `design/design.md` 并从 README 链接。默认只维护这一份设计文档，接口、数据库和上线策略使用章节记录；专题文件仅在用户确认需要独立维护时新增。写入后自审需求覆盖、矛盾、占位内容和范围，再请用户审阅实际设计文件。书面设计未获确认，不得生成实施计划或开始实现。
5. 书面设计获确认后，读取 `workspace-writing-plan`，由它生成和自审 `plans/implementation.md` 草案。计划、基线、分支和执行方式的书面确认属于计划 Skill；计划获批前需求保持 `planning`。
6. 实现中发现影响范围、验收标准或关键方案的新事实时，回到需求或设计讨论。`testing/verification.md` 只在首次实际验证时创建，记录真实命令、退出状态和结果。

## 边界

本 Skill 不在确认前 clone、创建或切换分支，也不自动 commit、push、合并或解决冲突。跨仓职责先用 `workspace-cross-repo-analysis`；需要同步或测试交付时转交对应 Skill。
