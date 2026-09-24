---
name: workspace-verify
description: Run authorized repository validation for the current workspace feature and record implementation progress and verification evidence for handoff or later continuation.
---

# Workspace Verify

用于实现完成后、自测前后或切换窗口前，保留可复核的开发进度和验证结果。

## 轻量路径

没有 feature 目录的轻量改动，从本轮已选仓和任务取得范围，读取其验证入口并执行已授权的本地检查。在当前回复给出工作目录、命令、退出状态和结果；不调用 feature resolve、不写 `testing/verification.md`、不更新需求状态。未声明命令、外部环境或范围变化仍先核对实际影响。

## 文档位置

新需求摘要为根目录 `verification.md`，旧记录沿已有 `testing/verification.md`；实际位置由 Kit 返回。机器证据仍在 `testing/evidence/`。普通活动没有独立计划时按 change.md 的实际工作项与验收验证，使用 `verify finish` 记录 verificationBatch，不要求生成计划或逐任务证据。新计划使用 plan.md，旧 plans/implementation.md 保留原位置。

## 流程

1. 在治理根运行 `python3 scripts/workspace_status.py --root . --json`，按用户本轮明确指定、有效活跃指针、唯一未完成需求的顺序定位当前需求及其仓库、工作分支、基线、计划进度和验证记录。无法确定时停止询问，不猜测需求。
2. 用户治理模式从 `.workspace/docs/features/<slug>/` 读取当前需求；公共 Kit 维护模式从 `docs/development/features/<slug>/` 读取当前需求。默认只读取 README、验收标准、未完成计划项和最近验证摘要；需要追溯时才定向读取设计或历史验证正文。用户模式再读取 `.workspace/docs/repositories/<repo>.md`、仓内 `AGENTS.md` 或登记的 `sourceInstruction`。不要加载无关历史需求。
3. 新计划必须先确认 `trustedProgress` 已全部可信完成；任务证据缺失、零测试、跳过、交付路径漂移或验证性质不足时返回实现阶段。旧计划保持原复选框语义。
4. 从仓 profile 的 `validation`、仓内规范、项目 manifest、CI 或现有测试入口核实验证命令。已获授权的同范围离线验证直接执行，不因缺少预登记重复询问；涉及新增外部环境、部署或真实接口时核对授权。不能执行文档中任意未核实的代码块。
5. 执行获批命令并复核执行 Skill 的整体审查结论。有前端接入或回归影响且相关实现与本地验证通过时，先按 `workspace-api-contract` 生成或核对同一份前端指南、更新适用版本并登记 README。已有设计稿须对照字段、包装和页面行为校准，部署未确认时单独说明。所有检查及必要交付核对结束后运行 `python3 scripts/kit.py verify snapshot <slug> --root . --json`，取得当前需求涉及仓库的代码状态。v2 计划用 `python3 scripts/kit.py verify record <slug> --input <json-file> --json` 写入批次，再生成 `testing/verification.md`；v1 才追加旧 Markdown 批次。无论成功、失败或阻塞，都记录一个批次，不把计划中的预期当作实际证据：

   ```json
   {
     "kind": "verificationBatch",
     "recordedAt": "YYYY-MM-DDTHH:MM:SS+08:00",
     "overallResult": "passed",
     "reviewResult": "passed",
     "verificationScope": "后端本地离线验证",
     "pendingExternalChecks": ["R3：测试负责人在测试环境核对实际计费与审计记录后关闭"],
     "codeState": {"service":"sha256:<digest>"},
     "checks": [{"workingDirectory":"<path>","command":"<command>","exitStatus":0,"result":"<actual result>"}],
     "blockers": [],
     "artifactRefs": []
   }
   ```

   只有总体结果和审查结论均为“通过”、至少一项检查存在、全部退出状态为 `0` 且代码状态仍与当前仓匹配时，status 才会把批次视为通过。实际 SQL 交付应演练实际文件及适用的重复、增量、核对和回退路径；纯映射函数测试不能替代。目标测试未执行时，即使退出状态为 `0` 也不得记为通过。失败或阻塞也记录实际结论和检查结果，但不得伪装为通过。旧 `## 执行记录 YYYY-MM-DD` 保留可读，不能作为新完成证据。

   `verificationScope` 是本批次实际验证范围；`pendingExternalChecks` 是当前完整待外部验证清单，逐项写清相关 R、事项、负责方和完成条件。写入前核对上一批次与计划，不能因本次没运行就遗漏旧待办；关闭事项须有实际证据或明确的范围调整依据。无待办时明确写 `[]`。两个字段对旧证据可选，缺失显示“未记录”，不会推断外部验收通过，也不改变 `verificationPassed` 或 `trustedProgress` 的原有判定。

   `blockers` 记录本批次问题，摘要同时显示任务失败和文档诊断。交付引用复用 `artifactRefs` 的 `path/sha256/bytes/type`，路径相对 Feature；文件存在或被引用不代表已经验证或交付。旧 v1 Markdown 批次的 `## 验证批次`、`覆盖验收` 与 `执行情况` 字段继续只读兼容，不为新字段改写历史证据。
6. 仅对已有证据支持的工作在 `plans/implementation.md` 勾选完成；失败或未执行的步骤保持未完成。检查证据是否覆盖本次行为和风险，不以新增测试数量或覆盖率数字判断完成。更新需求 README 的最后更新日期。
7. 补齐验证摘要与交付说明的 README 入口，运行 `brief <slug> --check --json` 检查已有文档入口与链接，处理本次新增的导航遗漏；不预建无用附件或手工编辑生成的验证摘要。导航或交付入口有变化时重建摘要，保证引用反映当前结果。
8. 再运行 status，向用户汇报剩余步骤、失败项和 doctor 摘要。需要另行授权的数据库、Provider、部署、生产迁移或联调写入“待外部验证”，不伪装为已通过，也不回退已完成的本地任务。提测与部署事实仅在 README 交付状态记录，摘要链接该入口，不从验证通过推断已部署。`testing` 由成功提测动作更新；`done` 仍需业务完成确认。

## 边界

- 本 Skill 只执行用户批准的验证并写当前需求记录，不修改实现代码。
- 不自动启动外部环境、发布、部署、提交测试分支或访问真实接口；这些动作需要对应流程和本轮授权。
- 不 commit、不 push、不合并、不解决冲突，也不操作未指定仓库。
- 不在验证记录中写入令牌、Cookie、密码、私钥或个人数据。
