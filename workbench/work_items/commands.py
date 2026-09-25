"""Controlled changes to one WorkItem; evidence precedes the atomic state commit."""
from __future__ import annotations

import json
import re
import shutil
import copy
from contextlib import nullcontext
from datetime import datetime
from pathlib import Path

from workbench.work_items.documents import content_roles
from workbench.work_items.query import WorkItemQuery, repository_roots
from workbench.git import _git
from workbench.work_items.store import (WorkItemError, DIGEST_RE, SCHEMA_VERSION, atomic_write, canonical, check_revision,
                           digest, item_lock, item_path, load_state, read_bytes, read_json, safe_path,
                           save_state, stamp, write_evidence)
from workbench.workspace.model import effective_branch_policy, load_workspace, resolve_repository
from workbench.validation import object_fields, text, array


def create(root: Path, slug: str, *, title: str, repositories: list[str], summary='', document_kind='change',
           risk='normal', activity='develop', branch: str | None = None) -> dict:
    root = Path(root).resolve()
    path = item_path(root, slug)
    if path.exists():
        raise WorkItemError('ITEM_EXISTS', '目标需求目录已存在，不覆盖原记录')
    if not repositories or document_kind not in {'change', 'requirements'} or risk not in {'light', 'normal', 'major'}:
        raise WorkItemError('ITEM_INVALID', '仓库、文档类型或风险无效')
    if risk == 'major' and document_kind != 'requirements':
        raise WorkItemError('ITEM_INVALID', '重大风险需要复杂文档模式')
    bindings = []
    workspace = load_workspace(root) if (root / '.workspace/workspace.json').exists() else None
    for name in dict.fromkeys(repositories):
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*', name):
            raise WorkItemError('REPOSITORY_INVALID', name)
        base = None
        if workspace:
            repository = resolve_repository(workspace.repositories, name)
            name = repository.path
            base = effective_branch_policy(workspace, repository).work_base
        location = repository_roots(root, {'bindings': [{'repository': name}]})[name]
        current = _git(location, ['branch', '--show-current'], 5, 65536).decode().strip()
        work_branch = branch or current
        if not work_branch:
            raise WorkItemError('BRANCH_REQUIRED', '当前为 detached HEAD，请明确指定工作分支')
        _git(location, ['check-ref-format', '--branch', work_branch], 5, 65536)
        bindings.append({'repository': name, 'workBranch': work_branch, 'baseBranch': base or current or work_branch})
    path.mkdir(parents=True)
    state = {'schemaVersion': SCHEMA_VERSION, 'slug': slug, 'title': title or slug, 'summary': summary,
             'lifecycle': 'active', 'activity': activity, 'risk': risk, 'documentKind': document_kind,
             'iteration': 'i01', 'bindings': bindings, 'reviews': {}, 'tasks': {}, 'currentTasks': [], 'maxTaskNumber': 0,
             'verification': None, 'evidence': [], 'delivery': {}, 'externalChecks': [], 'history': [],
             'updatedAt': stamp(), 'stateRevision': '', 'blockers': [], 'cancellation': None, 'changes': []}
    filename = document_kind + '.md'
    initial = (f'# {title or slug}\n\n## 目标\n\n{summary}\n\n## 范围与验收\n\n'
               '## 方案与工作项\n' if document_kind == 'change' else
               f'# {title or slug} 需求\n\n**目标：** {summary}\n\n## 范围与边界\n\n## 功能需求与验收\n\n### R1 可观察行为\n\n## 非目标\n')
    atomic_write(path / filename, initial.encode())
    state = save_state(path, state)
    render(root, slug)
    return {'path': str(path.relative_to(root)), 'state': state}


def _finish(root: Path, slug: str, state: dict, **extra) -> dict:
    result = {'committed': True, 'state': state, **extra}
    try:
        render(root, slug)
        result['summaryUpdated'] = True
    except (OSError, ValueError) as exc:
        result.update(summaryUpdated=False, diagnostic=f'状态已提交，摘要待重建：{exc}')
    if 'id' in extra:
        try:
            decision = WorkItemQuery(root, slug).decision()
            result['nextStep'] = {'stage': decision['currentStage'], 'taskId': next(iter(decision['readyTasks']), None),
                                  'blockers': decision['blockers'], 'stateRevision': state['stateRevision']}
        except (OSError, ValueError) as exc:
            result['nextStep'] = {'blockers': [str(exc)], 'stateRevision': state['stateRevision']}
    return result


