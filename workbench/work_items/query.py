"""Request-scoped facts and shared decisions for CLI and IDE consumers."""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
import time
import re
from pathlib import Path
from urllib.parse import unquote, urlsplit
from workbench.work_items.documents import ROLES, LINK, content_roles, instructions, parse_tasks, predecessor, source
from workbench.git import _git, git_fingerprint, branch_fingerprint, legacy_git_fingerprint, legacy_branch_fingerprint
from workbench.work_items.store import WorkItemError, digest, item_area, item_path, load_state, read_evidence, read_bytes, safe_path, state_path, text_digest
from workbench.workspace.model import load_workspace, repository_path, resolve_repository
from workbench.workspace.paths import context_file, workspace_file
from workbench.work_items.references import validate_workflow_check

STAGE_SKILLS = {'item.design': 'workspace-item-design', 'item.implement': 'workspace-execute-plan',
                'item.verify': 'workspace-verify', 'item.submit-test': 'workspace-submit-test',
                'item.complete': 'workspace-verify', 'item.prepare-branch': 'workspace-execute-plan'}
ARTIFACT_SUFFIXES = {'.md', '.txt', '.json', '.sql', '.csv'}
ARTIFACT_READ_LIMIT = 1024 * 1024


def recorded_task_status(state: dict, task_id: str, evidence: dict | None) -> str:
    if evidence is None:
        return 'pending'
    if (evidence.get('taskId') != task_id and task_id not in evidence.get('taskCoverage', {})) or evidence.get('iteration') != state['iteration']:
        raise WorkItemError('EVIDENCE_SUBJECT_MISMATCH', '任务证据归属不一致')
    return 'completed' if evidence['result'] == 'passed' else evidence['result']


def repository_roots(root: Path, state: dict, names=None) -> dict[str, Path]:
    result = {}
    workspace = load_workspace(root) if workspace_file(root).exists() else None
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
        if binding.get('checkout'):
            if workspace:
                from workbench.workspace.model import validate_repository_state
                validate_repository_state(workspace, resolve_repository(workspace.repositories, name), allow_missing_remote=False)
            result[name] = approved_checkout(result[name], binding['checkout'])
    return result


def approved_checkout(canonical: Path, checkout: str) -> Path:
    """Accept only a real worktree of the registered repository, never another clone."""
    candidate = Path(checkout)
    if not candidate.is_absolute() or candidate.is_symlink() or candidate.resolve() != candidate:
        raise WorkItemError('CHECKOUT_INVALID', 'checkout 必须为无符号链接的绝对 Git 根路径')
    safe_path(candidate, '.git')
    def git(path, *args):
        return _git(path, list(args), 5, 1024 * 1024).decode().strip()
    if Path(git(candidate, 'rev-parse', '--show-toplevel')).resolve() != candidate:
        raise WorkItemError('CHECKOUT_INVALID', 'checkout 不是独立 Git 根目录')
    original_common = git(canonical, 'rev-parse', '--path-format=absolute', '--git-common-dir')
    actual_common = git(candidate, 'rev-parse', '--path-format=absolute', '--git-common-dir')
    registered = git(canonical, 'worktree', 'list', '--porcelain', '-z').split('\0')
    if (Path(original_common).resolve() != Path(actual_common).resolve() or
            'worktree ' + str(candidate) not in registered or
            git(canonical, 'remote', 'get-url', 'origin') != git(candidate, 'remote', 'get-url', 'origin')):
        raise WorkItemError('CHECKOUT_INVALID', 'checkout 不属于登记仓库或 remote 不一致')
    return candidate


