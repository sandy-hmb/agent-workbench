"""Validate structured references from WorkItem delivery facts to Workflow attempts."""
from __future__ import annotations

from pathlib import Path

from workbench.extensions import attempts
from workbench.workspace.paths import workflow_run_file
from workbench.work_items.store import WorkItemError, read_json
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
        object_fields(ref, allowed={'kind', 'runId', 'requestId', 'stage'},
                      required={'kind', 'runId', 'requestId', 'stage'}, field=location)
        for name in ('kind', 'runId', 'requestId', 'stage'):
            text(ref[name], f'{location}.{name}')
        try:
            validate_workflow_run_id(ref['runId'], field=f'{location}.runId')
            validate_request_id(ref['requestId'], field=f'{location}.requestId')
            validate_stage_id(ref['stage'], field=f'{location}.stageId')
        except ValueError as exc:
            raise WorkItemError('EVIDENCE_REFERENCE_INVALID', 'Workflow 证据引用格式无效', location)
        run_path = workflow_run_file(root, ref['runId'])
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
            'status': request.get('status'),
        })
    return result
