# agent-workbench

面向本地编码 Agent 的多仓开发工作流工具。它把仓库上下文、需求记录、离线验证和可选本地 Extension 放在同一套可检查的约定中，而每个业务仓仍是独立 Git 仓。

运行条件：Python 3.10+、Git 2.23+，不安装第三方运行依赖。Linux 和 macOS 已在 CI 中验证；其他平台尚未验证。

## 快速开始

```bash
git clone https://github.com/sandy-hmb/agent-workbench.git
cd agent-workbench
python3 scripts/kit.py setup init draft --output workspace-input.json --json
```

补齐 `workspace-input.json` 中的工作区名称和兄弟业务仓信息后，让 Agent 依次完成 `init plan`、确认精确 clone 清单、`init clone`、`init preview` 和 `init apply`。每一步的实际命令、确认边界和可复制提示见[开始使用](docs/getting-started.md)。完成后检查：

```bash
python3 scripts/kit.py status --root . --json
python3 scripts/kit.py doctor --root .
```

`.workspace/` 是本机状态，公共更新不会覆盖它，也不应提交。目录职责、备份和恢复见[本地工作区布局](docs/reference/local-workspace-layout.md)。

## 日常路径与速查

- [开始使用](docs/getting-started.md)：环境要求、拓扑结构与初始化流程。
- [第一个需求](docs/guides/first-feature.md)：从轻量改动或标准需求开始，到验证和新会话续接。
- [常用命令速查 (Cheat Sheet)](docs/guides/cheat-sheet.md)：高频 CLI、状态机阶段及决策树一览。
- [Agent 指令手册 (Prompt Cookbook)](docs/guides/prompt-cookbook.md)：发起需求、审阅批准、TDD 执行等实战 Prompt 模板。
- [疑难排查 (Troubleshooting FAQ)](docs/guides/troubleshooting-faq.md)：常见阻塞原因（多需求冲突、Hash 漂移等）与解决方案。
- [核心工作流](docs/foundation/README.md)：模式、阶段、分支、验证与完成状态的参考。
- [本地 Extension](docs/guides/local-extensions.md)：按需接入本地 Provider 或 Action。
- [自定义工作流](docs/guides/custom-workflows.md)：为已确认的本地 Action 配置 Stage Overlay。
- [Agent 兼容性](docs/reference/agent-compatibility.md)：读取入口和兄弟仓访问范围。
- [指令分层参考](docs/reference/instruction-layers.md)：规则轴、事实轴、作用域和迁移判据。

## IDE 插件与周边生态

- **IntelliJ IDEA 插件**：[agent-workbench-intellij](https://github.com/sandy-hmb/agent-workbench-intellij) —— 基于 `kit.py inspect` 协议构建的 JetBrains IDE 官方插件，提供需求管理、任务跟踪、设计/计划文档预览、变更审查与验证状态可视化的原生图形界面。

## 架构阅读

[架构说明](docs/architecture.md)面向人工阅读，介绍组件职责、开发协作、状态与扩展机制；不属于 Agent 日常流程的读取清单。

## Core Skill

日常开发：`workspace-init`、`workspace-repo-onboarding`、`workspace-feature-design`、`workspace-writing-plan`、`workspace-execute-plan`、`workspace-verify`、`workspace-sync-base`、`workspace-submit-test`。

按需分析：`workspace-cross-repo-analysis`、`workspace-api-contract`、`workspace-feature-workflow`、`workspace-instruction`。

本地维护：`workspace-extension`、`workspace-update`。

各 Skill 的目录和 frontmatter 是机器事实来源；不具备 Skill 发现能力的 Agent 可以直接读取对应的 `SKILL.md`。

## 更新与验证

公共文件更新先使用[workspace-update](.agents/skills/workspace-update/SKILL.md)生成兼容性计划（`workspace_update.py plan`）；它不会覆盖 `.workspace/`。仓内离线检查如下：

```bash
python3 -m unittest discover -s tests -p 'test_*.py'
python3 scripts/kit.py doctor --root .
```

完整检查清单在 [CONTRIBUTING.md](CONTRIBUTING.md)。安全边界见 [SECURITY.md](SECURITY.md)。
