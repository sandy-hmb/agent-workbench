"""Inspect 2: bounded, targeted, read-only views over the shared WorkItem model."""
from __future__ import annotations
import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from workbench.work_items.query import list_items
from workbench.work_items.query import WorkItemQuery
from workbench.work_items.store import WorkItemError, digest, item_path, read_bytes, safe_path, text_digest
from workbench.workspace.model import load_workspace, repository_path, effective_branch_policy
from workbench.workspace.paths import workflow_file, workflow_runs_root, workspace_file

API_MAJOR = 2
API_MINOR = 2
MAX_RESPONSE = 8 * 1024 * 1024
OPERATIONS = ['workspace', 'items', 'projection', 'document', 'artifacts', 'verification', 'evidence', 'handoff', 'search', 'workflow', 'runs', 'run']


class Deadline:
    def __init__(self, seconds=10): self.end = time.monotonic() + seconds
    def check(self):
        if time.monotonic() > self.end: raise WorkItemError('INSPECT_TIMEOUT', '查询超过时间限制')


class InspectParser(argparse.ArgumentParser):
    def error(self, message):
        raise WorkItemError('INSPECT_ARGUMENT_INVALID', message)


def _page(items, offset, limit):
    if offset < 0 or not 1 <= limit <= 200: raise WorkItemError('INSPECT_ARGUMENT_INVALID', '分页参数无效')
    return {'items': items[offset:offset + limit], 'counts': {'total': len(items)},
            'collectionRevision': digest(items),
            'page': {'offset': offset, 'limit': limit, 'total': len(items), 'hasMore': offset + limit < len(items)}}


def workspace(root):
    model = load_workspace(root) if workspace_file(root).exists() else None
    repos = [{'id': root.name, 'role': 'kit', 'absolutePath': str(root), 'aliases': [], 'category': 'kit', 'description': '工作流 Kit', 'availability': 'present', 'effectiveBranchPolicy': None}]
    if model:
        for repo in model.repositories:
            path = repository_path(model, repo)
            policy = effective_branch_policy(model, repo)
            repos.append({'id': repo.path, 'role': 'business', 'absolutePath': str(path), 'aliases': list(repo.aliases),
                          'category': repo.category, 'description': repo.description, 'availability': 'present' if path.is_dir() else 'missing',
                          'effectiveBranchPolicy': policy.as_dict()})
    return {'mode': 'workspace' if model else 'maintenance', 'identity': model.identity.as_dict() if model else {'name': root.name},
            'repositories': repos, 'localContext': {}, 'configuration': {},
            'protocol': {'apiVersion': {'major': API_MAJOR, 'minor': API_MINOR}, 'operations': OPERATIONS,
                         'kitVersion': (root / 'VERSION').read_text().strip() if (root / 'VERSION').exists() else '2.0.0'}}


def _documents(reader, *, include_artifacts=False):
    docs = [{**doc, 'exists': True, 'kind': doc['role'], 'mediaType': 'text/markdown'} for doc in reader.documents()]
    known = {row['path'] for row in docs}
    for name in ['references'] + (['artifacts'] if include_artifacts else []):
        folder = safe_path(reader.path, name)
        if not folder.exists(): continue
        for count, path in enumerate(folder.rglob('*')):
            reader.remaining()
            if count >= 10000:
                raise WorkItemError('INSPECT_LIMIT', '扫描条目超过上限')
            if len(docs) >= 500: raise WorkItemError('INSPECT_LIMIT', '文档条目超过上限')
            relative = path.relative_to(reader.path).as_posix()
            safe_path(reader.path, relative)
            if relative not in known and path.is_file() and path.suffix in {'.md', '.txt', '.json', '.sql', '.csv'}:
                docs.append({'path': relative, 'role': name, 'kind': name, 'exists': True, 'documentRevision': text_digest(read_bytes(path, 1024 * 1024)), 'mediaType': 'text/plain'})
                known.add(relative)
    return docs


