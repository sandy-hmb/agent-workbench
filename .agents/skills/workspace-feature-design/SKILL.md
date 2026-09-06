---
name: workspace-feature-design
description: Classify a workspace change as lightweight or standard and create or review its scoped requirements, design, plan, and testing records.
---

# Workspace Feature Design

用于技术方案、需求整理和进入开发前的设计门禁。

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
4. 再讨论方案设计，明确复用的现有能力、关键取舍、风险和待确认项。用户确认后才创建 `design/design.md` 并从 README 链接。默认只维护这一份设计文档，接口、数据库和上线策略使用章节记录；只有某部分需要独立讨论、维护或按需读取，且用户确认后才新增专题文件并从主设计链接。正式数据库迁移和自动化测试必需 fixture 仍随业务仓版本化，不复制到 feature。
5. 再讨论实施计划和验证策略，说明修改范围、任务顺序、最小验证和回退风险。用户确认后才创建 `plans/implementation.md`，其中保留一个任务复选框清单；更新 README 链接，并将需求状态更新为 `development`。用户治理模式下，每个仓调用 `python3 scripts/workspace_registry.py branch <repo> --type <type> --slug <slug> --json` 计算候选分支；owner 默认取工作区配置，仅在用户明确覆盖时追加 `--owner <owner>`。直接使用返回的 `baseBranch` 和 `branch`，不要由 Agent 手工替换 `namePattern` 模板。公共 Kit 维护模式按治理仓规范和实际 Git 状态记录基线与候选分支。
6. 创建分支前检查工作树并展示工作类型、实际基线和完整候选分支名，等待确认。计划确认后，同一范围内实现、进度记录和离线验证连续执行；发现影响范围、验收标准或关键方案的新事实时，回到对应讨论阶段。`testing/verification.md` 只在首次实际验证时创建，记录真实命令、退出状态和结果。需求目录中的实际基线以当次 Git 检查结果为准，不把策略默认值追溯成历史事实。

## 边界

本 Skill 不在确认前 clone、创建或切换分支，也不自动 commit、push、合并或解决冲突。跨仓职责先用 `workspace-cross-repo-analysis`；需要同步或测试交付时转交对应 Skill。
