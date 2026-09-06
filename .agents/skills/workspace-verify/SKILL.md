---
name: workspace-verify
description: Run authorized repository validation for the current workspace feature and record implementation progress and verification evidence for handoff or later continuation.
---

# Workspace Verify

用于实现完成后、自测前后或切换窗口前，保留可复核的开发进度和验证结果。

## 轻量路径

没有 feature 目录的轻量改动，从本轮已选仓和任务取得范围，读取其验证入口并执行已授权的本地检查。在当前回复给出工作目录、命令、退出状态和结果；不调用 feature resolve、不写 `testing/verification.md`、不更新需求状态。未声明命令、外部环境或范围变化仍先核对实际影响。

## 流程

1. 在治理根运行 `python3 scripts/workspace_status.py --root . --json`，按用户本轮明确指定、有效活跃指针、唯一未完成需求的顺序定位当前需求及其仓库、工作分支、基线、计划进度和验证记录。无法确定时停止询问，不猜测需求。
2. 用户治理模式从 `.workspace/docs/features/<slug>/` 读取当前需求；公共 Kit 维护模式从 `docs/development/features/<slug>/` 读取当前需求。默认只读取 README、验收标准、未完成计划项和最近验证摘要；需要追溯时才定向读取设计或历史验证正文。用户模式再读取 `.workspace/docs/repositories/<repo>.md`、仓内 `AGENTS.md` 或登记的 `sourceInstruction`。不要加载无关历史需求。
3. 只采用仓 profile 的 `validation` 或仓内规范明确声明的验证命令。已获授权的同范围离线验证直接执行；外部环境、部署、真实接口和未声明命令仍需单独确认。没有声明时先询问，不自行发明命令。
4. 执行获批命令。无论成功、失败或阻塞，都在 `testing/verification.md` 记录日期、工作目录、精确命令、退出状态和关键结果，不把计划中的预期当作实际证据。新记录使用下列可识别格式：

   ```markdown
   ## 执行记录 YYYY-MM-DD
   - 工作目录：`<path>`
   - 命令：`<command>`
   - 退出状态：<code>
   - 结果：<actual result>
   ```
5. 仅对已有证据支持的工作在 `plans/implementation.md` 勾选完成；失败或未执行的步骤保持未完成。检查证据是否覆盖本次行为和风险，不以新增测试数量或覆盖率数字判断完成。更新需求 README 的最后更新日期。
6. 再运行 status，向用户汇报剩余步骤、失败项和 doctor 摘要。`testing` 由成功提测动作更新；`done` 仍需业务完成确认。

## 边界

- 本 Skill 只执行用户批准的验证并写当前需求记录，不修改实现代码。
- 不自动启动外部环境、发布、部署、提交测试分支或访问真实接口；这些动作需要对应流程和本轮授权。
- 不 commit、不 push、不合并、不解决冲突，也不操作未指定仓库。
- 不在验证记录中写入令牌、Cookie、密码、私钥或个人数据。
