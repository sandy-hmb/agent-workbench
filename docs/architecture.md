# Agent Workbench 2 架构

Kit 将 Markdown 内容、可变运行状态和不可变验证证据分开管理；CLI 与 IDE 使用同一套事实读取和判定。

## 状态与写入

每个 WorkItem 的 state.json 保存身份、当前活动、审批、任务编号高水位、证据指针、交付与归档入口。需求、设计、任务定义不复制进 JSON。README 与 verification.md 是可重建视图。

item_store 负责路径、哈希、安全读写和锁；item_documents 解析当前文档；item_engine 在一个请求中按需加载事实、计算阶段与完成条件；item_actions 实现受控修改。CLI 和 Inspect 不互调，不另建完成判定。

证据先写成自包含的不可变 JSON，再原子更新唯一 state.json。提交前中断不产生完成事实，提交后摘要失败不撤销真实结果。重复提交同一输入和状态版本返回原结果。最新失败覆盖旧通过。

## 内容与迭代

普通需求使用 change.md，重大需求使用 requirements.md、design.md、plan.md。任务使用稳定 T 编号并在整个 WorkItem 内递增。新迭代归档当前文档及状态，保留证据文件，重新登记本轮任务和整体验证。历史不进入默认接手上下文。

批准记录绑定文档版本。变化需分类：非实质修改附理由沿用批准；范围、验收、关键方案或验证强度变化重新审阅受影响内容。脚本不声称自动识别语义或证明用户已同意。

## 查询与工作台

status 返回轻量列表；brief 定向读取指定 WorkItem 或任务。Inspect 2 提供 summary/task/change/flow、文档、证据和运行记录查询。每次请求只加载所需事实并复用结果，无长期缓存或守护进程。

执行检查实际工作树和分支；工作台浏览不切分支。未检查当前代码时返回 not_checked。任务已完成、上次验证通过、当前代码有效、部署和外部验收分别展示。

## Extension

未启用时不创建 Run。Provider 处理已定义能力，Action 挂在 Core Stage 前后。命令执行尝试是唯一执行事实，阶段视图即时派生。requestId 防重复调度；中断或结果未知先 reconcile，再按实际结果 retry。哈希只防输入漂移，不代表新增授权。

## 版本边界

新版不读取旧 Markdown 状态、旧目录和旧证据协议，不提供迁移。旧本地数据保留原状，用户在新目录初始化。公共工作区配置、WorkItem、Inspect 和 Extension 各自版本明确，不将不同格式的版本号混为一谈。

## 包与公开边界

scripts/kit.py 是唯一启动入口，workbench/cli 负责命令参数和输出；work_items 管内容、状态、查询与受控变更，workspace 管工作区配置和仓登记，extensions 管扩展执行，inspection 管只读协议。公共资源根由 workbench.resources 统一提供。

item update 仅改变允许的结构事实；开发阻塞与实际验证分开保存。取消是独立终态，重新开展走新迭代。Action 新派发校验生命周期、当前迭代、仓库绑定和阻塞，历史结果查询与 reconcile 不受这些调度条件限制。

状态、文档与响应版本分别使用 stateRevision、documentRevision、responseRevision。任务快照与通过证据只核对目标仓，最终完成仍检查全部涉及仓。摘要按未解决事项优先展示，并说明截断数量和完整入口；相同内容不重复写入。

测试夹具位于 tests/support，测试模块不导入其他 test_*.py。真实 CLI 生成的协议样例供工作台消费测试复用。
