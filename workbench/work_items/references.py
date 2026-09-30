"""Validate structured references from WorkItem delivery facts to Workflow attempts."""
from __future__ import annotations

import fcntl
from contextlib import nullcontext
from pathlib import Path

from workbench.extensions import attempts
from workbench.workspace.paths import workflow_run_file
from workbench.work_items.store import WorkItemError, digest, read_json, safe_path
from workbench.validation import object_fields, text, array
from workbench.identifiers import (
    request_id as validate_request_id,
    stage_id as validate_stage_id,
    workflow_run_id as validate_workflow_run_id,
)


def validate_evidence_refs(root: Path, item_slug: str, value: object, field: str) -> list[dict]:
    """Validate and normalize optional Workflow evidence references."""
    refs = array(value, field)
    if len(refs) > 20:
        raise WorkItemError('EVIDENCE_REFERENCE_LIMIT', '证据引用不能超过 20 项', field)
    result = []
    for index, ref in enumerate(refs):
        location = f'{field}[{index}]'
        object_fields(ref, allowed={'kind', 'runId', 'requestId', 'stage', 'status'},
                      required={'kind', 'runId', 'requestId', 'stage'}, field=location)
        for name in ('kind', 'runId', 'requestId', 'stage'):
            text(ref[name], f'{location}.{name}')
        if ref['kind'] != 'workflow':
            raise WorkItemError('EVIDENCE_REFERENCE_INVALID', '证据引用 kind 必须为 workflow', location + '.kind')
        try:
            validate_workflow_run_id(ref['runId'], field=f'{location}.runId')
            validate_request_id(ref['requestId'], field=f'{location}.requestId')
            validate_stage_id(ref['stage'], field=f'{location}.stageId')
        except ValueError as exc:
            raise WorkItemError('EVIDENCE_REFERENCE_INVALID', 'Workflow 证据引用格式无效', location)
        run_path = workflow_run_file(root, ref['runId'])
        safe_path(root, run_path.relative_to(root).as_posix())
        run = read_json(run_path)
        if run.get('itemSlug') != item_slug:
            raise WorkItemError('EVIDENCE_REFERENCE_SUBJECT_MISMATCH', '证据引用不属于当前 WorkItem', location)
        records = attempts.read(root, ref['runId']).get('requests', {})
        request = records.get(ref['requestId']) if isinstance(records, dict) else None
        if not isinstance(request, dict) or request.get('stage') != ref['stage']:
            raise WorkItemError('EVIDENCE_REFERENCE_NOT_FOUND', 'Workflow 请求或阶段不存在', location)
        result.append({
            'kind': 'workflow',
            'runId': ref['runId'],
            'requestId': ref['requestId'],
            'stage': ref['stage'],
            'status': attempts.observed(root, request)['status'],
        })
    return result


def workflow_checks_lock(root: Path, checks: list[dict]):
    """Serialize acceptance with Action execution using the existing Extension lock."""
    if any(isinstance(check, dict) and check.get('evidenceRefs') for check in checks):
        from workbench.extensions.management import extension_lock
        return extension_lock(root, fcntl.LOCK_SH, create_cache=False)
    return nullcontext()


def merge_check_refs(root: Path, slug: str, previous: list[dict], current: object, field: str) -> list[dict]:
    """Keep each required Stage/repository while allowing a new run or retry receipt."""
    previous = validate_evidence_refs(root, slug, previous, field)
    current = validate_evidence_refs(root, slug, current, field)
    merged = {}
    for ref in [*previous, *current]:
        run = read_json(workflow_run_file(root, ref['runId']))
        key = (ref['stage'], run.get('repository'))
        old = merged.get(key)
        if old and old['runId'] != ref['runId']:
            latest = attempts.latest(attempts.read(root, old['runId']), old['stage'])
            if latest and attempts.observed(root, latest)['status'] in {'running', 'unknown'}:
                raise WorkItemError('WORKFLOW_CHECK_INVALID', '旧 Run 的 Action 结果尚未确认，不能用新 Run 替换引用', field)
        merged[key] = ref
    if len(merged) > 20:
        raise WorkItemError('EVIDENCE_REFERENCE_LIMIT', '证据引用不能超过 20 项', field)
    return list(merged.values())


def validate_workflow_check(root: Path, state: dict, check: dict) -> None:
    """Check live Action evidence for an explicitly closed external acceptance check."""
    if check['status'] not in {'passed', 'waived'} or not check.get('evidenceRefs'):
        return
    from workbench.extensions.runner import _load_run, _resolve, _stage_fingerprint, _assert_predecessors
    try:
        refs = validate_evidence_refs(root, state['slug'], check['evidenceRefs'], '$.evidenceRefs')
        resolved = _resolve(root) if check['status'] == 'passed' else None
        for ref in refs:
            run = _load_run(root, ref['runId'])
            if check['status'] == 'passed' and (run['iteration'] != state['iteration'] or run['bindingRevision'] != digest(state['bindings'])):
                raise ValueError('Action 证据不属于当前迭代或仓库绑定')
            latest = attempts.latest(attempts.read(root, ref['runId']), ref['stage'])
            if latest is None or latest['requestId'] != ref['requestId']:
                raise ValueError('必须引用当前 Stage 的最新尝试并重新核对结果')
            latest = attempts.observed(root, latest)
            if latest['status'] in {'running', 'unknown'}:
                raise ValueError('Action 结果尚未确认，先 reconcile，不能通过或豁免')
            if resolved is not None:
                core, overlay, workflow, bindings = resolved
                stage = next((row for row in overlay.stages if row.id == ref['stage']), None)
                if latest['status'] != 'succeeded':
                    raise ValueError('Action 尚未成功；明确范围调整应记录 waived 和理由')
                if run['workflow'] != core.id or stage is None or latest['fingerprint'] != _stage_fingerprint(stage, bindings[stage.uses], run):
                    raise ValueError('Action 配置已变化，需要重新执行并验收')
                _assert_predecessors(root, run, workflow.direct_predecessors.get(stage.id, ()))
    except (OSError, ValueError) as exc:
        raise WorkItemError('WORKFLOW_CHECK_INVALID', f"验收 {check['id']} 的 Workflow 依据无效：{exc}",
                            '$.externalChecks') from exc
