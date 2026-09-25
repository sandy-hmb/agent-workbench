# 常用入口

```bash
python3 scripts/kit.py status --json
python3 scripts/kit.py brief <slug> --json
python3 scripts/kit.py brief <slug> --task T01 --json
python3 scripts/kit.py brief <slug> --check-code --json
python3 scripts/kit.py verify snapshot <slug> --json
python3 scripts/kit.py verify record <slug> --input evidence.json --state-revision <stateRevision> --json
python3 scripts/kit.py verify evidence <slug> --json
python3 scripts/kit.py verify history <slug> --json
python3 scripts/kit.py verify render <slug> --json
python3 scripts/kit.py item complete <slug> --state-revision <stateRevision> --json
python3 scripts/kit.py item next-iteration <slug> --state-revision <stateRevision> --json
python3 scripts/kit.py doctor --root .
```

stateRevision 使用当前 brief/snapshot 的输出，不是用户需理解的审批对象。需求和证据格式见[第一个需求](first-item.md)。未知命令使用 --help 或 kit.py describe。
