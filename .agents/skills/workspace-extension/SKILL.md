---
name: workspace-extension
description: Inspect, preview, activate, validate, and diagnose local agent-workbench Extensions.
---

# Workspace Extension

用于管理当前治理仓 `.workspace/extensions/` 中的本地 Extension。安装等同于目录已存在；激活仍必须经过 preview 和用户确认后的 apply。

团队源码位于其他本地业务仓时，先使用显式本地安装流程，不访问网络：

```bash
python3 scripts/workspace_extension.py install preview \
  --root . --source <local-extension-directory> --json
```

展示来源、目标、digest 和 `previewHash`，获得确认后执行返回的 apply 命令。安装只复制目录；Provider 绑定和 Workflow Overlay 仍使用各自的 preview/apply。

同一交付同时包含安装、激活和 Workflow Overlay 更新时，先准备可得到的 preview、目标路径、Provider/Action、effects 及后续派生步骤，作为一组具体变更请求一次确认。批准覆盖已展示的整组操作；随后仍逐步重算并使用每一步当前哈希。技术哈希变化但实际影响未变时继续，目标、路径、Provider、Action、effects 或外部影响扩大时重新确认。

## 只读检查

先列出本地已安装 Extension 或校验一个目录：

```bash
python3 scripts/workspace_extension.py list --root . --json
python3 scripts/workspace_extension.py validate-extension .workspace/extensions/<extension-id> --json
python3 scripts/workspace_extension.py capability list --root . --json
python3 scripts/workspace_extension.py doctor --root . --json
```

这些命令不会下载内容、不会扫描用户目录或厂商缓存，也不会执行 Provider。

## Scaffold

写第一个 Extension 时不需要手工照抄 manifest 例子：

```bash
python3 scripts/workspace_extension.py scaffold --root . --id <extension-id> --json
```

在 `.workspace/extensions/<extension-id>/` 生成一个能立刻通过 `validate-extension` 并激活 `preview` 的最小骨架（占位 Skill-only Action，`effects` 为空）。目标已存在或 `.workspace/` 未初始化时拒绝执行。

需要参考真实成品而不是空骨架时，`examples/extensions/` 有两个可以直接安装的完整示例（替换 Provider 与通用 webhook 通知 Action）。

## 激活流程

1. 将需要的本地目录放到 `.workspace/extensions/<extension-id>/`，其中必须有 `workspace-extension.json` 和声明的 `skills/<skill>/SKILL.md`。
2. 编写 `.workspace/extensions/.state/input.json`，只声明本次要激活的 `extensions`、每个 capability 的单一 Provider 绑定及非敏感 `config`。
3. 运行 preview，展示 `previewHash` 和完整 `applyCommand`：

   ```bash
   python3 scripts/workspace_extension.py preview --root . --config .workspace/extensions/.state/input.json --json
   ```

4. 向用户说明激活项、Provider 绑定、会生成或移除的 `local-*` Skill Adapter；没有覆盖本次实际影响的已有授权时等待明确确认。
5. 只使用 preview 返回的哈希执行 apply：

   ```bash
   python3 scripts/workspace_extension.py apply --root . --config .workspace/extensions/.state/input.json --preview-hash <previewHash>
   ```

apply 会同时更新 `.workspace/workspace.json`、`.workspace/extensions/.state/lock.json` 和受管 Adapter。只为已激活 Extension 生成 `.agents/skills/local-<extension>-<skill>/`；Claude 使用对应的相对符号链接。

## 边界

- 一个 capability 的默认绑定和单个仓级覆盖各只能指向一个 Provider。当前 Core capability 只有 `branch.naming` 和 `context.term-router`。
- manifest v2 可以声明任意命名 Action；Action 不绑定 capability，也不会生成全局 `local-*` Adapter。通过 `workspace-feature-workflow` 在对应 Stage 按需读取它。
- 需要输出 feature 相关文件的 Extension，在其 `SKILL.md` 声明具体位置、文件归属及重跑方式；不定义统一的扩展文档目录。只有交接需要时，需求主文档保留该产物入口链接。
- 不手工修改受管 `local-*` Adapter。内容、管理标记或符号链接失配时，先运行 doctor；系统会拒绝删除或覆盖未受管路径。
- 停用 Extension 前，若 `.workspace/workspace.local.json` 的 `extensions` 仍包含该 Extension 配置，preview 会拒绝。先显式移除该本地配置；系统不自动删除本地配置。
- 不将凭据写入 manifest、desired state、workspace 配置或 lock。凭据由运行环境在需要调用 Provider 时单独提供。
- 不自动创建分支、提交、推送、合并或修改业务仓。