def repository_context(root: Path, repository: Path, paths=None, task=None, repository_name=None) -> dict:
    configured = workspace_file(root).exists()
    facts = []
    source_instruction = None
    if configured:
        workspace = load_workspace(root)
        repo = resolve_repository(workspace.repositories, repository_name or repository.name)
        source_instruction = repo.source_instruction
        facts = [(context_file(root), 'workspace-context', True),
                 (safe_path(root / '.workspace', repo.instruction), 'repository-profile', True)]
        if source_instruction and Path(source_instruction).name != 'AGENTS.md':
            facts.append((safe_path(repository, source_instruction), 'repository-reference', True))
    facts.append((repository / 'CONTEXT.md', 'repository-context', False))
    context = instructions(root, repository, task, paths=paths, source_instruction=source_instruction)
    context['facts'] = []
    for path, kind, required in facts:
        try:
            safe_path(path.parent, path.name)
            if not path.exists() and not required:
                continue
            data = read_bytes(path, 1024 * 1024)
            context['facts'].append({'path': str(path), 'kind': kind, 'documentRevision': text_digest(data)})
        except (OSError, ValueError) as exc:
            context['diagnostics'].append({'code': 'FACT_SOURCE_UNAVAILABLE', 'severity': 'warning', 'path': str(path), 'message': str(exc)})
    return context


