# 证据输入

只记实际执行，不由 record 运行命令。默认 stdin/临时输入，不把 JSON 副本留在 artifacts。revision/codeState 来自同批检查的 snapshot；记录时即时核对内容。

```json
{
  "scope": "item",
  "taskCoverage": {"T01": [0], "T02": [0]},
  "recordedAt": "2026-10-21T10:00:00+08:00",
  "result": "passed",
  "reviewResult": "passed",
  "codeState": {"service": "sha256:<snapshot-digest>"},
  "checks": [{"type": "测试", "workingDirectory": "service", "command": "exact-command",
    "target": "实际覆盖的行为", "executed": 2, "skipped": 0, "exitStatus": 0, "result": "验收与反例通过"}]
}
```

scope=item 覆盖全部仓并含整体复核，可同时更新 taskCoverage 的任务；scope=batch 只登记覆盖任务，codeState 只含其仓；旧 scope=task/taskId=T01 仍可用。映射值为 checks 的零起始索引，必须实际证明对应任务、匹配验证性质和 checkout；同批允许依赖但不能跳过外部未完成依赖。失败不完成任务。每批只写一份检查事实、一次摘要。

检查类型：测试、行为检查、集成、结构、迁移、静态检查、编译。测试需 executed>0、skipped=0；持久化需集成/结构/迁移。旧路径变化说明主路径、残留引用和兼容边界。artifactRefs 可用 `{path,sha256,bytes,type}`，path 相对 WorkItem，文件须匹配，不放凭据。

交付 stdin 示例：`{"repositories":{"service":{"version":"<commit>","submission":"submitted","deployment":"未确认","evidence":"回执"}}}`。仓按需合并；externalChecks 更新须完整保留未关闭项，passed/waived 附依据。必要 Action 用 evidenceRefs 的 workflow/runId/requestId/stage 关联当前成功尝试；running 等待，unknown 先 reconcile，不能 skip 未知结果。Action 成功不自动完成验收。