def projection(root, slug, view, task_id=None, check_code=False, deadline=None):
    reader = WorkItemQuery(root, slug, deadline=deadline)
    base = {'view': view, 'slug': slug, 'stateRevision': reader.state['stateRevision']}
    if view in {'summary', 'task'}:
        summary = reader.summary(check_code=check_code)
        available = summary['currentStage'] == 'item.complete' and not summary['blockers'] and not reader.execution_blocks()['records']
        summary['completionAction'] = {'available': available,
                                       'reason': None if available else '仍有未完成事项；以 Kit 再次校验为准'}
        base.update(summary=summary, progression=reader.decision(), documents=summary['documents'])
        if view == 'task':
            base['tasks'] = [reader.task(task_id)] if task_id else reader.task_states()
    elif view == 'change':
        base.update(repositories=[{**b, 'absolutePath': str(reader.roots()[b['repository']])} for b in reader.state['bindings']],
                    comparison={'kind': 'merge-base', 'source': 'item-bindings'})
    elif view == 'flow':
        base.update(status=reader.state['lifecycle'], delivery={'repositories': reader.state['delivery'], 'externalChecks': reader.state['externalChecks']},
                    verification=reader.verification(), workflow=workflow(root),
                    executionBlockers=reader.state['blockers'], cancellation=reader.state['cancellation'])
    else: raise WorkItemError('INSPECT_ARGUMENT_INVALID', '未知视图')
    reader.assert_unchanged()
    return base


def document(root, slug, relative, document_revision=None, deadline=None):
    reader = WorkItemQuery(root, slug, deadline=deadline)
    path = safe_path(reader.path, relative)
    from workbench.work_items.documents import ROLES
    historical = next((item for item in reader.state['history'] if relative.startswith(item['path'] + '/')), None)
    member = relative[len(historical['path']) + 1:] if historical else relative
    if member not in ROLES.values() and not (member.startswith(('references/', 'artifacts/')) and path.suffix in {'.md', '.txt', '.json', '.sql', '.csv'}):
        raise WorkItemError('INSPECT_DOCUMENT_INVALID', '文档不在当前 WorkItem 可读集合')
    data = read_bytes(path, 1024 * 1024)
    actual = text_digest(data)
    if document_revision and document_revision != actual: raise WorkItemError('INSPECT_REVISION_CHANGED', '文档已变化，请重新定位')
    result = {'slug': slug, 'path': relative, 'documentRevision': actual, 'content': data.decode('utf-8'), 'bytes': len(data), 'lineCount': len(data.splitlines()), 'mediaType': 'text/markdown' if path.suffix == '.md' else 'text/plain'}
    reader.assert_unchanged()
    return result


def artifacts(root, slug, offset=0, limit=50, deadline=None):
    reader = WorkItemQuery(root, slug, deadline=deadline)
    result = reader.artifact_page(offset, limit)
    reader.assert_unchanged()
    return {'slug': slug, **result}


def verification(root, slug, check_code=False, deadline=None):
    reader = WorkItemQuery(root, slug, deadline=deadline)
    result = reader.verification(check_code=check_code, browse=True)
    batch = reader.evidence(reader.state['verification'])
    result.update(slug=slug, stateRevision=reader.state['stateRevision'], record=batch,
                  tasks=[{'id': t['id'], 'status': t['status'], 'evidenceId': t['evidenceId']} for t in reader.task_states()],
                  progression=reader.decision())
    reader.assert_unchanged()
    return result


def handoff(root, slug, check_code=False, deadline=None):
    from workbench.context_measure import estimate_tokens
    brief = WorkItemQuery(root, slug, deadline=deadline).continuation(check_code=check_code)
    sources = [{'kind': d['role'], 'path': d['path'], 'documentRevision': d['documentRevision'], 'startLine': 1} for d in brief['documents']]
    content = '# ' + slug + ' 接手\n\n' + '\n'.join(f'- {key}：{json.dumps(brief.get(key), ensure_ascii=False)}' for key in ['iteration', 'activity', 'lifecycle', 'currentStage', 'currentTask', 'blockers', 'nextActions'])
    content += '\n\n文档：\n' + '\n'.join(f"- [{s['kind']}]({s['path']})" for s in sources)
    for label, rows in [('开发阻塞', brief.get('executionBlockers', [])), ('待外部验收', brief.get('verification', {}).get('pendingExternalChecks', []))]:
        opened = [row for row in rows if row.get('status') not in {'resolved', 'passed', 'waived'}]
        if opened:
            content += '\n\n' + label + '：\n' + '\n'.join('- ' + json.dumps(row, ensure_ascii=False) for row in opened[:10])
            if len(opened) > 10:
                content += f'\n其余 {len(opened)-10} 项使用 kit.py brief {slug} 查看。'
    return {'slug': slug, 'content': content, 'estimatedTokens': estimate_tokens(content)['estTokens'], 'sources': sources,
            'stateRevision': brief['stateRevision'], 'progression': {key: brief[key] for key in ['currentStage', 'nextActions', 'blockers']}}


