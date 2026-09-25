"""Parse current Markdown content; runtime state never comes from checkboxes."""
from __future__ import annotations

import re
from pathlib import Path
from workbench.work_items.store import WorkItemError, read_bytes, safe_path, text_digest

ROLES = {'change': 'change.md', 'requirements': 'requirements.md', 'design': 'design.md', 'plan': 'plan.md',
         'readme': 'README.md', 'verification': 'verification.md'}
TASK_HEADING = re.compile(r'^### (T\d{2,})\s+(.+?)\s*$')
LINK = re.compile(r'\[[^\]]*\]\(([^)]+)\)')
KINDS = {'行为', '声明式', '持久化'}


def source(item: Path, role: str) -> dict | None:
    path = safe_path(item, ROLES[role])
    if not path.exists():
        return None
    data = read_bytes(path, 1024 * 1024)
    try:
        text = data.decode('utf-8')
    except UnicodeError as exc:
        raise WorkItemError('DOCUMENT_INVALID', f'文档必须是 UTF-8：{path}') from exc
    return {'role': role, 'path': ROLES[role], 'documentRevision': text_digest(data), 'content': text,
            'bytes': len(data), 'lineCount': len(text.splitlines())}


def content_roles(state: dict) -> list[str]:
    return ['change', 'plan'] if state['documentKind'] == 'change' else ['requirements', 'design', 'plan']


def parse_tasks(text: str, path: str = 'plan.md') -> list[dict]:
    lines = text.splitlines()
    starts = []
    fenced = False
    for i, line in enumerate(lines):
        if line.lstrip().startswith(('```', '~~~')):
            fenced = not fenced
        if fenced:
            continue
        if match := TASK_HEADING.fullmatch(line):
            starts.append((i, match.group(1), match.group(2)))
        elif re.match(r'^\s*-\s*\[[ xX]\].*\bT\d+', line):
            raise WorkItemError('PLAN_FORMAT_INVALID', '新版任务使用 ### T01 标题，不维护复选框')
    ids = [item[1] for item in starts]
    if any(identifier != f'T{int(identifier[1:]):02d}' or int(identifier[1:]) == 0 for identifier in ids):
        raise WorkItemError('TASK_ID_INVALID', '任务编号必须使用 T01、T02 等规范形式')
    if len(set(ids)) != len(ids):
        raise WorkItemError('TASK_ID_DUPLICATE', '任务编号重复')
    result = []
    for index, (start, identifier, title) in enumerate(starts):
        end = starts[index + 1][0] if index + 1 < len(starts) else len(lines)
        for j in range(start + 1, end):
            if re.match(r'^#{1,2}\s', lines[j]):
                end = j
                break
        body = '\n'.join(lines[start:end]).rstrip()
        def field(name: str, required: bool = True) -> str:
            values = re.findall(r'^\s*' + re.escape(name) + r'：\s*(.*?)\s*$', body, re.M)
            if len(values) > 1 or (required and (not values or not values[0])):
                raise WorkItemError('PLAN_FIELD_INVALID', f'{identifier} 缺少或重复 {name}')
            return values[0].strip('`') if values else ''
        basis = field('依据')
        dependency_text = field('依赖')
        dependencies = re.findall(r'\bT\d{2,}\b', dependency_text)
        if dependency_text != '无' and (not dependencies or re.search(r'T\d+\s*[-~至]\s*T\d+', dependency_text)):
            raise WorkItemError('PLAN_DEPENDENCY_INVALID', f'{identifier} 依赖必须显式列出任务编号')
        repository, kind = field('目标仓'), field('验证性质')
        if kind not in KINDS:
            raise WorkItemError('PLAN_VALIDATION_INVALID', f'{identifier} 验证性质无效')
        deliverables = [{'kind': k, 'path': p, 'repository': repository}
                        for k, p in re.findall(r'^\s*-\s*(Create|Modify|Test|Delete|Verify)：\s*`([^`]+)`', body, re.M)]
        if not deliverables or '通过条件' not in body:
            raise WorkItemError('PLAN_INCOMPLETE', f'{identifier} 缺少交付路径或通过条件')
        result.append({'id': identifier, 'title': title, 'path': path, 'startLine': start + 1, 'endLine': end,
                       'body': body, 'documentRevision': text_digest(body), 'repository': repository,
                       'validationKind': kind, 'dependencies': list(dict.fromkeys(dependencies)),
                       'references': LINK.findall(body), 'basis': basis, 'deliverables': deliverables})
    known = set(ids)
    for task in result:
        if set(task['dependencies']) - known or task['id'] in task['dependencies']:
            raise WorkItemError('PLAN_DEPENDENCY_INVALID', f"{task['id']} 引用了未知任务或自身")
    by_id = {task['id']: task for task in result}
    visiting, visited = set(), set()
    def visit(identifier):
        if identifier in visiting:
            raise WorkItemError('PLAN_DEPENDENCY_CYCLE', '任务依赖存在循环')
        if identifier in visited:
            return
        visiting.add(identifier)
        for dependency in by_id[identifier]['dependencies']:
            visit(dependency)
        visiting.remove(identifier)
        visited.add(identifier)
    for identifier in ids:
        visit(identifier)
    return result


def instructions(root: Path, repository: Path, task: dict | None = None, *, paths=None, source_instruction=None) -> dict:
    """Discover scoped pointers without loading rule prose into the response."""
    root, repository = Path(root).resolve(), Path(repository).resolve()
    targets = list(dict.fromkeys([*(paths or []), *[item['path'] for item in (task or {}).get('deliverables', [])]]))
    configured = (root / '.workspace/workspace.json').exists()
    candidates = [(root / 'AGENTS.md', 1, root, True),
                  (root / '.workspace/AGENTS.md', 2, root, configured),
                  (repository / 'AGENTS.md', 3, repository, True)]
    if source_instruction and Path(source_instruction).name == 'AGENTS.md':
        declared = safe_path(repository, source_instruction)
        if declared != repository / 'AGENTS.md':
            candidates.append((declared, 4, declared.parent, True))
    for relative in targets:
        target = safe_path(repository, relative)
        scope = target if target.is_dir() else target.parent
        for parent in [*reversed(scope.parents), scope]:
            if parent != repository and parent.is_relative_to(repository):
                candidates.append((parent / 'AGENTS.md', 4, parent, False))
    rules, diagnostics, seen = [], [], set()
    for candidate, level, scope, required in sorted(candidates, key=lambda row: (row[1], len(row[2].parts), str(row[0]))):
        if candidate in seen:
            continue
        seen.add(candidate)
        try:
            safe_path(candidate.parent, candidate.name)
            if not candidate.exists():
                if required:
                    diagnostics.append({'code': 'RULE_SOURCE_MISSING', 'severity': 'error', 'path': str(candidate), 'message': '必需规范入口缺失，读取并确认规范后才能实施'})
                continue
            data = read_bytes(candidate, 1024 * 1024)
            data.decode('utf-8')
            rules.append({'path': str(candidate), 'scope': str(scope), 'level': level, 'documentRevision': text_digest(data)})
        except (OSError, ValueError, UnicodeError) as exc:
            diagnostics.append({'code': 'RULE_SOURCE_UNREADABLE', 'severity': 'error', 'path': str(candidate), 'message': str(exc)})
    return {'rules': rules, 'targets': [{'repository': str(repository), 'paths': targets}],
            'scopedRulesPending': not bool(targets), 'diagnostics': diagnostics}