def review(root: Path, slug: str, *, decision: str, reason: str, expected_revision: str,
           roles: list[str] | None = None, affected_tasks: list[str] | None = None) -> dict:
    if decision not in {'approved', 'unchanged', 'needs-review'} or not reason.strip():
        raise WorkItemError('REVIEW_INVALID', '必须记录审阅结论和依据')
    directory = item_path(root, slug)
    with item_lock(directory):
        state = load_state(directory)
        check_revision(state, expected_revision)
        if state['lifecycle'] != 'active':
            raise WorkItemError('ITEM_INACTIVE', '先恢复或开启新迭代')
        reader = WorkItemQuery(root, slug, state=state)
        roles = roles or [role for role in content_roles(state) if reader.document(role)]
        if not roles or any(role not in content_roles(state) for role in roles):
            raise WorkItemError('REVIEW_INVALID', '审阅角色无效')
        tasks = reader.tasks()
        old_ids = set(state['currentTasks'])
        if 'plan' in roles and decision != 'needs-review':
            for task in tasks:
                if task['id'] not in old_ids and int(task['id'][1:]) <= state['maxTaskNumber']:
                    raise WorkItemError('TASK_ID_REUSED', f"任务编号不得复用：{task['id']}")
        invalidated = set(affected_tasks or [])
        if invalidated - set(state['tasks']):
            raise WorkItemError('REVIEW_INVALID', '受影响任务不存在')
        for role in roles:
            document = reader.document(role)
            if document is None:
                raise WorkItemError('DOCUMENT_MISSING', role)
            old = state['reviews'].get(role)
            predecessor = {'design': 'requirements', 'plan': 'design'}.get(role)
            if state['risk'] == 'major' and predecessor and decision == 'approved':
                prior = state['reviews'].get(predecessor)
                prior_doc = reader.document(predecessor)
                if not prior or prior['decision'] != 'approved' or not prior_doc or prior['revision'] != reader.review_revision(predecessor):
                    raise WorkItemError('REVIEW_ORDER_INVALID', f'先审阅 {predecessor}')
            if decision == 'unchanged' and (old is None or old['decision'] != 'approved'):
                raise WorkItemError('REVIEW_INVALID', '没有可沿用的已批准内容')
            document_revision = reader.review_revision(role)
            changed = old is not None and old['revision'] != document_revision
            if affected_tasks is None and (decision == 'needs-review' or (decision != 'unchanged' and changed and role in {'change', 'requirements', 'design'})):
                invalidated.update(old_ids)
            state['reviews'][role] = {'revision': document_revision,
                                      'semanticRevision': old['semanticRevision'] if decision == 'unchanged' else document_revision,
                                      'decision': 'approved' if decision == 'unchanged' else decision,
                                      'reason': reason, 'recordedAt': stamp()}
        if 'plan' in roles and decision != 'needs-review':
            for task in tasks:
                old = state['tasks'].get(task['id'])
                evidence = old.get('evidence') if old else None
                if old and old['definitionRevision'] != task['documentRevision'] and decision != 'unchanged':
                    invalidated.add(task['id'])
                state['tasks'][task['id']] = {'definitionRevision': task['documentRevision'], 'iteration': state['iteration'], 'evidence': evidence}
            state['currentTasks'] = [task['id'] for task in tasks]
            state['maxTaskNumber'] = max([state['maxTaskNumber'], *[int(t['id'][1:]) for t in tasks]])
        while True:
            dependents = {t['id'] for t in tasks if set(t['dependencies']) & invalidated}
            if dependents <= invalidated:
                break
            invalidated.update(dependents)
        for task_id in invalidated & state['tasks'].keys():
            state['tasks'][task_id]['evidence'] = None
        if decision != 'unchanged':
            state['verification'] = None
        reader.assert_unchanged()
        state = save_state(directory, state)
    return _finish(root, slug, state)


