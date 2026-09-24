---
name: workspace-api-contract
description: Create or update API integration notes for workspace features that change front-end/back-end contracts, request or response fields, auth, errors, or API-dependent UI behavior.
---

# Workspace API Contract

用于标准需求中涉及 API、前后端契约或联调风险的变更。通常在 `workspace-cross-repo-analysis` 识别提供方/消费方之后，由 `workspace-feature-design` 管理需求目录时使用。

## 文档布局适配

本文件旧 design/ 和 testing/ 路径适用于已有 Feature。新复杂需求主设计为 design.md，独立接口附件为 references/api-contract.md；普通活动优先在 change.md 说明必要契约。文件位置从当前工作项确认，按独立对接需要拆分，不因 API 变化机械新增附件。

## 触发条件

仅当本次需求满足至少一项时使用：

- REST、GraphQL、WebSocket 或其他接口新增、删除或行为变化。
- 请求字段、响应字段、状态码、错误码、分页、排序或过滤规则变化。
- 鉴权、权限、租户、角色或安全校验变化。
- 前端页面、客户端或脚本依赖后端新能力。
- Mock、测试环境、联调环境或兼容发布顺序影响交付。

不涉及接口契约的单仓局部改动，不创建接口联调文档。

## 设计期文档

用户治理模式使用 `.workspace/docs/features/<slug>/`；公共 Kit 维护模式使用 `docs/development/features/<slug>/`。默认在 `design/design.md` 的接口契约章节写入或更新内容；主设计必须保留接口清单、提供/消费方向、主要字段或状态映射、兼容策略和联调依赖，并以 D01-DNN 定义规范决策。只有接口内容需要跨团队独立审阅、维护或按需读取，且用户确认后才创建 `design/api-integration.md` 并从主设计链接。该附件开头链接回 `design.md`、声明扩展的 D 编号，只展开接口细节，不替代主设计的接口结论，也不重复总体方案、数据模型或需求正文。内容按需包含：

- 变更摘要：说明本次接口或契约变化。
- 参与仓库：列出提供方、消费方和只读对照方。
- 契约来源：优先引用 OpenAPI、Swagger、已有接口文档、路由、测试或代码位置。
- 接口清单：提供方、消费方、端点或事件、请求响应字段、鉴权、错误码、状态映射、幂等和版本兼容。
- 联调计划：Mock、真实环境、数据准备、顺序、发布依赖和待确认项。
- 未决问题：按事实、推断、待确认标注，不把推断写成事实。

已有 OpenAPI/Swagger/接口文档是事实来源；主设计保留本次接口的可审阅摘要。附件按 `templates/feature/api-integration.md` 组织为变更概览、契约明细、集成与兼容约束、待确认契约。新增接口写完整契约，已有接口只展开本次变化并链接原定义；未决项集中维护责任方、影响范围和确认条件。内部实现、完整测试清单、SQL 操作步骤引用其权威文档。

## 前端交接文档

存在前端接入或回归影响时，使用 `templates/feature/frontend-integration.md` 生成 `artifacts/frontend-integration.md`；接口简单时只需主设计与前端指南，不强制同时创建接口设计附件。只按“页面改动、接口说明、联调注意事项”组织，保留必要示例、响应包装、字段语义、页面处理、特殊错误和易错规则，不重复后端内部方案及验证日志。

默认在相关实现和本地验证完成后生成；前端需要并行开发时可在设计期生成同一文件，标明“设计稿”。实现后核对真实字段、序列化、响应包装、错误与页面行为，更新适用端、对应版本和接口文档链接，再标为“已按实现核对”。部署可用性单独记录，未确认时写“未确认”；修复后只更新受影响章节，不另建最终版文件。

已纳入本次批准范围的指南直接生成，不重复请求生成许可。纯交接说明、示例或环境信息更新不触发设计重新审阅；发现契约或业务行为变化时先修订对应 R/D。生成或更新后补齐 README 入口、交付状态及已有证据适用的 `artifactRefs`，运行 `brief <slug> --check --json` 核对导航与链接。

历史 `design/frontend-integration.md` 或以 `design/api-integration.md` 命名的前端指南保持可读，按正文确认职责，不自动重命名、改写或迁移。

## 测试期文档

开发自测、正式提测或修复联调问题时，实际证据进入 `testing/evidence/` 并由脚本生成 `testing/verification.md`；旧 v1 保持原记录方式。只有联调记录需要独立维护时才创建 `testing/api-integration.md`，并从证据引用，不作为第二份前端指南。内容包含：

- 验证范围：关联页面、接口、主要场景和反向兼容场景。
- 执行记录：只记录已获授权并实际执行的命令、请求或人工检查。
- 请求样例：保留无密钥、无个人数据的最小样例。
- 结果清单：标注通过、失败、阻塞和待确认项。
- 后续处理：列出仍需前端、后端或测试环境处理的问题。

## 边界

- 默认只读；执行服务启动、接口请求、外部环境访问或业务仓写入前，必须取得本轮明确授权。
- 不自动启动服务、不自动 curl 真实环境、不自动修改业务代码、不提交、不 push。
- 不把令牌、Cookie、密码、私钥或个人数据写入文档。
- 没有正式接口文档时，可以从代码、README、测试和路由中提取最小契约，但必须标注事实、推断和待确认。
