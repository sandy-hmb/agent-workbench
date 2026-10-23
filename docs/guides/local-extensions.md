# 使用本地 Extension

本地 Extension 用于接入团队或项目特有规则。安装副本位于 `.workspace/extensions/`，只对当前机器的当前工作区生效；公共 Kit 更新不会上传、覆盖或同步它。

Extension 有两种声明：

- **Provider**：替换 Core 已主动调用的能力。当前只有 `branch.naming` 和 `context.term-router`。
- **Action**：团队自由命名的操作，例如测试、部署、扫描或通知。Action 不需要公共 Kit 预先登记名称。

需要在没有 WorkItem、Run 或 Stage 时按需使用的 Skill-only Action，可以显式声明 `"standalone": true`，并省略 `command`。只读发现入口为：

```bash
python3 scripts/kit.py action list --root . --json
python3 scripts/kit.py action resolve <extension>/<action> --root . --json
```

入口只返回短元数据、精确 Skill 路径、资源目录、effects、确认摘要、非敏感配置和 Extension digest，不启动命令、MCP、Provider 或 Workflow Run。Agent 只有在用户实际需要时才读取返回的 Skill 和资源。命令型或未声明 standalone 的 Action 仍只能通过 Workflow Stage 使用。

`action resolve` 定向校验所选扩展和实际 Provider 依赖的路径、digest 与配置；不因无关扩展损坏阻断本次独立动作。`action list`、doctor 与激活仍做完整健康检查。resolve 不豁免所选动作漂移、依赖缺失或执行授权。

## 准备来源

团队可把源码保存在任意已有私有业务仓，例如：

```text
<business-repository>/.agent-workspace/
└── team-delivery/
    ├── workspace-extension.json
    ├── skills/
    │   ├── integration-test/SKILL.md
    │   └── deploy-test/SKILL.md
    └── commands/
        └── deploy.py
```

个人一次性规则也可以直接放入 `.workspace/extensions/<id>/`。不从网络下载，不接受符号链接或特殊文件。

## 从 scaffold 到激活

写第一个 Extension 时，不需要手工照抄 manifest 例子：`scaffold` 直接在 `.workspace/extensions/<id>/` 生成一个能立刻通过校验的最小骨架（一个不含 `command`、`effects` 为空的占位 Action）。目标已存在时拒绝覆盖；`.workspace/` 尚未初始化时会报错，不会代替初始化。

```bash
python3 scripts/kit.py extension scaffold --root . --id demo-ext --json
```

预期输出要点：`id` 为 `demo-ext`，`path` 为 `.workspace/extensions/demo-ext`，`action` 为 `example-action`。生成物立刻可校验：

```bash
python3 scripts/kit.py extension validate-extension \
  .workspace/extensions/demo-ext --json
```

预期输出：`actions` 含 `example-action`，`capabilities` 为空数组。

写一份最小激活配置声明它（`.workspace/config/extensions.draft.json`）：

```json
{"extensions": [{"id": "demo-ext", "version": "0.1.0"}], "providers": {}, "config": {"demo-ext": {}}}
```

预览并检查 `previewHash` 和 `applyCommand`：

```bash
python3 scripts/kit.py extension preview \
  --root . --config .workspace/config/extensions.draft.json --json
```

确认后执行 preview 返回的 `applyCommand`；普通 Action 不生成 `local-*` Skill Adapter，显式 `standalone: true` 的 Skill-only Action 会生成受管入口。用 doctor 确认无 drift：

```bash
python3 scripts/kit.py extension doctor --root . --json
```

预期输出：`SUMMARY ERROR=0`。接下来把占位的 `skills/example-action/SKILL.md` 和 `workspace-extension.json` 里的 `confirmation`/`effects` 换成团队真实操作，再重新 preview/apply 一遍（内容变化会体现为 digest 变化，drift 检测机制不变）。Action 的插入、计划和执行见[自定义工作流](custom-workflows.md)。

## 从其他仓安装

从显式本地目录安装时先预览：

```bash
python3 scripts/kit.py extension install preview \
  --root . --source <local-extension-directory> --json
```

检查来源、目标、digest 和 `previewHash` 后，确认再执行 preview 返回的 `applyCommand`。安装只复制目录，不激活 Extension，不绑定 Provider，也不执行 Action。