def validate_checks(payload: dict, validation_kind: str | None) -> None:
    checks = array(payload.get('checks'), '$.checks')
    if not checks:
        raise WorkItemError('EVIDENCE_CHECK_INVALID', '至少需要一项实际检查', '$.checks')
    kinds = set()
    for index, check in enumerate(checks):
        object_fields(check, required={'type','workingDirectory','command','target','result','exitStatus'}, field=f'$.checks[{index}]')
        for field in ('type', 'workingDirectory', 'command', 'target', 'result'):
            if not isinstance(check.get(field), str) or not check[field].strip() or '\0' in check[field]:
                raise WorkItemError('EVIDENCE_CHECK_INVALID', f'检查缺少 {field}', f'$.checks[{index}].{field}')
        if type(check.get('exitStatus')) is not int:
            raise WorkItemError('EVIDENCE_CHECK_INVALID', '缺少实际退出码', f'$.checks[{index}].exitStatus')
        kinds.add(check['type'])
        if check['type'] not in {'测试', '行为检查', '集成', '结构', '迁移', '静态检查', '编译'}:
            raise WorkItemError('EVIDENCE_CHECK_INVALID', '未知检查类型')
        if payload['result'] == 'passed':
            if check['exitStatus'] != 0 or check['result'].lstrip().startswith(('失败', '未执行', '不通过', '未通过')):
                raise WorkItemError('EVIDENCE_CHECK_INVALID', '失败检查不能作为通过证据')
            if check['type'] == '测试' and (type(check.get('executed')) is not int or check['executed'] <= 0 or check.get('skipped') != 0):
                raise WorkItemError('EVIDENCE_CHECK_INVALID', '目标测试必须实际执行且没有跳过')
    allowed = {'行为': {'测试', '行为检查', '集成', '结构', '迁移'},
               '声明式': {'测试', '行为检查', '集成', '结构', '迁移', '静态检查', '编译'},
               '持久化': {'集成', '结构', '迁移'}}
    if payload['result'] == 'passed' and validation_kind and not kinds.intersection(allowed[validation_kind]):
        raise WorkItemError('EVIDENCE_KIND_INSUFFICIENT', '检查不足以证明任务验证性质')


