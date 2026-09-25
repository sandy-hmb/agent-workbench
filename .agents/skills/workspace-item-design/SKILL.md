---
name: workspace-item-design
description: Classify a change and prepare concrete requirements and design for risk-based review.
---

# 需求与设计

先查事实，再提出影响业务结果的关键问题。明确目标、验收、非目标及约束；无需重新询问已确定事项。

## 选择记录强度

小改明确、局部、可逆：直接实施和定向检查，无须创建 WorkItem。普通需求：`change.md` 记录目标、必要方案、工作项和验收，一次审阅。重大需求涉及资金、权限语义、破坏性契约、难回滚迁移或关键架构：分别审阅 requirements.md、design.md、plan.md。跨仓、新接口和新依赖本身不提高风险。

调查、接手、修复、验收从当前版本和已有事实开始，不补造原作者的文档链。普通需求确需独立任务时增加 plan.md，与方案一起审阅，不因此升级风险。

## 创建与写作

使用 `kit.py item create <slug> --repo <repo> --title <title> --summary <summary> --json`；复杂需求增加 `--document-kind requirements --risk-tier major`。维护模式同样使用此命令。不会建分支或初始化业务工作区。

按对应模板写内容：Requirements 维护完整业务规格、稳定 R 编号、角色条件、触发、可观察结果和反例；Design 维护完整技术方案、稳定 D 决策、选择理由、复用与新增职责、数据和接口、兼容回退及验证。

关键验收选择能证明要求的观察入口。例如隐私约束检查接口响应，页面不显示仅证明展示行为。只记录会改变方案的假设，写明依据、验证方法及失败影响。可以通过仓库核实的事实自行调查。

主设计保留完整方案与关键结论；字段字典、请求样例等较长细节按需放 references/ 并引用 D。在批准范围内自行选择文件组织，不为拆附件额外确认。前端交接用 workspace-api-contract。

写完检查需求覆盖、矛盾、关键取舍、引用和可验收性；复杂表格或图按需检查预览。展示实际内容，等用户批准。用户批准已有明确审阅对象后，使用最新 brief 的 revision 记录：

```bash
python3 scripts/kit.py item approval <slug> --decision approved --reason "用户已批准实际方案" --state-revision <stateRevision> --json
```

重大需求每阶段用 `--role requirements`、`--role design`、`--role plan` 定向记录。普通需求一次审阅全部实际内容。脚本记录批准对应的版本，不代替用户决策。

## 修改已批准内容

发现版本变化先分类。排版、措辞和文件组织未改变行为时，`item approval --decision unchanged --reason <依据>` 沿用批准；范围、验收、关键方案或验证强度改变，先记录 needs-review，说明受影响 R/D/T，再审阅实际变化。使用 `--affected-task` 指定影响任务；无法精确确定时按整个当前范围处理。

复杂需求设计批准后生成计划草案；普通方案批准后直接执行。开启已结束 WorkItem 的新一轮时，先读取 references/item-iterations.md。

## 调整工作项

创建后需要调整标题、活动、风险、文档模式或仓库分支时，使用 `item update <slug> --input changes.json --reason <理由> --state-revision <stateRevision>`；先用可选 `--preview` 查看受影响审批与任务。仅使用允许字段，不手改 state.json。重大风险使用 requirements 文档模式；已有正文不会自动删除或改写。

工作项取消使用 `item cancel` 并记录原因，不能把未验收工作标为完成。取消后重新启动走 next-iteration，保留原结论和任务编号高水位。
