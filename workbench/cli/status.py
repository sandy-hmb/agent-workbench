"""Bounded workspace overview or a directly selected WorkItem."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
from workbench.cli.brief import _diagnostic_lines, _item_lines, _verification_text
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


def status_text(value: dict) -> str:
    extensions, workflow = value['extensions'], value['workflow']
    lines = [f"工作区：{value['workspace']['name']}", f"模式：{value['mode']}",
             'Extension：' + ('、'.join(extensions.get('activeIds', [])) or '无已启用扩展'),
             'Workflow：' + ('已启用' if workflow.get('enabled') else '未启用')]
    for label, section in [('Extension', extensions), ('Workflow', workflow)]:
        if section.get('blockedCodes'):
            lines.append(label + ' 阻塞：' + '、'.join(section['blockedCodes']))
    for key, label in [('runs', 'Run 数量'), ('actions', 'Action 数量'), ('pending', '待执行'),
                       ('failed', '失败'), ('interrupted', '待核对'), ('running', '执行中')]:
        if key in workflow:
            lines.append(f'{label}：{workflow[key]}')
    if 'nextStage' in workflow:
        lines.append('Workflow 下一阶段：' + (workflow['nextStage'] or '无下一阶段'))
    selected = value.get('item')
    items = [selected] if selected is not None else value['items']
    lines.append(f'工作项数量：{len(items)}' + ('（所选项）' if selected is not None else ''))
    if not items:
        lines.append('暂无工作项')
    for item in items:
        progress = item['progress']
        line = f"- {item['slug']} / {item['title']}；记录进度：{progress['completed']}/{progress['total']}"
        if selected is None:
            line += f"；状态：{item['status']}；活动：{item['activity']}；" + _verification_text(item['verification'])
        lines.append(line)
        if item.get('openBlockerCount'):
            lines.append(f"  未解除开发阻塞：{item['openBlockerCount']}")
    if selected is not None:
        lines += _item_lines(selected)
    lines += _diagnostic_lines(value.get('diagnostics', []))
    if context := value.get('contextSources'):
        lines += ['上下文来源：', '  - 工作区：' + context['workspace']]
        lines += [f'  - 仓库：{path}' for path in context['repositories']] or ['  - 无仓库上下文来源']
    return '\n'.join(lines)


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path.cwd()); parser.add_argument('--item')
    parser.add_argument('--context-sources', action='store_true'); parser.add_argument('--json', action='store_true')
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        value = status_result(args.root, context_sources=args.context_sources, item_slug=args.item)
        print(json.dumps(value, ensure_ascii=False) if args.json else status_text(value))
        return 0
    except (ValueError, OSError) as exc:
        error = {'error': getattr(exc, 'code', 'STATUS_ERROR'), 'message': str(exc)}
        print(json.dumps(error, ensure_ascii=False) if args.json else f"错误：{error['error']}；{error['message']}")
        return 1

if __name__ == '__main__': raise SystemExit(main())