def record(root: Path, slug: str, payload: dict, *, expected_revision: str) -> dict:
    allowed = {'scope', 'taskId', 'recordedAt', 'result', 'codeState', 'checks', 'reviewResult', 'verificationScope', 'artifactRefs'}
    object_fields(payload, allowed=allowed, required={'scope', 'recordedAt', 'result', 'codeState', 'checks'})
    text(payload['scope'], '$.scope')
    text(payload['result'], '$.result')
    if payload.get('scope') not in {'task', 'item'} or payload.get('result') not in {'passed', 'failed'}:
        raise WorkItemError('EVIDENCE_INVALID', '证据范围、结果或字段无效')
    try:
        timestamp = datetime.fromisoformat(payload['recordedAt'].replace('Z', '+00:00'))
        if timestamp.tzinfo is None:
            raise ValueError('timezone')
    except (KeyError, AttributeError, ValueError) as exc:
        raise WorkItemError('EVIDENCE_INVALID', 'recordedAt 必须是带时区的 ISO 时间') from exc
    directory = item_path(root, slug)
    receipt = digest({'input': payload, 'expectedRevision': expected_revision})
    with item_lock(directory):
        state = load_state(directory)
        reader = WorkItemQuery(root, slug, state=state)
        for identifier in reversed(state['evidence']):
            if reader.evidence(identifier).get('receipt') == receipt:
                return _finish(root, slug, state, id=identifier, replayed=True)
        check_revision(state, expected_revision)
        if state['lifecycle'] != 'active':
            raise WorkItemError('ITEM_INACTIVE', '当前需求不是可执行状态')
        task = None
        if payload['scope'] == 'task':
            task = next((t for t in reader.tasks() if t['id'] == payload.get('taskId')), None)
            if not task or task['id'] not in state['currentTasks']:
                raise WorkItemError('TASK_UNREGISTERED', '任务尚未通过计划审阅登记')
        elif payload.get('taskId') is not None:
            raise WorkItemError('EVIDENCE_INVALID', '整体验证不能指定 taskId')
        validate_checks(payload, task['validationKind'] if task else None)
        if payload['result'] == 'passed':
            if any(value != 'approved' for value in reader.review_state().values()):
                raise WorkItemError('REVIEW_REQUIRED', '当前内容尚未批准或需要分类')
            blocked = reader.execution_blocks()
            if blocked['itemBlocked'] or (task and task['id'] in blocked['tasks']) or (not task and blocked['records']):
                raise WorkItemError('EXECUTION_BLOCKED', '先解除适用的开发阻塞，再记录通过结果')
            names = {task['repository']} if task else None
            if any(row['state'] != 'matched' for row in reader.branches(names)):
                raise WorkItemError('WORKING_BRANCH_MISMATCH', '实际工作分支与 WorkItem 不一致')
            if task:
                complete = {t['id'] for t in reader.task_states() if t['completed']}
                if not set(task['dependencies']) <= complete:
                    raise WorkItemError('TASK_DEPENDENCY_PENDING', '任务依赖尚未完成')
                for item in task['deliverables']:
                    target = safe_path(reader.roots({task['repository']})[task['repository']], item['path'])
                    if item['kind'] == 'Delete' and target.exists() or item['kind'] != 'Delete' and not target.is_file():
                        raise WorkItemError('DELIVERABLE_INVALID', f"交付路径不符合声明：{item['path']}")
                if any(item['kind'] == 'Test' for item in task['deliverables']) and not any(check['type'] == '测试' for check in payload['checks']):
                    raise WorkItemError('EVIDENCE_KIND_INSUFFICIENT', 'Test 交付必须有实际测试证据')
            elif any(not t['completed'] for t in reader.task_states()):
                raise WorkItemError('TASKS_INCOMPLETE', '仍有未完成任务')
            if not task and payload.get('reviewResult') != 'passed':
                raise WorkItemError('REVIEW_RESULT_REQUIRED', '整体验证需要实际复核结论')
        code = payload.get('codeState')
        names = {task['repository']} if task else {row['repository'] for row in state['bindings']}
        if not isinstance(code, dict) or set(code) != names or any(not isinstance(v, str) or not DIGEST_RE.fullmatch(v) for v in code.values()):
            raise WorkItemError('EVIDENCE_CODE_INVALID', '代码状态必须对应验证涉及的仓库')
        if payload['result'] == 'passed' and code != reader.code_state(names):
            raise WorkItemError('CODE_CHANGED', '验证代码状态与当前代码不一致')
        for index, artifact in enumerate(array(payload.get('artifactRefs', []), '$.artifactRefs')):
            object_fields(artifact, allowed={'path','sha256','bytes','type'}, required={'path','sha256','bytes','type'}, field=f'$.artifactRefs[{index}]')
            text(artifact['path'], f'$.artifactRefs[{index}].path')
            path = safe_path(directory, artifact['path'])
            from workbench.work_items.store import text_digest
            data = read_bytes(path)
            if artifact.get('sha256') != text_digest(data) or artifact.get('bytes') != len(data):
                raise WorkItemError('ARTIFACT_CHANGED', '交付文件哈希或字节数不匹配')
        value = {**payload, 'schemaVersion': SCHEMA_VERSION, 'itemSlug': slug, 'iteration': state['iteration'],
                 'basisRevision': reader.basis_revision(), 'receipt': receipt}
        if task:
            value['definitionRevision'] = task['documentRevision']
        reader.assert_unchanged()
        identifier = write_evidence(directory, value)
        state['evidence'].append(identifier)
        if task:
            state['tasks'][task['id']]['evidence'] = identifier
            state['verification'] = None
        else:
            state['verification'] = identifier
        state = save_state(directory, state)
    return _finish(root, slug, state, id=identifier, replayed=False)


def complete(root: Path, slug: str, *, expected_revision: str) -> dict:
    directory = item_path(root, slug)
    with item_lock(directory):
        state = load_state(directory)
        check_revision(state, expected_revision)
        reader = WorkItemQuery(root, slug, state=state)
        if not reader.decision(execution=True, check_code=True)['canComplete']:
            raise WorkItemError('ITEM_NOT_READY', '仍有审阅、任务、验证或外部验收未完成')
        reader.assert_unchanged()
        state['lifecycle'] = 'done'
        state = save_state(directory, state)
    return _finish(root, slug, state)


