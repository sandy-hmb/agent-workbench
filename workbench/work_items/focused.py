"""Focused document projections; legacy items never opt in implicitly."""
from __future__ import annotations

import re
from collections import defaultdict
from urllib.parse import urlsplit

from workbench.work_items.store import WorkItemError, safe_path, text_digest

POLICY = 'focused-v1'
README_BUDGET = 1500
TASK_BUDGET = 15000
IDENTIFIER = r'[RD]\d+(?:\.\d+)*'


def enabled(state):
    return state.get('documentPolicy') == POLICY


def headings(text):
    lines = text.splitlines()
    result, fence = [], None
    for i, line in enumerate(lines):
        marker = re.match(r'^\s*(`{3,}|~{3,})', line)
        if marker:
            if fence is None:
                fence = marker[1][0]
            elif fence == marker[1][0]:
                fence = None
            continue
        if fence is None and (match := re.match(r'^(#{1,6})\s+(.+?)\s*#*$', line)):
            result.append((i, len(match[1]), match[2]))
    return [(i, next((j for j, level, _ in result[n + 1:] if level <= depth), len(lines)), title)
            for n, (i, depth, title) in enumerate(result)]


def section(text, title):
    return next(('\n'.join(text.splitlines()[a + 1:b]).strip()
                 for a, b, name in headings(text) if name == title), '')


def identifiers(basis):
    pattern = rf'(?<![A-Za-z0-9])({IDENTIFIER})(?:\s*[-~至–]\s*({IDENTIFIER}))?(?![A-Za-z0-9.])'
    result = []
    for match in re.finditer(pattern, basis):
        first, last = match.groups()
        if not last:
            result.append(first)
            continue
        left, right = first[1:].split('.'), last[1:].split('.')
        if (first[0] != last[0] or left[:-1] != right[:-1] or
                len(left) != len(right) or not 0 <= int(right[-1]) - int(left[-1]) < 50):
            raise WorkItemError('BASIS_RANGE_INVALID', f'无效或过宽的依据范围：{match[0]}')
        for value in range(int(left[-1]), int(right[-1]) + 1):
            result.append(first[0] + '.'.join([*left[:-1], str(value).zfill(len(left[-1]))]))
    if not result:
        raise WorkItemError('BASIS_MISSING', '依据必须明确引用 R/D 编号')
    return list(dict.fromkeys(result))


def pointer(document, start, end, **extra):
    body = '\n'.join(document['content'].splitlines()[start:end])
    return {'path': document['path'], 'documentRevision': document['documentRevision'],
            'startLine': start + 1, 'endLine': end, 'fragmentRevision': text_digest(body.encode()),
            'located': True, **extra}


def sources(reader, task):
    rows = []
    role = 'change' if reader.state['documentKind'] == 'change' else 'requirements'
    for name in (role, 'design'):
        document = reader.document(name)
        if document:
            for a, b, title in headings(document['content']):
                if title in {'目标与边界', '关键约束'}:
                    rows.append(pointer(document, a, b, kind='global', title=title))
    for identifier in identifiers(task['basis']):
        document = reader.document('design' if identifier.startswith('D') else role)
        matches = [(a, b) for a, b, title in headings(document['content'])
                   if re.match(re.escape(identifier) + r'(?![A-Za-z0-9.])', title)] if document else []
        if len(matches) != 1:
            raise WorkItemError('BASIS_REFERENCE_INVALID', f"{task['id']} 的 {identifier} 缺失或不唯一")
        rows.append(pointer(document, *matches[0], id=identifier))
    merged = []
    for row in sorted(rows, key=lambda x: (x['path'], x['startLine'], -x['endLine'])):
        if merged and row['path'] == merged[-1]['path'] and row['startLine'] <= merged[-1]['endLine']:
            prior = merged[-1]
            doc = next(reader.document(name) for name in (role, 'design')
                       if reader.document(name) and reader.document(name)['path'] == row['path'])
            ids = list(dict.fromkeys([*prior.get('ids', []), *([prior['id']] if 'id' in prior else []),
                                     *([row['id']] if 'id' in row else [])]))
            merged[-1] = pointer(doc, prior['startLine'] - 1, max(prior['endLine'], row['endLine']), ids=ids)
        else:
            merged.append(row)
    for reference in task.get('references', []):
        path = urlsplit(reference).path
        if path and not urlsplit(reference).scheme and not any(x.get('path') == path or x.get('reference') == reference for x in merged):
            safe_path(reader.path, path)
            if not safe_path(reader.path, path).is_file():
                raise WorkItemError('BASIS_REFERENCE_INVALID', f'附件不存在：{reference}')
            merged.append({'reference': reference})
    return merged


