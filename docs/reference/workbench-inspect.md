# Workbench Inspect 只读查询

Inspect 为本地工作台提供现有工作区、工作项、文档、验证与流程记录。普通活动和复杂需求使用同一只读查询边界；没有复杂计划的小改动不会被当作损坏记录。它按需运行一个 Python 进程，不启动服务、不调用 Agent，不执行 Provider、Action、文档命令或 Git 写操作。现有 `status` / `brief` 调用保持原入口；使用插件不要求 Agent 额外维护展示字段。

```bash
python3 -B scripts/kit.py inspect --root /path/to/kit --api-major 1 --json workspace
```

`--root` 是含 `.workspace/` 的 Kit 根，不是所有仓库的父目录。没有工作区配置时显示维护模式；存在但损坏的配置报错。业务仓按配置登记集合返回，当前 Kit 单独标为 `role: kit`。兄弟目录解析基于该根的父目录，查询不会自动登记或创建目录。

## 操作

| operation | 参数 | data 主要内容 |
|---|---|---|
| `workspace` | 无 | 模式、工作区名称、Kit/业务仓、有效分支策略与逐字段来源、本机上下文、安全配置摘要、协议能力 |
| `features` | `--status`、`--offset`、`--limit` | 全生命周期 Feature 概况、任务计数、审阅与记录概况、分页和坏记录计数 |
| `feature <slug>` | 无 | legacy 完整详情：summary、tasks、files/artifacts、progression、featureRevision |
| `projection <slug>` | `--view summary\|task\|change\|handoff\|flow`，task 可带 `--task <id>` | 只读指定工作项与当前视图；summary 为入口，task 包含任务和依赖，change 返回比较对象声明，handoff 返回接手摘要，flow 返回交付事实；不因切换视图展开其他正文 |
| `document <slug>` | `--path <relative>`、可选 `--revision <sha256>` | 原始 UTF-8 content、bytes、lineCount、mediaType、revision |
| `verification <slug>` | 可选 `--check-code` | 批次定位、最新批次及检查、每仓代码状态、记录完整性与当前适用性 |
| `handoff <slug>` | 可选 `--check-code` | 即时生成的接手正文、当前任务、直接依赖、阶段、最近验证和带版本来源 |
| `search` | `--query`，可选 `--repo`、`--status`、`--offset`、`--limit` | 当前工作区 Feature 文档的匹配片段、原文位置和分页 |
| `workflow` | 无 | Core/Overlay 的解析顺序、当前声明、扩展与绑定摘要 |
| `runs` | 可选 `--feature`、`--offset`、`--limit` | 独立 Run 列表、记录数量与分页 |
| `run <id>` | 无 | 已校验 rawRecord、逐步骤记录及独立 configurationMatch |

列表的 `data` 为 `{items, counts, page}`。Feature/Run 默认每页 100 项、最多 200 项；搜索默认 20 条、最多 50 条。`page` 包含 `offset, limit, total, hasMore`。完整筛选集合的 revision 在分页间稳定；读取期间发生变化，消费方应丢弃已合并页并有界重试。不能用一页数据推断整个工作区没有其他需求。

## 响应与兼容

每个响应有 `apiVersion, operation, status, observedAt, root, revision, data, diagnostics`。API 当前为 `{major: 1, minor: 2}`，`--api-major` 默认 1，消费方应显式指定。未知 major 返回错误；新 minor 的可选字段可以忽略，未知枚举不得当作成功。参数无法识别 operation 时错误信封的 operation 为 null。

- `status: ok`：读取成功，退出 0。
- `status: partial`：保留成功记录并单列诊断，退出 0；不代表工作流成功。
- `status: error`：读取失败，data/revision 为 null；一般退出 1，参数或版本错误退出 2。
- `observedAt` 是响应生成时间，不能作为 Git Fetch 时间；失败响应的时间不能覆盖 UI 的上次成功时间。
- `revision` 是 SHA-256；不同 operation 的 revision 不直接比较。任务定位使用其文档 path/revision/1-based 行号，验证和 Feature 页使用 featureRevision 核对输入版本。
- `diagnostics` 含 code、severity、message、scope、subject、path、line、retryable；位置不适用为 null。

消费者同时校验进程退出码、信封、operation、canonical root 和 data 类型。协议 Schema 使用本仓支持的 JSON Schema 子集：先校验整个信封，再使用同名 `#/$defs/<operation>` 校验非 error 的 data；这一步不能省略。

## 显示语义

任务的 `trustedProgress` / `trusted` 区分“已打勾”和“有有效完成证据”：最新任务证据须通过交付核对，检查类型满足验证性质，测试实际执行且没有跳过，交付路径也须成立。浏览时，已检出 Feature 工作分支的仓检查当前工作目录；未检出时只读检查 README 记录的本地工作分支已提交树，不把其他 Feature 的现场混入。分支缺失或无法读取时给出诊断，不自动切分支、Fetch 或替换成主分支；Feature 的 artifacts 仍检查工作区文件。例如浏览 A 时检出 B，不会仅因 B 没有 A 的文件而将 A 判为可信 0。此处的可信不证明当前代码仍通过测试，代码指纹有效性由验证查询单独核对；执行用的 `status` / `brief` 仍检查实际工作目录。