def lifecycle(root: Path, slug: str, value: str, *, expected_revision: str) -> dict:
    if value not in {'active', 'paused'}:
        raise WorkItemError('ITEM_INVALID', '只能暂停或恢复；完成使用 complete')
    directory = item_path(root, slug)
    with item_lock(directory):
        state = load_state(directory)
        check_revision(state, expected_revision)
        if state['lifecycle'] in {'done', 'cancelled'}:
            raise WorkItemError('ITEM_INACTIVE', '已完成 WorkItem 使用 next-iteration')
        if state['lifecycle'] == value:
            return {'changed': False, 'state': state}
        state['lifecycle'] = value
        state = save_state(directory, state)
    return _finish(root, slug, state)


def delivery(root: Path, slug: str, changes: dict, *, expected_revision: str) -> dict:
    object_fields(changes, allowed={'repositories', 'externalChecks'})
    if 'repositories' in changes:
        object_fields(changes['repositories'], field='$.repositories')
    if 'externalChecks' in changes:
        array(changes['externalChecks'], '$.externalChecks')
    directory = item_path(root, slug)
    with item_lock(directory):
        state = load_state(directory)
        check_revision(state, expected_revision)
        before = copy.deepcopy(state)
        if 'externalChecks' in changes:
            checks = changes['externalChecks']
            seen = set()
            previously_open = {row['id'] for row in state['externalChecks'] if row['status'] not in {'passed', 'waived'}}
            for index, check in enumerate(checks):
                fields = {'id', 'requirement', 'description', 'owner', 'status', 'evidence'}
                object_fields(check, allowed=fields, required=fields, field=f'$.externalChecks[{index}]')
                for field, value in check.items():
                    text(value, f'$.externalChecks[{index}].{field}', empty=field == 'evidence')
                if not check['id'] or check['id'] in seen or check['status'] not in {'pending', 'failed', 'passed', 'waived'}:
                    raise WorkItemError('DELIVERY_INVALID', '外部验收编号或状态无效')
                if check['status'] in {'passed', 'waived'} and not check['evidence'].strip():
                    raise WorkItemError('DELIVERY_INVALID', '关闭验收需要实际依据或范围调整理由')
                if state['lifecycle'] in {'done', 'cancelled'} and check['status'] not in {'passed', 'waived'} and check['id'] not in previously_open:
                    raise WorkItemError('ITERATION_REQUIRED', '已结束轮次不能新增或重开未完成验收，请通过 next-iteration 处理')
                seen.add(check['id'])
            if {c['id'] for c in state['externalChecks'] if c['status'] not in {'passed', 'waived'}} - seen:
                raise WorkItemError('DELIVERY_INVALID', '不能静默丢弃未关闭的外部验收')
            state['externalChecks'] = checks
        for name, row in changes.get('repositories', {}).items():
            object_fields(row, allowed={'version','submission','deployment','acceptance','evidence'}, field=f'$.repositories.{name}')
            if name not in {b['repository'] for b in state['bindings']}:
                raise WorkItemError('DELIVERY_INVALID', '仓库交付归属无效')
            if set(row) - {'version', 'submission', 'deployment', 'acceptance', 'evidence'}:
                raise WorkItemError('DELIVERY_INVALID', '仓库交付字段无效')
            text(row.get('evidence'), f'$.repositories.{name}.evidence')
            for field, value in row.items():
                text(value, f'$.repositories.{name}.{field}')
            state['delivery'][name] = {**state['delivery'].get(name, {}), **row}
        if state == before:
            return {'changed': False, 'state': state}
        state = save_state(directory, state)
    return _finish(root, slug, state, changed=True)