## Manifest

首发版本只接受 schemaVersion 1 manifest；Provider-only Extension 也必须声明空的 `actions`：

```json
{
  "schemaVersion": 1,
  "id": "team-rules",
  "version": "1.0.0",
  "kitApi": 1,
  "provides": [
    {
      "capability": "branch.naming",
      "provider": "branching",
      "apiVersion": 1,
      "skill": "branch-rules"
    }
  ],
  "actions": [],
  "requires": [],
  "effects": []
}
```

带 Action 的 manifest 同样使用版本 1：

```json
{
  "schemaVersion": 1,
  "id": "team-delivery",
  "version": "1.0.0",
  "kitApi": 1,
  "provides": [],
  "actions": [
    {
      "id": "integration-test",
      "apiVersion": 1,
      "skill": "integration-test",
      "confirmation": {
        "title": "运行集成测试",
        "summary": "对当前变更运行团队集成测试。"
      },
      "effects": ["process.exec"]
    },
    {
      "id": "deploy-test",
      "apiVersion": 1,
      "skill": "deploy-test",
      "confirmation": {
        "title": "部署到测试环境",
        "summary": "将当前变更部署到测试环境。"
      },
      "command": ["python3", "commands/deploy.py"],
      "environment": ["DEPLOY_TOKEN"],
      "effects": ["network", "process.exec"]
    }
  ],
  "requires": [],
  "effects": ["network", "process.exec"]
}
```

每个声明的 `skills/<name>/SKILL.md` 必须有同名 frontmatter。Action API v1 必须包含单行 `confirmation.title` 和 `confirmation.summary`，准确说明将执行的操作，且不能包含凭据。Action 的 effects 必须是 Extension 顶层 effects 的子集。凭据只通过运行环境提供，不能写入 manifest、配置、lock 或文档。

先检查目录：

```bash
python3 scripts/kit.py extension validate-extension \
  .workspace/extensions/team-delivery --json
```

## 激活 Provider 和 Action

在 `.workspace/config/extensions.draft.json` 中声明需要激活的 Extension、单一 Provider 绑定和非敏感配置：

```json
{
  "extensions": [
    {"id": "team-delivery", "version": "1.0.0"}
  ],
  "providers": {},
  "config": {"team-delivery": {}}
}
```

每个 capability 只能有一个默认 Provider；仓级覆盖同样只能有一个实现。Action 不进入 Provider 绑定，多个 Action 应通过多个 Workflow Stage 排序。

```bash
python3 scripts/kit.py extension preview \
  --root . --config .workspace/config/extensions.draft.json --json
```

确认后使用返回的 `applyCommand`。apply 更新 `.workspace/config/workspace.json` 与 `extensions/.state/lock.json`。Provider Skill 和显式 `standalone: true` 的 Skill-only Action 会生成受管 `local-*` Adapter；普通 Action Skill 保留在 Extension 目录，直到 Workflow 到达该 Action 才按需读取。standalone Action 的 Adapter 只承担 Skill inventory 发现，正文仍由 Agent 在实际选择后读取。

无需新增 manifest 字段。Adapter 沿用 `.agents/skills/local-<extension>-<skill>/`、Claude 相对链接以及原 lock/marker/digest 格式；Provider 和 standalone Action 使用同一 Skill 时会报错，不能覆盖。旧版本已激活 standalone Action 的 lock 缺少 Adapter 时，doctor 会报告 `ADAPTER_DRIFT`，使用当前 `extension preview` 和其返回的 `applyCommand` 补建即可。不要手工复制 Skill 或修改 lock。新建或重新打开会话后由宿主刷新 Skill inventory；Kit 不保证某个自然语言请求必然选中 Skill。

standalone Adapter 使用 namespaced `local-<extension>-<skill>` frontmatter 和短 wrapper，不复制源 Skill 正文、references、运行时配置或凭据。wrapper 要求先通过 `action resolve <extension>/<action>` 取得当前配置、`skillPath` 与 `resourceRoot`，再读取原扩展目录中的 Skill 和附件；不能把 Adapter 目录当成扩展资源根。实际 MCP 操作仍由 Agent 按现有授权执行。

Provider 可显式调用：

