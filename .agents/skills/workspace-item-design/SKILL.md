---
name: workspace-item-design
description: Classify a change and prepare concrete requirements and design for risk-based review.
---

# 需求与设计

先查事实，再提出影响业务结果的关键问题。明确目标、验收、非目标及约束；无需重新询问已确定事项。目标不清或范围可能扩张时，回复中临时列出 Goal、Success Evidence、Stop Condition、Non-goals；小改不落文件。

## 选择记录强度

小改明确、局部、可逆：直接实施和定向检查，无须创建 WorkItem。普通需求：`change.md` 记录目标、必要方案、工作项和验收，一次审阅。重大需求涉及资金、权限语义、破坏性契约、难回滚迁移或关键架构：分别审阅 requirements.md、design.md、plan.md。跨仓、新接口和新依赖本身不提高风险。

调查、接手、修复、验收从当前版本和已有事实开始，不补造原作者的文档链。普通需求确需独立任务时增加 plan.md，与方案一起审阅，不因此升级风险。

## 创建与写作

使用 `kit.py item create <slug> --repo <repo> --title <title> --summary <summary> --json`；重大需求加 `--document-kind requirements --risk-tier major`。

重大需求首轮只写 Requirements，批准后才写 Design，再批准后才写 Plan。后续模板使用 `item prepare <slug> --role design|plan --state-revision <revision>`；不覆盖正文。Kit 约束生成入口，直接 Markdown 编辑仍须遵循阶段规则。

按对应模板写内容：Requirements 维护完整业务规格、稳定 R 编号、角色条件、触发、可观察结果和反例；Design 维护完整技术方案、稳定 D 决策、选择理由、复用与新增职责、数据和接口、兼容回退及验证。

验收选择能证明要求的观察入口；只记录会改变方案的假设及依据、验证和失败影响。涉及 fallback、adapter、provider、兼容分支或旧路径删除时说明处置，其他任务不加治理段落。

主设计保留完整方案与关键结论；字段字典、请求样例等较长细节按需放 references/ 并引用 D。在批准范围内自行选择文件组织，不为拆附件额外确认。前端交接用 workspace-api-contract。

自审需求覆盖、矛盾、引用、验收和过度设计。新增抽象、配置、依赖或兼容分支，须说明当前需要及更简单方案的不足，不为假设的未来预留。不削减安全、数据完整性、已承诺兼容或必要验证。结论就近写，不另加报告或审批。复杂图表按需预览；展示内容等用户批准，再用最新 brief revision 记录：

```bash
python3 scripts/kit.py item approval <slug> --decision approved --reason "用户已批准实际方案" --state-revision <stateRevision> --json
```

重大需求每次只批准一个角色，默认当前阶段；普通需求一次批准方案包。批准记录绑定实际审阅版本。

## 修改已批准内容

排版、措辞和文件组织未改变行为时，`item approval --decision unchanged --reason <依据>` 沿用批准；范围、验收、关键方案或验证强度改变，先记录 needs-review，说明受影响 R/D/T，再审阅变化。重大需求实质修改使后置审批失效。用 `--affected-task` 指定影响任务，无法确定时按当前整体范围处理。

普通方案批准后执行；开启新一轮先读 references/item-iterations.md。

## 调整工作项

创建后需要调整标题、活动、风险、文档模式或仓库分支时，使用 `item update <slug> --input changes.json --reason <理由> --state-revision <stateRevision>`；先用可选 `--preview` 查看受影响审批与任务。仅使用允许字段，不手改 state.json。重大风险使用 requirements 文档模式；已有正文不会自动删除或改写。

工作项取消使用 `item cancel` 并记录原因，不能把未验收工作标为完成。取消后重新启动走 next-iteration，保留原结论和任务编号高水位。
