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
2. 确认稳定的小写 `kebab-case` `<slug>`、目标仓和范围后，再创建或更新需求 README。
3. 公共 Kit 维护模式在 `docs/development/features/<slug>/` 写入适用内容，不使用运行时需求模板。用户治理模式使用：

   ```bash
   python3 scripts/feature_context.py create <slug> \
     --repo <repo> --title "<title>" --summary "<summary>" --json
   ```

   每个涉及仓重复一次 `--repo`。命令从 `templates/feature/README.md` 创建 `.workspace/docs/features/<slug>/README.md`、`requirements/requirements.md`、`design/design.md`、`plans/implementation.md` 和 `testing/verification.md`，并写入 registry 计算的分支与基线。需求绑定的 SQL、DDL、DML、fixture 和其他交付物按需放入 `artifacts/`，SQL 使用 `artifacts/sql/`；填完需求内容后运行 `python3 scripts/feature_context.py list --json` 校验元数据。
4. 用户治理模式下，每个仓调用 `python3 scripts/workspace_registry.py branch <repo> --type <type> --slug <slug> --json` 计算分支；owner 默认取工作区配置，仅在用户明确覆盖时追加 `--owner <owner>`。直接使用返回的 `baseBranch` 和 `branch`，不要由 Agent 手工替换 `namePattern` 模板。公共 Kit 维护模式按治理仓规范和实际 Git 状态记录基线与候选分支。
5. 创建分支前检查工作树；对唯一且确定的候选分支直接采用 registry 返回的 `baseBranch` 和 `branch`。只有分支名、基线、仓库或工作树状态存在歧义时才询问。
6. 标准需求把范围、设计、实施计划和验证策略合并为一次进入实现确认；同一范围内实现、进度记录和离线验证连续执行，不因切换阶段重复确认。需求目录中的实际基线以当次 Git 检查结果为准，不把策略默认值追溯成历史事实。

## 边界

本 Skill 不在确认前 clone、创建或切换分支，也不自动 commit、push、合并或解决冲突。跨仓职责先用 `workspace-cross-repo-analysis`；需要同步或测试交付时转交对应 Skill。
