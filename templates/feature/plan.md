# {title} 实施计划

<!-- completion-policy: task-evidence-v2 -->

新版复杂需求使用根目录 `plan.md`。旧 Feature 的 `plans/implementation.md` 仍按原语义读取，不在新目录继续生成。

**目标：** 用一句话说明全部任务完成后的结果。

**方案：** 用两至三句话概括实现路径。

**技术栈：** 只列本计划实际使用的语言、框架和工具。

**设计来源：** [需求](requirements.md)、[设计](design.md)。

## 执行概览

记录当前迭代、目标仓、基线、工作分支和执行方式。规划快照注明日期；不记录当次会话的内部命令或校验 hash。

### 全局约束

只列适用于全部任务且会影响实现或验证的约束。

## 任务

- [ ] T01 任务标题

  依据：R1、D01
  依赖：无
  目标仓：`repository`
  验证性质：行为

  **文件**

  - Modify：`path/to/file.py`
  - Test：`tests/test_file.py`

  **接口**

  - Consumes：无
  - Produces：无

  **实施步骤**

  1. 先写一个能证明缺失行为的最小失败检查。
  2. 实现并运行目标检查，记录实际退出状态和结果。

  **验证**

  工作目录：`repository`

  ~~~bash
  exact-command
  ~~~

  通过条件：写明可观察业务结果，不只记录退出码。

## 整体验证与完成条件

记录跨任务回归、需求覆盖和待外部验证事项。实际证据由 Kit 写入 `testing/evidence/`，摘要由 `verification.md` 生成。