def next_iteration(root: Path, slug: str, *, expected_revision: str) -> dict:
    directory = item_path(root, slug)
    with item_lock(directory):
        state = load_state(directory)
        check_revision(state, expected_revision)
        if state['lifecycle'] not in {'done', 'cancelled'}:
            raise WorkItemError('ITERATION_UNFINISHED', '当前迭代尚未完成')
        archive = safe_path(directory, 'history/' + state['iteration'])
        stage = safe_path(directory, 'history/.' + state['iteration'] + '-pending')
        if not archive.exists():
            stage.mkdir(parents=True, exist_ok=True)
            for source in [*directory.glob('*.md'), directory / 'state.json']:
                atomic_write(stage / source.name, read_bytes(safe_path(directory, source.name)))
            for name in ('references', 'artifacts'):
                folder = safe_path(directory, name)
                if folder.exists():
                    for source in folder.rglob('*'):
                        if source.is_file():
                            relative = source.relative_to(directory).as_posix()
                            safe = safe_path(directory, relative)
                            if name == 'artifacts' and (source.suffix not in {'.md', '.txt', '.json', '.sql', '.csv'} or source.stat().st_size > 1024 * 1024):
                                continue
                            atomic_write(safe_path(stage, relative), read_bytes(safe))
            stage.rename(archive)
        archived = read_json(archive / 'state.json')
        if archived['stateRevision'] != state['stateRevision']:
            raise WorkItemError('ARCHIVE_CONFLICT', '已有归档与当前状态不一致')
        state['history'].append({'iteration': state['iteration'], 'path': 'history/' + state['iteration']})
        state.update(iteration=f"i{int(state['iteration'][1:]) + 1:02d}", lifecycle='active', reviews={}, currentTasks=[], verification=None, evidence=[], blockers=[], cancellation=None)
        state = save_state(directory, state)
    return _finish(root, slug, state)


def _modifiable(state: dict) -> None:
    if state['lifecycle'] not in {'active', 'paused'}:
        raise WorkItemError('ITEM_INACTIVE', '已结束工作项应开启新一轮')


def update(root: Path, slug: str, changes: dict, *, reason: str, expected_revision: str, preview=False) -> dict:
    allowed = {'title', 'summary', 'activity', 'risk', 'documentKind', 'bindings'}
    object_fields(changes, allowed=allowed)
    text(reason, '$.reason')
    directory = item_path(root, slug)
    with (nullcontext() if preview else item_lock(directory)):
        state = load_state(directory)
        check_revision(state, expected_revision)
        _modifiable(state)
        for key in {'title', 'summary', 'activity'} & changes.keys():
            text(changes[key], '$.' + key, empty=key == 'summary')
        candidate = {**copy.deepcopy(state), **changes}
        text(candidate['risk'], '$.risk')
        text(candidate['documentKind'], '$.documentKind')
        changed = {key: {'before': state[key], 'after': candidate[key]} for key in changes if candidate[key] != state[key]}
        if not changed:
            return {'changed': False, 'state': state, 'preview': preview}
        if candidate['risk'] not in {'light', 'normal', 'major'} or candidate['documentKind'] not in {'change', 'requirements'}:
            raise WorkItemError('ITEM_CLASSIFICATION_INVALID', '风险或文档模式无效')
        if candidate['risk'] == 'major' and candidate['documentKind'] != 'requirements':
            raise WorkItemError('ITEM_CLASSIFICATION_INVALID', '重大风险需要复杂文档模式')
        if not re.fullmatch(r'[a-z][a-z0-9-]*', candidate['activity']):
            raise WorkItemError('INPUT_INVALID', '活动类型必须是小写标识', '$.activity')
        bindings = array(candidate['bindings'], '$.bindings')
        if not bindings:
            raise WorkItemError('INPUT_INVALID', '至少保留一个仓库', '$.bindings')
        names = set()
        for index, binding in enumerate(bindings):
            object_fields(binding, allowed={'repository','workBranch','baseBranch'}, required={'repository','workBranch','baseBranch'}, field=f'$.bindings[{index}]')
            for field, value in binding.items():
                text(value, f'$.bindings[{index}].{field}')
            name = binding['repository']
            if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*', name) or name in names:
                raise WorkItemError('ITEM_BINDING_INVALID', '仓库重复或名称无效')
            names.add(name)
            if 'bindings' in changes:
                location = repository_roots(Path(root).resolve(), {'bindings': [binding]})[name]
                for field in ('workBranch', 'baseBranch'):
                    _git(location, ['check-ref-format', '--branch', binding[field]], 5, 65536)
        reader = WorkItemQuery(root, slug, state=state)
        tasks = reader.tasks() if {'risk', 'documentKind', 'bindings'} & changed.keys() else []
        old_bindings = {row['repository']: row for row in state['bindings']}
        new_bindings = {row['repository']: row for row in bindings}
        removed = old_bindings.keys() - new_bindings.keys()
        if any(task['repository'] in removed for task in tasks):
            raise WorkItemError('ITEM_REPOSITORY_REFERENCED', '先审阅并移除仍引用该仓的当前计划任务')
        if removed and (any(row['status'] == 'open' for row in state['blockers']) or any(row['status'] not in {'passed','waived'} for row in state['externalChecks'])):
            raise WorkItemError('ITEM_REPOSITORY_REFERENCED', '仍有未关闭事项，先明确处理范围再移除仓库')
        rebound = {name for name in old_bindings.keys() & new_bindings.keys() if old_bindings[name] != new_bindings[name]}
        invalidated = {task['id'] for task in tasks if task['repository'] in rebound}
        while True:
            dependents = {task['id'] for task in tasks if set(task['dependencies']) & invalidated}
            if dependents <= invalidated:
                break
            invalidated.update(dependents)
        reviews = set()
        if {'risk', 'documentKind'} & changed.keys() or 'bindings' in changed:
            reviews.update(candidate['reviews'])
            candidate['verification'] = None
        for role in reviews:
            candidate['reviews'][role] = {**candidate['reviews'][role], 'decision': 'needs-review', 'reason': reason, 'recordedAt': stamp()}
        for identifier in invalidated:
            if identifier in candidate['tasks']:
                candidate['tasks'][identifier]['evidence'] = None
        impact = {'reviews': sorted(reviews), 'tasks': sorted(invalidated), 'verificationInvalidated': candidate['verification'] != state['verification']}
        reader.assert_unchanged()
        if preview:
            return {'changed': True, 'preview': True, 'changes': changed, 'impact': impact, 'stateRevision': state['stateRevision']}
        candidate['changes'].append({'recordedAt': stamp(), 'reason': reason, 'changes': changed, 'impact': impact})
        candidate = save_state(directory, candidate)
    return _finish(root, slug, candidate, changed=True, impact=impact)


