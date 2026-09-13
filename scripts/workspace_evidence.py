#!/usr/bin/env python3
"""Content-addressed task-evidence-v2 storage."""
from __future__ import annotations

import base64
import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Mapping

from workspace_model import WorkspaceError, atomic_write_many


SCHEMA_VERSION = 2
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
TASK_RE = re.compile(r"^T\d{2,}$")
MAX_JSON_BYTES = 2 * 1024 * 1024


class EvidenceError(ValueError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(f"{code}: {message}")


@contextmanager
def mutation_lock(feature: Path):
    path = safe_path(feature, ".evidence.lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise EvidenceError("EVIDENCE_UNSAFE_PATH", "证据锁不是普通文件")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def content_id(value: object) -> str:
    return "sha256:" + hashlib.sha256(canonical_bytes(value)).hexdigest()


def _hex(identifier: str) -> str:
    if not isinstance(identifier, str) or not DIGEST_RE.fullmatch(identifier):
        raise EvidenceError("EVIDENCE_REFERENCE_INVALID", "内容 ID 必须是 sha256")
    return identifier.removeprefix("sha256:")


def safe_path(feature: Path, relative: str) -> Path:
    feature = Path(feature)
    for path in (feature, *list(feature.parents)[:3]):
        if path.is_symlink():
            raise EvidenceError("EVIDENCE_UNSAFE_PATH", f"Feature 路径包含符号链接：{path}")
    testing = feature / "testing"
    pure = PurePosixPath(relative)
    if not relative or pure.is_absolute() or ".." in pure.parts or "\0" in relative:
        raise EvidenceError("EVIDENCE_UNSAFE_PATH", f"证据路径无效：{relative}")
    try:
        feature_root = feature.resolve(strict=True)
        testing.resolve(strict=False).relative_to(feature_root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise EvidenceError("EVIDENCE_UNSAFE_PATH", "Feature 路径不可安全解析") from exc
    current = testing
    if testing.is_symlink() or (testing.exists() and not testing.is_dir()):
        raise EvidenceError("EVIDENCE_UNSAFE_PATH", f"证据目录不安全：{testing}")
    for part in pure.parts:
        current /= part
        if current.is_symlink():
            raise EvidenceError("EVIDENCE_UNSAFE_PATH", f"路径包含符号链接：{current}")
    try:
        current.resolve(strict=False).relative_to(feature_root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise EvidenceError("EVIDENCE_UNSAFE_PATH", f"证据路径越界：{relative}") from exc
    return current


def _read_json(feature: Path, relative: str, *, missing_code: str) -> tuple[dict[str, object], bytes]:
    path = safe_path(feature, relative)
    if not path.exists():
        raise EvidenceError(missing_code, f"缺少证据文件：{relative}")
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_JSON_BYTES:
        raise EvidenceError("EVIDENCE_UNSAFE_PATH", f"证据文件不安全或过大：{relative}")
    try:
        with os.fdopen(os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)), "rb") as handle:
            opened = os.fstat(handle.fileno())
            if not stat.S_ISREG(opened.st_mode) or opened.st_size != info.st_size:
                raise EvidenceError("EVIDENCE_UNSAFE_PATH", f"证据文件读取时发生变化：{relative}")
            data = handle.read(MAX_JSON_BYTES + 1)
        if len(data) > MAX_JSON_BYTES:
            raise EvidenceError("EVIDENCE_UNSAFE_PATH", f"证据文件过大：{relative}")
        value = json.loads(data.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise EvidenceError("EVIDENCE_JSON_INVALID", f"JSON 损坏：{relative}") from exc
    if not isinstance(value, dict):
        raise EvidenceError("EVIDENCE_JSON_INVALID", f"JSON 顶层必须是对象：{relative}")
    return value, data


def _index_revision(index: Mapping[str, object]) -> str:
    return content_id({key: value for key, value in index.items() if key != "revision"})


def _with_revision(index: Mapping[str, object]) -> dict[str, object]:
    value = dict(index)
    value["revision"] = _index_revision(value)
    return value


def _empty_index(feature: Path) -> dict[str, object]:
    return _with_revision(
        {
            "schemaVersion": SCHEMA_VERSION,
            "featureSlug": feature.name,
            "nextSequence": 1,
            "tasks": {},
            "latestBatch": None,
            "history": [],
            "archives": [],
            "legacyArchives": [],
            "summary": {"records": 0, "tasks": 0, "batches": 0},
        }
    )


def _validate_ref(ref: object, *, kind: str, subject: str | None = None) -> dict[str, object]:
    if not isinstance(ref, dict):
        raise EvidenceError("EVIDENCE_REFERENCE_INVALID", "证据引用必须是对象")
    identifier, path = ref.get("id"), ref.get("path")
    digest = _hex(identifier) if isinstance(identifier, str) else _hex("")
    expected = (
        f"evidence/tasks/{subject}/{digest}.json"
        if kind == "taskEvidence"
        else f"evidence/batches/{digest}.json"
        if kind == "verificationBatch"
        else f"evidence/objects/{digest}.json"
    )
    if path != expected:
        raise EvidenceError("EVIDENCE_REFERENCE_INVALID", f"引用路径与内容 ID 不匹配：{path}")
    if kind in {"taskEvidence", "verificationBatch"} and (
        not isinstance(ref.get("sequence"), int) or int(ref["sequence"]) < 1
    ):
        raise EvidenceError("EVIDENCE_REFERENCE_INVALID", "证据引用缺少 sequence")
    return ref


def _validate_history_entry(entry: object) -> dict[str, object]:
    if not isinstance(entry, dict) or not isinstance(entry.get("sequence"), int) or entry["sequence"] < 1:
        raise EvidenceError("EVIDENCE_INDEX_INVALID", "历史索引项无效")
    kind = entry.get("kind")
    subject = entry.get("subject") if kind == "task" else None
    if kind == "task" and (not isinstance(subject, str) or not TASK_RE.fullmatch(subject)):
        raise EvidenceError("EVIDENCE_INDEX_INVALID", "历史任务编号无效")
    if kind not in {"task", "batch"}:
        raise EvidenceError("EVIDENCE_INDEX_INVALID", "历史记录 kind 无效")
    _validate_ref(
        entry,
        kind="taskEvidence" if kind == "task" else "verificationBatch",
        subject=subject,
    )
    return entry


def _validate_index(index: object, feature: Path) -> dict[str, object]:
    if not isinstance(index, dict):
        raise EvidenceError("EVIDENCE_INDEX_INVALID", "索引顶层必须是对象")
    required = {
        "schemaVersion", "featureSlug", "revision", "nextSequence", "tasks",
        "latestBatch", "history", "archives", "legacyArchives", "summary",
    }
    if (
        set(index) != required
        or index.get("schemaVersion") != SCHEMA_VERSION
        or index.get("featureSlug") != feature.name
        or not isinstance(index.get("nextSequence"), int)
        or int(index["nextSequence"]) < 1
        or not isinstance(index.get("tasks"), dict)
        or not isinstance(index.get("history"), list)
        or not isinstance(index.get("archives"), list)
        or not isinstance(index.get("legacyArchives"), list)
        or not isinstance(index.get("summary"), dict)
    ):
        raise EvidenceError("EVIDENCE_INDEX_INVALID", "索引结构或 Feature 绑定无效")
    if index.get("revision") != _index_revision(index):
        raise EvidenceError("EVIDENCE_INDEX_INVALID", "索引 revision 不匹配")
    for task_id, pointers in index["tasks"].items():
        if not isinstance(task_id, str) or not TASK_RE.fullmatch(task_id) or not isinstance(pointers, dict):
            raise EvidenceError("EVIDENCE_INDEX_INVALID", "任务索引无效")
        if set(pointers) - {"latestObserved", "latestTrusted"} or "latestObserved" not in pointers:
            raise EvidenceError("EVIDENCE_INDEX_INVALID", f"任务指针无效：{task_id}")
        _validate_ref(pointers["latestObserved"], kind="taskEvidence", subject=task_id)
        if pointers.get("latestTrusted") is not None:
            _validate_ref(pointers["latestTrusted"], kind="taskEvidence", subject=task_id)
    if index["latestBatch"] is not None:
        _validate_ref(index["latestBatch"], kind="verificationBatch")
    all_history = [_validate_history_entry(entry) for entry in index["history"]]
    for archive in index["archives"]:
        if (
            not isinstance(archive, dict)
            or set(archive) != {"id", "path", "count"}
            or not isinstance(archive.get("count"), int)
            or archive["count"] < 1
            or archive.get("path") != f"testing/archive/history-{_hex(archive.get('id'))}.json"
        ):
            raise EvidenceError("EVIDENCE_INDEX_INVALID", "历史归档引用无效")
        archive_path = safe_path(feature, str(archive["path"]).removeprefix("testing/"))
        if not archive_path.is_file() or archive_path.is_symlink():
            raise EvidenceError("EVIDENCE_REFERENCE_MISSING", f"缺少历史归档：{archive['path']}")
        try:
            archive_data = archive_path.read_bytes()
            document = json.loads(archive_data.decode("utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError, RecursionError) as exc:
            raise EvidenceError("EVIDENCE_JSON_INVALID", f"历史归档损坏：{archive['path']}") from exc
        if (
            not isinstance(document, dict)
            or set(document) != {"schemaVersion", "kind", "history"}
            or document.get("schemaVersion") != SCHEMA_VERSION
            or document.get("kind") != "historyArchive"
            or not isinstance(document.get("history"), list)
            or len(document["history"]) != archive["count"]
            or archive_data != canonical_bytes(document) + b"\n"
            or content_id(document) != archive["id"]
        ):
            raise EvidenceError("EVIDENCE_HASH_MISMATCH", f"历史归档哈希不一致：{archive['path']}")
        for entry in document["history"]:
            validated = _validate_history_entry(entry)
            _expand_record(feature, validated, "taskEvidence" if validated["kind"] == "task" else "verificationBatch",
                           validated["subject"] if validated["kind"] == "task" else None)
            all_history.append(validated)
    for archive in index["legacyArchives"]:
        if (
            not isinstance(archive, dict)
            or set(archive) != {"id", "path", "bytes"}
            or not isinstance(archive.get("bytes"), int)
            or archive["bytes"] < 0
            or archive.get("path") != f"archive/verification-v1-{_hex(archive.get('id'))}.md"
        ):
            raise EvidenceError("EVIDENCE_INDEX_INVALID", "旧文档归档引用无效")
        archive_path = safe_path(feature, str(archive["path"]))
        if not archive_path.is_file() or archive_path.is_symlink():
            raise EvidenceError("EVIDENCE_REFERENCE_MISSING", f"缺少旧文档归档：{archive['path']}")
        if "sha256:" + hashlib.sha256(archive_path.read_bytes()).hexdigest() != archive["id"]:
            raise EvidenceError("EVIDENCE_HASH_MISMATCH", f"旧文档归档哈希不一致：{archive['path']}")
    sequences = [int(entry["sequence"]) for entry in all_history]
    if len(sequences) != len(set(sequences)) or (sequences and index["nextSequence"] <= max(sequences)):
        raise EvidenceError("EVIDENCE_INDEX_INVALID", "历史 sequence 冲突或 nextSequence 无效")
    by_sequence = {int(entry["sequence"]): entry for entry in all_history}
    for task_id, pointers in index["tasks"].items():
        for name in ("latestObserved", "latestTrusted"):
            pointer = pointers.get(name)
            if pointer is None:
                continue
            entry = by_sequence.get(int(pointer["sequence"]))
            if entry is None or entry.get("kind") != "task" or entry.get("subject") != task_id or entry.get("id") != pointer.get("id"):
                raise EvidenceError("EVIDENCE_INDEX_INVALID", f"任务指针不在历史中：{task_id}/{name}")
    if index["latestBatch"] is not None:
        pointer = index["latestBatch"]
        entry = by_sequence.get(int(pointer["sequence"]))
        if entry is None or entry.get("kind") != "batch" or entry.get("id") != pointer.get("id"):
            raise EvidenceError("EVIDENCE_INDEX_INVALID", "最新批次指针不在历史中")
    summary = index["summary"]
    if (
        set(summary) != {"records", "tasks", "batches"}
        or not all(isinstance(summary.get(name), int) and summary[name] >= 0 for name in summary)
        or summary["records"] != len(all_history)
        or summary["tasks"] != len(index["tasks"])
        or summary["batches"] != sum(entry.get("kind") == "batch" for entry in all_history)
    ):
        raise EvidenceError("EVIDENCE_INDEX_INVALID", "索引摘要与历史不一致")
    return index


def load_index(feature: Path, *, required: bool = True, allow_transaction: bool = False) -> dict[str, object]:
    marker = safe_path(feature, "evidence/.transaction.json")
    preparing = safe_path(feature, ".evidence-transaction.json")
    if not allow_transaction and (marker.exists() or marker.is_symlink() or preparing.exists() or preparing.is_symlink()):
        raise EvidenceError("EVIDENCE_TRANSACTION_INCOMPLETE", "证据事务尚未恢复")
    path = safe_path(feature, "evidence/index.json")
    if not path.exists() and not required:
        return _empty_index(Path(feature))
    try:
        index, data = _read_json(feature, "evidence/index.json", missing_code="EVIDENCE_INDEX_MISSING")
    except EvidenceError as exc:
        if exc.code in {"EVIDENCE_JSON_INVALID", "EVIDENCE_REFERENCE_MISSING"}:
            raise EvidenceError("EVIDENCE_INDEX_INVALID", str(exc)) from exc
        raise
    if data != canonical_bytes(index) + b"\n":
        raise EvidenceError("EVIDENCE_INDEX_INVALID", "索引不是规范 JSON")
    validated = _validate_index(index, Path(feature))
    _validate_active_references(Path(feature), validated)
    return validated


def _object_document(kind: str, value: object) -> dict[str, object]:
    if kind == "command":
        if not isinstance(value, str) or not value or "\0" in value:
            raise EvidenceError("EVIDENCE_RECORD_INVALID", "命令无效")
        return {"schemaVersion": SCHEMA_VERSION, "kind": kind, "command": value}
    if kind == "codeState":
        if not isinstance(value, dict) or not value or not all(
            isinstance(name, str) and name and isinstance(digest, str) and DIGEST_RE.fullmatch(digest)
            for name, digest in value.items()
        ):
            raise EvidenceError("EVIDENCE_RECORD_INVALID", "代码状态无效")
        return {"schemaVersion": SCHEMA_VERSION, "kind": kind, "repositories": dict(value)}
    if kind == "artifact":
        if not isinstance(value, dict):
            raise EvidenceError("EVIDENCE_RECORD_INVALID", "产物描述无效")
        path = value.get("path")
        if (
            not isinstance(path, str) or not path or PurePosixPath(path).is_absolute()
            or ".." in PurePosixPath(path).parts or "\0" in path
            or not isinstance(value.get("sha256"), str) or not DIGEST_RE.fullmatch(value["sha256"])
            or not isinstance(value.get("bytes"), int) or value["bytes"] < 0
            or not isinstance(value.get("type"), str) or not value["type"]
        ):
            raise EvidenceError("EVIDENCE_RECORD_INVALID", "产物字段无效")
        if set(value) != {"path", "sha256", "bytes", "type"}:
            raise EvidenceError("EVIDENCE_RECORD_INVALID", "产物包含未知字段")
        return {
            "schemaVersion": SCHEMA_VERSION,
            "kind": kind,
            "path": value["path"],
            "sha256": value["sha256"],
            "bytes": value["bytes"],
            "type": value["type"],
        }
    raise EvidenceError("EVIDENCE_RECORD_INVALID", f"不支持的对象类型：{kind}")


def _object_ref(document: dict[str, object]) -> tuple[str, tuple[str, bytes]]:
    identifier = content_id(document)
    relative = f"evidence/objects/{_hex(identifier)}.json"
    return identifier, (relative, canonical_bytes(document) + b"\n")


def _normalize_record(raw: Mapping[str, object], *, migrating: bool = False) -> tuple[dict[str, object], list[tuple[str, bytes]]]:
    kind = raw.get("kind")
    if kind not in {"taskEvidence", "verificationBatch"}:
        raise EvidenceError("EVIDENCE_RECORD_INVALID", "记录 kind 无效")
    recorded_at = raw.get("recordedAt")
    checks = raw.get("checks")
    artifacts = raw.get("artifactRefs", [])
    code_state = raw.get("codeState")
    migrated = migrating and raw.get("origin") is not None
    if (
        not isinstance(recorded_at, str) or not recorded_at
        or not isinstance(checks, list) or (not checks and not migrated)
        or not isinstance(artifacts, list)
    ):
        raise EvidenceError("EVIDENCE_RECORD_INVALID", "记录字段不完整")
    allowed = (
        {"kind", "taskId", "recordedAt", "repository", "codeState", "checks", "artifactRefs", "validationKind", "deliveryCheck", "result", "origin", "issues"}
        if kind == "taskEvidence"
        else {"kind", "recordedAt", "overallResult", "reviewResult", "codeState", "checks", "blockers", "artifactRefs", "origin", "issues"}
    )
    if set(raw) - allowed:
        raise EvidenceError("EVIDENCE_RECORD_INVALID", "记录包含未知字段")
    issues = raw.get("issues", [])
    if (issues and not migrated) or not isinstance(issues, list) or not all(
        isinstance(item, dict)
        and {"severity", "code", "message", "path", "line"} <= set(item)
        and isinstance(item.get("code"), str)
        and isinstance(item.get("message"), str)
        for item in issues
    ):
        raise EvidenceError("EVIDENCE_RECORD_INVALID", "迁移诊断无效")
    origin = raw.get("origin")
    if origin is not None and not migrating:
        raise EvidenceError("EVIDENCE_RECORD_INVALID", "origin 只允许迁移器写入")
    if origin is not None and (
        not isinstance(origin, dict)
        or set(origin) != {"path", "startLine", "endLine"}
        or not isinstance(origin.get("path"), str)
        or not origin["path"].startswith("testing/archive/")
        or PurePosixPath(origin["path"]).is_absolute()
        or ".." in PurePosixPath(origin["path"]).parts
        or not isinstance(origin.get("startLine"), int)
        or not isinstance(origin.get("endLine"), int)
        or origin["startLine"] < 1
        or origin["endLine"] < origin["startLine"]
    ):
        raise EvidenceError("EVIDENCE_RECORD_INVALID", "origin 无效")
    try:
        stamp = datetime.fromisoformat(recorded_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise EvidenceError("EVIDENCE_RECORD_INVALID", "recordedAt 不是 ISO-8601") from exc
    if stamp.tzinfo is None:
        raise EvidenceError("EVIDENCE_RECORD_INVALID", "recordedAt 必须包含时区")
    object_writes: list[tuple[str, bytes]] = []
    try:
        code_ref, output = _object_ref(_object_document("codeState", code_state))
        object_writes.append(output)
    except EvidenceError:
        if not migrated:
            raise
        code_ref = None
    normalized_checks = []
    for check in checks:
        if not isinstance(check, dict):
            raise EvidenceError("EVIDENCE_RECORD_INVALID", "检查项必须是对象")
        check_allowed = (
            {"type", "workingDirectory", "command", "target", "executed", "skipped", "exitStatus", "result"}
            if kind == "taskEvidence"
            else {"workingDirectory", "command", "exitStatus", "result", "duration", "testCount"}
        )
        if set(check) - check_allowed:
            raise EvidenceError("EVIDENCE_RECORD_INVALID", "检查项包含未知字段")
        if not migrated and (
            not isinstance(check.get("workingDirectory"), str) or not check["workingDirectory"]
            or not isinstance(check.get("target"), str) and kind == "taskEvidence"
            or not isinstance(check.get("exitStatus"), int)
            or not isinstance(check.get("result"), str) or not check["result"]
            or "\0" in str(check.get("workingDirectory"))
        ):
            raise EvidenceError("EVIDENCE_RECORD_INVALID", "检查项字段无效")
        if kind == "taskEvidence" and not migrated:
            if check.get("type") not in {"测试", "行为检查", "集成", "结构", "迁移", "静态检查", "编译"}:
                raise EvidenceError("EVIDENCE_RECORD_INVALID", "任务检查类型无效")
            for field in ("executed", "skipped"):
                if check.get(field) is not None and (not isinstance(check[field], int) or check[field] < 0):
                    raise EvidenceError("EVIDENCE_RECORD_INVALID", f"{field} 无效")
        elif not migrated and check.get("testCount") is not None and (not isinstance(check["testCount"], int) or check["testCount"] < 0):
            raise EvidenceError("EVIDENCE_RECORD_INVALID", "testCount 无效")
        try:
            command_ref, output = _object_ref(_object_document("command", check.get("command")))
            object_writes.append(output)
        except EvidenceError:
            if not migrated:
                raise
            command_ref = None
        value = {key: item for key, item in check.items() if key != "command"}
        value["commandRef"] = command_ref
        normalized_checks.append(value)
    artifact_refs = []
    for artifact in artifacts:
        artifact_ref, output = _object_ref(_object_document("artifact", artifact))
        object_writes.append(output)
        artifact_refs.append(artifact_ref)
    record = {
        key: value
        for key, value in raw.items()
        if key not in {"codeState", "checks", "artifactRefs", "trusted"}
    }
    record.update(
        {
            "schemaVersion": SCHEMA_VERSION,
            "codeStateRef": code_ref,
            "checks": normalized_checks,
            "artifactRefs": artifact_refs,
        }
    )
    if kind == "taskEvidence":
        task_id = raw.get("taskId")
        if (
            not isinstance(task_id, str) or not TASK_RE.fullmatch(task_id)
            or not isinstance(raw.get("repository"), str) or not raw["repository"]
            or raw.get("validationKind") not in {"行为", "声明式", "持久化"}
            or raw.get("deliveryCheck") not in {"passed", "failed"}
            or raw.get("result") not in {"passed", "failed", "blocked", "unknown"}
        ):
            raise EvidenceError("EVIDENCE_RECORD_INVALID", "任务证据字段无效")
    elif (
        raw.get("overallResult") not in {"passed", "failed", "blocked", "unknown"}
        or raw.get("reviewResult") not in {"passed", "failed", "unknown"}
        or not isinstance(raw.get("blockers", []), list)
        or not all(isinstance(item, str) for item in raw.get("blockers", []))
    ):
        raise EvidenceError("EVIDENCE_RECORD_INVALID", "验证批次字段无效")
    return record, object_writes


def _intrinsically_trusted(record: Mapping[str, object]) -> bool:
    if record.get("kind") != "taskEvidence" or record.get("deliveryCheck") != "passed" or record.get("result") != "passed":
        return False
    checks = record.get("checks")
    if not isinstance(checks, list) or not checks:
        return False
    successful = set()
    for check in checks:
        if not isinstance(check, Mapping) or check.get("exitStatus") != 0 or str(check.get("result", "")).lstrip().startswith(("失败", "未执行", "不通过", "未通过")):
            return False
        successful.add(check.get("type"))
        if check.get("type") == "测试" and (check.get("executed") in {None, 0} or check.get("skipped") != 0):
            return False
    allowed = {
        "行为": {"测试", "行为检查", "集成", "结构", "迁移"},
        "声明式": {"静态检查", "编译", "测试", "行为检查", "集成", "结构", "迁移"},
        "持久化": {"结构", "迁移", "集成"},
    }
    return bool(successful.intersection(allowed.get(record.get("validationKind"), set())))


def _write_candidates(feature: Path, candidates: list[tuple[str, bytes]]) -> list[tuple[Path, bytes]]:
    outputs = []
    for relative, data in candidates:
        path = safe_path(feature, relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            if path.read_bytes() != data:
                raise EvidenceError("EVIDENCE_HASH_MISMATCH", f"内容地址发生冲突：{relative}")
            continue
        outputs.append((path, data))
    return outputs


def record(
    feature: Path,
    raw: Mapping[str, object],
    *,
    preview: bool = False,
    trusted: bool | None = None,
    _allow_transaction: bool = False,
    _migrating: bool = False,
    _locked: bool = False,
) -> dict[str, object]:
    feature = Path(feature)
    if not preview and not _locked and not _allow_transaction:
        with mutation_lock(feature):
            return record(feature, raw, preview=False, trusted=trusted, _migrating=_migrating, _locked=True)
    index = load_index(feature, required=False, allow_transaction=_allow_transaction)
    normalized, object_candidates = _normalize_record(raw, migrating=_migrating)
    identifier = content_id(normalized)
    kind = normalized["kind"]
    if kind == "taskEvidence":
        task_id = str(normalized["taskId"])
        relative = f"evidence/tasks/{task_id}/{_hex(identifier)}.json"
        subject = task_id
        result = normalized.get("result")
    else:
        relative = f"evidence/batches/{_hex(identifier)}.json"
        subject = None
        result = normalized.get("overallResult")
    sequence = int(index["nextSequence"])
    ref: dict[str, object] = {
        "id": identifier,
        "path": relative,
        "recordedAt": normalized["recordedAt"],
        "result": result,
        "sequence": sequence,
    }
    entry = {"sequence": sequence, "kind": "task" if subject else "batch", "subject": subject, **ref}
    history = [*index["history"], entry]
    tasks = {key: dict(value) for key, value in index["tasks"].items()}
    latest_batch = index["latestBatch"]
    if subject:
        pointers = dict(tasks.get(subject, {}))
        pointers["latestObserved"] = ref
        is_trusted = trusted if trusted is not None else _intrinsically_trusted(raw)
        if is_trusted:
            pointers["latestTrusted"] = ref
        tasks[subject] = pointers
    else:
        latest_batch = ref
    records = int(index["summary"].get("records", len(index["history"]))) + 1
    batches = int(index["summary"].get("batches", 0)) + (1 if subject is None else 0)
    updated = _with_revision(
        {
            **index,
            "nextSequence": sequence + 1,
            "tasks": tasks,
            "latestBatch": latest_batch,
            "history": history,
            "summary": {"records": records, "tasks": len(tasks), "batches": batches},
        }
    )
    if preview:
        return {"changed": True, "id": identifier, "sequence": sequence, "indexRevision": updated["revision"]}
    testing = feature / "testing"
    testing.mkdir(parents=True, exist_ok=True)
    candidates = [
        *object_candidates,
        (relative, canonical_bytes(normalized) + b"\n"),
    ]
    outputs = _write_candidates(feature, candidates)
    index_path = safe_path(feature, "evidence/index.json")
    index_path.parent.mkdir(parents=True, exist_ok=True)
    outputs.append((index_path, canonical_bytes(updated) + b"\n"))
    try:
        atomic_write_many(outputs)
    except WorkspaceError as exc:
        raise EvidenceError("EVIDENCE_WRITE_FAILED", str(exc)) from exc
    return {"changed": True, "id": identifier, "sequence": sequence, "indexRevision": updated["revision"]}


def _load_content(feature: Path, ref: Mapping[str, object], *, kind: str, subject: str | None = None) -> dict[str, object]:
    _validate_ref(ref, kind=kind, subject=subject)
    relative = str(ref["path"])
    value, data = _read_json(feature, relative, missing_code="EVIDENCE_REFERENCE_MISSING")
    if data != canonical_bytes(value) + b"\n" or content_id(value) != ref["id"]:
        raise EvidenceError("EVIDENCE_HASH_MISMATCH", f"证据哈希不一致：{relative}")
    if value.get("schemaVersion") != SCHEMA_VERSION or value.get("kind") != kind:
        raise EvidenceError("EVIDENCE_REFERENCE_INVALID", f"引用对象类型不匹配：{relative}")
    return value


def _load_object(feature: Path, identifier: object, kind: str) -> dict[str, object]:
    digest = _hex(identifier) if isinstance(identifier, str) else _hex("")
    ref = {"id": identifier, "path": f"evidence/objects/{digest}.json"}
    return _load_content(feature, ref, kind=kind)


def _expand_record(feature: Path, ref: Mapping[str, object], kind: str, subject: str | None = None) -> dict[str, object]:
    value = dict(_load_content(feature, ref, kind=kind, subject=subject))
    code_ref = value.pop("codeStateRef", None)
    value["codeState"] = _load_object(feature, code_ref, "codeState")["repositories"] if code_ref is not None else None
    checks = []
    for check in value.get("checks", []):
        if not isinstance(check, dict):
            raise EvidenceError("EVIDENCE_REFERENCE_INVALID", "检查引用无效")
        command_ref = check.get("commandRef")
        expanded = {key: item for key, item in check.items() if key != "commandRef"}
        expanded["command"] = _load_object(feature, command_ref, "command")["command"] if command_ref is not None else None
        checks.append(expanded)
    value["checks"] = checks
    artifacts = []
    for identifier in value.get("artifactRefs", []):
        artifact = _load_object(feature, identifier, "artifact")
        artifacts.append({key: item for key, item in artifact.items() if key not in {"schemaVersion", "kind"}})
    value["artifactRefs"] = artifacts
    if kind == "taskEvidence" and value.get("taskId") != subject:
        raise EvidenceError("EVIDENCE_REFERENCE_INVALID", "任务引用与记录 taskId 不匹配")
    value["id"] = ref["id"]
    value["source"] = value.get("origin") or {"path": str(ref["path"]), "startLine": 1, "endLine": 1}
    return value


def _validate_active_references(feature: Path, index: Mapping[str, object]) -> None:
    for entry in index["history"]:
        kind = "taskEvidence" if entry.get("kind") == "task" else "verificationBatch"
        subject = entry.get("subject") if kind == "taskEvidence" else None
        _expand_record(feature, entry, kind, subject if isinstance(subject, str) else None)


def load_store(feature: Path, *, allow_transaction: bool = False) -> dict[str, object]:
    index = load_index(feature, allow_transaction=allow_transaction)
    latest = {
        task_id: _expand_record(feature, pointers["latestObserved"], "taskEvidence", task_id)
        for task_id, pointers in index["tasks"].items()
    }
    batch = (
        _expand_record(feature, index["latestBatch"], "verificationBatch")
        if index["latestBatch"] is not None
        else None
    )
    return {"index": index, "latestByTask": latest, "latestBatch": batch}


def encode_cursor(index_revision: str, next_sequence: int, filter_digest: str) -> str:
    payload = canonical_bytes({"version": 1, "indexRevision": index_revision, "nextSequence": next_sequence, "filterDigest": filter_digest})
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def decode_cursor(value: str) -> dict[str, object]:
    try:
        padded = value + "=" * (-len(value) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
    except (ValueError, UnicodeError, json.JSONDecodeError) as exc:
        raise EvidenceError("EVIDENCE_CURSOR_INVALID", "cursor 无效") from exc
    if not isinstance(payload, dict) or set(payload) != {"version", "indexRevision", "nextSequence", "filterDigest"}:
        raise EvidenceError("EVIDENCE_CURSOR_INVALID", "cursor 结构无效")
    if (
        payload.get("version") != 1
        or not isinstance(payload.get("indexRevision"), str) or not DIGEST_RE.fullmatch(payload["indexRevision"])
        or not isinstance(payload.get("filterDigest"), str) or not DIGEST_RE.fullmatch(payload["filterDigest"])
        or not isinstance(payload.get("nextSequence"), int) or payload["nextSequence"] < 1
    ):
        raise EvidenceError("EVIDENCE_CURSOR_INVALID", "cursor 字段无效")
    return payload


def _filter_digest(task_id: str | None, batch_id: str | None) -> str:
    return content_id({"task": task_id, "batch": batch_id})


def get_evidence(
    feature: Path, *, task_id: str | None = None, batch_id: str | None = None,
    evidence_id: str | None = None,
) -> dict[str, object]:
    if (task_id is None) == (batch_id is None) or (evidence_id is not None and task_id is None):
        raise EvidenceError("EVIDENCE_RECORD_INVALID", "必须指定 task 或 batch")
    if evidence_id is not None:
        _hex(evidence_id)
        cursor = None
        while True:
            page = history_page(feature, task_id=task_id, cursor=cursor, limit=100)
            entry = next((item for item in page["items"] if item.get("id") == evidence_id), None)
            if entry is not None:
                return _expand_record(feature, entry, "taskEvidence", task_id)
            cursor = page["nextCursor"]
            if cursor is None:
                raise EvidenceError("EVIDENCE_RECORD_NOT_FOUND", f"任务历史证据不存在：{task_id}/{evidence_id}")
    store = load_store(feature)
    if task_id is not None:
        if task_id not in store["latestByTask"]:
            raise EvidenceError("EVIDENCE_RECORD_NOT_FOUND", f"任务证据不存在：{task_id}")
        return store["latestByTask"][task_id]
    latest = store["latestBatch"]
    if latest is None or latest.get("id") != batch_id:
        index = store["index"]
        page = history_page(feature, batch_id=batch_id, limit=100)
        match = page["items"][0] if page["items"] else None
        if match is None:
            raise EvidenceError("EVIDENCE_RECORD_NOT_FOUND", f"验证批次不存在：{batch_id}")
        return _expand_record(feature, match, "verificationBatch")
    return latest


def history_page(
    feature: Path,
    *,
    cursor: str | None = None,
    task_id: str | None = None,
    batch_id: str | None = None,
    limit: int = 20,
) -> dict[str, object]:
    if not 1 <= limit <= 100 or (task_id is not None and batch_id is not None):
        raise EvidenceError("EVIDENCE_CURSOR_INVALID", "历史分页参数无效")
    index = load_index(feature)
    digest = _filter_digest(task_id, batch_id)
    entries = [
        item
        for item in index["history"]
        if (task_id is None or item.get("subject") == task_id)
        and (batch_id is None or item.get("id") == batch_id)
    ]
    for archive in index.get("archives", []):
        if not isinstance(archive, dict) or not isinstance(archive.get("path"), str):
            raise EvidenceError("EVIDENCE_INDEX_INVALID", "归档索引项无效")
        path = safe_path(feature, str(archive["path"]).removeprefix("testing/"))
        if not path.is_file() or path.is_symlink():
            raise EvidenceError("EVIDENCE_REFERENCE_MISSING", f"缺少历史归档：{archive['path']}")
        try:
            data = path.read_bytes()
            document = json.loads(data.decode("utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError, RecursionError) as exc:
            raise EvidenceError("EVIDENCE_JSON_INVALID", f"历史归档损坏：{archive['path']}") from exc
        if not isinstance(document, dict) or document.get("kind") != "historyArchive" or content_id(document) != archive.get("id"):
            raise EvidenceError("EVIDENCE_HASH_MISMATCH", f"历史归档哈希不一致：{archive['path']}")
        entries.extend(
            item for item in document.get("history", [])
            if isinstance(item, dict)
            and (task_id is None or item.get("subject") == task_id)
            and (batch_id is None or item.get("id") == batch_id)
        )
    entries = list({int(item["sequence"]): item for item in entries}.values())
    entries.sort(key=lambda item: int(item["sequence"]), reverse=True)
    start = 0
    if cursor is not None:
        payload = decode_cursor(cursor)
        if payload.get("version") != 1 or payload.get("indexRevision") != index["revision"] or payload.get("filterDigest") != digest:
            raise EvidenceError("EVIDENCE_CURSOR_INVALID", "cursor 已失效")
        start = next((i for i, item in enumerate(entries) if item["sequence"] < int(payload["nextSequence"])), len(entries))
    page = entries[start : start + limit]
    next_cursor = None
    if start + limit < len(entries):
        next_cursor = encode_cursor(index["revision"], int(page[-1]["sequence"]), digest)
    return {
        "items": page,
        "total": len(entries),
        "hasMore": next_cursor is not None,
        "nextCursor": next_cursor,
        "indexRevision": index["revision"],
    }


def render_summary(
    feature: Path,
    *,
    state: Mapping[str, object] | None = None,
    action_summaries: list[Mapping[str, object]] | None = None,
    _allow_transaction: bool = False,
) -> str:
    store = load_store(feature, allow_transaction=_allow_transaction)
    state = state or {}
    trusted = state.get("trustedProgress")
    lines = ["# 验证摘要", "", "## 当前验证结论", ""]
    passed = state.get("verificationPassed")
    lines.append(f"- verificationPassed：{'通过' if passed is True else '未通过或未执行'}")
    if isinstance(trusted, Mapping):
        lines.append(f"- trustedProgress：{trusted.get('completed', 0)}/{trusted.get('total', 0)}")
    lines.extend(["", "## 任务完成概览", "", f"- 已记录任务证据：{len(store['latestByTask'])}"])
    failures = [item for item in store["latestByTask"].values() if item.get("result") in {"failed", "blocked"}]
    lines.extend(["", "## 最新失败和阻塞", ""])
    if failures:
        for item in failures[:10]:
            lines.append(f"- {item.get('taskId')}：{item.get('result')}")
        if len(failures) > 10:
            lines.append(f"- 其余 {len(failures) - 10} 项通过 history 查询")
    else:
        lines.append("- 无")
    blockers = state.get("blockers")
    if isinstance(blockers, list):
        for blocker in blockers[:10]:
            if isinstance(blocker, Mapping):
                lines.append(f"- {blocker.get('code', 'BLOCKED')}：{blocker.get('message', '')}")
            elif isinstance(blocker, str):
                lines.append(f"- {blocker}")
    lines.extend(["", "## 当前代码状态", ""])
    code_state = state.get("codeState")
    if isinstance(code_state, Mapping):
        for name, digest in list(sorted(code_state.items()))[:20]:
            lines.append(f"- {name}：{digest}")
    else:
        lines.append("- 未检查")
    lines.extend(["", "## 最终报告或产物入口", ""])
    artifacts = state.get("artifacts")
    if isinstance(artifacts, list) and artifacts:
        for artifact in artifacts[:20]:
            if isinstance(artifact, Mapping):
                lines.append(f"- {artifact.get('path', '(unknown)')}")
    else:
        lines.append("- 无")
    lines.extend(["", "## Workflow Action", ""])
    actions = action_summaries or []
    if actions:
        for action in actions[-1:]:
            lines.append(f"- {action.get('stage')}：{action.get('status')}（{action.get('summary', '')}）")
    else:
        lines.append("- 无")
    lines.extend(["", "## 历史证据", "", f"- 共 {store['index']['summary'].get('records', len(store['index']['history']))} 条；使用 `kit.py verify history` 分页查询。"])
    result = "\n".join(lines) + "\n"
    if len(result.splitlines()) > 200:
        raise EvidenceError("EVIDENCE_SUMMARY_LIMIT", "人类摘要超过 200 行")
    return result


def _backup_file(source: Path, backup: Path) -> None:
    if source.is_symlink() or not source.is_file() or backup.is_symlink():
        raise EvidenceError("EVIDENCE_UNSAFE_PATH", f"备份来源或目标不安全：{source}")
    data = source.read_bytes()
    if backup.exists():
        if not backup.is_file() or backup.read_bytes() != data:
            raise EvidenceError("EVIDENCE_CONFLICT", f"现有备份与来源不一致：{backup}")
        return
    atomic_write_many(((backup, data),))


def _file_digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _write_manifest(path: Path, operation: str, files: Mapping[str, Path], **extra: object) -> None:
    value = {
        "schemaVersion": 1,
        "operation": operation,
        "files": {name: {"path": source.name, "sha256": _file_digest(source)} for name, source in files.items()},
        **extra,
    }
    text = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if path.exists() or path.is_symlink():
        if path.is_symlink() or not path.is_file() or path.read_text(encoding="utf-8") != text:
            raise EvidenceError("EVIDENCE_CONFLICT", f"现有事务 manifest 不一致：{path}")
        return
    atomic_write_many(((path, text),))


def _verified_backups(transaction: Path, operation: str) -> dict[str, Path]:
    manifest = transaction / "manifest.json"
    if manifest.is_symlink() or not manifest.is_file():
        raise EvidenceError("EVIDENCE_TRANSACTION_INCOMPLETE", "事务 manifest 缺失")
    try:
        value = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise EvidenceError("EVIDENCE_TRANSACTION_INCOMPLETE", "事务 manifest 损坏") from exc
    files = value.get("files") if isinstance(value, dict) else None
    if not isinstance(value, dict) or value.get("schemaVersion") != 1 or value.get("operation") != operation or not isinstance(files, dict):
        raise EvidenceError("EVIDENCE_TRANSACTION_INCOMPLETE", "事务 manifest 无效")
    result = {}
    for name, item in files.items():
        if not isinstance(name, str) or not isinstance(item, dict) or set(item) != {"path", "sha256"} or not isinstance(item.get("path"), str):
            raise EvidenceError("EVIDENCE_TRANSACTION_INCOMPLETE", "事务备份引用无效")
        path = transaction / item["path"]
        if path.parent != transaction or path.is_symlink() or not path.is_file() or _file_digest(path) != item.get("sha256"):
            raise EvidenceError("EVIDENCE_TRANSACTION_INCOMPLETE", f"事务备份哈希不一致：{name}")
        result[name] = path
    return result


def _feature_policy(feature: Path) -> str | None:
    plan = feature / "plans" / "implementation.md"
    if not plan.is_file() or plan.is_symlink():
        return None
    text = plan.read_text(encoding="utf-8")
    match = re.search(r"completion-policy:\s*([^\s>]+)", text)
    if match:
        return match.group(1)
    match = re.search(r"完成门禁：`?([^`\s]+)", text)
    return match.group(1) if match else None


def _recover(feature: Path, operation: str) -> bool:
    preparing = safe_path(feature, ".evidence-transaction.json")
    marker = preparing if preparing.exists() or preparing.is_symlink() else safe_path(feature, "evidence/.transaction.json")
    if not marker.exists() and not marker.is_symlink():
        return False
    if marker.is_symlink() or not marker.is_file():
        raise EvidenceError("EVIDENCE_UNSAFE_PATH", "恢复标记不安全")
    try:
        value = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise EvidenceError("EVIDENCE_TRANSACTION_INCOMPLETE", "恢复标记损坏") from exc
    if not isinstance(value, dict) or value.get("operation") != operation:
        raise EvidenceError("EVIDENCE_TRANSACTION_INCOMPLETE", "存在其他未完成证据事务")
    if operation == "migrate":
        identifier = value.get("archiveId")
        digest = _hex(identifier) if isinstance(identifier, str) else _hex("")
        transaction = safe_path(feature, f"archive/transactions/{digest}")
        backups = _verified_backups(transaction, "migrate")
        old_verification = backups.get("verification")
        old_plan = backups.get("plan")
        if old_verification is None:
            raise EvidenceError("EVIDENCE_TRANSACTION_INCOMPLETE", "迁移备份缺失")
        outputs: list[tuple[Path, bytes]] = [(feature / "testing" / "verification.md", old_verification.read_bytes())]
        if old_plan is not None:
            outputs.append((feature / "plans" / "implementation.md", old_plan.read_bytes()))
        atomic_write_many(outputs)
        evidence = feature / "testing" / "evidence"
        if evidence.exists() or evidence.is_symlink():
            if evidence.is_symlink() or not evidence.is_dir():
                raise EvidenceError("EVIDENCE_UNSAFE_PATH", "待恢复 evidence 目录不安全")
            shutil.rmtree(evidence)
        preparing.unlink(missing_ok=True)
        return True
    archive = value.get("archive")
    if not isinstance(archive, str) or not archive.startswith("testing/archive/history-"):
        raise EvidenceError("EVIDENCE_TRANSACTION_INCOMPLETE", "压缩恢复标记无效")
    digest = Path(archive).stem.removeprefix("history-")
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise EvidenceError("EVIDENCE_TRANSACTION_INCOMPLETE", "压缩恢复 ID 无效")
    transaction = safe_path(feature, f"archive/transactions/compact-{digest}")
    backups = _verified_backups(transaction, "compact")
    old_index = backups.get("index")
    if old_index is None:
        raise EvidenceError("EVIDENCE_TRANSACTION_INCOMPLETE", "压缩索引备份缺失")
    outputs = [(feature / "testing" / "evidence" / "index.json", old_index.read_bytes())]
    old_verification = backups.get("verification")
    if old_verification is not None:
        outputs.append((feature / "testing" / "verification.md", old_verification.read_bytes()))
    atomic_write_many(outputs)
    marker.unlink()
    safe_path(feature, "evidence/.transaction.json").unlink(missing_ok=True)
    preparing.unlink(missing_ok=True)
    return True


def recover_transaction(feature: Path, operation: str, *, _locked: bool = False) -> bool:
    if operation not in {"migrate", "compact"}:
        raise EvidenceError("EVIDENCE_ARGUMENT_INVALID", "未知事务类型")
    if not _locked:
        with mutation_lock(feature):
            return recover_transaction(feature, operation, _locked=True)
    return _recover(Path(feature), operation)


def migrate_feature(
    feature: Path,
    *,
    preview: bool = True,
    task_records: list[Mapping[str, object]] | None = None,
    batch_record: Mapping[str, object] | None = None,
    _locked: bool = False,
) -> dict[str, object]:
    """Convert legacy Markdown records without deleting their original bytes."""
    feature = Path(feature)
    if not preview and not _locked:
        with mutation_lock(feature):
            return migrate_feature(feature, preview=False, task_records=task_records, batch_record=batch_record, _locked=True)
    if not preview:
        _recover(feature, "migrate")
    if _feature_policy(feature) == "task-evidence-v2":
        raise EvidenceError("EVIDENCE_ALREADY_V2", "Feature 已是 task-evidence-v2")
    verification = feature / "testing" / "verification.md"
    if verification.is_symlink() or (verification.exists() and not verification.is_file()):
        raise EvidenceError("EVIDENCE_UNSAFE_PATH", "旧验证记录不安全")
    if not verification.is_file():
        raise EvidenceError("EVIDENCE_INDEX_MISSING", "缺少旧 verification.md")
    evidence_dir = feature / "testing" / "evidence"
    if evidence_dir.exists() or evidence_dir.is_symlink():
        raise EvidenceError("EVIDENCE_CONFLICT", "迁移目标 evidence 目录已存在")
    text = verification.read_text(encoding="utf-8")
    from workspace_verification import describe_task_evidence_document, describe_verification_document, evaluate_task_evidence
    from workspace_status import SECTION_RE, VERIFICATION_RECORD_RE

    described_tasks = describe_task_evidence_document(text)["records"]
    batch_records: list[Mapping[str, object]] = []
    for match in VERIFICATION_RECORD_RE.finditer(text):
        boundary = SECTION_RE.search(text, match.end())
        segment = text[match.start():boundary.start() if boundary else len(text)].strip()
        parsed = describe_verification_document(segment)["selectedBatch"]
        if parsed is None:
            continue
        offset = text.count("\n", 0, match.start())
        source = {**parsed["source"], "startLine": parsed["source"]["startLine"] + offset,
                  "endLine": parsed["source"]["endLine"] + offset}
        stamp = parsed["recordedAt"]
        if parsed["completeness"] == "legacy":
            stamp += "T00:00:00Z"
        state_match = re.search(r"^- 代码状态：(.*)$", segment, re.MULTILINE)
        try:
            batch_state = json.loads(state_match.group(1)) if state_match else None
        except json.JSONDecodeError:
            batch_state = None
        batch_records.append({
            "kind": "verificationBatch", "recordedAt": stamp,
            "overallResult": parsed["recordedResult"], "reviewResult": parsed["recordedReview"],
            "codeState": batch_state,
            "checks": [
                {**{key: value for key, value in check.items() if key not in {"id", "source", "exitStatus"}},
                 "exitStatus": int(check["exitStatus"]) if str(check.get("exitStatus", "")).isdigit() else -1}
                for check in parsed.get("checks", [])
            ],
            "blockers": [], "artifactRefs": [],
            "origin": {**source, "path": f"testing/archive/verification-v1-{hashlib.sha256(text.encode('utf-8')).hexdigest()}.md"},
            "issues": [
                {"severity": "error", **issue, "line": issue["line"] + offset,
                 "path": f"testing/archive/verification-v1-{hashlib.sha256(text.encode('utf-8')).hexdigest()}.md"}
                for issue in parsed.get("issues", [])
            ],
        })
    if batch_record is not None:
        batch_records = [batch_record]
    if task_records is None:
        task_records = []
        from workspace_status import plan_analysis

        analysis = plan_analysis(feature / "plans" / "implementation.md")
        by_task = {item["id"]: item for item in analysis["tasks"]}
        root = feature.parents[3]
        mode = "workspace" if (root / ".workspace/workspace.json").is_file() else "maintenance"
        from workspace_status import _task_repository_roots

        repositories = sorted({str(task.get("repository")) for task in analysis["tasks"] if task.get("repository")})
        roots = _task_repository_roots(root, mode, {"repositories": repositories or [root.name]})
        for item in described_tasks:
            task = by_task.get(item["taskId"], {})
            recorded_state = item.get("codeState")
            payload = {
                "kind": "taskEvidence",
                "taskId": item["taskId"],
                "recordedAt": item["recordedAt"],
                "repository": task.get("repository") or (next(iter(recorded_state), None) if isinstance(recorded_state, dict) else None) or "__legacy__",
                "codeState": recorded_state,
                "checks": [
                    {
                        **{key: value for key, value in check.items() if key not in {"id", "source", "exitStatus"}},
                        "exitStatus": int(check["exitStatus"]) if str(check.get("exitStatus", "")).isdigit() else -1,
                    }
                    for check in item["checks"]
                ],
                "artifactRefs": [],
                "validationKind": task.get("validationKind") or "声明式",
                "deliveryCheck": "passed" if item["deliveryCheck"] == "通过" else "failed",
                "result": "passed" if item["completeness"] == "complete" and item["deliveryCheck"] == "通过" else "failed",
                "origin": {**item["source"], "path": f"testing/archive/verification-v1-{hashlib.sha256(text.encode('utf-8')).hexdigest()}.md"},
                "issues": [
                    {**issue, "path": f"testing/archive/verification-v1-{hashlib.sha256(text.encode('utf-8')).hexdigest()}.md"}
                    for issue in item["issues"]
                ],
            }
            trusted = evaluate_task_evidence(
                task,
                item,
                roots,
                workspace_root=root,
                feature_root=feature,
            )["trusted"]
            task_records.append({**payload, "_trusted": trusted})
    plan = feature / "plans" / "implementation.md"
    if plan.is_symlink() or not plan.is_file() or plan.parent.is_symlink():
        raise EvidenceError("EVIDENCE_UNSAFE_PATH", "实施计划不安全或缺失")
    plan_text = plan.read_text(encoding="utf-8") if plan.is_file() else ""
    updated_plan = re.sub(
        r"(completion-policy:\s*)task-evidence-v1",
        r"\1task-evidence-v2",
        plan_text,
    )
    updated_plan = re.sub(r"(完成门禁：\s*`?)task-evidence-v1", r"\1task-evidence-v2", updated_plan)
    archive_id = "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()
    backup_relative = f"archive/transactions/{_hex(archive_id)}/verification.md"
    preview_result = {
        "preview": True,
        "taskCount": len(task_records),
        "batch": bool(batch_records),
        "batchCount": len(batch_records),
        "legacyArchive": f"archive/verification-v1-{_hex(archive_id)}.md",
        "policyChange": "task-evidence-v1 -> task-evidence-v2",
    }
    if preview:
        return preview_result
    testing = feature / "testing"
    archive = safe_path(feature, "archive")
    transaction = safe_path(feature, f"archive/transactions/{_hex(archive_id)}")
    transaction.mkdir(parents=True, exist_ok=True)
    _backup_file(verification, transaction / "verification.md")
    _backup_file(plan, transaction / "implementation.md")
    backups = {"verification": transaction / "verification.md", "plan": transaction / "implementation.md"}
    _write_manifest(transaction / "manifest.json", "migrate", backups, source=backup_relative, archiveId=archive_id)
    legacy_archive = safe_path(feature, f"archive/verification-v1-{_hex(archive_id)}.md")
    preparing = safe_path(feature, ".evidence-transaction.json")
    atomic_write_many(((preparing, json.dumps({"operation": "migrate", "archiveId": archive_id}, ensure_ascii=False) + "\n"),))
    marker = safe_path(feature, "evidence/.transaction.json")
    marker.parent.mkdir(parents=True, exist_ok=True)
    try:
        marker.write_text(
            json.dumps({"operation": "migrate", "archiveId": archive_id}, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        if legacy_archive.exists() or legacy_archive.is_symlink():
            if legacy_archive.is_symlink() or not legacy_archive.is_file() or legacy_archive.read_text(encoding="utf-8") != text:
                raise EvidenceError("EVIDENCE_CONFLICT", "现有旧文档归档与来源不一致")
        else:
            atomic_write_many(((legacy_archive, text),))
        for payload in task_records:
            trusted = payload.pop("_trusted", None) if isinstance(payload, dict) else None
            record(feature, payload, trusted=trusted, _allow_transaction=True, _migrating=True)
        for batch in batch_records:
            record(feature, batch, _allow_transaction=True, _migrating=True)
        index_path = safe_path(feature, "evidence/index.json")
        if not index_path.is_file():
            atomic_write_many(((index_path, canonical_bytes(_empty_index(feature)) + b"\n"),))
        index = load_index(feature, allow_transaction=True)
        index["legacyArchives"].append({"id": archive_id, "path": f"archive/verification-v1-{_hex(archive_id)}.md", "bytes": len(text.encode("utf-8"))})
        index = _with_revision(index)
        safe_path(feature, "evidence/index.json").write_bytes(canonical_bytes(index) + b"\n")
        if plan.is_file():
            plan.write_text(updated_plan, encoding="utf-8")
        summary = render_summary(feature, _allow_transaction=True)
        verification.write_text(summary, encoding="utf-8")
        marker.unlink(missing_ok=True)
        preparing.unlink(missing_ok=True)
    except (OSError, UnicodeError, ValueError, WorkspaceError) as exc:
        evidence = testing / "evidence"
        if evidence.exists() and not evidence.is_symlink():
            shutil.rmtree(evidence)
        if transaction.joinpath("verification.md").is_file():
            shutil.copy2(transaction / "verification.md", verification)
        if transaction.joinpath("implementation.md").is_file():
            shutil.copy2(transaction / "implementation.md", plan)
        marker.unlink(missing_ok=True)
        preparing.unlink(missing_ok=True)
        raise EvidenceError("EVIDENCE_MIGRATION_FAILED", str(exc)) from exc
    return {**preview_result, "preview": False, "archive": str(legacy_archive.relative_to(feature))}


def compact_feature(feature: Path, *, preview: bool = True, _locked: bool = False) -> dict[str, object]:
    if not preview and not _locked:
        with mutation_lock(feature):
            return compact_feature(feature, preview=False, _locked=True)
    if not preview:
        _recover(Path(feature), "compact")
    index = load_index(feature)
    history = list(index["history"])
    if len(history) <= 1:
        return {"preview": preview, "changed": False, "archived": 0}
    keep_sequences = set()
    for pointers in index["tasks"].values():
        keep_sequences.add(pointers["latestObserved"]["sequence"])
        if pointers.get("latestTrusted") is not None:
            keep_sequences.add(pointers["latestTrusted"]["sequence"])
    if index.get("latestBatch") is not None:
        keep_sequences.add(index["latestBatch"]["sequence"])
    retained = [item for item in history if item.get("sequence") in keep_sequences]
    archived = [item for item in history if item.get("sequence") not in keep_sequences]
    if not archived:
        return {"preview": preview, "changed": False, "archived": 0}
    archive_document = {"schemaVersion": 2, "kind": "historyArchive", "history": archived}
    archive_id = content_id(archive_document)
    relative = f"testing/archive/history-{_hex(archive_id)}.json"
    result = {"preview": preview, "changed": True, "archived": len(archived), "archive": relative}
    if preview:
        return result
    marker = safe_path(feature, "evidence/.transaction.json")
    preparing = safe_path(feature, ".evidence-transaction.json")
    transaction = safe_path(feature, f"archive/transactions/compact-{_hex(archive_id)}")
    transaction.mkdir(parents=True, exist_ok=True)
    index_path = safe_path(feature, "evidence/index.json")
    _backup_file(index_path, transaction / "index.json")
    verification = feature / "testing" / "verification.md"
    if verification.is_file() and not verification.is_symlink():
        _backup_file(verification, transaction / "verification.md")
    compact_backups = {"index": transaction / "index.json"}
    if (transaction / "verification.md").is_file():
        compact_backups["verification"] = transaction / "verification.md"
    _write_manifest(transaction / "manifest.json", "compact", compact_backups, archive=relative)
    transaction_marker = json.dumps({"operation": "compact", "archive": relative}, ensure_ascii=False) + "\n"
    atomic_write_many(((preparing, transaction_marker),))
    marker.write_text(transaction_marker, encoding="utf-8")
    try:
        path = safe_path(feature, relative.removeprefix("testing/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        data = archive_document
        archive_data = canonical_bytes(data) + b"\n"
        if path.exists() or path.is_symlink():
            if path.is_symlink() or not path.is_file() or path.read_bytes() != archive_data:
                raise EvidenceError("EVIDENCE_CONFLICT", "现有历史归档与候选内容不一致")
        else:
            atomic_write_many(((path, archive_data),))
        index["archives"].append({"id": archive_id, "path": relative, "count": len(archived)})
        index["history"] = retained
        # Keep history in the index as the active query source; archives are an audit manifest.
        index = _with_revision(index)
        safe_path(feature, "evidence/index.json").write_bytes(canonical_bytes(index) + b"\n")
        marker.unlink(missing_ok=True)
        preparing.unlink(missing_ok=True)
    except (OSError, UnicodeError, ValueError) as exc:
        marker.unlink(missing_ok=True)
        preparing.unlink(missing_ok=True)
        if (transaction / "index.json").is_file():
            shutil.copy2(transaction / "index.json", index_path)
        if (transaction / "verification.md").is_file():
            shutil.copy2(transaction / "verification.md", verification)
        raise EvidenceError("EVIDENCE_COMPACT_FAILED", str(exc)) from exc
    return result


def rollback_migration(feature: Path, archive: str) -> None:
    digest = Path(archive).stem.removeprefix("verification-v1-")
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise EvidenceError("EVIDENCE_TRANSACTION_INCOMPLETE", "迁移回滚 ID 无效")
    transaction = safe_path(feature, f"archive/transactions/{digest}")
    backups = _verified_backups(transaction, "migrate")
    verification = backups.get("verification")
    plan = backups.get("plan")
    if verification is None or plan is None:
        raise EvidenceError("EVIDENCE_TRANSACTION_INCOMPLETE", "迁移回滚备份缺失")
    atomic_write_many(((Path(feature) / "testing" / "verification.md", verification.read_bytes()), (Path(feature) / "plans" / "implementation.md", plan.read_bytes())))
    evidence = Path(feature) / "testing" / "evidence"
    if evidence.exists():
        if evidence.is_symlink() or not evidence.is_dir():
            raise EvidenceError("EVIDENCE_UNSAFE_PATH", "迁移回滚目录不安全")
        shutil.rmtree(evidence)


def rollback_compact(feature: Path, archive: str) -> None:
    digest = Path(archive).stem.removeprefix("history-")
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise EvidenceError("EVIDENCE_TRANSACTION_INCOMPLETE", "压缩回滚 ID 无效")
    transaction = safe_path(feature, f"archive/transactions/compact-{digest}")
    backups = _verified_backups(transaction, "compact")
    index = backups.get("index")
    if index is None:
        raise EvidenceError("EVIDENCE_TRANSACTION_INCOMPLETE", "压缩回滚备份缺失")
    outputs = [(Path(feature) / "testing" / "evidence" / "index.json", index.read_bytes())]
    verification = backups.get("verification")
    if verification is not None:
        outputs.append((Path(feature) / "testing" / "verification.md", verification.read_bytes()))
    atomic_write_many(outputs)