def diagnostics(reader):
    rows = []
    document = reader.document('plan')
    if document:
        from workbench.work_items.documents import parse_tasks
        for task in parse_tasks(document['content']):
            try:
                sources(reader, task)
            except WorkItemError as exc:
                rows.append({'severity': 'error', 'code': exc.code, 'message': str(exc), 'taskId': task['id']})
    from workbench.context_measure import estimate_tokens
    readme = reader.document('readme')
    if readme and estimate_tokens(readme['content'])['estTokens'] > README_BUDGET:
        rows.append({'severity': 'warning', 'code': 'README_BUDGET', 'message': 'README 超出软预算，精简重复而不隐藏风险'})
    from workbench.work_items.documents import LINK
    for role in ('change', 'requirements', 'design', 'plan'):
        doc = reader.document(role)
        if not doc:
            continue
        for link in LINK.findall(doc['content']):
            parsed = urlsplit(link.strip('<>'))
            if parsed.scheme or not parsed.path:
                continue
            try:
                if not safe_path(reader.path, parsed.path).is_file():
                    raise WorkItemError('DOCUMENT_LINK_INVALID', f'文件不存在：{link}')
            except WorkItemError as exc:
                rows.append({'severity': 'error', 'code': exc.code, 'message': str(exc), 'path': doc['path']})
    for path in reader.path.glob('*.md'):
        if path.name not in {'README.md', 'change.md', 'requirements.md', 'design.md', 'plan.md'}:
            rows.append({'severity': 'warning', 'code': 'DOCUMENT_EXTRA', 'message': '根目录额外文档应合并或放入固定交付目录', 'path': path.name})
    runs = safe_path(reader.path, 'artifacts/runs')
    if runs.exists():
        from workbench.extensions.model import load_manifest
        from workbench.workspace.paths import workspace_file
        from workbench.extensions.item_outputs import output_path
        for directory in runs.iterdir():
            safe_path(reader.path, directory.relative_to(reader.path).as_posix())
            manifest_path = reader.root / '.workspace/extensions' / directory.name / 'workspace-extension.json'
            if not workspace_file(reader.root).exists() or not manifest_path.is_file():
                rows.append({'severity': 'warning', 'code': 'OUTPUT_OWNER_UNAVAILABLE', 'path': directory.name,
                             'message': '无法核对运行产物所属扩展，保留现场并核查来源'})
                continue
            manifest = load_manifest(manifest_path)
            declared = [row for values in (manifest.item_outputs or {}).values() for row in values]
            for path in directory.rglob('*'):
                safe_path(reader.path, path.relative_to(reader.path).as_posix())
                if not path.is_file():
                    continue
                relative = path.relative_to(directory).parts
                try:
                    matches = len(relative) >= 2 and any(
                        output_path(reader.path, row, relative[0]) == path for row in declared if row['writeMode'] == 'append-run')
                except (ValueError, WorkItemError):
                    matches = False
                if not matches:
                    rows.append({'severity': 'error', 'code': 'OUTPUT_UNDECLARED', 'path': path.relative_to(reader.path).as_posix(),
                                 'message': '运行产物不符合所属扩展声明；不自动删除'})
    return rows


