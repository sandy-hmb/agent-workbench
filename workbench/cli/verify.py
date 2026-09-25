"""Capture code state, record validation once, and read or rebuild evidence views."""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path
from workbench.validation import CommandParser
import workbench.work_items.commands as item_actions
from workbench.work_items.query import WorkItemQuery
from workbench.work_items.store import WorkItemError, MAX_BYTES, item_path, read_json
from workbench.work_items.evidence import get_evidence, history_page


def snapshot_result(root: Path, slug: str, task_id=None):
    reader = WorkItemQuery(root, slug)
    if task_id:
        task = reader.task(task_id)
        states = reader.code_state({task['repository']})
    else:
        states = reader.code_state()
    reader.assert_unchanged()
    return {'itemSlug': slug, 'stateRevision': reader.state['stateRevision'], 'codeState': states}


def summary_text(root: Path, slug: str, item: Path, **_kwargs):
    # Action callers only request the current generated view; no mutation here.
    path = item / 'verification.md'
    return path.read_text() if path.is_file() else ''


def build_parser():
    parser = CommandParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    snapshot = commands.add_parser('snapshot'); snapshot.add_argument('--task')
    record = commands.add_parser('record'); record.add_argument('--input', type=Path, required=True); record.add_argument('--state-revision', required=True)
    evidence = commands.add_parser('evidence'); evidence.add_argument('--task'); evidence.add_argument('--id')
    history = commands.add_parser('history'); history.add_argument('--task'); history.add_argument('--iteration'); history.add_argument('--offset', type=int, default=0); history.add_argument('--limit', type=int, default=20)
    commands.add_parser('render')
    for command in commands.choices.values():
        command.add_argument('item'); command.add_argument('--root', type=Path, default=Path.cwd()); command.add_argument('--json', action='store_true')
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        item = item_path(args.root, args.item)
        if args.command == 'snapshot': result = snapshot_result(args.root, args.item, args.task)
        elif args.command == 'record':
            if str(args.input) == '-':
                data = sys.stdin.buffer.read(MAX_BYTES + 1)
                if len(data) > MAX_BYTES: raise WorkItemError('READ_LIMIT', '输入超过上限')
                try:
                    payload = json.loads(data)
                except (ValueError, UnicodeError) as exc:
                    raise WorkItemError('JSON_INVALID', '输入必须是有效 JSON', '$') from exc
            else: payload = read_json(args.input)
            result = item_actions.record(args.root, args.item, payload, expected_revision=args.state_revision)
            state = result.pop('state'); result.update(itemSlug=state['slug'], stateRevision=state['stateRevision'])
        elif args.command == 'evidence': result = get_evidence(item, task_id=args.task, evidence_id=args.id)
        elif args.command == 'history': result = history_page(item, offset=args.offset, limit=args.limit, task_id=args.task, iteration=args.iteration)
        else: result = item_actions.render(args.root, args.item)
        print(json.dumps(result, ensure_ascii=False)); return 0
    except (ValueError, OSError) as exc:
        from workbench.errors import WorkbenchError
        print(json.dumps({'error': exc.as_dict() if isinstance(exc, WorkbenchError) else {'code': 'VERIFICATION_ERROR', 'message': str(exc)}}, ensure_ascii=False)); return 1

if __name__ == '__main__': raise SystemExit(main())
