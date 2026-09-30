# 从 JUnit 报告采集验证草稿

先运行实际测试命令生成 JUnit XML，再保存同一次检查对应的代码快照。报告放在 WorkItem 的 `artifacts/` 等目录；快照可放在 WorkItem 外，但避免新增文件影响业务仓指纹。下面的测试命令与报告路径须按项目已有测试工具填写，Kit 不执行该命令。

```bash
<actual-test-command-that-writes-junit-xml>
python3 scripts/kit.py verify snapshot <slug> --json > <snapshot.json>
python3 scripts/kit.py verify collect <slug> --snapshot <snapshot.json> \
  --junit artifacts/junit.xml --command '<actual-test-command>' \
  --working-directory '<actual cwd>' --exit-status <actualExitStatus> \
  --artifact artifacts/report.txt --json > <collection.json>
```

`--exit-status` 使用实际测试进程退出码。`--junit` 和 `--artifact` 均可重复，路径相对 WorkItem，不相对当前 shell 目录；只接受普通文件，拒绝绝对路径、越界、反斜杠和符号链接。报告及附件去重后最多 20 个，每个文件和快照均最多 2 MiB。重复报告只统计一次，报告同时作为附件时保留 `test-report` 类型。

snapshot 始终返回 `taskId`：逐任务验证为实际任务 ID，整体验证为 null。snapshot 和 collect 必须使用相同的 `--task T01`，collect 精确核对快照任务身份；整体验证快照不能用于任务，同仓不同任务也不能互用。缺少 taskId 的旧任务快照须重新生成；旧整体快照缺少该字段仍兼容。快照代码集合必须恰好包含该任务仓，整体验证则包含全部绑定仓。旧状态、错误工作项、非活动状态、仓集合不匹配或代码漂移会拒绝采集。

## 审阅后正式记录

collect 无论是否带 `--json` 都只打印 JSON，包含身份、状态版本、汇总计数、报告/附件元数据及 `input`。它不执行命令、不创建输入文件、不记录证据、不更新 README、不改变工作项状态。上例的文件重定向由 shell 显式执行。

人工核对实际命令、退出码、测试范围、快照与报告是否来自同一次检查，再将 `input` 提取为 `evidence.json`。可以使用标准库提取：

```bash
python3 -c 'import json,sys; print(json.dumps(json.load(sys.stdin)["input"], ensure_ascii=False))' \
  < <collection.json> > evidence.json
```

通过的整体验证还须在实际复核后显式填写 `reviewResult: "passed"`；collect 不预填该字段，也不替代内容审批。使用 collection 返回的 `stateRevision` 正式提交：

```bash
python3 scripts/kit.py verify record <slug> --input evidence.json \
  --state-revision <collection.stateRevision> --json
```

`record` 继续核对当前代码、审批、门禁和附件哈希。报告或附件改动后，旧 `artifactRefs` 会被拒绝；重新采集前先确认改动仍对应本次真实检查，不能用新快照给旧报告补造归属。

## 支持范围与结果

支持 `testsuite`/`testsuites` 根、嵌套 suite、XML namespace，以及 testcase 的 `failure`、`error`、`skipped`、`system-out`、`system-err`。常见 `status`/`result` 值支持 passed/success/ok、skipped/notrun、failed/failure、error；没有结果标记的普通 testcase 按通过统计。未知或冲突结果、畸形 XML、DTD/实体声明和非法计数会拒绝。suite 声明的 tests/failures/errors/skipped 必须与后代明细一致。

实际执行数为总数减跳过数。仅所有报告都有 testcase、没有失败/错误/跳过且实际退出码为 0 时，草稿 `input.result` 才是 passed；空报告、跳过或非零退出码都会生成 failed。成功解析失败报告的 CLI 退出码仍为 0，自动化须读取 `input.result`。解析或校验错误返回错误对象和非零退出码。

草稿只保存计数、路径、精确字节数及带 `sha256:` 前缀的哈希，不复制 XML、错误正文或 stdout/stderr。collect 不证明命令已经实际执行，不自动批准、登记或完成 WorkItem。
