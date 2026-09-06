# 使用本地 Extension

本地 Extension 用于接入团队或项目特有规则。安装副本位于 `.workspace/extensions/`，只对当前机器的当前工作区生效；公共 Kit 更新不会上传、覆盖或同步它。

Extension 有两种声明：

- **Provider**：替换 Core 已主动调用的能力。当前只有 `branch.naming` 和 `context.term-router`。
- **Action**：团队自由命名的操作，例如测试、部署、扫描或通知。Action 不需要公共 Kit 预先登记名称。

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
python3 scripts/workspace_extension.py scaffold --root . --id demo-ext --json
```

预期输出要点：`id` 为 `demo-ext`，`path` 为 `.workspace/extensions/demo-ext`，`action` 为 `example-action`。生成物立刻可校验：

```bash
python3 scripts/workspace_extension.py validate-extension \
  .workspace/extensions/demo-ext --json
```

预期输出：`actions` 含 `example-action`，`capabilities` 为空数组。

写一份最小激活配置声明它（`.workspace/extensions/.state/input.json`）：

```json
{"extensions": [{"id": "demo-ext", "version": "0.1.0"}], "providers": {}, "config": {"demo-ext": {}}}
```

预览并检查 `previewHash` 和 `applyCommand`：

```bash
python3 scripts/workspace_extension.py preview \
  --root . --config .workspace/extensions/.state/input.json --json
```

确认后执行 preview 返回的 `applyCommand`；因为是 Action（不是 Provider），apply 不会生成任何 `local-*` Skill Adapter。用 doctor 确认无 drift：

```bash
python3 scripts/workspace_extension.py doctor --root . --json
```

预期输出：`SUMMARY ERROR=0`。接下来把占位的 `skills/example-action/SKILL.md` 和 `workspace-extension.json` 里的 `confirmation`/`effects` 换成团队真实操作，再重新 preview/apply 一遍（内容变化会体现为 digest 变化，drift 检测机制不变）。Action 的插入、计划和执行见[自定义工作流](custom-workflows.md)。

## 从其他仓安装

从显式本地目录安装时先预览：

```bash
python3 scripts/workspace_extension.py install preview \
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
python3 scripts/workspace_extension.py validate-extension \
  .workspace/extensions/team-delivery --json
```

## 激活 Provider 和 Action

在 `.workspace/extensions/.state/input.json` 中声明需要激活的 Extension、单一 Provider 绑定和非敏感配置：

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
python3 scripts/workspace_extension.py preview \
  --root . --config .workspace/extensions/.state/input.json --json
```

确认后使用返回的 `applyCommand`。apply 更新 `.workspace/workspace.json` 与 `extensions/.state/lock.json`。只有 Provider Skill 会生成 `local-*` Adapter；Action Skill 保留在 Extension 目录，直到 Workflow 到达该 Action 才按需读取。

Provider 可显式调用：

```bash
python3 scripts/workspace_provider.py run <capability> \
  --root . --operation <operation> --input .workspace/provider-input.json --json
```

`context.term-router` capability 可以直接测试路由结果，不需要先声明 Provider：

```bash
python3 scripts/workspace_context.py route --text "<要路由的文本>" --root . --json
```

这是只读命令，用来验证一段文本会被路由到哪个 term；激活对应 Provider 后可以据此确认它是否按预期接管路由。

Action 的插入、计划和执行见[自定义工作流](custom-workflows.md)。

## 维护和停用

修改已激活 Extension 会造成 digest 漂移。先检查，再重新 preview/apply：

```bash
python3 scripts/workspace_extension.py doctor --root . --json
```

停用前，先从 `.workspace/workspace.local.json` 移除对应本地配置；再从 `extensions/.state/input.json` 移除 Extension 并执行 preview、确认、apply。系统不会自动删除本地配置，也不会覆盖手工改动的受管 Adapter。

`effects` 只用于审阅预期影响，不是操作系统级权限隔离。任何网络、Git、文件写入、部署或外部系统操作都仍须在执行前取得当前会话授权。

升级公共 Kit 前，使用 `workspace_update.py plan` 检查 Extension 兼容性；不要手工修改 lock。

## 直接体验示例

`examples/extensions/` 提供两个可以直接安装的完整示例：

- `example-branch-naming`：替换 `branch.naming` 的最小 Provider。
- `example-webhook-notify`：通用 webhook 进度通知 Action。

复制到 `.workspace/extensions/`，按上文流程体验一轮：

```bash
cp -r examples/extensions/example-branch-naming .workspace/extensions/example-branch-naming
python3 scripts/workspace_extension.py validate-extension \
  .workspace/extensions/example-branch-naming --json
```

其余步骤（激活、preview/apply）与本文档前述流程完全一致。
