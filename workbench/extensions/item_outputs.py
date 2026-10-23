"""Small, declarative output boundaries, not a filesystem sandbox."""
from __future__ import annotations

import re
from pathlib import PurePosixPath

from workbench.extensions.model import ExtensionError


def normalize(value, extension_id, skills=None):
    if not isinstance(value, dict):
        raise ExtensionError('itemOutputs must be a skill-to-outputs object')
    result, owners = {}, {}
    for skill, rows in value.items():
        if not re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*', skill) or skills is not None and skill not in skills:
            raise ExtensionError('itemOutputs references an unknown skill')
        if not isinstance(rows, list):
            raise ExtensionError('itemOutputs entries must be arrays')
        result[skill] = []
        for row in rows:
            if not isinstance(row, dict) or set(row) != {'path', 'kind', 'writeMode', 'purpose'}:
                raise ExtensionError('output requires path, kind, writeMode, purpose')
            path, kind, mode = row['path'], row['kind'], row['writeMode']
            if not isinstance(kind, str) or not isinstance(mode, str):
                raise ExtensionError('output kind and writeMode must be strings')
            if (not isinstance(path, str) or not path or not re.fullmatch(r'[A-Za-z0-9_./{}-]+', path)
                    or any(p in {'', '.', '..'} for p in path.split('/')) or PurePosixPath(path).is_absolute()):
                raise ExtensionError('output path must be a safe relative path')
            if not isinstance(row['purpose'], str) or not row['purpose'].strip() or '\n' in row['purpose']:
                raise ExtensionError('output purpose is required')
            if kind == 'handoff':
                if (mode != 'update' or not path.startswith('artifacts/') or path.startswith('artifacts/runs/')
                        or '{' in path or '}' in path or not path.endswith('.md')):
                    raise ExtensionError('handoff requires a stable artifacts Markdown path and update mode')
                if re.search(r'(?:final|v\d+|\d{8}|\d{4}-\d{2}-\d{2})(?:[./_-]|$)', path):
                    raise ExtensionError('handoff must update the current file, not a dated/final version')
            elif kind in {'evidence', 'log'}:
                prefix = f'artifacts/runs/{extension_id}/{{runId}}/'
                if mode != 'append-run' or not path.startswith(prefix) or path.count('{runId}') != 1:
                    raise ExtensionError('evidence/log requires its extension run directory and append-run mode')
                if '{' in path[len(prefix):] or '}' in path[len(prefix):]:
                    raise ExtensionError('only the runId placeholder is supported')
            else:
                raise ExtensionError('unknown output kind')
            if path in owners:
                raise ExtensionError(f'output has multiple owners: {path}')
            owners[path] = skill
            result[skill].append(dict(row))
    return result


def check_conflicts(manifests):
    owners = {}
    for manifest in manifests:
        for skill, outputs in (manifest.item_outputs or {}).items():
            for row in outputs:
                path = row['path']
                if path in owners:
                    raise ExtensionError(f'output owner conflict: {path}: {owners[path]} / {manifest.id}/{skill}')
                owners[path] = f'{manifest.id}/{skill}'


def context(root, slug, manifest, skill, effects):
    outputs = (manifest.item_outputs or {}).get(skill)
    result = {'declared': outputs is not None, 'outputs': outputs or []}
    if not slug:
        return result
    from workbench.work_items.query import WorkItemQuery
    from workbench.work_items.store import safe_path
    reader = WorkItemQuery(root, slug)
    result['documentPolicy'] = reader.state.get('documentPolicy', 'legacy')
    if result['documentPolicy'] != 'focused-v1':
        return result
    if outputs is None and set(effects) & {'write', 'filesystem.write', 'policy.write'}:
        raise ExtensionError(f'ITEM_OUTPUTS_UNDECLARED: {manifest.id}/{skill} must declare itemOutputs for this item')
    for row in outputs or []:
        safe_path(reader.path, row['path'].replace('{runId}', 'output-preview'))
    reader.assert_unchanged()
    return result


def output_path(item, declaration, run_id=None):
    from workbench.work_items.store import safe_path
    raw = declaration['path']
    if '{runId}' in raw:
        if not isinstance(run_id, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,159}', run_id):
            raise ExtensionError('output runId must be one safe path segment')
        raw = raw.replace('{runId}', run_id)
    return safe_path(item, raw)
