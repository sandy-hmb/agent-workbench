"""Request-scoped facts and shared decisions for CLI and IDE consumers."""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
import time
from pathlib import Path
from urllib.parse import unquote, urlsplit
from workbench.work_items.documents import ROLES, LINK, content_roles, instructions, parse_tasks, source
from workbench.git import _git, git_fingerprint, branch_fingerprint
from workbench.work_items.store import WorkItemError, digest, item_area, item_path, load_state, read_evidence, read_bytes, safe_path, text_digest
from workbench.workspace.model import load_workspace, repository_path, resolve_repository

STAGE_SKILLS = {'item.design': 'workspace-item-design', 'item.implement': 'workspace-execute-plan',
                'item.verify': 'workspace-verify', 'item.submit-test': 'workspace-submit-test',
                'item.complete': 'workspace-verify', 'item.prepare-branch': 'workspace-execute-plan'}


def recorded_task_status(state: dict, task_id: str, evidence: dict | None) -> str:
    if evidence is None:
        return 'pending'
    if evidence.get('taskId') != task_id or evidence.get('iteration') != state['iteration']:
        raise WorkItemError('EVIDENCE_SUBJECT_MISMATCH', '任务证据归属不一致')
    return 'completed' if evidence['result'] == 'passed' else evidence['result']


def repository_roots(root: Path, state: dict, names=None) -> dict[str, Path]:
    result = {}
    workspace = load_workspace(root) if (root / '.workspace/workspace.json').exists() else None
    for binding in state['bindings']:
        name = binding['repository']
        if names is not None and name not in names:
            continue
        if workspace:
            result[name] = repository_path(workspace, resolve_repository(workspace.repositories, name))
        else:
            path = root if name == root.name else safe_path(root.parent, name)
            if not path.is_dir():
                raise WorkItemError('REPOSITORY_UNAVAILABLE', f'仓库不存在：{name}')
            result[name] = path
    return result