```bash
python3 scripts/kit.py provider run <capability> \
  --root . --operation <operation> --input .workspace/provider-input.json --json
```

`context.term-router` capability 可以直接测试路由结果，不需要先声明 Provider：

```bash
python3 scripts/kit.py context route --text "<要路由的文本>" --root . --json
```

这是只读命令，用来验证一段文本会被路由到哪个 term；激活对应 Provider 后可以据此确认它是否按预期接管路由。

Action 的插入、计划和执行见[自定义工作流](custom-workflows.md)。

## 维护和停用

### WorkItem 相关输出

公共 Kit 更新不会修改本地 Extension。若 Extension 会写入需求、设计、计划、验证或交付物，先审阅其 `SKILL.md`：它必须说明具体输出位置、文件归属，以及重复运行时追加还是覆盖。不要为了统一格式创建额外目录；只有交接需要时，在 work item README 中保留产物入口链接。

Extension 不得改写需求、设计或实施计划正文，除非用户已经确认当前阶段的内容变更。升级后重新 preview/apply，使 lock 的 digest 与已审阅内容一致。

修改已激活 Extension 会造成 digest 漂移。先检查，再重新 preview/apply：

```bash
python3 scripts/kit.py extension doctor --root . --json
```

停用前，先从 `.workspace/config/local.json` 移除对应本地配置；再从 `.workspace/config/extensions.draft.json` 移除 Extension 并执行 preview、确认、apply。系统不会自动删除本地配置，也不会覆盖手工改动的受管 Adapter。

`effects` 只用于审阅预期影响，不是操作系统级权限隔离。任何网络、Git、文件写入、部署或外部系统操作都仍须在执行前取得当前会话授权。

升级公共 Kit 前，使用 `kit.py update plan` 检查 Extension 兼容性；不要手工修改 lock。

## 直接体验示例

`examples/extensions/` 提供两个可以直接安装的完整示例：

- `example-branch-naming`：替换 `branch.naming` 的最小 Provider。
- `example-webhook-notify`：通用 webhook 进度通知 Action。

复制到 `.workspace/extensions/`，按上文流程体验一轮：

```bash
cp -r examples/extensions/example-branch-naming .workspace/extensions/example-branch-naming
python3 scripts/kit.py extension validate-extension \
  .workspace/extensions/example-branch-naming --json
```

其余步骤（激活、preview/apply）与本文档前述流程完全一致。

## 新 WorkItem 的输出边界

2.11 新建项使用 `documentPolicy=focused-v1`，旧项缺省为 legacy，不迁移历史。扩展的 `itemOutputs` 按 Skill 声明；`[]` 表示不生成 Item 文件，缺失不是任意写入许可。例如：

```json
{
  "itemOutputs": {
    "query": [],
    "run": [
      {"path": "artifacts/runs/team-test/{runId}/case-results.json", "kind": "evidence", "writeMode": "append-run", "purpose": "逐 Case 验收证据"}
    ]
  }
}
```

扩展 ID 为 team-test 时，evidence/log 只能在上述所属运行目录，runId 是唯一安全路径段；handoff 使用 `artifacts/` 下固定 Markdown 路径与 `update`，不使用日期/final/v2 文件。真实新执行追加，同次查询或恢复使用原记录；已归档证据不覆盖。SKILL.md 写触发条件、唯一写入者和恢复规则。源码/测试、缓存和锁仍放原本所属仓或受管目录，不复制为 Item 文档。

`extension validate-extension`/preview 校验声明与归属冲突；`action resolve <ref> --item <slug> --json` 或绑定 Item 的 Workflow plan 只核对当前能力。新项的写文件 Skill 必须传目标并检查 itemOutputContext；只读无 Item 查询不必建项。声明不是外部执行授权，也不是任意脚本沙箱。扩展写入函数仍需前置路径/符号链接检查；发现越界保留现场、不自动删除或重放。

不输出成功预检、阶段总结、接手摘要或验证输入副本。已有章节/同用途文件优先更新；规范细节放 `references/` 纳入审批，扩展不能改主规格、README 或 `.state/`。有持久权威原件就引用；当前 artifactRefs 仍仅支持 Item 内文件，必要最小验收 JSON 保留一份，不放宽为任意外部文件路径。
