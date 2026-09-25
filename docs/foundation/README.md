# 核心工作流

Kit 2 使用唯一新版 WorkItem 格式、workspace 配置主版本 3 与 Inspect 2。业务仓独立，用户状态保存在 `.workspace/`，公共 Kit 维护记录保存在被忽略的 docs/development/items/。

按实际风险选择小改直接验证、普通方案一次审阅、重大需求分阶段审阅。活动可以从调查、接手、修复或验收开始，不强制补齐全部开发阶段。

Markdown 保存业务与技术内容。state.json 保存审批、任务证据引用和交付事实。README 与 verification.md 自动生成；任务没有可编辑完成复选框。

生命周期只有 active、paused、done。currentStage、readyTasks、nextActions 从当前事实计算，不另外持久化。Core Stage 锚点继续用于按需 Extension，不代表每个工作项必须走满全部阶段。

日常入口与例子见[第一个需求](../guides/first-item.md)，数据职责见[架构](../architecture.md)。已知 slug 直接 brief，未知目标才 status，不设置全局活动需求。
