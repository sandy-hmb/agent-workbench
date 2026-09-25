"""WorkItem creation, review, delivery, iteration and completion commands."""
from __future__ import annotations
import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence
from workbench.validation import CommandParser
import workbench.work_items.commands as actions
from workbench.work_items.query import WorkItemQuery
from workbench.work_items.store import WorkItemError, SLUG_RE, item_area, item_path, load_state, read_json, read_evidence

ITEM_STATUSES = frozenset({'active', 'paused', 'done', 'cancelled'})

from workbench.work_items.query import item_summary, list_items, resolve_items


def build_parser():
    parser = CommandParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    create = commands.add_parser('create')
    create.add_argument('slug'); create.add_argument('--repo', action='append', required=True)
    create.add_argument('--title', default=''); create.add_argument('--summary', default='')
    create.add_argument('--document-kind', choices=['change', 'requirements'], default='change')
    create.add_argument('--risk-tier', choices=['light', 'normal', 'major'], default='normal')
    create.add_argument('--activity', default='develop'); create.add_argument('--branch')
    listing = commands.add_parser('list'); listing.add_argument('--status', choices=sorted(ITEM_STATUSES))
    resolve = commands.add_parser('resolve'); resolve.add_argument('--repo', required=True); resolve.add_argument('--branch', required=True)
    for name in ['approval', 'update', 'block', 'unblock', 'cancel', 'delivery', 'complete', 'pause', 'resume', 'next-iteration']:
        command = commands.add_parser(name)
        command.add_argument('slug'); command.add_argument('--state-revision', required=True)
        if name == 'approval':
            command.add_argument('--decision', choices=['approved', 'unchanged', 'needs-review'], required=True)
            command.add_argument('--reason', required=True); command.add_argument('--role', action='append')
            command.add_argument('--affected-task', action='append')
        if name in {'delivery', 'update'}: command.add_argument('--input', type=Path, required=True)
        if name in {'update','block','unblock','cancel'}: command.add_argument('--reason', required=True)
        if name == 'update': command.add_argument('--preview', action='store_true')
        if name == 'block':
            command.add_argument('--owner', required=True); command.add_argument('--condition', required=True)
            command.add_argument('--task', action='append')
        if name == 'unblock': command.add_argument('--blocker', required=True)
    for command in commands.choices.values():
        command.add_argument('--root', type=Path, default=Path.cwd()); command.add_argument('--json', action='store_true')
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        root = args.root.resolve()
        if args.command == 'create':
            result = actions.create(root, args.slug, title=args.title, summary=args.summary, repositories=args.repo,
                                    document_kind=args.document_kind, risk=args.risk_tier, activity=args.activity, branch=args.branch)
        elif args.command == 'list': result = list_items(root, args.status)
        elif args.command == 'resolve':
            result = {'matches': [dict(slug=x.slug, path=str(x.path), status=x.status, repository=x.repository, baseBranch=x.base_branch) for x in resolve_items(root, args.repo, args.branch)]}
        elif args.command == 'approval':
            result = actions.review(root, args.slug, decision=args.decision, reason=args.reason, roles=args.role,
                                    affected_tasks=args.affected_task, expected_revision=args.state_revision)
        elif args.command == 'delivery': result = actions.delivery(root, args.slug, read_json(args.input), expected_revision=args.state_revision)
        elif args.command == 'update': result = actions.update(root, args.slug, read_json(args.input), reason=args.reason, expected_revision=args.state_revision, preview=args.preview)
        elif args.command == 'block': result = actions.block(root, args.slug, reason=args.reason, owner=args.owner, condition=args.condition, task_ids=args.task, expected_revision=args.state_revision)
        elif args.command == 'unblock': result = actions.unblock(root, args.slug, args.blocker, reason=args.reason, expected_revision=args.state_revision)
        elif args.command == 'cancel': result = actions.cancel(root, args.slug, reason=args.reason, expected_revision=args.state_revision)
        elif args.command == 'next-iteration': result = actions.next_iteration(root, args.slug, expected_revision=args.state_revision)
        elif args.command == 'complete': result = actions.complete(root, args.slug, expected_revision=args.state_revision)
        else: result = actions.lifecycle(root, args.slug, 'paused' if args.command == 'pause' else 'active', expected_revision=args.state_revision)
        if isinstance(result.get('state'), dict):
            state = result.pop('state'); result.update(itemSlug=state['slug'], stateRevision=state['stateRevision'], lifecycle=state['lifecycle'])
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (OSError, ValueError) as exc:
        from workbench.errors import WorkbenchError
        print(json.dumps({'error': exc.as_dict() if isinstance(exc, WorkbenchError) else {'code': 'ITEM_ERROR', 'message': str(exc)}}, ensure_ascii=False))
        return 1

if __name__ == '__main__':
    raise SystemExit(main())
