# 贡献指南

公共 Kit 的改动只涉及被跟踪的 Core Skill、脚本、Schema、模板、文档和测试。不要提交 `.workspace/`、`workspace-input.json`、`new-repo.json`、本地 Extension、`local-*` Adapter 或 IDE 缓存。

## 准备改动

使用 Python 3.10+、Git 2.23+ 和项目自带的标准库实现。新任务先运行：

```bash
python3 scripts/kit.py status --root . --json
```

公共工作流行为变化在 `docs/development/items/<item-slug>/` 记录。按实际风险选择普通需求一次审阅方案包或重大需求分阶段审阅；已有审批不因开始实施而重复。新复杂需求使用根目录文档，旧本地记录保持原状且不加载；新计划使用唯一新版状态与证据格式，验证记录在实际执行后创建。局部、可定向验证的维护可以走轻量流程。

按行为与风险选最小有效验证，先复用已有覆盖，不按文件、类、方法、分层或覆盖率机械配测试。每条规则选主要验证入口，其他层只补独有风险；已有覆盖的重构不必新增测试。TDD/RED 是可选方法，不是完成门槛，也不要求解释跳过 RED；可复现 Bug 优先用聚焦失败用例固定回归，复杂规则可先测后写，新功能可先实现再补必要验证。纯文档、元数据和机械调整使用适用的静态检查或必要编译。

测试设施与方案一起检查过度设计：复用已有 fixture 和入口，不为测试扩展生产 API、保留废弃重载或搭建新框架。数据库按真实 SQL、类型、约束、事务、并发和迁移风险验证，不默认启动本地数据库；不能以 Mock 或其他引擎冒充目标数据库通过。具体选择见[验证策略](.agents/skills/workspace-verify/references/strategy.md)。

不要为了测试公共模板而把使用者状态放到根目录。需要工作区 fixture 时，在临时目录创建 `.workspace/`，并验证它保持 Git 忽略。

## 验证改动

提交前在 Kit 根目录运行完整离线检查：

```bash
python3 -m unittest discover -s tests -p 'test_*.py'
python3 -m compileall -q workbench scripts
for schema in schemas/*.json; do python3 -m json.tool "$schema" >/dev/null; done
python3 -m json.tool upgrades/manifest.json
python3 -m json.tool workflows/item-development.json
python3 scripts/kit.py doctor --root .
git diff --check
```

这些检查不安装依赖、不访问业务系统。doctor 在未初始化的公共 Kit 中应报告一条 `WORKSPACE_UNINITIALIZED` 信息，但不应报告错误。

## 可选：推送前自动检查

不想每次手动执行上面的清单，可以让 Git 在 `push` 前自动运行同一套检查：

```bash
git config core.hooksPath scripts/hooks
```

这是一次性的本地设置，只影响当前 clone；不执行这条命令时，`push` 行为完全不变。设置后，`scripts/hooks/pre-push` 会在每次 `git push` 前运行与上面完全一致的检查，任意一步失败都会阻止本次推送，且不修改任何文件——这不是自动修复，只是提前拦截。确有必要临时跳过时可用 `git push --no-verify`，但这会跳过本地这一层防线，需要清楚自己在做什么。若托管方连接了 CI（例如仓内已有的 `.github/workflows/governance.yml`），二者运行同一组检查，本地 hook 只是提前发现问题，不假设、也不依赖任何特定托管平台或协作流程。

## 审阅范围

保持单一、可审阅的改动范围。检查 diff 不包含敏感值或使用者本地状态；文档必须说明用户动作、授权点和失败时的停止条件。新增或修改 Core Skill 时，同时更新相对客户端适配、doctor 的 Skill 白名单和 surface 测试。

## 发布

发布是手工三步，不做自动化、不引入发布工具链：

1. 改 `VERSION` 为新的 semver 字符串；同步改 `upgrades/manifest.json` 的 `kitVersion`。
2. `CHANGELOG.md` 把当前"未发布"标题改为 `## <VERSION> - <日期>`，标题上方新建空的"未发布"承接后续改动。
3. `git tag -a v<VERSION> -m "<版本标题>"`（本地打；是否推送到远端由维护者自行决定，Kit 不假设任何托管方式）。

CHANGELOG 条目规范：

- 面向使用者的行为变化措辞——说清"发生了什么、使用者要不要做什么"，不出现内部错误码常量（如 `MULTIPLE_ACTIVE_ITEMS`）、脚本或函数名（如 `item_context.py`）、`docs/development/` 内部路径。
- 破坏性变更或需要使用者手动执行的升级动作单列"破坏性变更与升级动作"小节；其余条目保持扁平列表。
