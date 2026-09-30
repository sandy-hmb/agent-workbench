"""Read-only JUnit report collection into the existing evidence input contract."""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path

from workbench.validation import object_fields, text
from workbench.work_items.query import WorkItemQuery
from workbench.work_items.store import (
    DIGEST_RE, WorkItemError, check_revision, read_bytes, read_json, safe_path, stamp, text_digest,
)

COUNTERS = ('total', 'executed', 'failed', 'errors', 'skipped')
OUTCOMES = {'passed': 'passed', 'success': 'passed', 'ok': 'passed',
            'skipped': 'skipped', 'notrun': 'skipped',
            'failed': 'failed', 'failure': 'failed', 'error': 'errors'}


class _NoDtd(ET.TreeBuilder):
    def doctype(self, name, pubid, system):
        raise WorkItemError('JUNIT_INVALID', '不支持 DOCTYPE 或 ENTITY 声明', '$.junit')


def _tag(element):
    return element.tag.rsplit('}', 1)[-1]


def _metadata(element):
    name = _tag(element)
    if name in {'system-out', 'system-err'} and not len(element):
        return
    if name == 'properties' and all(_tag(child) == 'property' and not len(child) for child in element):
        return
    raise WorkItemError('JUNIT_INVALID', f'不支持的 JUnit 子节点：{name}', '$.junit')


def _case_counts(element):
    outcomes = set()
    for name in ('status', 'result'):
        if name in element.attrib:
            value = element.attrib[name].strip().lower()
            if value not in OUTCOMES:
                raise WorkItemError('JUNIT_INVALID', f'未知 testcase {name}：{value}', '$.junit')
            outcomes.add(OUTCOMES[value])
    for child in element:
        name = _tag(child)
        if name in {'failure', 'error', 'skipped'}:
            if len(child):
                raise WorkItemError('JUNIT_INVALID', '结果节点不能包含嵌套元素', '$.junit')
            outcomes.add(OUTCOMES[name])
        else:
            _metadata(child)
    if len(outcomes) > 1:
        raise WorkItemError('JUNIT_INVALID', 'testcase 含有冲突结果', '$.junit')
    outcome = next(iter(outcomes), 'passed')
    counts = dict.fromkeys(COUNTERS, 0)
    counts.update(total=1, executed=int(outcome != 'skipped'))
    if outcome != 'passed':
        counts[outcome] = 1
    return counts


def _add_counts(total, counts):
    for key in COUNTERS:
        total[key] += counts[key]


def parse_junit(data: bytes) -> dict:
    try:
        root = ET.fromstring(data, parser=ET.XMLParser(target=_NoDtd()))
    except (ET.ParseError, LookupError, UnicodeError) as exc:
        raise WorkItemError('JUNIT_INVALID', f'JUnit XML 无效：{exc}', '$.junit') from exc
    if _tag(root) not in {'testsuite', 'testsuites'}:
        raise WorkItemError('JUNIT_INVALID', '根节点必须为 testsuite 或 testsuites', '$.junit')
    # Accumulate each testcase once; suite totals are checked when their children finish.
    stack = [(root, iter(root), dict.fromkeys(COUNTERS, 0))]
    while stack:
        suite, children, counts = stack[-1]
        child = next(children, None)
        if child is not None:
            name = _tag(child)
            if name == 'testcase':
                _add_counts(counts, _case_counts(child))
            elif name in {'testsuite', 'testsuites'}:
                stack.append((child, iter(child), dict.fromkeys(COUNTERS, 0)))
            else:
                _metadata(child)
            continue
        for attribute, key in [('tests', 'total'), ('failures', 'failed'), ('errors', 'errors'), ('skipped', 'skipped')]:
            if attribute in suite.attrib:
                value = suite.attrib[attribute]
                if not re.fullmatch(r'[0-9]+', value) or (value.lstrip('0') or '0') != str(counts[key]):
                    raise WorkItemError('JUNIT_INVALID', f'{attribute} 声明与 testcase 明细不一致或不是非负整数', '$.junit')
        stack.pop()
        if stack:
            _add_counts(stack[-1][2], counts)
        else:
            return counts


