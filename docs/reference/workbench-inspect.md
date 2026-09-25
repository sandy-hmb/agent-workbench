# Inspect 2.2

通过 `kit.py inspect --root <kit> --api-major 2 --json <operation>` 只读查询。仅支持 major=2；旧客户端和旧 WorkItem 明确拒绝，不自动转换数据。

信封包含 apiVersion、operation、status、observedAt、root、responseRevision、data、diagnostics。ok/partial 表示读取结果，不能推断工作流通过。error 返回退出码 1 与明确诊断。读取期间版本变化应重新查询。

| operation | 输入 | 内容 |
|---|---|---|
| workspace | 无 | 工作区、仓库位置和能力 |
| items | status/offset/limit | 不读取代码指纹的 WorkItem 列表 |
| projection | slug、view=summary/task/change/flow，可选 task | 当前视图所需事实 |
| document | slug、path、可选 documentRevision | 安全 UTF-8 正文 |
| artifacts | slug、offset、limit | 按需分页的交付物索引，不读取正文 |
| verification | slug、可选 check-code | 记录结果与当前代码适用性 |
| evidence | slug、可选 task/id | 一条实际证据 |
| handoff | slug | 引用当前文档的接手摘要 |
| search | query、可选 repo/status/offset/limit | 当前文档匹配片段 |
| workflow / runs / run | 对应筛选或 id | 已配置扩展与实际执行记录 |

任务视图包含 summary、tasks、progression、documents、stateRevision。documents 每项有 role、path、documentRevision；客户端按角色定位，不猜目录、不本地兜底读取。

summary.completionAction 指示可请求完成及阻塞原因；实际写入仍由 `item complete --state-revision` 再次核对。默认 verification.applicability=not_checked，显式 check-code 才检查当前代码。

查询不执行测试、Git 写操作、Provider 或 Action。单文本最多 1 MiB、响应最多 8 MiB，普通请求 10 秒、代码核对 30 秒；列表最多 200 项，搜索最多 50 项。非法路径、符号链接、未知版本或超限不能伪装为空成功。

summary 中的 executionBlockers 和 cancellation 分别展示开发阻塞与取消结论。任务包含 executionBlocked 与 waitingFor；readyTasks 排除实际阻塞和依赖未完成任务。取消项不出现在默认未完成视图。

JSON 输入错误返回 error.code、error.message 和 error.field；例如 externalChecks 必须为数组，错误定位 $.externalChecks。命令参数错误定位 $args，退出码 2；业务校验失败退出码 1。Inspect 仍使用版本化信封和 diagnostics。

## 集合版本与验证适用性

items、runs、search 的 data.collectionRevision 对完整筛选集合计算，分页消费者比较集合版本；responseRevision 仍描述单页响应，不能用它判断两页是否来自同一集合。集合真实变化时最多重新读取两次，不混合不同版本。

显式检查当前适用性时，同时核对当前批次和当前任务引用的交付物；artifactStates 标明 matched/changed/missing/unknown。修改交付物后旧证据保留，但不能支持当前完成。

接手包包含未解决阻塞的原因、负责人、解除条件及待外部验收摘要；完整事项通过定向 brief 查询，历史不默认展开。
