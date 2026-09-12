# {title}

- 状态：planning
- 涉及仓库：{repositories}
- 工作分支：{branches}
- 基线分支：{base_branches}
- 需求审阅：待审阅
- 设计审阅：未生成
- 计划审阅：未生成
- 当前迭代：i01
- 执行方式：单 Agent
- Workflow Run：未创建
- 最后更新：{updated}

## 当前进度

需求草案完成后等待实际文件审阅，批准后无新阻塞时在同一轮进入下一阶段。设计、实施计划和验证记录按阶段创建；`design/design.md` 与全部设计附件是一个 Design 审阅包。新建、删除或实质修改附件后，将设计审阅和已有计划审阅改为“待审阅”；实际验证产生后再记录证据。

## 本轮变更

首次开发写明初始目标；后续迭代简述新增、修改、移除和沿用内容。无内容的分类删除。

## 文档

- [需求](requirements/requirements.md)

范围、验收、非目标和来源见[需求](requirements/requirements.md)。需求绑定的 SQL、DDL、DML、fixture 和其他交付物按需放在 `artifacts/`；SQL 使用 `artifacts/sql/`。文件生成后在此补充入口链接。
