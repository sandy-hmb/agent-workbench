# 开始和接手一个工作项

小改直接处理并给出定向验证结果。普通需求用 change.md 一次审阅目标、方案、工作项和验收；重大需求使用 requirements.md、design.md、plan.md 分阶段审阅。跨仓本身不提高风险。

`brief` 和 `status` 默认输出简洁中文文本；自动化、工作台和插件消费这两个命令时显式加 `--json`，继续读取既有 JSON 字段与错误对象。查询保持只读，记录通过、当前代码适用性和生命周期完成分别展示。

Agent 使用 `brief <slug> --task T01` 定向续接。无任务时默认只给外部验收计数和前十项待办，不附长证据；`--full` 展开全部验收依据，Inspect 2 的完整查询保持兼容。`workflowHooks.before/after` 给出当前阶段实际挂载的 Action；为空时不创建 Run 或查询空流程。同会话按路径、版本和作用范围复用未变规则，record 返回的 nextStep/revision 可直接续接，不反复运行全量 status。

创建普通需求：

```bash
python3 scripts/kit.py item create payment-retry --repo service --title "支付重试" --summary "处理瞬时失败" --json
python3 scripts/kit.py brief payment-retry
```

重大需求创建时增加 `--document-kind requirements --risk-tier major`。编辑实际内容、检查覆盖和关键取舍后，请用户审阅。批准后记录对应版本：

```bash
python3 scripts/kit.py item approval payment-retry --decision approved --reason "用户批准实际内容" --state-revision <stateRevision> --json
```

重大需求按 Requirements→Design→Plan 分阶段生成和审阅：创建只生成 `requirements.md`；Requirements 批准后用 `item prepare <slug> --role design --state-revision <stateRevision>` 准备 Design，再批准 Design 后准备 Plan。入口拒绝前置未批准、不会覆盖已有正文；每次审批只接受一个角色，默认当前阶段。前置文档实质修改或记录 needs-review 会使后置审批失效，排版修改可用 `unchanged` 沿用批准。直接编辑 Markdown 仍须遵循阶段规则。普通需求创建时只有 `change.md`，存在真实依赖时增加 `plan.md` 并一次批准。任务使用 `### T01 标题`，完整字段见计划模板；同 WorkItem 内编号持续递增。

创建时可用 `--activity repair`、`investigate`、`takeover` 或 `acceptance`，分别从症状与复现、调查事实、接手风险或交付验收开始。change 模式使用 Kit 自带的对应活动模板；默认 `develop` 和其他合法自定义活动使用通用 `change.md`，requirements 模式始终使用 `requirements.md`。活动名须匹配 `^[a-z][a-z0-9-]*$`。模板只替换标题和摘要，摘要中的花括号按原文保留。活动仅选择初始正文，不改变生命周期、审批和验证门禁，也不重写已有工作项。

## 实施与验证

普通需求连续实施，收尾做一次整体验证。有独立任务时用 `brief <slug> --task T01`，只读任务、依赖和适用规则；完成一个后继续下一个。

计划顶部固定链接到 README 的自动任务进度。不要在 plan.md 勾选或手工同步状态；README、brief 和 Inspect 都从 `state.json` 与不可变证据生成。缺少执行事实时显示 `unknown`（未记录），不会把“没有证据”推断成未开始。跨会话或较长任务可显式记录：

```bash
python3 scripts/kit.py item task-progress payment-retry --task T01 --status in-progress --summary "正在实现重试边界" --state-revision <stateRevision> --json
python3 scripts/kit.py item task-progress payment-retry --task T01 --status implemented --summary "实现已落地，等待验证" --state-revision <stateRevision> --json
```

状态还可显式使用 `not-started`，但 Kit 不会自动推断它。执行记录只说明当前实施事实，不解除审批、依赖和阻塞，也不完成任务。验收另行显示 `unverified`、`passed`、`failed` 或 `needs-revalidation`；只有当前适用的通过证据才计入完成。任务定义、上游依据或仓绑定变化时，旧证据仍可从 history 查询，并显示失效原因与待补验证。

按风险选择 TDD 强度：复杂行为或回归先用一个聚焦失败场景证明缺失；简单改动、声明式变化或已有检查已覆盖时直接做最小有效检查，并说明跳过 RED 的理由。随后最小实现并验证。执行真实检查后取得代码状态：

```bash
python3 scripts/kit.py verify snapshot payment-retry --json
python3 scripts/kit.py verify record payment-retry --input - --state-revision <stateRevision> --json
```

输入默认从 stdin 或临时文件提供，不将输入副本长期留在 artifacts。同一批检查只记录一次：`taskCoverage` 用零起始 checks 索引说明实际覆盖哪些任务。例如 `{"scope":"item","taskCoverage":{"T01":[0],"T02":[0]}}` 关联同仓的一项真实检查，须另附 checks、codeState、recordedAt、result 和整体 reviewResult。不能把未覆盖的任务任意列入映射。