class WorkItemQuery:
    def __init__(self, root: Path, slug: str, *, state: dict | None = None, deadline: float | None = None):
        self.root = Path(root).resolve()
        self.path = item_path(self.root, slug)
        self.state = state if state is not None else load_state(self.path)
        self._documents = {}
        self._evidence = {}
        self._tasks = None
        self._code = {}
        self._roots = {}
        self._references = {}
        self.deadline = deadline

    def remaining(self, maximum: float = 25) -> float:
        if self.deadline is None:
            return maximum
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise WorkItemError('INSPECT_TIMEOUT', '查询超过时间限制')
        return min(maximum, remaining)

    def document(self, role: str) -> dict | None:
        self.remaining()
        if role not in self._documents:
            self._documents[role] = source(self.path, role)
        return self._documents[role]

    def documents(self) -> list[dict]:
        documents = [{k: v for k, v in value.items() if k != 'content'} for role in ROLES if (value := self.document(role))]
        for role in content_roles(self.state):
            self.review_revision(role)
        documents.extend({'role': 'references', 'path': name, 'documentRevision': value['revision']} for name, value in self._references.items())
        return documents

    def review_revision(self, role: str) -> str | None:
        document = self.document(role)
        if document is None:
            return None
        pending = [(document['path'], document['content'])]
        versions = {document['path']: document['documentRevision']}
        while pending:
            origin, text = pending.pop()
            for link in LINK.findall(text):
                parsed = urlsplit(link.strip('<>'))
                if parsed.scheme or not parsed.path:
                    continue
                target = (self.path / origin).parent / unquote(parsed.path)
                # Only design/reference attachments are part of the approval bundle;
                # delivery instructions and generated views have their own ownership.
                try:
                    relative = target.resolve().relative_to(self.path.resolve()).as_posix()
                except ValueError:
                    raise WorkItemError('DOCUMENT_LINK_INVALID', '内容链接越出当前 WorkItem')
                if not relative.startswith('references/') or relative in versions:
                    continue
                self.remaining()
                if len(versions) >= 100:
                    raise WorkItemError('DOCUMENT_LIMIT', '审阅附件数量超过上限')
                if relative not in self._references:
                    data = read_bytes(safe_path(self.path, relative), 1024 * 1024)
                    self._references[relative] = {'revision': text_digest(data), 'content': data.decode('utf-8')}
                reference = self._references[relative]
                versions[relative] = reference['revision']
                pending.append((relative, reference['content']))
        return digest(versions)

    def roots(self, names=None) -> dict[str, Path]:
        selected = set(names) if names is not None else {b['repository'] for b in self.state['bindings']}
        missing = selected - self._roots.keys()
        if missing:
            self._roots.update(repository_roots(self.root, self.state, missing))
        return {name: self._roots[name] for name in sorted(selected)}

    def evidence(self, identifier: str | None) -> dict | None:
        self.remaining()
        if identifier is None:
            return None
        if identifier not in self._evidence:
            self._evidence[identifier] = read_evidence(self.path, identifier)
        return self._evidence[identifier]

    def tasks(self) -> list[dict]:
        if self._tasks is None:
            document = self.document('plan')
            self._tasks = parse_tasks(document['content']) if document else []
            names = {b['repository'] for b in self.state['bindings']}
            if any(task['repository'] not in names for task in self._tasks):
                raise WorkItemError('PLAN_REPOSITORY_INVALID', '任务必须声明 WorkItem 中的唯一目标仓')
        return self._tasks

    def review_state(self) -> dict:
        required = ['change'] if self.state['documentKind'] == 'change' else ['requirements', 'design', 'plan']
        if (self.document('plan') or self.state['currentTasks']) and 'plan' not in required:
            required.append('plan')
        result = {}
        for role in required:
            current, review = self.document(role), self.state['reviews'].get(role)
            result[role] = ('missing' if current is None else 'pending' if review is None or review['decision'] == 'needs-review'
                            else 'changed' if self.review_revision(role) != review['revision'] else 'approved')
        if self.state['currentTasks'] and {t['id'] for t in self.tasks()} != set(self.state['currentTasks']):
            result['plan'] = 'changed' if self.document('plan') else 'missing'
        return result

    def basis_revision(self) -> str:
        versions = {}
        for role in content_roles(self.state):
            doc = self.document(role)
            if doc:
                review = self.state['reviews'].get(role, {})
                actual = self.review_revision(role)
                versions[role] = review.get('semanticRevision', actual) if review.get('revision') == actual else actual
        return digest(versions)

    def task_states(self) -> list[dict]:
        results = []
        for task in self.tasks():
            if task['id'] not in self.state['currentTasks']:
                continue
            recorded = self.state['tasks'].get(task['id'])
            status = 'pending'
            evidence_id = None
            if task['id'] in self.state['currentTasks'] and recorded:
                evidence_id = recorded.get('evidence')
                if recorded['definitionRevision'] != task['documentRevision']:
                    status = 'changed'
                elif evidence_id:
                    evidence = self.evidence(evidence_id)
                    status = recorded_task_status(self.state, task['id'], evidence)
            results.append({**{k: v for k, v in task.items() if k != 'body'}, 'status': status,
                            'evidenceId': evidence_id, 'completed': status == 'completed'})
        blocked = self.execution_blocks()
        completed = {task['id'] for task in results if task['completed']}
        for task in results:
            task['waitingFor'] = [identifier for identifier in task['dependencies'] if identifier not in completed]
            task['executionBlocked'] = task['id'] in blocked['tasks']
        return results

    def branches(self, names=None) -> list[dict]:
        values = []
        for binding in self.state['bindings']:
            name = binding['repository']
            if names is not None and name not in names:
                continue
            path = self.roots({name})[name]
            try:
                current = _git(path, ['branch', '--show-current'], self.remaining(5), 65536).decode().strip()
                status = 'matched' if current == binding['workBranch'] else 'mismatched'
            except (OSError, ValueError, subprocess.TimeoutExpired):
                current, status = None, 'unavailable'
            values.append({**binding, 'absolutePath': str(path), 'currentBranch': current, 'state': status})
        return values

    def code_state(self, names=None) -> dict[str, str]:
        roots = self.roots(names)
        for name, path in roots.items():
            if name not in self._code:
                self._code[name] = git_fingerprint(path, excluded=('docs/development', '.workspace') if path == self.root else (),
                                                   timeout=self.remaining(), max_untracked_files=10000, max_bytes=64 * 1024 * 1024)
        return {name: self._code[name] for name in roots}

    def execution_blocks(self) -> dict:
        opened = [row for row in self.state['blockers'] if row['status'] == 'open']
        tasks = self.tasks()
        item_blocked = any(not row['taskIds'] for row in opened)
        affected = {task['id'] for task in tasks} if item_blocked else {identifier for row in opened for identifier in row['taskIds']}
        while True:
            dependencies = {task['id'] for task in tasks if set(task['dependencies']) & affected}
            if dependencies <= affected:
                break
            affected.update(dependencies)
        return {'itemBlocked': item_blocked, 'tasks': affected, 'records': opened}

    def verification(self, *, check_code: bool = False, browse: bool = False) -> dict:
        record = self.evidence(self.state['verification'])
        result = {'recordedResult': record['result'] if record else 'unknown', 'evidenceId': self.state['verification'],
                  'applicability': 'not_checked', 'verificationScope': record.get('verificationScope', '') if record else '',
                  'pendingExternalChecks': self.state['externalChecks'], 'repositoryStates': []}
        if record is None:
            result['applicability'] = 'missing'
        elif record['iteration'] != self.state['iteration'] or record['basisRevision'] != self.basis_revision():
            result['applicability'] = 'invalid'
        elif check_code:
            try:
                sources = {}
                if browse:
                    current = {}
                    for row in self.branches():
                        name = row['repository']
                        if row['state'] == 'matched':
                            current[name] = git_fingerprint(self.roots()[name], excluded=('docs/development', '.workspace') if self.roots()[name] == self.root else (), timeout=self.remaining(), max_untracked_files=10000, max_bytes=64 * 1024 * 1024)
                            sources[name] = 'worktree'
                        else:
                            current[name] = branch_fingerprint(self.roots()[name], row['workBranch'], timeout=self.remaining(5))
                            sources[name] = 'local-branch'
                else:
                    current = self.code_state()
                result['repositoryStates'] = [{'repository': name, 'state': 'matched' if value == record['codeState'].get(name) else 'changed',
                                               'source': sources.get(name, 'worktree'), 'currentFingerprint': value,
                                               'recordedFingerprint': record['codeState'].get(name)} for name, value in current.items()]
                result['applicability'] = 'valid' if current == record['codeState'] else 'invalid'
            except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
                result.update(applicability='unknown', reason=str(exc))
        return result

    def decision(self, *, execution=False, check_code=False, repository_names=None) -> dict:
        reviews, tasks = self.review_state(), self.task_states()
        blocks = []
        if self.state['lifecycle'] != 'active':
            return {'currentStage': None, 'executionDecision': 'COMPLETE' if self.state['lifecycle'] == 'done' else 'BLOCKED',
                    'blockers': ['ITEM_' + self.state['lifecycle'].upper()] if self.state['lifecycle'] != 'done' else [], 'readyTasks': [], 'nextActions': [], 'canComplete': False}
        if any(value != 'approved' for value in reviews.values()):
            blocks.append('REVIEW_CLASSIFICATION_REQUIRED' if 'changed' in reviews.values() else 'REVIEW_REQUIRED')
        completed = {t['id'] for t in tasks if t['completed']}
        execution_blocks = self.execution_blocks()
        ready = [t['id'] for t in tasks if not t['completed'] and set(t['dependencies']) <= completed and t['id'] not in execution_blocks['tasks']]
        if execution and any(branch['state'] != 'matched' for branch in self.branches(repository_names)):
            blocks.append('WORKING_BRANCH_MISMATCH')
        verification = self.verification(check_code=check_code)
        external = [c for c in self.state['externalChecks'] if c['status'] not in {'passed', 'waived'}]
        if blocks:
            stage = 'item.design' if any(x.startswith('REVIEW') for x in blocks) else 'item.prepare-branch'
        elif tasks and len(completed) != len(tasks):
            stage = 'item.implement'
        elif verification['recordedResult'] != 'passed' or verification['applicability'] in {'missing', 'invalid', 'unknown'}:
            stage = 'item.verify' if tasks or self.state['verification'] else 'item.implement'
        elif external:
            stage = 'item.submit-test'
        else:
            stage = 'item.complete'
        if execution_blocks['itemBlocked'] or (execution_blocks['records'] and not ready):
            blocks.append('EXECUTION_BLOCKED')
        can_complete = not blocks and not execution_blocks['records'] and stage == 'item.complete' and verification['applicability'] == 'valid'
        next_actions = [{'stage': stage, 'runbook': '.agents/skills/' + STAGE_SKILLS[stage] + '/SKILL.md'}]
        return {'currentStage': stage, 'executionDecision': 'BLOCKED' if blocks else 'RUN' if stage == 'item.implement' else 'COMPLETE',
                'blockers': blocks, 'readyTasks': ready if not blocks else [], 'nextActions': next_actions, 'canComplete': can_complete}

    def summary(self, *, check_code=False) -> dict:
        tasks = self.task_states()
        result = {'slug': self.state['slug'], 'title': self.state['title'], 'description': self.state['summary'],
                  'path': str(self.path.relative_to(self.root)), 'status': self.state['lifecycle'], 'riskTier': self.state['risk'],
                  'activity': self.state['activity'], 'iteration': self.state['iteration'], 'stateRevision': self.state['stateRevision'],
                  'updatedAt': self.state['updatedAt'], 'repositoryBindings': self.state['bindings'],
                  'progress': {'completed': sum(t['completed'] for t in tasks), 'total': len(tasks)},
                  'reviews': self.review_state(), 'verification': self.verification(check_code=check_code),
                  'documents': self.documents(), 'delivery': self.state['delivery'],
                  'executionBlockers': self.state['blockers'], 'cancellation': self.state['cancellation']}
        result.update(self.decision(check_code=check_code))
        return result

    def task(self, task_id: str) -> dict:
        task = next((t for t in self.tasks() if t['id'] == task_id), None)
        if task is None:
            raise WorkItemError('TASK_NOT_FOUND', task_id)
        current = next((t for t in self.task_states() if t['id'] == task_id), None)
        if current is None:
            raise WorkItemError('TASK_UNREGISTERED', '当前任务尚未经过计划审阅')
        return {**task, 'status': current['status'], 'completed': current['completed'],
                'executionBlocked': current['executionBlocked'], 'waitingFor': current['waitingFor'],
                'instructionContext': {'rules': instructions(self.root, self.roots({task['repository']})[task['repository']], task)}}

    def continuation(self, *, task_id=None, check_code=False) -> dict:
        selected = self.task(task_id) if task_id else None
        decision = self.decision(execution=task_id is not None, check_code=check_code,
                                 repository_names={selected['repository']} if selected else None)
        if selected and (selected['executionBlocked'] or selected['waitingFor']):
            reason = 'EXECUTION_BLOCKED' if selected['executionBlocked'] else 'TASK_DEPENDENCY_PENDING'
            decision.update(executionDecision='BLOCKED', blockers=list(dict.fromkeys([*decision['blockers'], reason])), canComplete=False)
        result = {'itemSlug': self.state['slug'], 'stateRevision': self.state['stateRevision'], 'iteration': self.state['iteration'], **decision}
        if task_id:
            result['selectedTask'] = selected
            result['directDependencies'] = [task for task in self.task_states() if task['id'] in selected['dependencies']]
            result['repositoryContext'] = self.branches({selected['repository']})
        else:
            result.update(activity=self.state['activity'], riskTier=self.state['risk'], lifecycle=self.state['lifecycle'],
                          reviews=self.review_state(), verification=self.verification(check_code=check_code),
                          documents=self.documents(), repositoryContext=self.branches())
            if decision['readyTasks']:
                result['currentTask'] = next(task for task in self.task_states() if task['id'] == decision['readyTasks'][0])
        self.assert_unchanged()
        result['executionBlockers'] = self.execution_blocks()['records']
        return result

    def assert_unchanged(self) -> None:
        if load_state(self.path)['stateRevision'] != self.state['stateRevision']:
            raise WorkItemError('STATE_CHANGED', '读取期间状态发生变化')
        for role, document in self._documents.items():
            current = source(self.path, role)
            if (current or {}).get('documentRevision') != (document or {}).get('documentRevision'):
                raise WorkItemError('DOCUMENT_CHANGED', '读取期间文档发生变化')
        for relative, reference in self._references.items():
            if text_digest(read_bytes(safe_path(self.path, relative), 1024 * 1024)) != reference['revision']:
                raise WorkItemError('DOCUMENT_CHANGED', '读取期间附件发生变化')


