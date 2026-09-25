"""Single mutable WorkItem state and immutable, self-contained evidence records."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from functools import lru_cache
from workbench.errors import WorkbenchError
from workbench.validation import object_fields, text, array
from workbench.resources import KIT_ROOT
from workbench.schema_validation import validate, SchemaValidationError

SCHEMA_VERSION = 1
MAX_BYTES = 2 * 1024 * 1024
SLUG_RE = re.compile(r'^[a-z0-9]+(?:-[a-z0-9]+)*$')
TASK_RE = re.compile(r'^T[0-9]{2,}$')
DIGEST_RE = re.compile(r'^sha256:[a-f0-9]{64}$')


class WorkItemError(WorkbenchError):
    pass


def stamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def digest(value: object) -> str:
    return 'sha256:' + hashlib.sha256(canonical(value)).hexdigest()


def text_digest(value: str | bytes) -> str:
    return 'sha256:' + hashlib.sha256(value.encode() if isinstance(value, str) else value).hexdigest()


def safe_path(root: Path, relative: str) -> Path:
    pure = PurePosixPath(relative)
    if not relative or pure.is_absolute() or '..' in pure.parts or '\\' in relative or '\0' in relative:
        raise WorkItemError('UNSAFE_PATH', f'路径无效：{relative}')
    root = Path(root)
    for parent in (root, *root.parents):
        if parent.is_symlink():
            raise WorkItemError('UNSAFE_PATH', f'路径包含符号链接：{parent}')
    current = root
    for part in pure.parts:
        current /= part
        if current.is_symlink():
            raise WorkItemError('UNSAFE_PATH', f'路径包含符号链接：{current}')
    return current


def item_area(root: Path) -> Path:
    root = Path(root).resolve()
    return safe_path(root, '.workspace/items' if (root / '.workspace/workspace.json').exists() else 'docs/development/items')


def item_path(root: Path, slug: str) -> Path:
    if not SLUG_RE.fullmatch(slug):
        raise WorkItemError('ITEM_INVALID', '需求名称必须使用小写 kebab-case')
    return safe_path(item_area(root), slug)


def read_bytes(path: Path, limit: int = MAX_BYTES) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise WorkItemError('FILE_UNAVAILABLE', f'不是可读普通文件：{path}')
    with os.fdopen(os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)), 'rb') as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise WorkItemError('UNSAFE_PATH', '只能读取普通文件')
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise WorkItemError('READ_LIMIT', f'文件超过读取上限：{path}')
    return data


def read_json(path: Path) -> dict:
    try:
        value = json.loads(read_bytes(path))
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise WorkItemError('JSON_INVALID', f'JSON 无效：{path}', '$') from exc
    if not isinstance(value, dict):
        raise WorkItemError('JSON_INVALID', 'JSON 顶层必须是对象', '$')
    return value


def atomic_write(path: Path, data: bytes) -> bool:
    safe_path(path.parent, path.name)
    if path.exists() and read_bytes(path, max(MAX_BYTES, len(data))) == data:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name + '-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return True


@contextmanager
def item_lock(item: Path):
    path = safe_path(item, '.state.lock')
    if not item.is_dir():
        raise WorkItemError('ITEM_NOT_FOUND', str(item))
    fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise WorkItemError('UNSAFE_PATH', '状态锁必须是普通文件')
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


@lru_cache(maxsize=1)
def _state_schema():
    return json.loads((KIT_ROOT / 'schemas/item-state.schema.json').read_text())


def validate_state(state: dict, item: Path) -> None:
    try:
        validate(state, _state_schema())
    except SchemaValidationError as exc:
        raise WorkItemError('ITEM_STATE_INVALID', str(exc), '$') from exc
    required = {'schemaVersion', 'slug', 'title', 'summary', 'lifecycle', 'activity', 'risk', 'documentKind',
                'iteration', 'bindings', 'reviews', 'tasks', 'currentTasks', 'maxTaskNumber',
                'verification', 'evidence', 'delivery', 'externalChecks', 'history', 'updatedAt', 'stateRevision',
                'blockers', 'cancellation', 'changes'}
    object_fields(state, allowed=required, required=required)
    if set(state) != required or state.get('schemaVersion') != SCHEMA_VERSION:
        raise WorkItemError('ITEM_FORMAT_UNSUPPORTED', '仅支持新版 state.json；请使用新工作区')
    if state['slug'] != item.name or not SLUG_RE.fullmatch(state['slug']):
        raise WorkItemError('ITEM_INVALID', '需求身份不一致')
    if state['lifecycle'] not in {'active', 'paused', 'done', 'cancelled'} or state['risk'] not in {'light', 'normal', 'major'}:
        raise WorkItemError('ITEM_INVALID', '生命周期或风险无效')
    if state['documentKind'] not in {'change', 'requirements'} or not re.fullmatch(r'i\d{2,}', state['iteration']):
        raise WorkItemError('ITEM_INVALID', '文档类型或迭代无效')
    if type(state['maxTaskNumber']) is not int or state['maxTaskNumber'] < 0:
        raise WorkItemError('ITEM_INVALID', '任务编号高水位无效')
    if not isinstance(state['bindings'], list) or not state['bindings']:
        raise WorkItemError('ITEM_INVALID', '缺少仓库绑定')
    names = set()
    for binding in state['bindings']:
        if not isinstance(binding, dict) or set(binding) != {'repository', 'workBranch', 'baseBranch'}:
            raise WorkItemError('ITEM_INVALID', '仓库绑定无效')
        if any(not isinstance(v, str) or not v or '\n' in v for v in binding.values()):
            raise WorkItemError('ITEM_INVALID', '仓库绑定必须是非空单行文字')
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*', binding['repository']) or binding['repository'] in names:
            raise WorkItemError('ITEM_INVALID', '仓库名称重复或无效')
        names.add(binding['repository'])
    for key in ('reviews', 'tasks', 'delivery'):
        if not isinstance(state[key], dict):
            raise WorkItemError('ITEM_INVALID', f'{key} 必须是对象')
    for key in ('currentTasks', 'evidence', 'externalChecks', 'history', 'blockers', 'changes'):
        if not isinstance(state[key], list):
            raise WorkItemError('ITEM_INVALID', f'{key} 必须是数组')
    if len(set(state['currentTasks'])) != len(state['currentTasks']) or any(t not in state['tasks'] for t in state['currentTasks']):
        raise WorkItemError('ITEM_INVALID', '当前任务集合无效')
    for task_id, item in state['tasks'].items():
        if not TASK_RE.fullmatch(task_id) or int(task_id[1:]) > state['maxTaskNumber'] or not isinstance(item, dict):
            raise WorkItemError('ITEM_INVALID', '任务状态无效')
    for reference in [*state['evidence'], *([state['verification']] if state['verification'] else [])]:
        if not isinstance(reference, str) or not DIGEST_RE.fullmatch(reference):
            raise WorkItemError('ITEM_INVALID', '证据引用无效')
    if state['stateRevision'] != digest({k: v for k, v in state.items() if k != 'stateRevision'}):
        raise WorkItemError('STATE_HASH_MISMATCH', '状态内容与版本不一致')


def load_state(item: Path) -> dict:
    state = read_json(safe_path(item, 'state.json'))
    validate_state(state, item)
    return state


def save_state(item: Path, state: dict) -> dict:
    state = {**state, 'updatedAt': stamp()}
    state['stateRevision'] = digest({k: v for k, v in state.items() if k != 'stateRevision'})
    validate_state(state, item)
    data = canonical(state) + b'\n'
    if len(data) > MAX_BYTES:
        raise WorkItemError('STATE_LIMIT', '状态超过上限，请结束并归档当前迭代')
    atomic_write(safe_path(item, 'state.json'), data)
    return state


def check_revision(state: dict, expected: str | None) -> None:
    if expected != state['stateRevision']:
        raise WorkItemError('STATE_CHANGED', '状态已变化，请重新读取目标 WorkItem')


def evidence_path(item: Path, identifier: str) -> Path:
    if not DIGEST_RE.fullmatch(identifier):
        raise WorkItemError('EVIDENCE_INVALID', '证据 ID 无效')
    return safe_path(item, 'evidence/' + identifier[7:] + '.json')


def write_evidence(item: Path, record: dict) -> str:
    identifier = digest(record)
    path = evidence_path(item, identifier)
    data = canonical(record) + b'\n'
    if len(data) > MAX_BYTES:
        raise WorkItemError('EVIDENCE_LIMIT', '证据过大，请通过文件引用保存完整日志')
    if path.exists():
        if read_bytes(path) != data:
            raise WorkItemError('EVIDENCE_HASH_MISMATCH', '证据内容与 ID 不一致')
    else:
        atomic_write(path, data)
    return identifier


def read_evidence(item: Path, identifier: str) -> dict:
    value = read_json(evidence_path(item, identifier))
    if digest(value) != identifier or value.get('itemSlug') != item.name:
        raise WorkItemError('EVIDENCE_HASH_MISMATCH', '证据哈希或归属不一致')
    return {**value, 'id': identifier}
