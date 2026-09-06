---
name: workspace-cross-repo-analysis
description: Analyze a change that spans registered repositories and report ownership, contract direction, and implementation order without performing Git integration.
---

# Workspace Cross-Repo Analysis

用于跨仓需求的只读拆解与影响分析。

## 输入与读取

按需读取 `.workspace/workspace.json`、`.workspace/CONTEXT.md` 和相关 `.workspace/docs/repositories/<path>.md`；仓名或别名先用：

```bash
python3 scripts/workspace_registry.py resolve <name> --json
```

只读取本次需求涉及的仓内规范、接口和实现。未登记的目录不能当作已纳管仓；需要纳入工作区先转交 `workspace-init`。

## 输出

明确列出：

- 主改仓、配合仓、只读对照仓及每仓职责；
- 每条接口、事件、文件或数据契约的提供方与消费方；
- 依赖顺序，通常先确定提供方契约，再安排消费方改动和联调；
- 每仓的验证责任、兼容策略、未决假设和需要用户确认的写权限。

使用事实、推断、待确认三类标记，避免把仓库描述当成实现事实。

## 边界

默认只读。未经用户对具体仓的明确授权不写代码或文档，不创建分支，不提交、push、合并、提测或解决冲突。本 Skill 不负责 Git 集成；需要同步基线或交付测试时分别使用 `workspace-sync-base` 或 `workspace-submit-test`。
