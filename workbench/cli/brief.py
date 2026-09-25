"""Direct continuation or task context; no global active WorkItem pointer."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
from workbench.work_items.query import WorkItemQuery


def brief_result(root: Path, slug: str, *, task_id=None, check_code=False, deadline=None) -> dict:
    return WorkItemQuery(root, slug, deadline=deadline).continuation(task_id=task_id, check_code=check_code)


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('slug'); parser.add_argument('--root', type=Path, default=Path.cwd())
    parser.add_argument('--task', dest='task_id'); parser.add_argument('--check-code', action='store_true'); parser.add_argument('--json', action='store_true')
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        print(json.dumps(brief_result(args.root, args.slug, task_id=args.task_id, check_code=args.check_code), ensure_ascii=False)); return 0
    except (OSError, ValueError) as exc:
        print(json.dumps({'error': getattr(exc, 'code', 'BRIEF_ERROR'), 'message': str(exc)}, ensure_ascii=False)); return 1

if __name__ == '__main__': raise SystemExit(main())
