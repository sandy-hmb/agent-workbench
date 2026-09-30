# 排障

- 多个 WorkItem：显式传入 slug，不设置全局焦点。
- STATE_CHANGED：重新读取目标需求，核对实际变化后重试；不重复执行已经完成的测试或外部操作。
- REVIEW_CLASSIFICATION_REQUIRED：比较文档变化，非实质修改附理由沿用批准；实质修改审阅受影响范围。
- TASK_ID_REUSED：新任务使用高于当前高水位的编号，不重用上一迭代编号。
- CODE_CHANGED：验证记录对应的代码已经变化，重新核对并验证实际结果。
- 状态已提交、摘要待重建：执行 verify render，不回滚完成事实。
- Action unknown：查询真实目标，通过 reconcile 记录依据后再决定 retry，不能把进程消失当作操作未发生。
- WORKFLOW_CHECK_INVALID：读取 brief/Inspect 的 pendingExternalChecks.reason，按 runId/requestId 查询 workflow result。仍 running 时等待或查询；执行者退出且查询为 unknown 后才核对目标并 reconcile。重试、配置变化或轮次/仓绑定变化后，执行当前 Action 并引用最新成功尝试，再通过 item delivery 提交完整 externalChecks 清单、passed 和依据。范围调整可用 waived 并附理由、引用最新已结束尝试，不能豁免未知结果。省略引用不会移除旧引用，失效项也不能从清单丢弃。参见[必要 Action 与验收](custom-workflows.md#必要-action-与验收)。
- 旧格式不支持：旧 feature、旧 workspace 配置与旧证据协议在新目录初始化，不删除、覆盖或自动转换旧数据。已有 item storage-migrate 只把受支持 WorkItem 的机器状态迁入 .state/，不转换旧协议；先 preview，再按返回命令带备份 apply，见[本地工作区布局](../reference/local-workspace-layout.md)。

具体命令可通过 `kit.py describe --json` 查询。
