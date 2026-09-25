# 开始和接手一个工作项

小改直接处理并给出定向验证结果。普通需求用 change.md 一次审阅目标、方案、工作项和验收；重大需求使用 requirements.md、design.md、plan.md 分阶段审阅。跨仓本身不提高风险。

创建普通需求：

```bash
python3 scripts/kit.py item create payment-retry --repo service --title "支付重试" --summary "处理瞬时失败" --json
python3 scripts/kit.py brief payment-retry --json
```

重大需求创建时增加 `--document-kind requirements --risk-tier major`。编辑实际内容、检查覆盖和关键取舍后，请用户审阅。批准后记录对应版本：

```bash
python3 scripts/kit.py item approval payment-retry --decision approved --reason "用户批准实际内容" --state-revision <stateRevision> --json
```

重大需求用 `--role requirements`、`--role design`、`--role plan` 分别审阅。普通需求存在真实依赖时也可增加 plan.md，与方案一次批准。任务使用 `### T01 标题`，完整字段见计划模板；同 WorkItem 内编号持续递增。

## 实施与验证

普通需求连续实施，收尾做一次整体验证。有独立任务时用 `brief <slug> --task T01`，只读任务、依赖和适用规则；完成一个后继续下一个。

行为变化先验证缺失行为，再最小实现并验证。声明式变化使用最小有效检查。执行真实检查后取得代码状态：

```bash
python3 scripts/kit.py verify snapshot payment-retry --json
python3 scripts/kit.py verify record payment-retry --input evidence.json --state-revision <stateRevision> --json
```

逐任务 snapshot 加 `--task T01`；record 输入使用 scope=task。完整格式见[证据输入](../../.agents/skills/workspace-verify/references/evidence.md)。状态与摘要自动更新，不勾选计划、不手工改 README。摘要生成失败只运行 `verify render`。

记录通过不等于当前代码有效；接手时按需 `brief <slug> --check-code`。代码不匹配时重新验证，不能把旧结果当作当前通过。

## 修改与完成

已批准内容发生非实质修改时用 `item approval --decision unchanged --reason <原因>` 沿用批准。范围、验收或关键方案变化时记录 needs-review，并审阅实际变更；受影响任务不能沿用原完成证据。

通过 `item delivery` 记录实际版本、提测、部署和外部验收。未关闭待办需保留负责人和完成条件；关闭必须附证据或范围调整理由。没有部署事实显示未确认。

本次验收完成、用户确认结束后：

```bash
python3 scripts/kit.py item complete payment-retry --state-revision <stateRevision> --json
```

## 下一迭代

已完成 WorkItem 的新增范围使用 `item next-iteration <slug> --state-revision <stateRevision>`。脚本归档文档和状态快照，当前需求设计维护完整规格，计划只保留本轮任务。任务编号接着上一轮递增，旧证据不能解锁新任务。

历史只在追溯时读取。公共升级不转换、删除或覆盖旧版工作区；新版在新目录初始化。

## 调整、阻塞与取消

工作项结构事实通过 `item update` 维护。输入仅允许 title、summary、activity、risk、documentKind、bindings；绑定包含 repository、workBranch、baseBranch。每次提供当前 stateRevision 和调整理由，`--preview` 只显示变化与影响，不修改状态。

```bash
python3 scripts/kit.py item update payment-retry --input changes.json --reason "增加前端接入范围" --state-revision <stateRevision> --preview --json
python3 scripts/kit.py item update payment-retry --input changes.json --reason "增加前端接入范围" --state-revision <stateRevision> --json
python3 scripts/kit.py item block payment-retry --task T01 --reason "等待测试环境" --owner "测试负责人" --condition "环境恢复并可访问" --state-revision <stateRevision> --json
python3 scripts/kit.py item unblock payment-retry --blocker B01 --reason "环境已恢复" --state-revision <stateRevision> --json
python3 scripts/kit.py item cancel payment-retry --reason "目标已被其他方案替代" --state-revision <stateRevision> --json
```

标题和展示摘要变化不撤销审批；风险、文档模式和仓库绑定变化按实际影响处理。新增仓清除整体验证；修改分支使该仓任务及依赖方的完成证据失效。仍被计划或未关闭事项引用的仓库不能移除。绑定更新不会创建或切换分支。

取消保留任务、证据和未关闭验收，不执行回滚。取消后重新开展使用 next-iteration；resume 只恢复暂停项。工作台显示阻塞和取消原因，新操作由 Agent 通过 CLI 执行。

无变化的 update、delivery 和重复摘要生成不改文件时间或状态版本。新审批、验证及阻塞处理是新事实，正常保存。任务验证只读取目标仓，整体验证检查全部涉及仓。
