---
name: workspace-api-contract
description: Create or update API integration notes for workspace features that change front-end/back-end contracts, request or response fields, auth, errors, or API-dependent UI behavior.
---

# Workspace API Contract

用于标准需求中涉及 API、前后端契约或联调风险的变更。通常在 `workspace-cross-repo-analysis` 识别提供方/消费方之后，由 `workspace-feature-design` 管理需求目录时使用。

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

已有 OpenAPI/Swagger/接口文档是事实来源；主设计保留本次接口的可审阅摘要，附件只记录本次变更的完整字段、样例和联调细节。

## 测试期文档

开发自测、正式提测或修复联调问题时，默认在 `testing/verification.md` 记录联调证据；只有联调记录需要独立维护时才创建 `testing/api-integration.md`。内容包含：

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
