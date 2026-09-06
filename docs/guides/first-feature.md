# 第一个需求

每次新会话先读取状态和当前需求摘要：

```bash
python3 scripts/kit.py status --root . --json
python3 scripts/kit.py brief <feature-slug> --json
```

第二条只在你已明确选择某个未完成需求时使用。status 的 `mode` 决定需求目录：用户工作区使用 `.workspace/docs/features/`，维护公共 Kit 才使用 `docs/development/features/`。

## 先分类

单一业务仓的局部实现、无外部契约或核心状态变化、无需新增依赖且能定向验证时，可以走轻量路径：实现、运行该仓已有验证、在当前交付中记录命令和结果。不创建需求目录。

其他改动走标准需求。以下示例以 `payments-retry` 和已登记仓 `service` 为例：

```bash
python3 scripts/kit.py feature create payments-retry \
  --repo service --title "支付重试" --summary "处理瞬时失败" --json
python3 scripts/kit.py registry branch service \
  --type feature --slug payments-retry --json
```

按命令返回的 `baseBranch` 和 `branch` 写入需求记录；不要手工拼接分支名。创建分支需要工作树干净且用户确认，Kit 不自动创建、切换、提交或合并分支。

## 标准需求

需求确认后先创建：

```text
.workspace/docs/features/payments-retry/
├── README.md
└── requirements/requirements.md
```

随后按讨论结果按需增加：

- 方案确认后创建 `design/design.md` 并从 README 链接。默认用该文件的章节记录接口、数据库和上线策略；确需独立讨论或维护时才拆分专题文件。
- 实施计划和验证策略确认后创建 `plans/implementation.md`，保留一个任务复选框清单，从 README 链接，并将状态更新为 `development`。
- 首次实际验证时创建 `testing/verification.md`；它只记录实际命令、退出状态和结果，不使用占位内容。

每个阶段先讨论结论、假设和待决项，用户明确确认后才写入对应文档；未回复不是确认。行为变化先写最小失败测试；配置、文档或已有结构检查覆盖充分的改动采用最小有效验证。实现期间把计划项逐项勾选。

需要临时 SQL、DDL、DML 或交付 fixture 时，将其放在当前需求的 `artifacts/`，SQL 使用 `artifacts/sql/`。数据模型、迁移顺序、兼容和回退策略默认写在主设计文档的数据库章节；业务正式数据库迁移和自动化测试必需 fixture 必须随业务仓版本化。

## 验证、续接与完成

完成实现后运行仓登记的验证命令，再运行：

```bash
python3 scripts/kit.py status --root . --json
python3 scripts/kit.py doctor --root .
```

`workspace-verify` 会把已授权的离线验证证据记录到 `testing/verification.md`。无 Extension 时流程没有额外步骤；`testTarget: null` 时不运行提测，保持当前现场并说明目标未配置。

新会话通过 `status` 和 `brief payments-retry --json` 恢复。验证通过且确认结束后才执行：

```bash
python3 scripts/kit.py feature set-status payments-retry done
```

`done` 只改需求状态，不会删除记录、分支或业务代码。

## 等价命令

```bash
python3 scripts/feature_context.py create payments-retry \
  --repo service --title "支付重试" --summary "处理瞬时失败" --json
python3 scripts/workspace_registry.py branch service \
  --type feature --slug payments-retry --json
python3 scripts/feature_context.py set-status payments-retry done
```