def render(reader):
    state = reader.state
    def one(value):
        return ' '.join(str(value).splitlines()).replace('|', '\\|')
    role = 'change' if state['documentKind'] == 'change' else 'requirements'
    main = (reader.document(role) or {}).get('content', '')
    design = (reader.document('design') or {}).get('content', '')
    lines = [f"# {one(state['title'])}", '', '<!-- Generated by Kit. Edit source documents, not this summary. -->', '',
             f"- 状态：{state['lifecycle']}", f"- 迭代：{state['iteration']}；风险：{state['risk']}",
             f"- 目标：{one(state['summary'])}"]
    for title in ('目标与边界', '关键约束', '本轮变更', '待决策'):
        value = section(main, title)
        if value:
            lines += ['', '## ' + title, '', value]
    choice = section(design, '选定方案') or section(main, '方案与工作项')
    if choice:
        lines += ['', '## 选定方案', '', choice]
    progress = reader.progress_summary()
    decision = reader.decision()
    lines += ['', '## 进展与验证摘要', '',
              f"- 当前阶段：{decision['currentStage']}；下一步：{decision['executionDecision']}。",
              '- 审阅：' + '；'.join(f'{role} {status}' for role, status in reader.review_state().items()),
              f"- 当前通过 {progress['completed']}/{progress['total']}；待审批 {progress['pendingApproval']}。",
              f"- 上次整体验证：{reader.verification()['recordedResult']}；当前代码适用性：读取时另行核对。"]
    if state.get('cancellation'):
        lines.append('- 取消原因：' + one(state['cancellation']['reason']))
    grouped = defaultdict(list)
    for task in reader.task_progress():
        if task['completed']:
            continue
        reason = (task.get('invalidation') or {}).get('reason') or task['nextStep']
        grouped[reason].append(task['id'])
    for reason, tasks in list(grouped.items())[:8]:
        lines.append(f"- {', '.join(tasks)}：{one(reason)}")
    if len(grouped) > 8:
        lines.append(f'- 另有 {len(grouped) - 8} 组任务事项，详见任务查询。')
    for block in reader.execution_blocks()['records']:
        lines.append(f"- 阻塞 {one(block['id'])}：{one(block['reason'])}；{one(block['owner'])}；解除：{one(block['condition'])}")
    checks = reader.external_checks()
    opened = [c for c in checks if c['status'] not in {'passed', 'waived'}]
    lines += ['', '## 待外部验收', '', f'- 未关闭 {len(opened)}/{len(checks)} 项；不等于部署或业务验收已完成。']
    for check in sorted(opened, key=lambda c: (c['status'] != 'failed', c['id']))[:8]:
        lines.append(f"- {one(check['id'])} / {one(check['requirement'])}：{one(check['description'])}；{one(check['owner'])}；{check['status']}" +
                     (f"；{one(check['reason'])}" if check.get('reason') else ''))
    if len(opened) > 8:
        lines.append(f'- 另有 {len(opened) - 8} 项未关闭，详见 flow 查询。')
    lines += ['', '## 交付状态', '', '| 仓库 | 版本 | 提测 | 部署 | 验收 |', '|---|---|---|---|---|']
    for binding in state['bindings']:
        row = state['delivery'].get(binding['repository'], {})
        lines.append('| ' + ' | '.join(one(x) for x in [binding['repository'], row.get('version', '未记录'),
                     row.get('submission', '未执行'), row.get('deployment', '未确认'), row.get('acceptance', '未确认')]) + ' |')
    lines += ['', '## 阅读入口', '']
    for name in ('change', 'requirements', 'design', 'plan'):
        if reader.document(name):
            lines.append(f'- [{name}]({name}.md)')
    from workbench.work_items.documents import LINK
    for link in LINK.findall(section(main, '交付入口'))[:8]:
        if urlsplit(link).scheme or not urlsplit(link).path:
            continue
        path = safe_path(reader.path, urlsplit(link).path)
        if path.is_file():
            lines.append(f'- [{one(link)}]({link})')
    slug = state['slug']
    lines += [f'- 任务与风险：`kit.py inspect projection {slug} --view task|flow`（分别查询）',
              f'- 全部产物：`kit.py inspect artifacts {slug}`；历史与证据按需查询。']
    return '\n'.join(lines) + '\n'
