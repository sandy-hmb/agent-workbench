# 本地工作区布局

公共 Kit 是可更新的模板；`.workspace/` 是单台机器上的运行时状态。二者必须分开看待。

## 区分公共文件和本地状态

| 位置 | 作用 | Git 行为 |
| --- | --- | --- |
| `.agents/skills/` | Core Skill 的源文件 | 公共 Kit 跟踪 |
| `.claude/skills/` | 指向 Core Skill 的相对适配链接 | 公共 Kit 跟踪 |
| `scripts/`、`migrations/`、`schemas/`、`workflows/` | 工作流实现、迁移、机器契约和公共 Stage 定义 | 公共 Kit 跟踪 |
| `templates/`、`examples/`、`docs/` | 起步材料、模板和公开说明 | 公共 Kit 跟踪 |
| `.workspace/` | 使用者的工作区运行时状态 | 被忽略，不同步 |
| `.agents/skills/local-*` | 已激活本地 Extension 生成的 Adapter | 被忽略，不手改 |
| `.claude/skills/local-*` | 指向本地 Adapter 的相对链接 | 被忽略，不手改 |
| `workspace-input.json`、`new-repo.json` | 初始化或登记时的临时输入 | 被忽略，不是事实来源 |

公共更新只变更被跟踪的文件。`.workspace/` 不会被 `workspace_update.py apply` 覆盖，也不会自动同步到另一台机器；更新 plan 会读取本地 Extension 的兼容性与来源记录。

## 状态版本与迁移

`.workspace/workspace.json` 的 `version.major` 是状态格式版本。首发格式为 1，当前没有注册迁移步骤；`python3 scripts/workspace_migrate.py preview --root . --json` 会返回当前版本和空步骤列表。未来需要升级时，先运行 preview，再以返回的 `previewHash` 和独立备份目录执行 apply。不要手工改写版本号或 lock 文件。

## 三份上下文文件的分工

- 根 `AGENTS.md`：模式判定与总边界，宿主每次会话自动加载，覆盖公共 Kit 维护模式和用户治理模式两种场景。
- `.workspace/AGENTS.md`：用户治理模式下的工作区操作约定，初始化时从 `templates/workspace/AGENTS.md` 渲染生成，只在新初始化时更新。
- `.workspace/CONTEXT.md`：跨仓业务事实（数据，不是规则），登记仓库或仓内规范变化时更新；不要把操作约定写进这里，也不要把业务事实写进 AGENTS.md。

## 初始化后会生成什么

```text
.workspace/
├── AGENTS.md
├── workspace.json
├── workspace.local.json
├── CONTEXT.md
├── docs/
│   ├── repositories/
│   └── features/
├── extensions/
│   ├── <extension-id>/
│   └── .state/
│       ├── input.json
│       ├── lock.json
│       ├── sources.json
│       └── cache/
```

| 文件或目录 | 使用方式 |
| --- | --- |
| `AGENTS.md` | 当前用户工作区的最小执行约定。 |
| `workspace.json` | 非敏感的共享登记、分支策略、Extension Provider 绑定和共享配置。 |
| `workspace.local.json` | 本机使用者的分支 owner、角色、本地 Extension 配置和活跃需求指针。 |
| `CONTEXT.md` | 跨仓上下文的生成内容。 |
| `docs/repositories/` | 每个已登记仓的生成 profile。 |
| `docs/features/` | 用户工作区的需求、设计、计划和验证记录。 |
| `docs/features/<slug>/artifacts/` | 需求绑定的 SQL、DDL、DML、fixture 和其他交付物；按需创建，不是业务仓代码目录。 |
| `extensions/` | 本地 Extension 源目录；`.state/` 收纳其输入、锁、可移植来源摘要和缓存。 |
| `workspace.local.json` | 还保存本机绝对 Extension 来源路径；复制状态时不携带到其他机器。 |
| `workflow.json` | 已确认的 Custom Stage Overlay；不存在时走零扩展快速路径。 |
| `runs/` | 每次启用 Workflow 的状态、fingerprint 和短摘要；不保存日志或凭据。 |
| `cache/` | Extension 操作的短期运行文件，不是配置或历史事实。 |

启用自定义 Workflow 后，额外出现：

```text
.workspace/
├── workflow.json
└── runs/
```

`workflow.json` 是已确认的 Overlay；每个 Run 只保存流程续接状态和短摘要。没有 Overlay 时这两个路径不存在，Core 工作流不因此增加上下文或执行开销。

不要手工修改 `extensions/.state/lock.json`、`workflow.json`、`runs/` 或受管 `local-*` Adapter。使用 `workspace-extension` 和 `workspace-feature-workflow` 的 preview、apply 与 Run 命令保持它们一致。

## 多个进行中的需求

存在多个未完成需求时，`status` 默认会报告 `MULTIPLE_ACTIVE_FEATURES` 阻塞，要求先选定一个。`workspace.local.json` 的 `activeFeature` 字段可以显式记录"这台机器当前在做哪个需求"：设置有效指针后，`status` 直接针对该需求给出阶段和下一步，`blockers` 不再阻塞，并在 `otherActiveFeatures` 中列出其余进行中的需求。

```bash
python3 scripts/feature_context.py set-active <feature-slug> --root . --json
python3 scripts/feature_context.py set-active --clear --root . --json
```

切换只改 `workspace.local.json` 这一个文件，不改任何需求自身的状态、README 或分支记录；指针必须指向当前未完成（非 `done`）的需求，否则拒绝执行。指针指向的需求已完成或被删除时，`status` 会报告新的 `ACTIVE_FEATURE_INVALID` 阻塞，提示重新选择。没有 `activeFeature` 字段的既有 `.workspace/` 继续按原有行为工作，不需要手工迁移。

## 备份、清理和恢复

需要迁移机器或删除目录前，先复制整个 `.workspace/` 到受控备份位置。恢复时将其放回同一 Kit 根目录，再运行：

```bash
python3 scripts/workspace_doctor.py --root .
```

没有 Extension preview、apply 或 Provider 正在运行时，可以删除 `.workspace/extensions/.state/cache/`；下一次需要时会重建。不要把缓存当作可以恢复工作区的备份。

本地状态不应包含密码、令牌、私钥或带凭据地址。脚本会拒绝常见敏感字段，但使用者仍应在写入前检查内容。

## 模板更新

公共 Kit 更新不会重写已有 `.workspace/AGENTS.md`、`CONTEXT.md`、repository profile 或需求记录。新初始化会使用当时的模板；已有工作区只在相应 preview/apply 流程明确生成这些文件时变化。更新前先备份 `.workspace/`，需要采用新版模板时先审阅生成内容再写入。