def collect_result(root: Path, slug: str, *, snapshot: Path, junit: list[str], command: str,
                   working_directory: str, exit_status: int, task_id=None, artifacts=()) -> dict:
    text(command, '$.command')
    text(working_directory, '$.workingDirectory')
    if type(exit_status) is not int:
        raise WorkItemError('INPUT_INVALID', 'exitStatus 必须为整数', '$.exitStatus')
    if not junit:
        raise WorkItemError('INPUT_INVALID', '至少提供一个 JUnit 报告', '$.junit')
    reader = WorkItemQuery(root, slug)
    if reader.state['lifecycle'] != 'active':
        raise WorkItemError('ITEM_INACTIVE', '只能采集活动 WorkItem 的验证草稿')
    saved = read_json(Path(snapshot))
    object_fields(saved, required={'itemSlug', 'stateRevision', 'codeState'})
    if saved['itemSlug'] != slug:
        raise WorkItemError('EVIDENCE_SUBJECT_MISMATCH', '快照不属于当前 WorkItem')
    check_revision(reader.state, saved['stateRevision'])
    names = {reader.task(task_id)['repository']} if task_id else {row['repository'] for row in reader.state['bindings']}
    code = saved['codeState']
    if not isinstance(code, dict) or set(code) != names or any(not isinstance(value, str) or not DIGEST_RE.fullmatch(value) for value in code.values()):
        raise WorkItemError('EVIDENCE_CODE_INVALID', '快照代码指纹必须对应本次验证的仓库集合')
    if saved.get('taskId') != task_id:
        raise WorkItemError('EVIDENCE_SUBJECT_MISMATCH', '快照任务与采集任务不一致')
    paths = {}
    for kind, values in [('test-report', junit), ('artifact', artifacts)]:
        for relative in values:
            text(relative, '$.junit' if kind == 'test-report' else '$.artifact')
            path = safe_path(reader.path, relative)
            normalized = path.relative_to(reader.path).as_posix()
            paths.setdefault(normalized, (path, kind))
    if len(paths) > 20:
        raise WorkItemError('EVIDENCE_REFERENCE_LIMIT', '报告与附件去重后不能超过 20 个')
    reports, attachments, refs, checks = [], [], [], []
    summary = dict.fromkeys(COUNTERS, 0)
    for relative, (path, kind) in paths.items():
        data = read_bytes(path)
        ref = {'path': relative, 'sha256': text_digest(data), 'bytes': len(data), 'type': kind}
        refs.append(ref)
        if kind == 'artifact':
            attachments.append(ref)
            continue
        try:
            counts = parse_junit(data)
        except WorkItemError as exc:
            raise WorkItemError(exc.code, f'{relative}：{exc}', '$.junit') from exc
        reports.append({**ref, **counts})
        _add_counts(summary, counts)
        passed = counts['total'] > 0 and not any(counts[key] for key in ('failed', 'errors', 'skipped')) and exit_status == 0
        result = ('通过' if passed else '失败') + '：' + (
            f"总数 {counts['total']}，执行 {counts['executed']}，失败 {counts['failed']}，"
            f"错误 {counts['errors']}，跳过 {counts['skipped']}；退出码 {exit_status}；需人工审阅")
        checks.append({'type': '测试', 'workingDirectory': working_directory, 'command': command,
                       'target': relative, 'executed': counts['executed'], 'skipped': counts['skipped'],
                       'exitStatus': exit_status, 'result': result})
    if code != reader.code_state(names):
        raise WorkItemError('CODE_CHANGED', '快照代码指纹与当前代码不一致')
    reader.assert_unchanged()
    passed = all(row['total'] > 0 for row in reports) and not any(summary[key] for key in ('failed', 'errors', 'skipped')) and exit_status == 0
    payload = {'scope': 'task' if task_id else 'item', 'taskId': task_id, 'recordedAt': stamp(),
               'result': 'passed' if passed else 'failed', 'codeState': code, 'checks': checks,
               'verificationScope': 'JUnit 报告：' + '、'.join(row['path'] for row in reports) + '；需人工审阅实际执行、报告归属与结果',
               'artifactRefs': refs}
    return {'itemSlug': slug, 'stateRevision': saved['stateRevision'], 'summary': summary,
            'reports': reports, 'artifacts': attachments, 'input': payload}