`scope=item` 可原子登记相关任务及整体验证，代码快照包含全部涉及仓；`scope=batch` 仅更新覆盖任务，代码快照只含对应仓。逐任务仍用 `scope=task,taskId=T01` 和 `snapshot --task T01`。不同仓的检查必须分别对应实际 checkout；同批可包含真实依赖，但外部未完成依赖仍阻断。每个任务的验证性质、实际执行/跳过、分支、阻塞、交付路径与代码内容均校验，失败不会完成任务。重复相同输入和原 revision 返回已有回执，中断后状态未提交就不算完成。

JUnit 采集可重复 `verify collect --task T01 --task T02`，在同仓报告确实覆盖两任务时复用一份快照与 checks；仍须人工核对映射，整体验收改为 scope=item 并填写 reviewResult=passed。完整格式见[证据输入](../../.agents/skills/workspace-verify/references/evidence.md)。状态与 README 自动更新，摘要失败只 verify render，不重复执行或登记。原始 Case 报告、必要失败日志保留；不默认另写 results、validation、review、delivery-input 和普通预检成功报告。复杂独立审查、前端指南、SQL 及真实发布/回退说明仍按需交付，旧文件不自动清理。

记录通过不等于当前代码有效；接手时按需 `brief <slug> --check-code`。代码不匹配时重新验证，不能把旧结果当作当前通过。

2.10 起的新证据按完整 Git 输入的实际内容识别代码，另存来源 commit 和 checkout；相同内容暂存或提交后不重登“沿用”证据。真实源码、配置、资源、测试、文件模式或未跟踪输入变化仍失效；忽略文件不在 Git 输入集合内，需要验证的构建输入应纳入版本化或受审阅证据。子模块有未提交输入时保守拒绝核对。旧不可变证据继续按旧指纹规则读取，不重写历史。

## 修改与完成

已批准内容发生非实质修改时用 `item approval --decision unchanged --reason <原因>` 沿用批准。范围、验收或关键方案变化时记录 needs-review，并审阅实际变更；受影响任务不能沿用原完成证据。

实质修改用 `--affected-task T01` 明确影响任务，自动传播到依赖方；独立新增范围且没有旧任务受影响时，使用 `--no-affected-tasks --reason <依据>`。两者互斥；未声明影响则保守整体失效。任务定义真实变化仍会失效，不能用“无影响”绕过；整体范围变化需新整体验证，未变任务保留原证据，不复制旧检查重新登记。

通过 `item delivery --input -` 记录实际版本、提测、部署和外部验收。未关闭待办需保留负责人和完成条件；关闭必须附证据或范围调整理由。没有部署事实显示未确认。已有 Workflow Action 结果可选地以 `evidenceRefs` 引用同一 WorkItem 的 runId、requestId 和 stage；引用不会代替人工确认或自动关闭验收。机器 attempt/receipt 为外部事实，不另造同义报告。

本次验收完成、用户确认结束后：

```bash
python3 scripts/kit.py item complete payment-retry --state-revision <stateRevision> --json
```

complete 内部即时深检，无须先重复 brief --check-code；后者只用于诊断。可信执行者已经在同一内容与模块/profile/参数下完成的检查可核对凭据复用，不因切换 Agent 重跑。漂移、覆盖缺口或可疑结果才重验；目标检查和有证据的历史基线失败分开，不为全仓绿扩大修复范围。

## 下一迭代

已完成 WorkItem 的新增范围使用 `item next-iteration <slug> --state-revision <stateRevision>`。脚本归档文档和状态快照，当前需求设计维护完整规格，计划只保留本轮任务。任务编号接着上一轮递增，旧证据不能解锁新任务。

历史只在追溯时读取。公共升级不转换、删除或覆盖旧版工作区；新版在新目录初始化。

## 调整、阻塞与取消

工作项结构事实通过 `item update` 维护。输入仅允许 title、summary、activity、risk、documentKind、bindings；绑定包含 repository、workBranch、baseBranch，以及可选 checkout。每次提供当前 stateRevision 和调整理由，`--preview` 只显示变化与影响，不修改状态。

`checkout` 是已授权、已存在 worktree 的绝对真实路径，不是任意克隆副本；Kit 核对 Git common-dir、worktree 登记、remote 与工作分支，命令不创建或切换 worktree。通过 item update 登记后，brief、规则发现、snapshot、record、Inspect 和 submit 使用同一实际位置。仅位置改变且内容等价时保留新式证据，分支/范围或内容变化使受影响证据失效。不要绕过门禁在 Registry 主目录提测另一 worktree 的代码。

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
