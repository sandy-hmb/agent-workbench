# {title}

- 状态：planning
- 涉及仓库：{repositories}
- 工作分支：{branches}
- 基线分支：{base_branches}
- 需求审阅：待审阅
- 设计审阅：未生成
- 计划审阅：未生成
- 当前迭代：i01
- 执行方式：单 Agent
- Workflow Run：未创建
- 最后更新：{updated}

## 当前进度

需求草案已生成，待审阅。下一步：确认需求范围与验收。

## 本轮变更

简述本轮交付目标；完整行为与技术变化分别见需求、设计，不重复正文。

## 交付状态

| 仓库 | 提交或版本 | 提测结果 | 部署情况 | 外部验收 |
|---|---|---|---|---|
| {repositories} | 未记录 | 未执行 | 未确认 | 未记录 |

多仓分别记录；只依据实际操作结果更新，不从任务完成、`testing`、push 或 PR 状态推断部署和验收。实际验证产生后链接验证摘要；测试交接需要说明的范围、数据与限制按需写在此处。

## 文档

- [需求](requirements.md)

按实际活动登记已生成的文档：普通活动使用 `change.md`，复杂活动按需使用 `design.md` 和 `plan.md`，验证后生成 `verification.md`。旧 Feature 的 `plans/implementation.md` 继续可读；新文档不预建空附件。需求绑定的交付物放在 `artifacts/`；SQL 等成组文件通过目录 README 导航。
