# 证据输入

只记录已经执行的检查；命令文本是证据，不由 record 执行。record 的 stateRevision 使用 snapshot/brief 返回的当前状态版本。

```json
{
  "scope": "item",
  "taskId": null,
  "recordedAt": "2026-09-25T10:00:00+08:00",
  "result": "passed",
  "reviewResult": "passed",
  "verificationScope": "本地后端验证",
  "codeState": {"service": "sha256:<snapshot-digest>"},
  "checks": [{
    "type": "测试", "workingDirectory": "service",
    "command": "python3 -m unittest tests.test_service",
    "target": "tests.test_service", "executed": 1, "skipped": 0,
    "exitStatus": 0, "result": "关键行为与反例通过"
  }]
}
```

逐任务使用 scope=task、taskId=T01，codeState 只包含目标仓；整体验证包含全部涉及仓。result 为 passed/failed；尚未执行的环境等待使用 item block；整体验证通过必须 reviewResult=passed。检查类型为测试、行为检查、集成、结构、迁移、静态检查或编译；性质必须足以覆盖任务。

可选 artifactRefs 为 `{path,sha256,bytes,type}` 数组，path 相对 WorkItem，实际文件哈希必须一致。不要放密钥或完整敏感日志。

交付记录示例：

```json
{
  "repositories": {"service": {"version": "<commit>", "submission": "submitted", "deployment": "未确认", "evidence": "测试分支推送结果"}},
  "externalChecks": [{"id": "integration", "requirement": "R1", "description": "测试环境联调符合接口契约", "owner": "测试负责人", "status": "pending", "evidence": ""}]
}
```

externalChecks 更新提交当前完整清单；passed/waived 必须填写 evidence。repositories 按仓合并，不提交不变仓。验证记录不能自行关闭外部待办。