def workflow(root):
    from workbench.extensions.management import extension_status
    enabled = workflow_file(root).is_file()
    extension = extension_status(root) if workspace_file(root).exists() else {'activeIds': []}
    result = {'enabled': enabled, 'configState': 'enabled' if enabled else 'disabled', 'extensions': [{'id': name} for name in extension.get('activeIds', [])]}
    if enabled:
        from workbench.extensions.runner import _resolve
        _, _, resolved, _ = _resolve(root)
        result['orderedStages'] = [{'id': stage.id, 'core': stage.core, 'action': stage.action} for stage in resolved.stages]
    return result


def runs(root, item, offset, limit):
    from workbench.extensions.runner import _load_run
    folder = workflow_runs_root(root)
    values = []
    if folder.exists():
        for path in sorted(folder.glob('*.json')):
            row = _load_run(root, path.stem)
            if item is None or row.get('itemSlug') == item:
                import workbench.extensions.attempts as attempts
                entries = list(attempts.read(root, row['id'])['requests'].values())
                values.append({'id': row['id'], 'itemSlug': row.get('itemSlug'),
                               'updatedAt': max((item['updatedAt'] for item in entries), default=None),
                               'recordCounts': {'total': len(entries)}, 'workflow': row['workflow']})
    return _page(values, offset, limit)


def run(root, identifier):
    from workbench.extensions.runner import _load_run
    import workbench.extensions.attempts as attempts
    value = _load_run(root, identifier)
    rows = [attempts.observed(root, row) for row in attempts.read(root, identifier)['requests'].values()]
    return {'id': identifier, 'itemSlug': value.get('itemSlug'), 'records': rows, 'attempts': rows}


def search(root, query, repo, status, offset, limit, deadline):
    if not query.strip() or limit > 50: raise WorkItemError('INSPECT_ARGUMENT_INVALID', '搜索条件或分页无效')
    words = query.casefold().split(); hits = []; consumed = 0
    for item in list_items(root, status)['items']:
        deadline.check()
        if repo and repo not in {b['repository'] for b in item['repositoryBindings']}: continue
        reader = WorkItemQuery(root, item['slug'], deadline=deadline.end)
        for reference in _documents(reader, include_artifacts=True):
            text = read_bytes(safe_path(reader.path, reference['path']), 1024 * 1024).decode()
            consumed += len(text.encode())
            if consumed > 16 * 1024 * 1024: raise WorkItemError('INSPECT_LIMIT', '搜索文本超过上限')
            if not all(word in text.casefold() for word in words): continue
            for line, content in enumerate(text.splitlines(), 1):
                if any(word in content.casefold() for word in words):
                    hits.append({'slug': item['slug'], 'title': item['title'], 'status': item['status'], 'lastUpdated': item['updatedAt'],
                                 'path': reference['path'], 'line': line, 'heading': None, 'snippet': content[:300]})
                    break
    return {'query': query, **_page(hits, offset, limit)}


