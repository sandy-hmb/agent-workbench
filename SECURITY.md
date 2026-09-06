# 安全策略

## 支持版本

| 版本 | 安全更新 |
| --- | --- |
| 1.x | 支持 |
| 其他版本 | 不支持 |

## 报告问题

不要在公开讨论、公开提交或公开变更说明中披露漏洞细节。首选入口是仓库页面的 `Security` -> `Private vulnerability reporting`（如果仓库启用了该功能）；如果入口不可用，请联系项目维护者，要求提供一次性私密渠道，不要在公开 Issue 中粘贴细节。私密报告应说明受影响版本、影响、最小复现步骤和建议修复方向。维护者确认前，不要公开利用细节或敏感值。

## 本地状态

- `.workspace/`、初始化输入和本地 Extension 都不得包含密码、令牌、私钥或带凭据地址。
- 远端地址只能使用无凭据 HTTPS、SSH 或 `null`。
- `.workspace/` 被 Git 忽略，不代表它不需要备份或访问控制；删除、迁移或复制前由使用者自行审查。
- 运行迁移或 Extension apply 前先审阅 preview、`previewHash` 和 `applyCommand`。

## Provider 边界

Extension 是本地代码，激活前必须审阅 manifest、Skill、命令、环境变量声明和 `effects`。一个 capability 在当前作用域只允许一个已绑定 Provider；运行时只接受已锁定的 Extension 快照。

Provider 通过受限 JSON 输入输出协议运行。命令路径、输出大小、执行时间和环境变量都会被检查；结果中的已声明敏感值会被脱敏。`effects` 用于声明和审阅预期影响，不是操作系统级沙箱；这些检查不能替代对本地代码的审查。

敏感值只应在调用环境中临时提供，不要写入 workspace、manifest、lock、文档或测试资料。

## Git 和更新

clone、fetch、pull、分支、提交和推送都可能访问外部仓或改变 Git 状态。更新公共 Kit 时，先用 `workspace-update` 检查工作树与 `origin`，确认网络访问和目标提交后才使用 `git pull --ff-only`。快进失败时停止并保留现场，不自动处理冲突。