def block(root: Path, slug: str, *, reason: str, owner: str, condition: str, expected_revision: str, task_ids=None) -> dict:
    for field, value in [('reason', reason), ('owner', owner), ('condition', condition)]:
        text(value, '$.' + field)
    task_ids = list(dict.fromkeys(task_ids or []))
    directory = item_path(root, slug)
    with item_lock(directory):
        state = load_state(directory)
        check_revision(state, expected_revision)
        _modifiable(state)
        if set(task_ids) - set(state['currentTasks']):
            raise WorkItemError('TASK_UNREGISTERED', '阻塞只能引用当前已登记任务')
        identifier = f"B{len(state['blockers']) + 1:02d}"
        state['blockers'].append({'id': identifier, 'taskIds': task_ids, 'reason': reason, 'owner': owner,
                                  'condition': condition, 'status': 'open', 'recordedAt': stamp(), 'resolution': None})
        state = save_state(directory, state)
    return _finish(root, slug, state, blockerId=identifier)


def unblock(root: Path, slug: str, identifier: str, *, reason: str, expected_revision: str) -> dict:
    text(reason, '$.reason')
    directory = item_path(root, slug)
    with item_lock(directory):
        state = load_state(directory)
        check_revision(state, expected_revision)
        _modifiable(state)
        row = next((row for row in state['blockers'] if row['id'] == identifier), None)
        if row is None:
            raise WorkItemError('BLOCKER_NOT_FOUND', identifier)
        if row['status'] == 'resolved':
            return {'changed': False, 'state': state}
        row.update(status='resolved', resolution={'reason': reason, 'recordedAt': stamp()})
        state = save_state(directory, state)
    return _finish(root, slug, state, changed=True)


def cancel(root: Path, slug: str, *, reason: str, expected_revision: str) -> dict:
    text(reason, '$.reason')
    directory = item_path(root, slug)
    with item_lock(directory):
        state = load_state(directory)
        check_revision(state, expected_revision)
        _modifiable(state)
        state.update(lifecycle='cancelled', cancellation={'reason': reason, 'recordedAt': stamp()})
        state = save_state(directory, state)
    return _finish(root, slug, state)


