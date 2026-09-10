# Agent 指令实战手册 (Prompt Cookbook)

本文档提供与 Coding Agent（如 Claude、Cursor、Pi、Codex 等）配合使用 `agent-workbench` 时的常用指令（Prompt）模板。你可以直接复制或根据实际业务微调。

---

## 1. 开启新需求与规划阶段

### 发起一个新功能需求
```markdown
请帮我开发一个新功能：【简要描述需求，例如：在订单服务中增加支付超时自动取消与库存回补逻辑】。
请先检查当前工作区状态，判断改动规模。若符合标准需求流程，请使用 kit.py 创建需求并为我梳理需求要点（范围、验收条件与边界用例）。
```

### 审查需求文档后的批准指令
```markdown
我已阅读并审阅了 requirements/requirements.md。
需求范围与验收标准符合预期，予以批准。请更新需求审阅状态并进入技术方案设计阶段（design/design.md）。如有关键架构取舍请提出。
```

### 审查设计方案后的批准指令
```markdown
我已审阅 design/design.md 技术方案，同意推荐的设计决策。
设计予以批准。请根据设计文档生成可执行的实施计划（plans/implementation.md），采用标准的 task-evidence-v1 格式拆解任务。
```

---

## 2. 计划审阅与启动执行

### 批准实施计划并启动开发
```markdown
我已审阅实施计划 plans/implementation.md。任务拆解合理，分支策略确认无误。
计划已批准，请将状态更新为 development。
请按照 TDD 原则逐项执行任务：
- 先写最小失败测试；
- 编写实现并通过测试；
- 捕获代码快照并记录任务证据；
- 只要 executionDecision 为 RUN，请连续自动推进下一个任务，不要打扰我；遇到真实阻塞时再暂停汇报。
```

---

## 3. 任务执行与调试阶段

### 遇到单测失败时要求根因分析（禁止假装成功或绕过测试）
```markdown
任务【T02】的单测执行失败。请按照如下原则排查：
1. 严禁弱化断言或跳过该测试；
2. 打印详细错误堆栈并检查根因；
3. 检查是否涉及外部契约不一致或边界状态未覆盖；
4. 修复实现后重新运行完整单元测试，并提供通过证据。
```

### 新会话中唤醒与断点续接
```markdown
这是一个新会话。请按以下顺序恢复当前进度：
1. 运行 `python3 scripts/kit.py status --root . --json` 查看工作区；
2. 读取当前活跃需求以及 plans/implementation.md；
3. 展开当前未完成的下一个任务上下文（`kit brief <slug> --task <id>`）；
4. 检查前置依赖是否满足，若满足请直接继续执行。
```

---

## 4. 验证与交付收尾

### 运行全量离线验证与生成报告
```markdown
计划中的开发任务已全部标记完成。请执行以下收尾动作：
1. 运行目标仓登记的完整自动化测试与静态检查；
2. 捕获当前的 Git 代码指纹快照；
3. 将执行结果、退出码与覆盖情况更新到 testing/verification.md；
4. 运行 `python3 scripts/kit.py doctor` 确认工作区无异常。
```

### 归档需求
```markdown
验证全部通过。请运行 `python3 scripts/kit.py feature set-status <slug> done` 归档当前需求，并为我输出最终的代码修改摘要与交付说明。
```