验证记录与代码核对是不同事实。默认查询只读记录，每仓为 not_checked；显式 `--check-code` 才计算代码指纹。`selectedBatch.recordedResult` 是 passed/failed/unknown，completeness 是 complete/incomplete/legacy/missing。applicability 使用 valid/invalid/unknown/not_checked/historical；完成需求属于历史记录。每仓可以同时出现 matched、changed 和 unknown，不能用整体失败替代逐仓结果。无耗时或测试数量就返回 null，不根据命令推断。

Run 的 rawRecord 保留兼容快照；records 叠加最新命令尝试，attempts 返回权威结果及核对来源。结果未知在兼容 records 中投影为 failed，并在 attempts 中明确为 unknown；running 结合当前执行者锁判断，旧记录不能证明进程存活。`configurationMatch` 的 matched/changed/unknown/removed 只说明当前步骤配置和记录的关系，不推断代码有效性，也不还原不存在的历史配置内容。rawRecord 仅返回已校验的字段，扩展参数与环境变量不通过该入口补充。

文档限于所选需求的标准文档、明确链接的安全附件和 artifacts 文本。绝对路径、父级跳转、符号链接、设备文件、非法 UTF-8、超限文件均不能作为成功正文。`INSPECT_REVISION_CHANGED` 表示原定位版本已变化，消费方应重读详情与文档再定位。原始 Markdown/HTML 是展示数据，消费方还需转义原始 HTML、禁自动加载图片及外部资源。

接手包只汇总已有 Feature 文件和当前适用规则，不创建另一份持久状态，也不补造对话中的临时结论。搜索覆盖标准 Markdown 与 `design/*.md`，关键词按空白拆分、英文忽略大小写，同一文档需包含全部关键词；结果按标题、章节和正文命中排序，再按 Feature 更新时间排列。搜索不读取业务源码、缓存或二进制文件。

## 读取边界与错误

普通请求预算 10 秒，代码核对 30 秒；消费方另留进程清理余量。单个文本/JSON 文件最多 1 MiB，响应最多 8 MiB，扫描目录项最多 10,000，显式指纹核对最多读取 64 MiB 与 10,000 个未跟踪文件。超限不能返回截断正文或半截指纹。操作是否成功以实际响应为准，不能用耗时/异常后空列表代替诊断。

常用错误码包括 `INSPECT_INVALID_ARGUMENT`、`INSPECT_UNSUPPORTED_VERSION`、`INSPECT_INVALID_DATA`、`INSPECT_NOT_FOUND`、`INSPECT_UNSAFE_PATH`、`INSPECT_LIMIT_EXCEEDED`、`INSPECT_TIMEOUT`、`INSPECT_INPUT_CHANGED`、`INSPECT_REVISION_CHANGED`。坏记录可成为 partial 中的诊断，单资源不可读时为 error；消费方应保留上次成功内容并标记读取失败。

[Schema](../../schemas/inspect-result.schema.json) 与旧十类合成响应一同维护；新版 projection 的定向读取另由工作项测试覆盖。样例由 `python3 -B tests/test_inspect_examples.py --update-examples` 对临时工作区调用真实 CLI 生成，仅规范化临时根路径和 observedAt；不含真实业务记录。`test_inspect_examples.py` 同时校验实时查询和发布样例，`test_inspect_compatibility.py` 对固定旧基线比较默认 status/brief 输出。

## 客户端与 IDE 插件参考实现

- **[agent-workbench-intellij](https://github.com/sandy-hmb/agent-workbench-intellij)**：基于本 Inspect 协议构建的 IntelliJ IDEA / JetBrains 插件，详情以计划、变更、流程三页为主；文档原文和低频能力通过次级入口按需打开。

## 通用续接与新文档布局

`brief <slug> --projection resume --json` 与 handoff 共用定向事实，返回 activity、taskExecution、verification、repositoryContext 和带版本 sources。taskExecution.applicable=false 表示普通活动没有独立计划，不能解释成流程阻塞。默认验证 applicability=not_checked；显式 --check-code 才核对当前代码，记录通过不等于当前通过。检测到分支不匹配时报告，不自动 checkout。

新工作项标记 documentLayout=flat-v1，根目录使用 requirements.md、design.md、plan.md、verification.md；旧记录按已有路径读取。同一角色有多个有效文件时报告冲突。机器证据仍在 testing/evidence/，历史默认不进入接手上下文。

工作区的活动绑定由调用方显式传入 Feature；Kit 不要求 Cindy 或其他宿主的会话接口。公开协议新字段为可选增量，原有完成、验证和交付字段的含义保持不变。