def render(root: Path, slug: str) -> dict:
    reader = WorkItemQuery(root, slug)
    state = reader.state
    def one(value):
        return ' '.join(str(value).splitlines()).replace('|', '\\|')
    lines = [f"# {one(state['title'])}", '', '<!-- Generated by Kit. Edit source documents, not this summary. -->', '',
             f"- 状态：{state['lifecycle']}", f"- 当前迭代：{state['iteration']}", f"- 风险：{state['risk']}",
             f"- 目标：{one(state['summary'])}", '', '## 文档', '']
    for role in content_roles(state):
        if reader.document(role):
            lines.append(f'- [{role}]({role}.md)')
    if state['verification']:
        lines.append('- [验证摘要](verification.md)')
    for name in ('references', 'artifacts'):
        directory = safe_path(reader.path, name)
        if directory.exists():
            paths = sorted(directory.rglob('*.md'))
            for path in paths[:30]:
                relative = path.relative_to(reader.path).as_posix()
                safe_path(reader.path, relative)
                lines.append(f'- [{one(relative)}]({relative})')
            if len(paths) > 30:
                lines.append(f'- {name} 共 {len(paths)} 项，展示 30 项，其余 {len(paths)-30} 项见 `{name}/`。')
    if state['cancellation']:
        lines += ['', '## 取消原因', '', one(state['cancellation']['reason'])]
    opened = [row for row in state['blockers'] if row['status'] == 'open']
    if opened:
        lines += ['', '## 开发阻塞', '', *[f"- {row['id']}：{one(row['reason'])}；{one(row['owner'])}；解除条件：{one(row['condition'])}" for row in opened[:20]]]
        if len(opened) > 20:
            lines.append(f'- 共 {len(opened)} 项，展示 20 项，其余 {len(opened)-20} 项见 `kit.py brief {slug}`。')
    lines += ['', '## 交付状态', '', '| 仓库 | 版本 | 提测 | 部署 | 验收 |', '|---|---|---|---|---|']
    for binding in state['bindings']:
        row = state['delivery'].get(binding['repository'], {})
        lines.append('| ' + ' | '.join(one(x) for x in [binding['repository'], row.get('version', '未记录'), row.get('submission', '未执行'), row.get('deployment', '未确认'), row.get('acceptance', '未确认')]) + ' |')
    if state['history']:
        lines += ['', '## 历史', '', *[f"- [{row['iteration']}]({row['path']}/README.md)" for row in state['history']]]
    changed = atomic_write(reader.path / 'README.md', ('\n'.join(lines) + '\n').encode())
    if state['verification'] or state['evidence'] or (reader.path / 'verification.md').exists():
        verification = reader.verification()
        lines = ['# 验证摘要', '', '<!-- Generated by Kit. Evidence is authoritative. -->', '',
                 f"- 上次整体验证：{verification['recordedResult']}", '- 当前代码适用性：读取时另行核对',
                 '- [README 交付状态](README.md)', '', '## 当前任务', '']
        task_states = sorted(reader.task_states(), key=lambda row: (row['completed'], int(row['id'][1:])))
        lines += [f"- {task['id']}：{task['status']}" for task in task_states[:30]] or ['- 本次活动使用整体验证']
        if len(task_states) > 30:
            lines += [f"- 共 {len(task_states)} 项，展示 30 项，其余 {len(task_states)-30} 项见 `kit.py inspect projection {slug} --view task`。"]
        lines += ['', '## 待外部验收', '']
        external = sorted(state['externalChecks'], key=lambda row: (row['status'] in {'passed','waived'}, row['id']))
        lines += [f"- {one(c['requirement'])}：{one(c['description'])}；{one(c['owner'])}；{c['status']}" for c in external[:20]] or ['- 无已登记待办']
        if len(external) > 20:
            lines += [f"- 共 {len(external)} 项，展示 20 项，其余 {len(external)-20} 项见 `kit.py inspect projection {slug} --view flow`。"]
        lines += ['', f"详细结果：`kit.py verify evidence {slug}`；历史记录：`kit.py verify history {slug}`。"]
        changed = atomic_write(reader.path / 'verification.md', ('\n'.join(lines) + '\n').encode()) or changed
    return {'itemSlug': slug, 'rendered': True, 'changed': changed}
