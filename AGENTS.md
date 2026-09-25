# agent-workbench 2

本仓是公共工作流 Kit；业务仓保持独立。用户状态放在被忽略的 `.workspace/`；维护记录放在被忽略的 `docs/development/items/`。`.workspace/config/` 保存受管 JSON，根目录只保留规则、事实、WorkItem、Extension 与 Run。只支持新版配置和 WorkItem，不转换或覆盖旧数据。

## 协作与授权

使用用户当前语言，默认中文。只讨论会改变范围、验收、关键方案或外部影响的未决项，提供建议。先完成实际草案和自审，再请用户审阅；格式、措辞、文件组织由 Agent 处理。

已批准范围持续覆盖实施、定向验证与修复，不因切换任务、Skill 或刷新哈希重复确认。外部环境、部署、远端 Git 或其他新增实际影响需核对授权。默认当前目录、单 Agent；worktree、并行、提交和推送必须在授权范围内。只修改本次明确目标仓，不丢弃用户改动。

## 入口

已知需求直接 `python3 scripts/kit.py brief <slug> --json`；任务执行加 `--task T01`。未知目标才用 `kit.py status --json` 查看候选，不维护全局活动指针。执行前核对实际分支，不能自动切换。未知参数用 `--help`，能力变化用 `kit.py describe --json`。

小改直接实施与定向验证；普通需求一次审阅 `change.md`，默认整体验证；重大需求分阶段审阅 Requirements、Design、Plan。跨仓本身不提高风险。调查、接手和修复从已有事实开始，不补造开发文档。

## 内容与状态

Markdown 只保存内容；`state.json` 是生命周期、审批、任务和交付的唯一可变事实源。使用 Kit 写状态和证据，不手工改状态、README 或 Verification。任务使用 `### T01 标题`，同 WorkItem 内编号持续递增，不使用复选框。R/D 定义只维护一处，计划引用它们。

`verify record` 校验并记录实际结果，自动更新完成事实和摘要。零执行、跳过目标检查或没有有效证据不能完成。记录通过、当前代码有效、部署和业务验收分别说明。没有部署事实就写未确认。状态已提交但摘要失败时重建摘要，不重复执行检查或外部动作。

根、工作区、仓、目录规则按范围读取；同会话按路径、版本和作用范围复用未变内容，新会话重新读取。小改可用 `brief --repo <repo> --path <path>` 定位规范，无须建工作项。CONTEXT/profile 是事实来源。详细历史、日志与附件按需读取。

## Skill 路由

初始化与接入：`workspace-init`、`workspace-repo-onboarding`；澄清设计：`workspace-item-design`；独立任务：`workspace-writing-plan`；实施：`workspace-execute-plan`；验证：`workspace-verify`；跨仓与接口：`workspace-cross-repo-analysis`、`workspace-api-contract`；提测和基线：`workspace-submit-test`、`workspace-sync-base`；扩展与更新：`workspace-extension`、`workspace-item-workflow`、`workspace-update`；规则维护：`workspace-instruction`。

只读当前适用 Skill；无技能发现能力时直接读取 `.agents/skills/<name>/SKILL.md`。Extension 未启用不加载。账号、令牌、私钥不进入配置、证据或文档。需求交付物放 `artifacts/`；正式迁移和测试 fixture 随业务仓版本化。
