# 排障

- 多个 WorkItem：显式传入 slug，不设置全局焦点。
- STATE_CHANGED：重新读取目标需求，核对实际变化后重试；不重复执行已经完成的测试或外部操作。
- REVIEW_CLASSIFICATION_REQUIRED：比较文档变化，非实质修改附理由沿用批准；实质修改审阅受影响范围。
- TASK_ID_REUSED：新任务使用高于当前高水位的编号，不重用上一迭代编号。
- CODE_CHANGED：验证记录对应的代码已经变化，重新核对并验证实际结果。
- 状态已提交、摘要待重建：执行 verify render，不回滚完成事实。
- Action unknown：查询真实目标，通过 reconcile 记录依据后再决定 retry，不能把进程消失当作操作未发生。
- 旧格式不支持：在新目录初始化；不要删除、覆盖或自动迁移旧数据。

具体命令可通过 `kit.py describe --json` 查询。
