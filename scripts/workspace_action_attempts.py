"""Durable command Action attempts; reads never create files or acquire a lease."""
from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
import re
import stat
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from workspace_model import atomic_write_many

ID = re.compile(r'^[a-z0-9][a-z0-9._-]{0,127}$')
STATES = {'running', 'succeeded', 'failed', 'unknown', 'skipped'}
LIMIT = 1024 * 1024


def identifier(value: str) -> str:
    if not isinstance(value, str) or not ID.fullmatch(value) or '..' in value:
        raise ValueError('ACTION_REQUEST_INVALID: 请求编号无效')
    return value


def default_request_id(run_id: str, stage: str, plan_hash: str) -> str:
    return 'request-' + hashlib.sha256(f'{run_id}\0{stage}\0{plan_hash}'.encode()).hexdigest()[:32]


def _path(root: Path, run_id: str, request_id: str | None = None) -> Path:
    identifier(run_id)
    base = root / '.workspace/runs/attempts'
    path = base / (f'{run_id}.json' if request_id is None else f'{run_id}.{identifier(request_id)}.lock')
    for parent in (path, *path.parents):
        if parent == root:
            break
        if parent.is_symlink():
            raise ValueError(f'ACTION_RECORD_UNSAFE: 不允许符号链接：{parent}')
    return path


def read(root: Path, run_id: str) -> dict:
    path = _path(root, run_id)
    if not path.exists():
        return {'schemaVersion': 1, 'run': run_id, 'requests': {}}
    if not path.is_file() or path.stat().st_size > LIMIT:
        raise ValueError('ACTION_RECORD_INVALID: 执行记录不是有界普通文件')
    data = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(data, dict) or set(data) != {'schemaVersion', 'run', 'requests'} or data['schemaVersion'] != 1 or data['run'] != run_id or not isinstance(data['requests'], dict):
        raise ValueError('ACTION_RECORD_INVALID: 执行记录结构无效')
    seen = set()
    for key, item in data['requests'].items():
        identifier(key)
        if not isinstance(item, dict) or item.get('requestId') != key or item.get('run') != run_id:
            raise ValueError('ACTION_RECORD_INVALID: 请求归属无效')
        if item.get('status') not in STATES or item.get('origin') not in {'runner', 'reconciled'}:
            raise ValueError('ACTION_RECORD_INVALID: 执行状态或来源无效')
        for field in ('stage', 'planHash', 'fingerprint', 'summary', 'updatedAt'):
            if not isinstance(item.get(field), str):
                raise ValueError('ACTION_RECORD_INVALID: 请求字段无效')
        if not re.fullmatch(r'[a-z][a-z0-9]*(?:[.-][a-z0-9]+)*', item['stage']) or not re.fullmatch(r'[a-f0-9]{64}', item['planHash']) or not re.fullmatch(r'sha256:[a-f0-9]{64}', item['fingerprint']):
            raise ValueError('ACTION_RECORD_INVALID: 请求指纹无效')
        sequence = item.get('sequence')
        if type(sequence) is not int or sequence < 1 or sequence in seen:
            raise ValueError('ACTION_RECORD_INVALID: 请求顺序无效')
        seen.add(sequence)
    return data


def save(root: Path, run_id: str, data: dict) -> None:
    path = _path(root, run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2) + '\n'
    if len(content.encode()) > LIMIT:
        raise ValueError('ACTION_RECORD_LIMIT: 执行记录超过 1 MiB')
    atomic_write_many(((path, content),))


def stamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


@contextmanager
def lease(root: Path, run_id: str, request_id: str):
    path = _path(root, run_id, request_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError('ACTION_RECORD_UNSAFE: 执行锁不是普通文件')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError('ACTION_BUSY: 同一请求仍在执行') from exc
        yield
    finally:
        os.close(fd)


def alive(root: Path, run_id: str, request_id: str) -> bool:
    path = _path(root, run_id, request_id)
    try:
        fd = os.open(path, os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0))
    except FileNotFoundError:
        return False
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError('ACTION_RECORD_UNSAFE: 执行锁不是普通文件')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in {errno.EACCES, errno.EAGAIN}:
                return True
            raise
        return False
    finally:
        os.close(fd)


def observed(root: Path, item: dict) -> dict:
    value = dict(item)
    if value['status'] == 'running' and not alive(root, value['run'], value['requestId']):
        value.update(status='unknown', summary='执行者已离开，外部结果尚未核对')
    return value


def latest(data: dict, stage: str) -> dict | None:
    items = [item for item in data['requests'].values() if item['stage'] == stage]
    return max(items, key=lambda item: item['sequence'], default=None)


def project(root: Path, run: dict) -> dict:
    """Overlay authoritative attempts on the legacy stage snapshot in memory."""
    data = read(root, run['id'])
    run = {**run, 'stages': dict(run['stages'])}
    for stage in {row['stage'] for row in data['requests'].values()}:
        row = observed(root, latest(data, stage))
        row = {**row, **row.get('stageDisposition', {})}
        run['stages'][stage] = {
            'fingerprint': row['fingerprint'],
            'status': row['status'] if row['status'] != 'unknown' else 'failed',
            'updatedAt': row['updatedAt'], 'summary': row['summary'],
        }
    return run
