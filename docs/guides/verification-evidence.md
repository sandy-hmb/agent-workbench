# 从 JUnit 报告采集验证草稿

先按[验证策略](../../.agents/skills/workspace-verify/references/strategy.md)选择必要检查，不为采集证据新增测试或启动数据库。实际测试生成 JUnit XML 后保存同批代码快照。原始报告可放 WorkItem 的 `artifacts/`，快照和采集草稿默认临时文件，不长期保存同义输入副本；避免新增文件影响业务仓指纹。下面的命令与报告路径按已有工具填写，Kit 不执行测试。

```bash
<actual-test-command-that-writes-junit-xml>
python3 scripts/kit.py verify snapshot <slug> --json > <snapshot.json>
python3 scripts/kit.py verify collect <slug> --snapshot <snapshot.json> \
  --junit artifacts/junit.xml --command '<actual-test-command>' \
  --working-directory '<actual cwd>' --exit-status <actualExitStatus> \
  --artifact artifacts/report.txt --json > <collection.json>
```

`--exit-status` 使用实际测试进程退出码。`--junit` 和 `--artifact` 均可重复，路径相对 WorkItem，不相对当前 shell 目录；只接受普通文件，拒绝绝对路径、越界、反斜杠和符号链接。报告及附件去重后最多 20 个，每个文件和快照均最多 2 MiB。重复报告只统计一次，报告同时作为附件时保留 `test-report` 类型。

snapshot 始终返回 `taskId`：逐任务为实际任务 ID，整体为 null。单任务 collect 与 snapshot 使用相同的 `--task T01`；批量 collect 可重复 `--task`，返回 `scope=batch` 和待审阅的 `taskCoverage`，复用一份报告，不任意扩展覆盖。批量快照的 taskId 可为 null 或所覆盖任务之一，但 codeState 仓集合必须恰好等于覆盖任务的仓集合；整体快照含全部绑定仓，仅在集合匹配时适用，同仓批量可用其中一个任务的快照。缺少 taskId 的旧任务快照须重新生成，旧整体快照仍兼容。错误身份、状态、仓集合或代码漂移会拒绝采集。

## 审阅后正式记录

collect 无论是否带 `--json` 都只打印 JSON，包含身份、状态版本、汇总计数、报告/附件元数据及 `input`。它不执行命令、不创建输入文件、不记录证据、不更新 README、不改变工作项状态。上例的文件重定向由 shell 显式执行。

人工核对命令、退出码、实际覆盖和报告/快照归属，再将 `input` 提取到临时文件或 stdin。整体验证通过须在实际复核后填写 `reviewResult: "passed"`；collect 不预填、不代替审批。批量草稿逐项核实覆盖，不复制 checks 逐任务重登。可用标准库将已审阅草稿输入 stdin：

```bash
python3 -c 'import json,sys; print(json.dumps(json.load(sys.stdin)["input"], ensure_ascii=False))' \
  < <collection.json> | python3 scripts/kit.py verify record <slug> --input - \
  --state-revision <collection.stateRevision> --json
```

`record` 继续核对当前代码、审批、门禁和附件哈希。报告或附件改动后，旧 `artifactRefs` 会被拒绝；重新采集前先确认改动仍对应本次真实检查，不能用新快照给旧报告补造归属。

## 支持范围与结果

支持 `testsuite`/`testsuites` 根、嵌套 suite、XML namespace，以及 testcase 的 `failure`、`error`、`skipped`、`system-out`、`system-err`。常见 `status`/`result` 值支持 passed/success/ok、skipped/notrun、failed/failure、error；没有结果标记的普通 testcase 按通过统计。未知或冲突结果、畸形 XML、DTD/实体声明和非法计数会拒绝。suite 声明的 tests/failures/errors/skipped 必须与后代明细一致。

实际执行数为总数减跳过数。仅所有报告都有 testcase、没有失败/错误/跳过且实际退出码为 0 时，草稿 `input.result` 才是 passed；空报告、跳过或非零退出码都会生成 failed。成功解析失败报告的 CLI 退出码仍为 0，自动化须读取 `input.result`。解析或校验错误返回错误对象和非零退出码。

草稿只保存计数、路径、精确字节数及带 `sha256:` 前缀的哈希，不复制 XML、错误正文或 stdout/stderr。collect 不证明命令已经实际执行，不自动批准、登记或完成 WorkItem。
