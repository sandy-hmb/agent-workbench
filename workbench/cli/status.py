"""Bounded workspace overview or a directly selected WorkItem."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
from workbench.work_items.query import list_items
from workbench.work_items.query import WorkItemQuery
from workbench.workspace.model import load_workspace
from workbench.workspace.paths import context_file, profiles_root, workspace_file
from workbench.extensions.management import extension_status
from workbench.extensions.runner import status_result as workflow_status


def status_result(root: Path, *, context_sources=False, item_slug=None) -> dict:
    root = Path(root).resolve()
    configured = workspace_file(root).exists()
    extensions = extension_status(root) if configured else {'activeIds': [], 'blockedCodes': []}
    try:
        workspace = load_workspace(root) if configured else None
    except ValueError:
        if not extensions.get('blockedCodes'):
            raise
        workspace = None
    result = {'schemaVersion': 2, 'mode': 'workspace' if configured else 'maintenance',
              'workspace': workspace.identity.as_dict() if workspace else {'name': root.name},
              'extensions': extensions, 'workflow': workflow_status(root)}
    if item_slug:
        reader = WorkItemQuery(root, item_slug)
        result['item'] = reader.summary()
        reader.assert_unchanged()
    else:
        result.update(list_items(root))
    if context_sources:
        result['contextSources'] = {'workspace': str(context_file(root)),
                                    'repositories': [str(profiles_root(root) / (r.path + '.md')) for r in workspace.repositories] if workspace else []}
    return result


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path.cwd()); parser.add_argument('--item')
    parser.add_argument('--context-sources', action='store_true'); parser.add_argument('--json', action='store_true')
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        print(json.dumps(status_result(args.root, context_sources=args.context_sources, item_slug=args.item), ensure_ascii=False))
        return 0
    except (ValueError, OSError) as exc:
        print(json.dumps({'error': getattr(exc, 'code', 'STATUS_ERROR'), 'message': str(exc)}, ensure_ascii=False)); return 1

if __name__ == '__main__': raise SystemExit(main())
