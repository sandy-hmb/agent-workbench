"""Direct continuation or task context; no global active WorkItem pointer."""
from __future__ import annotations
import argparse
import json
import re
from pathlib import Path
from workbench.work_items.query import WorkItemQuery, repository_roots, repository_context
from workbench.work_items.store import WorkItemError
from workbench.workspace.model import load_workspace, resolve_repository


def brief_result(root: Path, slug: str | None = None, *, task_id=None, check_code=False, deadline=None, repository=None, paths=None) -> dict:
    root = Path(root).resolve()
    if repository and (root / '.workspace/workspace.json').exists():
        repository = resolve_repository(load_workspace(root).repositories, repository).path
    if slug:
        return WorkItemQuery(root, slug, deadline=deadline).continuation(task_id=task_id, check_code=check_code, repository=repository, paths=paths)
    if not repository or task_id or check_code:
        raise WorkItemError('CONTEXT_ARGUMENT_INVALID', '无工作项查询需要 --repo，可选 --path，不接受 --task 或 --check-code')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*', repository):
        raise WorkItemError('CONTEXT_ARGUMENT_INVALID', '仓库名称无效')
    location = repository_roots(root, {'bindings': [{'repository': repository}]})[repository]
    return {'repository': repository, 'instructionContext': repository_context(root, location, paths=paths)}


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('slug', nargs='?'); parser.add_argument('--root', type=Path, default=Path.cwd())
    parser.add_argument('--repo', dest='repository'); parser.add_argument('--path', action='append', dest='paths')
    parser.add_argument('--task', dest='task_id'); parser.add_argument('--check-code', action='store_true'); parser.add_argument('--json', action='store_true')
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        print(json.dumps(brief_result(args.root, args.slug, task_id=args.task_id, check_code=args.check_code, repository=args.repository, paths=args.paths), ensure_ascii=False)); return 0
    except (OSError, ValueError) as exc:
        print(json.dumps({'error': getattr(exc, 'code', 'BRIEF_ERROR'), 'message': str(exc)}, ensure_ascii=False)); return 1

if __name__ == '__main__': raise SystemExit(main())
