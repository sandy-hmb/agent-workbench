# 常用入口

`status`/`brief` 默认供人阅读；自动化、工作台和插件显式加 `--json`。JSON 字段、错误对象和退出码保持原行为。

```bash
python3 scripts/kit.py status
python3 scripts/kit.py status --item <slug> --context-sources
python3 scripts/kit.py brief <slug>
python3 scripts/kit.py brief <slug> --task T01
python3 scripts/kit.py brief --repo <repo> --path <path>
python3 scripts/kit.py brief <slug> --check-code
python3 scripts/kit.py item create <slug> --repo <repo> --title "修复目标" --activity repair --json
python3 scripts/kit.py verify snapshot <slug> --json
python3 scripts/kit.py verify collect <slug> --snapshot <snapshot.json> --junit artifacts/junit.xml --command '<actual-command>' --working-directory '<actual cwd>' --exit-status <actualExitStatus> --json
python3 scripts/kit.py verify record <slug> --input evidence.json --state-revision <stateRevision> --json
python3 scripts/kit.py verify evidence <slug> --json
python3 scripts/kit.py verify history <slug> --json
python3 scripts/kit.py verify render <slug> --json
python3 scripts/kit.py item complete <slug> --state-revision <stateRevision> --json
python3 scripts/kit.py item next-iteration <slug> --state-revision <stateRevision> --json
python3 scripts/kit.py doctor --root .
```

stateRevision 使用当前 brief/snapshot 的输出，不是用户需理解的审批对象。需求和证据格式见[第一个需求](first-item.md)。未知命令使用 --help 或 kit.py describe。

报告采集先运行实际测试、保存同次检查的 snapshot，再 collect；人工审阅输出的 input 后才 record，整体验证通过还需实际填写 reviewResult。collect 不执行命令或写状态，失败草稿也可能正常退出。完整流程见[验证证据采集](verification-evidence.md)。

change 模式的 `repair`、`investigate`、`takeover`、`acceptance` 使用对应活动模板；`develop` 和其他合法自定义活动回退通用模板。requirements 模式不按活动切换模板。