@dataclass(frozen=True)
class WorkItemMatch:
    slug: str
    path: Path
    status: str
    repository: str
    base_branch: str

@dataclass(frozen=True)
class WorkItemSummary:
    slug: str
    path: Path
    status: str
    repositories: tuple
    branches: tuple
    base_branches: tuple
    updated: str


def item_summary(workspace, item: Path) -> WorkItemSummary:
    state = load_state(item)
    return WorkItemSummary(state['slug'], item, state['lifecycle'], tuple(b['repository'] for b in state['bindings']),
                          tuple((b['repository'], b['workBranch']) for b in state['bindings']),
                          tuple((b['repository'], b['baseBranch']) for b in state['bindings']), state['updatedAt'])


def list_items(root: Path, status: str | None = None) -> dict:
    area = item_area(root)
    items, diagnostics, unsupported = [], [], 0
    if area.exists():
        for path in sorted(area.iterdir()):
            if not path.is_dir() or path.is_symlink():
                continue
            if not (path / 'state.json').exists():
                unsupported += 1
                continue
            try:
                state = load_state(path)
                if status is None or state['lifecycle'] == status:
                    records = {}
                    def evidence(identifier):
                        if identifier not in records:
                            records[identifier] = read_evidence(path, identifier)
                        return records[identifier]
                    completed = sum(recorded_task_status(state, task_id, evidence(identifier) if (identifier := state['tasks'][task_id].get('evidence')) else None) == 'completed' for task_id in state['currentTasks'])
                    batch = evidence(state['verification']) if state['verification'] else None
                    items.append({'slug': state['slug'], 'title': state['title'], 'status': state['lifecycle'],
                                  'iteration': state['iteration'], 'riskTier': state['risk'], 'activity': state['activity'],
                                  'path': str(path.relative_to(Path(root).resolve())), 'stateRevision': state['stateRevision'],
                                  'updatedAt': state['updatedAt'], 'repositoryBindings': state['bindings'],
                                  'progress': {'completed': completed, 'total': len(state['currentTasks']), 'basis': 'recorded'},
                                  'cancellation': state['cancellation'],
                                  'openBlockerCount': sum(row['status'] == 'open' for row in state['blockers']),
                                  'verification': {'evidenceId': state['verification'], 'recordedResult': batch['result'] if batch else 'unknown', 'applicability': 'not_checked'}})
            except (OSError, ValueError) as exc:
                diagnostics.append({'code': getattr(exc, 'code', 'ITEM_INVALID'), 'severity': 'error', 'message': str(exc), 'path': str(path)})
    if unsupported:
        diagnostics.append({'code': 'ITEM_FORMAT_UNSUPPORTED', 'severity': 'info', 'message': f'{unsupported} 个旧记录保持原状且未加载；请在新目录初始化'})
    return {'items': items, 'diagnostics': diagnostics}


def resolve_items(root: Path, repository: str, branch: str) -> list[WorkItemMatch]:
    result = []
    for item in list_items(root)['items']:
        for binding in item['repositoryBindings']:
            if binding['repository'] == repository and binding['workBranch'] == branch:
                result.append(WorkItemMatch(item['slug'], item_path(root, item['slug']), item['status'], repository, binding['baseBranch']))
    return result
