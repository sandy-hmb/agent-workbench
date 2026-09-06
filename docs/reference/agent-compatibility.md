# Agent 兼容性

当前验证范围为 Linux 和 macOS（2026-09-06）。其他宿主和操作系统可能可用，但不在本项目的已验证承诺内。

## 发现方式

- **原生读取 AGENTS.md**：具备文件系统访问能力的 Agent 直接读取仓根 `AGENTS.md` 完成初始化判断，这是本 Kit 的权威入口。
- **根目录指针文件**：`CLAUDE.md`（内容 `@AGENTS.md`）与 `GEMINI.md`（内容 `@./AGENTS.md`）供按约定读取指针文件的客户端使用；两者内容由公共 Kit 维护，不要手工改动。
- **Skill 发现**：`.agents/skills/<name>/SKILL.md` 是权威 Skill 源；`.claude/skills/<name>` 是指向它的相对符号链接。没有 Skill 路由机制的 Agent，把对应 `SKILL.md` 当成普通 Markdown runbook 直接读取即可，效果等价。

## 文件访问边界

跨仓能力（分析仓库间关系、接入业务仓等）依赖宿主能访问 Kit 父目录下的兄弟业务仓——Kit 与业务仓是同一父目录下的独立 Git 仓，不是 monorepo。

- 多数宿主提供"额外目录"/"工作区文件夹"一类的白名单机制，登记 Kit 的父目录后即可访问兄弟仓；具体设置项因宿主而异，请查阅宿主自身的文档确认。
- 严格限制当前工作目录（cwd）访问范围、且找不到上述白名单设置的宿主：替代姿势是从 Kit 的**父目录**（而不是 Kit 目录本身）启动会话，并显式请求 Agent 先读取 `<kit-directory>/AGENTS.md`——这样 Kit 目录和兄弟仓库都落在同一个可访问范围内。
