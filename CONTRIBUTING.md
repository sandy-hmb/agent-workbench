# 贡献指南

公共 Kit 的改动只涉及被跟踪的 Core Skill、脚本、Schema、模板、文档和测试。不要提交 `.workspace/`、`workspace-input.json`、`new-repo.json`、本地 Extension、`local-*` Adapter 或 IDE 缓存。

## 准备改动

使用 Python 3.10+、Git 2.23+ 和项目自带的标准库实现。新任务先运行：

```bash
python3 scripts/workspace_status.py --root . --json
```

公共工作流行为变化属于标准需求：在 `docs/development/features/<feature-slug>/` 逐阶段记录。先确认需求再创建需求文件，确认方案后创建设计，确认计划与验证策略后创建实施计划并编码；验证记录只在实际执行后创建。局部、可定向验证的维护可以走轻量流程。

默认采用 TDD，但以可观察行为和关键不变量为单位。行为变化先写最小失败测试；声明式元数据、配置、文档、生成物、已有测试完整保护的纯重构或已有 Schema、Migration、契约检查完整覆盖的结构变化，可以采用最小有效验证并记录理由。不要为了字段、类或代码行数机械增加测试文件。

不要为了测试公共模板而把使用者状态放到根目录。需要工作区 fixture 时，在临时目录创建 `.workspace/`，并验证它保持 Git 忽略。

## 验证改动

提交前在 Kit 根目录运行完整离线检查：

```bash
python3 -m unittest discover -s tests -p 'test_*.py'
python3 -m py_compile scripts/*.py migrations/*.py
for schema in schemas/*.json; do python3 -m json.tool "$schema" >/dev/null; done
python3 -m json.tool upgrades/manifest.json
python3 -m json.tool workflows/feature-development.json
python3 scripts/workspace_doctor.py --root .
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

- 面向使用者的行为变化措辞——说清"发生了什么、使用者要不要做什么"，不出现内部错误码常量（如 `MULTIPLE_ACTIVE_FEATURES`）、脚本或函数名（如 `feature_context.py`）、`docs/development/` 内部路径。
- 破坏性变更或需要使用者手动执行的升级动作单列"破坏性变更与升级动作"小节；其余条目保持扁平列表。