def combine_contexts(contexts: list[dict]) -> dict:
    result = {'rules': [], 'facts': [], 'diagnostics': [], 'targets': [], 'scopedRulesPending': False}
    for key in ('rules', 'facts', 'diagnostics'):
        seen = set()
        for context in contexts:
            for row in context.get(key, []):
                identity = (row['path'], row.get('code'))
                if identity not in seen:
                    result[key].append(row); seen.add(identity)
    result['rules'].sort(key=lambda row: (row['level'], len(Path(row['scope']).parts), row['path']))
    result['targets'] = [row for context in contexts for row in context['targets']]
    result['scopedRulesPending'] = any(context['scopedRulesPending'] for context in contexts)
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
        self._artifact_documents = None
        self._artifact_collection_revision = None
        self._external_checks = None
        self._task_progress = None
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

    def documents(self, *, include_artifacts=False) -> list[dict]:
        documents = [{k: v for k, v in value.items() if k != 'content'} for role in ROLES if (value := self.document(role))]
        for role in content_roles(self.state):
            self.review_revision(role)
        documents.extend({'role': 'references', 'path': name, 'documentRevision': value['revision']} for name, value in self._references.items())
        folder = safe_path(self.path, 'artifacts')
        if include_artifacts and self._artifact_documents is None:
            self._artifact_documents = []
            self._load_artifact_index()
        if include_artifacts:
            documents.extend(self._artifact_documents or [])
        return documents

    def _load_artifact_index(self) -> None:
        if self._artifact_documents is not None:
            return
        folder = safe_path(self.path, 'artifacts')
        rows, fingerprints = [], []
        for count, path in enumerate(folder.rglob('*') if folder.is_dir() else []):
            self.remaining()
            if count >= 10000:
                raise WorkItemError('DOCUMENT_LIMIT', '交付物条目超过上限')
            relative = path.relative_to(self.path).as_posix()
            safe_path(self.path, relative)
            if not path.is_file() or path.suffix not in ARTIFACT_SUFFIXES:
                continue
            size = path.stat().st_size
            readable = size <= ARTIFACT_READ_LIMIT
            fingerprints.append((relative, size, path.stat().st_mtime_ns))
            rows.append({'role': 'artifacts', 'path': relative, 'bytes': size,
                         'readable': readable, 'documentRevision': None,
                         **({'reason': '文件超过在线文档读取上限，可从交付目录打开'} if not readable else {})})
        self._artifact_documents = rows
        self._artifact_collection_revision = digest(fingerprints)

    def artifact_page(self, offset: int = 0, limit: int = 50) -> dict:
        if offset < 0 or not 1 <= limit <= 200:
            raise WorkItemError('DOCUMENT_ARGUMENT_INVALID', '交付物分页参数无效')
        self._load_artifact_index()
        items = self._artifact_documents or []
        return {'items': items[offset:offset + limit],
                'counts': {'total': len(items)},
                'collectionRevision': self._artifact_collection_revision,
                'page': {'offset': offset, 'limit': limit, 'total': len(items),
                          'hasMore': offset + limit < len(items)}}

    def artifact_summary(self) -> dict:
        self._load_artifact_index()
        items = self._artifact_documents or []
        return {'total': len(items), 'readable': sum(item['readable'] for item in items),
                'unreadable': sum(not item['readable'] for item in items),
                'collectionRevision': self._artifact_collection_revision}

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
            if self.state['risk'] == 'major' and not self.state['currentTasks'] and not self.approved_document('design'):
                return []
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
            prior = predecessor(role) if self.state['risk'] == 'major' else None
            result[role] = ('not_ready' if prior and result.get(prior) != 'approved'
                            else 'missing' if current is None else 'pending' if review is None or review['decision'] == 'needs-review'
                            else 'changed' if self.review_revision(role) != review['revision'] else 'approved')
        if self.state['currentTasks'] and {t['id'] for t in self.tasks()} != set(self.state['currentTasks']):
            if result.get('plan') != 'not_ready':
                result['plan'] = 'changed' if self.document('plan') else 'missing'
        return result

    def approved_document(self, role: str) -> bool:
        document = self.document(role)
        review = self.state['reviews'].get(role)
        prior = predecessor(role) if self.state['risk'] == 'major' else None
        return bool(document and review and review['decision'] == 'approved' and
                    review['revision'] == self.review_revision(role) and
                    (prior is None or self.approved_document(prior)))

    def document_progress(self) -> dict:
        """Derive the current document and the only missing template allowed now."""
        reviews = self.review_state()
        current = next((role for role, value in reviews.items() if value != 'approved'), None)
        return {'currentDocument': current,
                'nextDocument': current if current and reviews[current] == 'missing' else None}

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

    @staticmethod
    def _recorded_at(value: dict | None) -> datetime:
        if not value or not isinstance(value.get('recordedAt'), str):
            return datetime.min
        try:
            parsed = datetime.fromisoformat(value['recordedAt'].replace('Z', '+00:00'))
            return parsed.astimezone(timezone.utc).replace(tzinfo=None) if parsed.tzinfo else parsed
        except ValueError:
            return datetime.min

    def task_progress(self) -> list[dict]:
        """Project approved and pending plan tasks without changing execution gates."""
        if self._task_progress is not None:
            return self._task_progress
        document = self.document('plan')
        planned = parse_tasks(document['content']) if document else []
        names = {binding['repository'] for binding in self.state['bindings']}
        if any(task['repository'] not in names for task in planned):
            raise WorkItemError('PLAN_REPOSITORY_INVALID', '任务必须声明 WorkItem 中的唯一目标仓')
        by_id = {task['id']: task for task in planned}
        task_ids = set(by_id) | set(self.state['currentTasks'])
        latest, latest_passed = {}, {}
        for task_id in task_ids:
            stored = self.state['tasks'].get(task_id, {})
            for field, destination in [('latestEvidence', latest), ('latestPassedEvidence', latest_passed)]:
                if stored.get(field) and stored.get('iteration') == self.state['iteration']:
                    destination[task_id] = self.evidence(stored[field])
        for identifier in reversed(self.state['evidence']):
            legacy = {task_id for task_id in task_ids if 'latestEvidence' not in self.state['tasks'].get(task_id, {})}
            if not legacy:
                break
            if legacy <= latest.keys() & latest_passed.keys():
                break
            record = self.evidence(identifier)
            for task_id in [record.get('taskId'), *record.get('taskCoverage', {})]:
                if task_id not in legacy or record.get('iteration') != self.state['iteration']:
                    continue
                if task_id not in latest:
                    latest[task_id] = record
                if record.get('result') == 'passed' and task_id not in latest_passed:
                    latest_passed[task_id] = record
        current_states = {task['id']: task for task in self.task_states()}
        blocked = self.execution_blocks()
        completed = {task_id for task_id, row in current_states.items() if row['completed']}
        results = []
        for task_id in sorted(task_ids, key=lambda value: int(value[1:])):
            task = by_id.get(task_id)
            recorded = self.state['tasks'].get(task_id, {})
            definition_matches = bool(task and recorded.get('definitionRevision') == task['documentRevision'])
            if task_id not in self.state['currentTasks']:
                approval = 'pending'
            elif not definition_matches:
                approval = 'changed'
            else:
                approval = 'approved'
            current = current_states.get(task_id)
            current_evidence = self.evidence(recorded.get('evidence')) if recorded.get('evidence') else None
            last_evidence = latest.get(task_id)
            invalidation = recorded.get('verificationInvalidation')
            if approval == 'changed' and last_evidence and not invalidation:
                invalidation = {'evidenceId': last_evidence['id'], 'recordedAt': self.state['updatedAt'],
                                'reason': '当前计划任务定义尚未批准'}
            elif not current_evidence and last_evidence and not invalidation:
                invalidation = {'evidenceId': last_evidence['id'], 'recordedAt': self.state['updatedAt'],
                                'reason': '历史验证记录未被当前任务引用，失效原因未记录'}
            if approval != 'approved':
                verification = 'needs-revalidation' if last_evidence else 'unverified'
            elif current_evidence:
                verification = 'passed' if current_evidence['result'] == 'passed' else 'failed'
            elif last_evidence:
                verification = 'needs-revalidation'
            else:
                verification = 'unverified'
            execution = recorded.get('execution')
            if execution:
                execution_status, execution_source = execution['status'], 'recorded'
            elif task_id in latest_passed:
                execution_status, execution_source = 'implemented', 'verification-evidence'
            else:
                execution_status, execution_source = 'unknown', 'none'
            if execution_status == 'unknown':
                execution_applicability = 'unknown'
            elif invalidation and self._recorded_at(execution) <= self._recorded_at(invalidation):
                execution_applicability = 'needs-reassessment'
            else:
                execution_applicability = 'current'
            waiting = list(task.get('dependencies', [])) if task else []
            waiting = [dependency for dependency in waiting if dependency not in completed]
            blockers = [
                {key: row[key] for key in ('id', 'reason', 'owner', 'condition')}
                for row in blocked['records']
                if not row['taskIds'] or task_id in row['taskIds']
            ]
            if approval != 'approved':
                next_step = 'approve-plan'
            elif blockers:
                next_step = 'resolve-blocker'
            elif waiting:
                next_step = 'wait-dependencies'
            elif verification == 'passed':
                next_step = 'none'
            elif execution_status == 'implemented':
                next_step = 'verify-task'
            else:
                next_step = 'implement-task'
            last_verification = ({'evidenceId': last_evidence['id'], 'recordedAt': last_evidence['recordedAt'],
                                  'result': last_evidence['result']} if last_evidence else None)
            records = []
            if execution:
                records.append({'kind': 'execution', 'recordedAt': execution['recordedAt'],
                                'status': execution['status'], 'summary': execution['summary']})
            if last_verification:
                records.append({'kind': 'verification', **last_verification})
            last_record = max(records, key=self._recorded_at) if records else None
            base = {key: value for key, value in (task or {}).items() if key != 'body'}
            results.append({**base, 'id': task_id, 'title': base.get('title', '计划中已移除的已登记任务'),
                            'approvalStatus': approval, 'executionStatus': execution_status,
                            'executionApplicability': execution_applicability, 'executionSource': execution_source,
                            'verificationStatus': verification, 'lastImplementation': execution,
                            'lastVerification': last_verification, 'lastRecord': last_record,
                            'invalidation': invalidation, 'blockers': blockers, 'waitingFor': waiting,
                            'nextStep': next_step, 'status': current['status'] if current else ('changed' if approval == 'changed' else 'pending-approval'),
                            'evidenceId': recorded.get('evidence'), 'completed': verification == 'passed' and approval == 'approved',
                            'executionBlocked': bool(blockers)})
        self._task_progress = results
        return results

    def progress_summary(self) -> dict:
        tasks = self.task_progress()
        approved = [task for task in tasks if task['approvalStatus'] == 'approved']
        return {'completed': sum(task['completed'] for task in approved), 'total': len(self.state['currentTasks']),
                'plannedTotal': len(tasks), 'pendingApproval': sum(task['approvalStatus'] == 'pending' for task in tasks),
                'changedApproval': sum(task['approvalStatus'] == 'changed' for task in tasks),
                'execution': {status: sum(task['executionStatus'] == status for task in tasks)
                              for status in ('unknown', 'not-started', 'in-progress', 'implemented')},
                'verification': {status: sum(task['verificationStatus'] == status for task in tasks)
                                 for status in ('unverified', 'passed', 'failed', 'needs-revalidation')}}

    def compact_task_progress(self) -> list[dict]:
        fields = ('id', 'title', 'approvalStatus', 'executionStatus', 'executionApplicability',
                  'verificationStatus', 'lastRecord', 'invalidation', 'blockers', 'waitingFor',
                  'nextStep', 'evidenceId', 'completed')
        return [{key: task.get(key) for key in fields} for task in self.task_progress()]

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
                                                   timeout=self.remaining(), max_untracked_files=10000, max_bytes=256 * 1024 * 1024)
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

    def artifact_states(self, record: dict) -> list[dict]:
        identifiers = {self.state['tasks'][task_id].get('evidence') for task_id in self.state['currentTasks']}
        records = [self.evidence(identifier) for identifier in self.state['evidence'] if identifier in identifiers]
        records.append(record)
        references = {}
        for evidence in records:
            if evidence['result'] != 'passed':
                continue
            for reference in evidence.get('artifactRefs', []):
                references[reference['path']] = reference
        results = []
        for reference in references.values():
            self.remaining()
            row = {'path': reference['path'], 'state': 'unknown'}
            try:
                path = safe_path(self.path, reference['path'])
                if not path.exists():
                    row['state'] = 'missing'
                else:
                    data = read_bytes(path)
                    row['state'] = 'matched' if text_digest(data) == reference['sha256'] and len(data) == reference['bytes'] else 'changed'
            except (OSError, ValueError) as exc:
                row['reason'] = str(exc)
            results.append(row)
        return results

    def external_checks(self) -> list[dict]:
        if self._external_checks is None:
            self._external_checks = []
            for check in self.state['externalChecks']:
                self.remaining()
                effective = dict(check)
                if self.state['lifecycle'] in {'active', 'paused'}:
                    try:
                        validate_workflow_check(self.root, self.state, check)
                    except WorkItemError as exc:
                        effective.update(status='pending', recordedStatus=check['status'], reason=str(exc))
                self._external_checks.append(effective)
        return self._external_checks

    def verification(self, *, check_code: bool = False, browse: bool = False) -> dict:
        record = self.evidence(self.state['verification'])
        result = {'recordedResult': record['result'] if record else 'unknown', 'evidenceId': self.state['verification'],
                  'applicability': 'not_checked', 'verificationScope': record.get('verificationScope', '') if record else '',
                  'pendingExternalChecks': self.external_checks(), 'repositoryStates': []}
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
                            current[name] = self.code_state({name})[name]
                            sources[name] = 'worktree'
                        else:
                            current[name] = branch_fingerprint(self.roots()[name], row['workBranch'], timeout=self.remaining(5))
                            sources[name] = 'local-branch'
                else:
                    current = self.code_state()
                if record.get('codeIdentity') != 'content-v1':
                    current = {name: (legacy_branch_fingerprint(path, next(b['workBranch'] for b in self.state['bindings'] if b['repository'] == name), timeout=self.remaining(5))
                                     if sources.get(name) == 'local-branch' else
                                     legacy_git_fingerprint(path, excluded=('docs/development', '.workspace') if path == self.root else (), timeout=self.remaining(), max_untracked_files=10000, max_bytes=64 * 1024 * 1024))
                               for name, path in self.roots().items()}
                result['repositoryStates'] = [{'repository': name, 'state': 'matched' if value == record['codeState'].get(name) else 'changed',
                                               'source': sources.get(name, 'worktree'), 'currentFingerprint': value,
                                               'recordedFingerprint': record['codeState'].get(name)} for name, value in current.items()]
                result['applicability'] = 'valid' if current == record['codeState'] else 'invalid'
                result['artifactStates'] = self.artifact_states(record)
                if any(row['state'] != 'matched' for row in result['artifactStates']):
                    result['applicability'] = 'invalid'
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
        external = [c for c in verification['pendingExternalChecks'] if c['status'] not in {'passed', 'waived'}]
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
        if stage == 'item.submit-test' and any(check.get('recordedStatus') for check in external):
            blocks.append('WORKFLOW_CHECK_INVALID')
        can_complete = not blocks and not execution_blocks['records'] and stage == 'item.complete' and verification['applicability'] == 'valid'
        next_actions = [{'stage': stage, 'runbook': '.agents/skills/' + STAGE_SKILLS[stage] + '/SKILL.md'}]
        document_progress = self.document_progress()
        if self.state['risk'] == 'major' and document_progress['nextDocument']:
            next_actions[0]['document'] = document_progress['nextDocument']
        if 'WORKFLOW_CHECK_INVALID' in blocks:
            next_actions[0]['runbook'] = '.agents/skills/workspace-item-workflow/SKILL.md'
        return {'currentStage': stage, 'executionDecision': 'BLOCKED' if blocks else 'RUN',
                'blockers': blocks, 'readyTasks': ready if not blocks else [], 'nextActions': next_actions, 'canComplete': can_complete}

    def summary(self, *, check_code=False) -> dict:
        tasks = self.task_states()
        result = {'slug': self.state['slug'], 'title': self.state['title'], 'description': self.state['summary'],
                  'path': str(self.path.relative_to(self.root)), 'status': self.state['lifecycle'], 'riskTier': self.state['risk'],
                  'activity': self.state['activity'], 'iteration': self.state['iteration'], 'stateRevision': self.state['stateRevision'],
                  'updatedAt': self.state['updatedAt'], 'repositoryBindings': self.state['bindings'],
                  'documentPolicy': self.state.get('documentPolicy', 'legacy'),
                  'progress': self.progress_summary(),
                  'reviews': self.review_state(), 'verification': self.verification(check_code=check_code),
                  'documents': self.documents(), 'artifactSummary': self.artifact_summary(),
                  'delivery': self.state['delivery'],
                  'executionBlockers': self.state['blockers'], 'cancellation': self.state['cancellation']}
        if self.state['risk'] == 'major':
            result['documentProgress'] = self.document_progress()
        result.update(self.decision(check_code=check_code))
        return result

    def task(self, task_id: str) -> dict:
        task = next((t for t in self.tasks() if t['id'] == task_id), None)
        if task is None:
            raise WorkItemError('TASK_NOT_FOUND', task_id)
        current = next((t for t in self.task_progress() if t['id'] == task_id and t['approvalStatus'] == 'approved'), None)
        if current is None:
            raise WorkItemError('TASK_UNREGISTERED', '当前任务尚未经过计划审阅')
        return {**task, **{key: current[key] for key in ('status', 'completed', 'executionBlocked', 'waitingFor',
                'approvalStatus', 'executionStatus', 'executionApplicability', 'verificationStatus', 'lastRecord',
                'invalidation', 'blockers', 'nextStep')},
                'instructionContext': repository_context(self.root, self.roots({task['repository']})[task['repository']], task=task, repository_name=task['repository'])}

    def requirement_sources(self, task: dict) -> list[dict]:
        from workbench.work_items import focused
        if focused.enabled(self.state):
            return focused.sources(self, task)
        identifiers = list(dict.fromkeys(re.findall(r'(?<![A-Za-z0-9])(?:R\d+(?:\.\d+)*|D\d+)(?![A-Za-z0-9])', task['basis'])))
        sources = []
        for identifier in identifiers:
            role = 'design' if identifier.startswith('D') else ('change' if self.state['documentKind'] == 'change' else 'requirements')
            document = self.document(role)
            if not document:
                continue
            lines = document['content'].splitlines()
            headings = [(i, match) for i, line in enumerate(lines) if (match := re.match(r'^(#{1,6})\s+(.+)', line))]
            start, end = 0, len(lines)
            found = False
            for pos, (line, match) in enumerate(headings):
                if re.match(re.escape(identifier) + r'(?![A-Za-z0-9.])', match[2]):
                    start = line; found = True
                    end = next((following for following, heading in headings[pos + 1:] if len(heading[1]) <= len(match[1])), len(lines))
                    break
            sources.append({'id': identifier, 'path': document['path'], 'documentRevision': document['documentRevision'],
                            'startLine': start + 1, 'endLine': max(start + 1, end), 'located': found})
        # Explicit links may address detailed contracts whose headings are not R/D IDs.
        for reference in task.get('references', []):
            if not any(row['path'] == reference.split('#', 1)[0] for row in sources):
                sources.append({'reference': reference})
        return sources

    def continuation(self, *, task_id=None, check_code=False, repository=None, paths=None) -> dict:
        selected = self.task(task_id) if task_id else None
        if selected and repository and repository != selected['repository']:
            raise WorkItemError('CONTEXT_REPOSITORY_MISMATCH', '查询仓库与任务目标仓不一致')
        if not selected and repository and repository not in {row['repository'] for row in self.state['bindings']}:
            raise WorkItemError('CONTEXT_REPOSITORY_MISMATCH', '仓库不属于当前工作项')
        names = {selected['repository']} if selected else {repository} if repository else None
        if paths and names is None and len(self.state['bindings']) > 1:
            raise WorkItemError('CONTEXT_REPOSITORY_REQUIRED', '按路径查询时明确指定目标仓')
        decision = self.decision(execution=task_id is not None, check_code=check_code,
                                 repository_names={selected['repository']} if selected else None)
        if selected and (selected['executionBlocked'] or selected['waitingFor']):
            reason = 'EXECUTION_BLOCKED' if selected['executionBlocked'] else 'TASK_DEPENDENCY_PENDING'
            decision.update(executionDecision='BLOCKED', blockers=list(dict.fromkeys([*decision['blockers'], reason])), canComplete=False)
        result = {'itemSlug': self.state['slug'], 'stateRevision': self.state['stateRevision'], 'iteration': self.state['iteration'], **decision}
        if task_id:
            context = selected['instructionContext']
            if paths:
                context = repository_context(self.root, self.roots(names)[selected['repository']], paths=paths, task=selected, repository_name=selected['repository'])
            result['instructionContext'] = context
            result['selectedTask'] = {key: selected[key] for key in (
                'id', 'path', 'documentRevision', 'startLine', 'endLine', 'body', 'status', 'approvalStatus',
                'executionStatus', 'executionApplicability', 'verificationStatus', 'lastRecord', 'invalidation',
                'nextStep', 'executionBlocked', 'waitingFor')}
            result['sources'] = self.requirement_sources(selected)
            result['directDependencies'] = [{key: task[key] for key in (
                'id', 'status', 'executionStatus', 'verificationStatus', 'evidenceId')}
                for task in self.task_progress() if task['id'] in selected['dependencies']]
            result['repositoryContext'] = self.branches({selected['repository']})
            result['progress'] = self.progress_summary()
            result['taskProgress'] = [task for task in self.compact_task_progress() if task['id'] == task_id]
        else:
            result.update(activity=self.state['activity'], riskTier=self.state['risk'], lifecycle=self.state['lifecycle'],
                          reviews=self.review_state(), verification=self.verification(check_code=check_code),
                          documents=self.documents(), repositoryContext=self.branches(names),
                          progress=self.progress_summary(), taskProgress=self.compact_task_progress())
            if self.state['risk'] == 'major':
                result['documentProgress'] = self.document_progress()
            result['instructionContext'] = combine_contexts([repository_context(self.root, path, paths=paths, repository_name=name) for name, path in self.roots(names).items()])
            if decision['readyTasks']:
                candidate = next(task for task in self.task_states() if task['id'] == decision['readyTasks'][0])
                result['currentTask'] = {key: candidate[key] for key in ('id', 'title', 'repository', 'status')}
        rule_errors = [row['code'] for row in result['instructionContext']['diagnostics'] if row['severity'] == 'error']
        if rule_errors and result['currentStage'] in {'item.implement', 'item.prepare-branch'}:
            result.update(executionDecision='BLOCKED', blockers=list(dict.fromkeys([*result['blockers'], *rule_errors])), canComplete=False)
        self.assert_unchanged()
        result['executionBlockers'] = self.execution_blocks()['records']
        from workbench.extensions.runner import _active_overlay
        overlay = _active_overlay(self.root) if workspace_file(self.root).exists() else None
        result['workflowHooks'] = {side: [s.id for s in overlay.stages if getattr(s, side) == result['currentStage']]
                                   if overlay else [] for side in ('before', 'after')}
        result['documentPolicy'] = self.state.get('documentPolicy', 'legacy')
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
            if not state_path(path).is_file():
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
