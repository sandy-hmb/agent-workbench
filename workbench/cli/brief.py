"""Direct continuation or task context; no global active WorkItem pointer."""
from __future__ import annotations
import argparse
import json
import re
from pathlib import Path
from workbench.work_items.query import WorkItemQuery, repository_roots, repository_context
from workbench.work_items.store import WorkItemError
from workbench.workspace.model import load_workspace, resolve_repository
from workbench.workspace.paths import workspace_file


def brief_result(root: Path, slug: str | None = None, *, task_id=None, check_code=False, deadline=None, repository=None, paths=None) -> dict:
    root = Path(root).resolve()
    if repository and workspace_file(root).exists():
        repository = resolve_repository(load_workspace(root).repositories, repository).path
    if slug:
        return WorkItemQuery(root, slug, deadline=deadline).continuation(task_id=task_id, check_code=check_code, repository=repository, paths=paths)
    if not repository or task_id or check_code:
        raise WorkItemError('CONTEXT_ARGUMENT_INVALID', '无工作项查询需要 --repo，可选 --path，不接受 --task 或 --check-code')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*', repository):
        raise WorkItemError('CONTEXT_ARGUMENT_INVALID', '仓库名称无效')
    location = repository_roots(root, {'bindings': [{'repository': repository}]})[repository]
    return {'repository': repository, 'instructionContext': repository_context(root, location, paths=paths)}


def _verification_text(value: dict) -> str:
    recorded = value.get('recordedResult', 'unknown')
    applicability = value.get('applicability', 'not_checked')
    result = {'passed': '通过', 'failed': '失败', 'unknown': '未知'}.get(recorded, recorded)
    current = {'not_checked': '未检查', 'valid': '有效', 'invalid': '失效',
               'missing': '无记录', 'unknown': '未知'}.get(applicability, applicability)
    return f'记录结果：{result}（{recorded}）；代码适用性：{current}（{applicability}）'


def _diagnostic_lines(rows: list[dict]) -> list[str]:
    if not rows:
        return []
    return ['诊断：', *[f"  - {row.get('severity', '')} {row['code']}：{row['message']}" +
                       (f"；{row['path']}" if row.get('path') else '') for row in rows]]


def _item_lines(value: dict) -> list[str]:
    lines = []
    for key, label in [('iteration', '轮次'), ('activity', '活动')]:
        if key in value:
            lines.append(f'{label}：{value[key]}')
    lifecycle = value.get('lifecycle', value.get('status'))
    if lifecycle is not None:
        label = {'active': '进行中', 'paused': '暂停', 'done': '已完成', 'cancelled': '已取消'}.get(lifecycle, lifecycle)
        lines.append(f'生命周期：{lifecycle}（{label}）')
    lines += [f"阶段：{value.get('currentStage') or '无当前阶段'}",
              f"执行决策：{value.get('executionDecision', '未提供')}",
              '可完成：' + ('是' if value.get('canComplete') else '否'),
              '阻塞：' + ('、'.join(value.get('blockers', [])) or '无阻塞')]
    blocks = [row for row in value.get('executionBlockers', []) if row['status'] == 'open']
    lines.append('开发阻塞：' + ('' if blocks else '无开发阻塞'))
    for row in blocks:
        tasks = '、'.join(row['taskIds']) or '整个工作项'
        lines.append(f"  - {row['id']}（{tasks}）：{row['reason']}；负责人：{row['owner']}；解除条件：{row['condition']}")
    verification = value.get('verification')
    if verification is not None:
        lines.append(_verification_text(verification))
        if verification.get('reason'):
            lines.append('验证诊断：' + verification['reason'])
        for row in verification.get('repositoryStates', []):
            lines.append(f"  - 代码 {row['repository']}：{row['state']}")
        checks = [row for row in verification.get('pendingExternalChecks', []) if row['status'] not in {'passed', 'waived'}]
        lines.append('待外部验收：' + ('' if checks else '无待外部验收'))
        for row in checks:
            lines.append(f"  - {row['id']} / {row['requirement']}：{row['description']}；负责人：{row['owner']}；{row['status']}" +
                         (f"；原记录状态：{row['recordedStatus']}" if row.get('recordedStatus') else '') +
                         (f"；{row['reason']}" if row.get('reason') else ''))
    else:
        lines.append('待外部验收：未提供')
    actions = value.get('nextActions', [])
    lines.append('下一步：' + ('' if actions else '无下一步'))
    lines += [f"  - {row['stage']}；runbook：{row.get('runbook', '未提供')}" for row in actions]
    if value.get('stateRevision'):
        lines.append('状态版本：' + value['stateRevision'])
    return lines


def brief_text(value: dict) -> str:
    lines = []
    if value.get('itemSlug'):
        lines.append('工作项：' + value['itemSlug'])
    repositories = value.get('repositoryContext', [])
    lines.append('仓库：' + ('、'.join(row['repository'] for row in repositories) or value.get('repository', '未提供')))
    for row in repositories:
        lines.append(f"  - {row['repository']}：当前分支 {row.get('currentBranch') or '不可用'}；目标分支 {row['workBranch']}；{row['state']}")
    if value.get('itemSlug'):
        lines += _item_lines(value)
    if task := value.get('selectedTask'):
        lines += [f"所选任务：{task['id']}；{task['status']}",
                  '任务等待：' + ('、'.join(task['waitingFor']) or '无等待'),
                  f"任务来源：{task['path']}:{task['startLine']}-{task['endLine']}", task['body']]
        dependencies = value.get('directDependencies', [])
        lines.append('直接依赖：' + ('' if dependencies else '无直接依赖'))
        lines += [f"  - {row['id']}：{row['status']}；证据：{row.get('evidenceId') or '未记录'}" for row in dependencies]
        for row in value.get('sources', []):
            lines.append('依据来源：' + (f"{row['id']}；{row['path']}:{row['startLine']}-{row['endLine']}" +
                                        ('（未定位）' if not row['located'] else '') if 'id' in row else row['reference']))
    context = value.get('instructionContext', {})
    rules = context.get('rules', [])
    lines.append('规则来源：' + ('' if rules else '无可用规则来源'))
    lines += [f"  - {row['path']}；作用范围：{row['scope']}" for row in rules]
    facts = context.get('facts', [])
    if facts:
        lines += ['事实来源：', *[f"  - {row['path']}；{row['kind']}" for row in facts]]
    for target in context.get('targets', []):
        if target['paths']:
            lines.append('目标路径：' + '、'.join(target['paths']))
    if context.get('scopedRulesPending'):
        lines.append('目录规则：尚待按路径定位')
    lines += _diagnostic_lines(context.get('diagnostics', []))
    return '\n'.join(lines)


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('slug', nargs='?'); parser.add_argument('--root', type=Path, default=Path.cwd())
    parser.add_argument('--repo', dest='repository'); parser.add_argument('--path', action='append', dest='paths')
    parser.add_argument('--task', dest='task_id'); parser.add_argument('--check-code', action='store_true'); parser.add_argument('--json', action='store_true')
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        value = brief_result(args.root, args.slug, task_id=args.task_id, check_code=args.check_code, repository=args.repository, paths=args.paths)
        print(json.dumps(value, ensure_ascii=False) if args.json else brief_text(value))
        return 0
    except (OSError, ValueError) as exc:
        error = {'error': getattr(exc, 'code', 'BRIEF_ERROR'), 'message': str(exc)}
        print(json.dumps(error, ensure_ascii=False) if args.json else f"错误：{error['error']}；{error['message']}")
        return 1

if __name__ == '__main__': raise SystemExit(main())
