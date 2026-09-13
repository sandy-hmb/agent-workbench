# {title} 实施计划

<!-- completion-policy: task-evidence-v2 -->

**目标：** 用一句话说明全部任务完成后的结果。

**方案：** 用两至三句话概括实现路径。

**技术栈：** 只列本计划实际使用的语言、框架和工具。

**设计来源：** [需求](../requirements/requirements.md)、[设计](../design/design.md)。

## 执行概览

记录当前迭代、Workflow Run、feature slug、目标仓、基线、工作分支、当前工作目录和执行方式。执行方式从“单 Agent”（默认）、“按需子 Agent”或“每任务子 Agent＋独立审查”中选择，并随计划一起审阅；三种方式均不自动并行、创建 worktree、commit 或 push。新会话的读取命令与共同必读文件也放在这里，历史迭代只在追溯时读取。

### 全局约束

只列适用于全部任务且会影响实现或验证的约束，并明确计划获批后连续执行全部任务；不复制需求和设计正文。

## 任务

只有存在至少两个自然业务或交付领域时才增加三级分组；分组不带复选框，不计入进度。跨仓交付拆为不同任务；同仓任务按可独立验收的主要行为闭环拆分。能分别实现、验证和接受的流程不得混在一项；同一行为的数据组合和必要测试可以合并。

### 1. 示例分组

- [ ] T01 交付任务

  用一至两句话说明完成后的可观察结果。

  依据：R1.1、[D01](../design/design.md#d01)；细节（如适用）：[D01 扩展](../design/data-model.md#d01-字段设计)
  依赖：无
  目标仓：`repository`
  验证性质：行为

  **文件**

  - Modify：`path/to/existing-file.py`（`Service#method`）
  - Test：`tests/test_service.py`（`ServiceTest#test_observable_result`）

  **接口**

  - Consumes：前置任务提供的签名、字段或契约；没有时写“无”。
  - Produces：后续任务依赖的签名、字段或契约；没有时写“无”。

  **实施步骤**

  1. 在 `ServiceTest#test_observable_result` 增加具体失败场景和断言，运行目标命令，预期因当前缺少的行为而失败。
  2. 修改 `Service#method` 完成一个实现动作，并写明该动作产生的可观察结果。
  3. 重跑目标检查与必要回归，核对执行数、跳过数、退出状态和本任务 diff。

  **验证**

  工作目录：`repository`

  ~~~bash
  exact-command \
    --target ServiceTest.test_observable_result
  ~~~

  通过条件：写明目标检查的实际执行要求和业务结果，不只记录退出码。持久化任务使用“验证性质：持久化”，并包含结构、迁移或集成级真实写入检查；编译或纯 Mock 不能单独通过。

## 任务证据

每项任务按“验证 → 核对交付 → 获取代码状态 → 写结构化证据 → 勾选 → 重建摘要 → 重跑 brief”完成。实际机器证据写入 `testing/evidence/`，`testing/verification.md` 只由证据生成摘要：

~~~json
{
  "kind": "taskEvidence",
  "taskId": "T01",
  "recordedAt": "YYYY-MM-DDTHH:MM:SS+08:00",
  "repository": "repository",
  "codeState": {"repository": "sha256:<digest>"},
  "checks": [{
    "type": "测试",
    "workingDirectory": "repository",
    "command": "exact-command",
    "target": "ServiceTest.test_observable_result",
    "executed": 1,
    "skipped": 0,
    "exitStatus": 0,
    "result": "写明实际可观察结果"
  }],
  "artifactRefs": [],
  "validationKind": "行为",
  "deliveryCheck": "passed",
  "result": "passed"
}
~~~

## 整体验证与完成条件

记录跨任务回归、需求覆盖、范围核对和交付边界；不要重复每个任务已经定义的验证。需要额外授权的数据库、Provider、部署、生产迁移或跨团队联调列在“待外部验证”，不作为本地任务依赖。
