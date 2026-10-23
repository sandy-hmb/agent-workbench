---
name: workspace-item-design
description: Classify a change and prepare concrete requirements and design for risk-based review.
---

# 需求与设计

先查事实，仅问影响业务结果的问题；明确目标、验收、非目标和约束，不重复已定事项。目标不清或范围可能扩张时，回复中临时列出 Goal、Success Evidence、Stop Condition、Non-goals；小改不落文件。

## 选择记录强度

小改明确、局部、可逆：直接实施和定向检查，无须创建 WorkItem。普通需求：`change.md` 记录目标、必要方案、工作项和验收，一次审阅。重大需求涉及资金、权限语义、破坏性契约、难回滚迁移或关键架构：分别审阅 requirements.md、design.md、plan.md。跨仓、新接口和新依赖本身不提高风险。

调查、接手、修复、验收从当前版本和已有事实开始，不补造原作者的文档链。普通需求确需独立任务时增加 plan.md，与方案一起审阅，不因此升级风险。

## 创建与写作

使用 `kit.py item create <slug> --repo <repo> --title <title> --summary <summary> --json`；重大需求加 `--document-kind requirements --risk-tier major`。

重大需求首轮只写 Requirements，批准后才写 Design，再批准后才写 Plan。后续模板使用 `item prepare <slug> --role design|plan --state-revision <revision>`；不覆盖正文。Kit 约束生成入口，直接 Markdown 编辑仍须遵循阶段规则。

按 prepare 选定模板写当前规格：Requirements 写范围/非目标、不变量、稳定 R、可观察验收和反例；Design 写稳定 D、关键职责/流程、取舍、失败/兼容/回退与验证。完整指承诺无遗漏，不指复制调查历史、普通字段和实现清单。

验收复用主要入口，其他层只补独有风险；只记影响方案的假设与依据。涉及 fallback、adapter、provider、兼容或删旧时说明处置，其他任务不加治理段。

长字典/样例按需放 references 并引用 D，主设计保留结论。前端交接用 workspace-api-contract。

focused-v1 新项优先更新原章节/文件，不另写阶段总结、接手或日期版；规范细节在 references 受审，交付链接集中登记。legacy 不补模板或搬迁。

自审覆盖、矛盾、验收及测试成本。新增抽象/配置/兼容须说明必要性，不为未来或测试扩展生产能力；保留安全、完整性、兼容和验证。结论就近写，不另加报告；展示后等用户批准，用最新 revision 记录：

```bash
python3 scripts/kit.py item approval <slug> --decision approved --reason "用户已批准实际方案" --state-revision <stateRevision> --json
```

重大需求每次批准一阶段，普通需求一次批准；记录绑定实际版本。

## 修改已批准内容

排版、措辞和文件组织未改变行为时，`item approval --decision unchanged --reason <依据>` 沿用批准；范围、验收、关键方案或验证强度改变，先记录 needs-review，说明受影响 R/D/T，再审阅变化。重大需求实质修改使后置审批失效。用 --affected-task 指定影响任务；独立新增范围且不影响旧任务用 --no-affected-tasks 并附依据。未知则保守整体失效；未变任务保留原证据，不重登“沿用”副本。

普通方案批准后执行；开启新一轮先读 references/item-iterations.md。

## 调整工作项

创建后需要调整标题、活动、风险、文档模式或仓库分支时，使用 `item update <slug> --input changes.json --reason <理由> --state-revision <stateRevision>`；先用可选 `--preview` 查看受影响审批与任务。仅使用允许字段，不手改 state.json。重大风险使用 requirements 文档模式；已有正文不会自动删除或改写。

工作项取消使用 `item cancel` 并记录原因，不能把未验收工作标为完成。取消后重新启动走 next-iteration，保留原结论和任务编号高水位。
