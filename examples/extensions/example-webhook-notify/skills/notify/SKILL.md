---
name: notify
---

# Example Webhook Notify

`example-webhook-notify` 把当前 Workflow Stage 的上下文（workflow/run/stage/featureSlug/repository/branch）POST 到一个由 `WEBHOOK_URL` 环境变量指定的地址，纯标准库实现，不依赖任何具体通知渠道。

## 接入具体渠道

复制本示例目录，只改 `commands/notify.py` 的 `build_payload()` 函数——把六个固定字段翻译成目标渠道要求的报文结构（字段名、嵌套层级、签名头等）。其余读取请求、发起 POST、返回结果契约的部分不需要改。

## 挂载到 Workflow

触发时机完全由你在 `.workspace/workflow-input.json` 里决定，见[自定义工作流](../../../../../docs/guides/custom-workflows.md)的"添加 Stage"一节。示例挂载片段：

```json
{
  "id": "notify.on-implement",
  "after": "feature.implement",
  "uses": "example-webhook-notify/notify",
  "trigger": "auto",
  "with": {}
}
```

同一个 Action 可以在多个锚点重复挂载（例如同时在 `feature.implement` 之后和 `feature.submit-test` 之后各挂一次），`trigger` 各自独立选择 `auto` 或 `manual`。

## 边界

- 不做任何具体通知渠道的报文适配或签名校验——那是 `build_payload()` 的定制范围。
- 不重试、不排队、不发失败告警：POST 失败时脚本非零退出，由 Workflow 的既有错误处理接管。
- 不接收入站消息、不驱动工作流——这是纯粹的单向通知。