def build_parser():
    parser = InspectParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path.cwd()); parser.add_argument('--api-major', type=int, default=API_MAJOR); parser.add_argument('--json', action='store_true')
    commands = parser.add_subparsers(dest='operation', required=True)
    commands.add_parser('workspace'); commands.add_parser('workflow')
    items = commands.add_parser('items'); items.add_argument('--status')
    projects = commands.add_parser('projection'); projects.add_argument('slug'); projects.add_argument('--view', choices=['summary', 'task', 'change', 'flow'], default='summary'); projects.add_argument('--task')
    documents = commands.add_parser('document'); documents.add_argument('slug'); documents.add_argument('--path', required=True); documents.add_argument('--document-revision')
    artifact_parser = commands.add_parser('artifacts'); artifact_parser.add_argument('slug'); artifact_parser.add_argument('--offset', type=int, default=0); artifact_parser.add_argument('--limit', type=int, default=50)
    for op in ['verification', 'handoff']:
        command = commands.add_parser(op); command.add_argument('slug'); command.add_argument('--check-code', action='store_true')
    evidence = commands.add_parser('evidence'); evidence.add_argument('slug'); evidence.add_argument('--task'); evidence.add_argument('--id')
    search_parser = commands.add_parser('search'); search_parser.add_argument('--query', required=True); search_parser.add_argument('--repo'); search_parser.add_argument('--status')
    runs_parser = commands.add_parser('runs'); runs_parser.add_argument('--item')
    commands.add_parser('run').add_argument('id')
    for p in [items, runs_parser, search_parser]: p.add_argument('--offset', type=int, default=0); p.add_argument('--limit', type=int, default=20)
    return parser


def main(argv=None):
    try:
        args = build_parser().parse_args(argv)
    except WorkItemError as exc:
        print(json.dumps({'apiVersion': {'major': API_MAJOR, 'minor': API_MINOR}, 'operation': None,
                          'status': 'error', 'observedAt': datetime.now(timezone.utc).isoformat(),
                          'root': None, 'responseRevision': None, 'data': None,
                          'diagnostics': [{'code': exc.code, 'message': str(exc), 'severity': 'error'}]}, ensure_ascii=False))
        return 2
    root = args.root.resolve()
    envelope = {'apiVersion': {'major': API_MAJOR, 'minor': API_MINOR}, 'operation': args.operation,
                'observedAt': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'), 'root': str(root), 'diagnostics': []}
    deadline = Deadline(30 if getattr(args, 'check_code', False) else 10)
    try:
        if args.api_major != API_MAJOR: raise WorkItemError('INSPECT_UNSUPPORTED_VERSION', '仅支持 Inspect 2')
        op = args.operation
        if op == 'workspace': data = workspace(root)
        elif op == 'items':
            listed = list_items(root, args.status)
            data = _page(listed['items'], args.offset, args.limit)
            envelope['diagnostics'] = listed['diagnostics']
        elif op == 'projection': data = projection(root, args.slug, args.view, args.task, deadline=deadline.end)
        elif op == 'document': data = document(root, args.slug, args.path, args.document_revision, deadline.end)
        elif op == 'artifacts': data = artifacts(root, args.slug, args.offset, args.limit, deadline.end)
        elif op == 'verification': data = verification(root, args.slug, args.check_code, deadline.end)
        elif op == 'handoff': data = handoff(root, args.slug, args.check_code, deadline.end)
        elif op == 'evidence':
            from workbench.work_items.evidence import get_evidence
            data = get_evidence(item_path(root, args.slug), task_id=args.task, evidence_id=args.id)
        elif op == 'workflow': data = workflow(root)
        elif op == 'runs': data = runs(root, args.item, args.offset, args.limit)
        elif op == 'run': data = run(root, args.id)
        else: data = search(root, args.query, args.repo, args.status, args.offset, args.limit, deadline)
        deadline.check()
        envelope.update(status='partial' if envelope['diagnostics'] else 'ok', data=data, responseRevision=digest(data))
        encoded = json.dumps(envelope, ensure_ascii=False)
        if len(encoded.encode()) > MAX_RESPONSE: raise WorkItemError('INSPECT_LIMIT', '响应超过上限')
        print(encoded); return 0
    except (OSError, ValueError, KeyError, TypeError) as exc:
        envelope.update(status='error', data=None, responseRevision=None, diagnostics=[{'code': getattr(exc, 'code', 'INSPECT_INVALID_DATA'), 'message': str(exc), 'severity': 'error'}])
        print(json.dumps(envelope, ensure_ascii=False)); return 1

if __name__ == '__main__': raise SystemExit(main())
