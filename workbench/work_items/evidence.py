"""Read immutable WorkItem evidence and bounded current or archived history."""
from __future__ import annotations
from workbench.work_items.store import WorkItemError as EvidenceError, digest as content_id, read_evidence, load_state, read_json, safe_path


def get_evidence(item, *, task_id=None, evidence_id=None):
    state = load_state(item)
    if evidence_id is None:
        evidence_id = state['tasks'].get(task_id, {}).get('evidence') if task_id else state['verification']
    if not evidence_id:
        raise EvidenceError('EVIDENCE_NOT_FOUND', '没有对应验证记录')
    known = set(state['evidence'])
    known.update(item['evidence'] for item in state['tasks'].values() if item.get('evidence'))
    if evidence_id not in known:
        for history in state['history']:
            archived = read_json(safe_path(item, history['path'] + '/state.json'))
            known.update(archived['evidence'])
    if evidence_id not in known:
        raise EvidenceError('EVIDENCE_NOT_FOUND', '记录不属于本 WorkItem 的已提交历史')
    return read_evidence(item, evidence_id)


def history_page(item, *, offset=0, limit=20, task_id=None, iteration=None):
    if offset < 0 or not 1 <= limit <= 100:
        raise EvidenceError('EVIDENCE_ARGUMENT_INVALID', '分页参数无效')
    state = load_state(item)
    if iteration and iteration != state['iteration']:
        match = next((row for row in state['history'] if row['iteration'] == iteration), None)
        if not match: raise EvidenceError('EVIDENCE_NOT_FOUND', '历史迭代不存在')
        state = read_json(safe_path(item, match['path'] + '/state.json'))
    items = []
    for identifier in reversed(state['evidence']):
        record = read_evidence(item, identifier)
        if task_id is None or record.get('taskId') == task_id:
            items.append({key: record.get(key) for key in ('id', 'scope', 'taskId', 'recordedAt', 'result')})
    return {'items': items[offset:offset + limit], 'total': len(items), 'offset': offset, 'limit': limit, 'stateRevision': state['stateRevision']}
