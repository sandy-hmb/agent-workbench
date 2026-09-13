#!/usr/bin/env python3
"""Build Git code fingerprints and validate feature verification batches."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import time
import threading
from pathlib import Path, PurePosixPath
from typing import Iterable, Mapping

from workspace_model import atomic_write_many, load_workspace, repository_path, resolve_repository
from workspace_evidence import (
    EvidenceError,
    MAX_JSON_BYTES,
    compact_feature,
    get_evidence,
    history_page,
    load_store,
    migrate_feature,
    mutation_lock,
    record,
    recover_transaction,
    render_summary,
    rollback_compact,
    rollback_migration,
)


DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
BATCH_HEADER_RE = re.compile(
    r"^## 验证批次 \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:Z|[+-]\d{2}:\d{2})\s*$"
)
TASK_EVIDENCE_HEADER_RE = re.compile(
    r"^## 任务证据 (T\d{2,}) "
    r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:Z|[+-]\d{2}:\d{2}))\s*$",
    re.MULTILINE,
)
SECTION_RE = re.compile(r"^##\s", re.MULTILINE)
CHECK_HEADER_RE = re.compile(r"^### 检查 [1-9][0-9]*\s*$", re.MULTILINE)
CHECK_FIELDS = ("工作目录：", "命令：", "退出状态：", "结果：")
TASK_CHECK_FIELDS = ("类型：", "工作目录：", "命令：", "目标：", "退出状态：", "结果：")
INSPECT_MAX_UNTRACKED_FILES = 10_000
INSPECT_MAX_FINGERPRINT_BYTES = 64 * 1024 * 1024


def _git(repository: Path, arguments: list[str], timeout: float | None = None, max_output_bytes: int | None = None) -> bytes:
    if max_output_bytes is not None:
        process = subprocess.Popen(
            ["git", "-C", str(repository), *arguments], stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, env=dict(os.environ, GIT_OPTIONAL_LOCKS="0"),
        )
        assert process.stdout is not None and process.stderr is not None
        output: list[bytes] = []
        error: list[bytes] = []
        total = [0]
        lock = threading.Lock()
        exceeded = threading.Event()
        def drain(handle: object, target: list[bytes]) -> None:
            while chunk := handle.read1(4096):
                with lock:
                    total[0] += len(chunk)
                    if total[0] > max_output_bytes:
                        exceeded.set()
                        if process.poll() is None: process.kill()
                    else:
                        target.append(chunk)
        threads = [threading.Thread(target=drain, args=(process.stdout, output)), threading.Thread(target=drain, args=(process.stderr, error))]
        for thread in threads: thread.start()
        started = time.monotonic()
        while process.poll() is None:
            if exceeded.is_set() or (timeout is not None and time.monotonic() - started >= timeout):
                process.kill(); break
            time.sleep(0.005)
        process.wait()
        for thread in threads: thread.join()
        process.stdout.close(); process.stderr.close()
        if exceeded.is_set(): raise ValueError("代码核对 Git 输出超过限制")
        if timeout is not None and time.monotonic() - started >= timeout:
            raise subprocess.TimeoutExpired("git", timeout)
        result_stdout, result_stderr = b"".join(output), b"".join(error)
        if process.returncode:
            message = result_stderr.decode("utf-8", errors="replace").strip()
            raise ValueError(message or f"Git 命令失败：{' '.join(arguments)}")
        return result_stdout
    result = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=False,
        capture_output=True,
        timeout=timeout,
        env=dict(os.environ, GIT_OPTIONAL_LOCKS="0"),
    )
    if result.returncode:
        message = result.stderr.decode("utf-8", errors="replace").strip()
        raise ValueError(message or f"Git 命令失败：{' '.join(arguments)}")
    return result.stdout


def _excluded_paths(values: Iterable[str]) -> tuple[str, ...]:
    result = set()
    for value in values:
        path = PurePosixPath(value)
        if not value or path.is_absolute() or ".." in path.parts:
            raise ValueError(f"排除路径必须是仓库内相对路径：{value}")
        result.add(path.as_posix())
    return tuple(sorted(result))


def _digest_part(digest: object, label: bytes, value: bytes) -> None:
    digest.update(label)
    digest.update(len(value).to_bytes(8, "big"))
    digest.update(value)


def git_fingerprint(repository: Path, excluded: Iterable[str] = (), *, timeout: float | None = None, max_untracked_files: int | None = None, max_bytes: int | None = None) -> str:
    deadline = time.monotonic() + timeout if timeout is not None else None
    def remaining() -> float | None:
        if deadline is None:
            return None
        value = deadline - time.monotonic()
        if value <= 0:
            raise subprocess.TimeoutExpired("git", timeout)
        return value
    repository = Path(repository).resolve()
    output_limit = max_bytes
    top = Path(os.fsdecode(_git(repository, ["rev-parse", "--show-toplevel"], remaining(), output_limit).strip())).resolve()
    if top != repository:
        raise ValueError(f"仓库不是独立 Git 根目录：{repository}")
    head = _git(repository, ["rev-parse", "--verify", "HEAD"], remaining(), output_limit).strip()
    excluded_paths = _excluded_paths(excluded)
    pathspecs = [".", *(f":(exclude){path}" for path in excluded_paths)]
    diff = _git(
        repository, ["diff", "--binary", "--no-ext-diff", "HEAD", "--", *pathspecs], remaining(), output_limit,
    )
    untracked = _git(repository, ["ls-files", "--others", "--exclude-standard", "-z", "--", "."], remaining(), output_limit)

    digest = hashlib.sha256()
    _digest_part(digest, b"HEAD", head)
    _digest_part(digest, b"DIFF", diff)
    paths = sorted(path for path in untracked.split(b"\0") if path)
    if max_untracked_files is not None and len(paths) > max_untracked_files:
        raise ValueError("代码核对未跟踪文件数量超过限制")
    consumed = len(head) + len(diff) + len(untracked)
    for raw_path in paths:
        remaining()
        relative = os.fsdecode(raw_path)
        normalized = PurePosixPath(relative).as_posix()
        if any(
            normalized == excluded or normalized.startswith(excluded.rstrip("/") + "/")
            for excluded in excluded_paths
        ):
            continue
        path = repository / relative
        file_stat = path.lstat()
        if stat.S_ISLNK(file_stat.st_mode):
            content = os.fsencode(os.readlink(path))
        elif stat.S_ISREG(file_stat.st_mode):
            if max_bytes is not None and consumed + file_stat.st_size > max_bytes:
                raise ValueError("代码核对读取字节超过限制")
            chunks = []
            with path.open("rb") as handle:
                while chunk := handle.read(64 * 1024):
                    remaining(); chunks.append(chunk)
            content = b"".join(chunks)
        else:
            raise ValueError(f"未跟踪路径不是普通文件或符号链接：{path}")
        _digest_part(digest, b"PATH", raw_path)
        _digest_part(digest, b"DATA", content)
        consumed += len(raw_path) + len(content)
    return f"sha256:{digest.hexdigest()}"


def _valid_code_state(value: object) -> bool:
    return (
        isinstance(value, dict)
        and bool(value)
        and all(
            isinstance(name, str)
            and bool(name)
            and isinstance(digest, str)
            and DIGEST_RE.fullmatch(digest)
            for name, digest in value.items()
        )
    )


def encode_code_state(states: Mapping[str, str]) -> str:
    value = dict(states)
    if not _valid_code_state(value):
        raise ValueError("代码状态必须包含仓库名和 sha256 指纹")
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _field(lines: list[str], prefix: str) -> str | None:
    values = [line[len(prefix) + 2 :].strip() for line in lines if line.startswith(f"- {prefix}")]
    return values[0] if len(values) == 1 and values[0] else None


def _integer_field(lines: list[str], prefix: str) -> int | None:
    value = _field(lines, prefix)
    return int(value) if value is not None and re.fullmatch(r"[0-9]{1,9}", value) else None


def describe_task_evidence_document(text: str) -> dict[str, object]:
    """Parse task checkpoints without treating them as final verification."""
    revision = "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()
    matches = list(TASK_EVIDENCE_HEADER_RE.finditer(text))
    records = []
    for match in matches:
        boundary = SECTION_RE.search(text, match.end())
        end = boundary.start() if boundary else len(text)
        record = text[match.start():end].strip()
        source_line = text.count("\n", 0, match.start()) + 1
        check_matches = list(CHECK_HEADER_RE.finditer(record))
        header = (
            record[:check_matches[0].start()].splitlines()
            if check_matches
            else record.splitlines()
        )
        issues = []

        def issue(code: str, message: str, line: int = source_line) -> None:
            issues.append(
                {
                    "severity": "error",
                    "code": code,
                    "message": message,
                    "path": "testing/verification.md",
                    "line": line,
                }
            )

        def required(lines: list[str], field: str, line: int) -> str | None:
            value = _field(lines, field)
            if value is None:
                issue(
                    "TASK_EVIDENCE_FIELD_INCOMPLETE",
                    f"字段缺失、为空或重复：{field}",
                    line,
                )
            return value

        delivery = required(header, "交付核对：", source_line)
        encoded = required(header, "代码状态：", source_line)
        try:
            code_state = json.loads(encoded) if encoded else None
        except (ValueError, RecursionError):
            code_state = None
        if not _valid_code_state(code_state):
            issue("TASK_EVIDENCE_CODE_STATE_INVALID", "任务证据没有合法的仓库代码指纹")
        if not check_matches:
            issue("TASK_EVIDENCE_CHECKS_MISSING", "任务证据没有检查记录")

        checks = []
        for index, check_match in enumerate(check_matches):
            check_end = (
                check_matches[index + 1].start()
                if index + 1 < len(check_matches)
                else len(record)
            )
            lines = record[check_match.end():check_end].splitlines()
            line = source_line + record.count("\n", 0, check_match.start())
            fields = {
                field: required(lines, field, line) for field in TASK_CHECK_FIELDS
            }
            checks.append(
                {
                    "id": check_match.group(0).strip().split(" ")[-1],
                    "source": {
                        "path": "testing/verification.md",
                        "startLine": line,
                        "endLine": source_line
                        + len(record[:check_end].rstrip().splitlines())
                        - 1,
                    },
                    "type": fields["类型："],
                    "workingDirectory": fields["工作目录："],
                    "command": fields["命令："],
                    "target": fields["目标："],
                    "executed": _integer_field(lines, "执行数："),
                    "skipped": _integer_field(lines, "跳过数："),
                    "exitStatus": fields["退出状态："],
                    "result": fields["结果："],
                }
            )
        records.append(
            {
                "taskId": match.group(1),
                "recordedAt": match.group(2),
                "source": {
                    "path": "testing/verification.md",
                    "startLine": source_line,
                    "endLine": source_line + len(record.splitlines()) - 1,
                },
                "raw": record,
                "deliveryCheck": delivery,
                "codeState": code_state,
                "checks": checks,
                "completeness": "incomplete" if issues else "complete",
                "issues": issues,
            }
        )
    latest = {}
    for record in records:
        latest[record["taskId"]] = record
    return {
        "documentRevision": revision,
        "records": records,
        "latestByTask": latest,
    }


def _deliverable_exists(repository: Path, relative: str, *, file_only: bool) -> bool:
    pure = PurePosixPath(relative)
    if not relative or pure.is_absolute() or ".." in pure.parts:
        return False
    target = repository.joinpath(*pure.parts)
    current = repository
    for part in pure.parts:
        current /= part
        if current.is_symlink():
            return False
    return target.is_file() if file_only else target.exists()


def _feature_artifact_root(
    workspace_root: Path | None, feature_root: Path | None, relative: str
) -> Path | None:
    if workspace_root is None or feature_root is None:
        return None
    try:
        feature_relative = Path(feature_root).resolve().relative_to(Path(workspace_root).resolve())
    except ValueError:
        return None
    expected = PurePosixPath(feature_relative.as_posix()) / "artifacts"
    path = PurePosixPath(relative)
    if path.is_absolute() or ".." in path.parts:
        return None
    return Path(workspace_root).resolve() if path.parts[:len(expected.parts)] == expected.parts else None


def evaluate_task_evidence(
    task: Mapping[str, object],
    evidence: Mapping[str, object] | None,
    repository_roots: Mapping[str, Path],
    *,
    workspace_root: Path | None = None,
    feature_root: Path | None = None,
) -> dict[str, object]:
    """Check deterministic task evidence and current deliverable paths."""
    task_id = str(task.get("id") or "")
    diagnostics = []
    source = evidence.get("source") if evidence is not None else None

    def issue(code: str, message: str, line: int | None = None) -> None:
        diagnostics.append(
            {
                "severity": "error",
                "code": code,
                "path": str((source or {}).get("path", "testing/verification.md")),
                "line": line or int((source or {}).get("startLine", 1)),
                "message": message,
            }
        )

    if evidence is None:
        issue("TASK_EVIDENCE_MISSING", f"已勾选任务 {task_id} 没有任务证据")
        return {
            "taskId": task_id,
            "trusted": False,
            "source": None,
            "diagnostics": diagnostics,
        }
    diagnostics.extend(evidence.get("issues", []))
    if evidence.get("taskId") != task_id:
        issue("TASK_EVIDENCE_TASK_MISMATCH", "任务证据编号与计划任务不一致")
    if evidence.get("deliveryCheck") != "通过":
        issue("TASK_EVIDENCE_DELIVERY_FAILED", "任务交付核对未通过")

    repository_name = task.get("repository")
    code_state = evidence.get("codeState")
    if (
        not isinstance(repository_name, str)
        or not isinstance(code_state, dict)
        or set(code_state) != {repository_name}
    ):
        issue("TASK_EVIDENCE_REPOSITORY_MISMATCH", "任务证据仓库与目标仓不一致")

    checks = evidence.get("checks")
    checks = checks if isinstance(checks, list) else []
    successful_types = set()
    for check in checks:
        check_type = check.get("type")
        exit_status = check.get("exitStatus")
        result = check.get("result")
        line = int(check.get("source", {}).get("startLine", 1))
        failed_result = not isinstance(result, str) or result.lstrip().startswith(
            ("失败", "未执行", "不通过", "未通过")
        )
        if exit_status != "0" or failed_result:
            issue("TASK_EVIDENCE_CHECK_FAILED", "任务检查未成功完成", line)
            continue
        if isinstance(check_type, str):
            successful_types.add(check_type)
        if check_type == "测试":
            if check.get("executed") in {None, 0}:
                issue("TASK_EVIDENCE_ZERO_TESTS", "目标测试没有实际执行", line)
            if check.get("skipped") is None or int(check["skipped"]) > 0:
                issue("TASK_EVIDENCE_SKIPPED_TESTS", "目标测试存在跳过或未记录跳过数", line)

    validation_kind = task.get("validationKind")
    allowed = {
        "行为": {"测试", "行为检查", "集成", "结构", "迁移"},
        "声明式": {"静态检查", "编译", "测试", "行为检查", "集成", "结构", "迁移"},
        "持久化": {"结构", "迁移", "集成"},
    }
    if validation_kind not in allowed or not successful_types.intersection(
        allowed.get(validation_kind, set())
    ):
        issue("TASK_EVIDENCE_KIND_INSUFFICIENT", "检查类型不足以证明任务验证性质")

    repository = (
        Path(repository_roots[repository_name]).resolve()
        if isinstance(repository_name, str) and repository_name in repository_roots
        else None
    )
    for deliverable in task.get("deliverables", []):
        kind = deliverable.get("kind")
        relative = deliverable.get("path")
        line = int(deliverable.get("line", 1))
        target_root = (
            _feature_artifact_root(workspace_root, feature_root, relative)
            if isinstance(relative, str)
            else None
        ) or repository
        if target_root is None or not isinstance(relative, str):
            issue("TASK_DELIVERABLE_MISSING", "无法定位任务交付路径", line)
            continue
        if kind == "Delete":
            target = target_root.joinpath(*PurePosixPath(relative).parts)
            if target.exists() or target.is_symlink():
                issue("TASK_DELETED_PATH_PRESENT", f"声明删除的路径仍存在：{relative}", line)
        elif not _deliverable_exists(
            target_root, relative, file_only=kind in {"Create", "Test", "Modify"}
        ):
            issue("TASK_DELIVERABLE_MISSING", f"任务交付路径不存在或不安全：{relative}", line)

    if any(item["kind"] == "Test" for item in task.get("deliverables", [])):
        test_checks = [check for check in checks if check.get("type") == "测试"]
        if not test_checks:
            issue("TASK_EVIDENCE_KIND_INSUFFICIENT", "Test 交付没有对应测试执行证据")

    return {
        "taskId": task_id,
        "trusted": not diagnostics,
        "source": source,
        "diagnostics": diagnostics,
    }


def describe_structured_evidence(feature: Path) -> dict[str, object]:
    """Adapt v2 JSON records to the existing deterministic evaluation shape."""
    store = load_store(feature)
    latest = {}
    for task_id, raw in store["latestByTask"].items():
        source = raw["source"]
        checks = [
            {**check, "id": str(index), "source": source, "exitStatus": str(check.get("exitStatus"))}
            for index, check in enumerate(raw["checks"], 1)
        ]
        latest[task_id] = {
            **raw,
            "deliveryCheck": "通过" if raw.get("deliveryCheck") == "passed" else "失败",
            "checks": checks,
            "completeness": "incomplete" if raw.get("issues") else "complete",
            "issues": [
                *raw.get("issues", []),
                *([{"severity": "error", "code": "TASK_EVIDENCE_RESULT_FAILED",
                   "message": "任务证据结果未通过", "path": source["path"], "line": source["startLine"]}]
                  if raw.get("origin") is None and raw.get("result") != "passed" else []),
            ],
        }
    return {"index": store["index"], "latestByTask": latest, "latestBatch": store["latestBatch"]}


def structured_verification_passed(
    batch: Mapping[str, object] | None,
    current_states: Mapping[str, str] | None,
) -> bool:
    if batch is None or current_states is None:
        return False
    if batch.get("overallResult") != "passed" or batch.get("reviewResult") != "passed":
        return False
    recorded = batch.get("codeState")
    if not _valid_code_state(recorded) or not _valid_code_state(dict(current_states)):
        return False
    checks = batch.get("checks")
    return (
        recorded == dict(current_states)
        and isinstance(checks, list)
        and bool(checks)
        and all(
            isinstance(check, Mapping)
            and str(check.get("exitStatus")) == "0"
            and isinstance(check.get("result"), str)
            and bool(check["result"])
            for check in checks
        )
    )


def describe_verification_document(text: str) -> dict[str, object]:
    """Explain existing evidence without executing checks or inferring missing facts."""
    # Use the same record boundaries as status; defer the import to avoid its
    # verification -> status -> verification module cycle at import time.
    from workspace_status import SECTION_RE, VERIFICATION_RECORD_RE

    revision = "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()
    matches = list(VERIFICATION_RECORD_RE.finditer(text))
    batches = []
    for match in matches:
        boundary = SECTION_RE.search(text, match.end())
        end = boundary.start() if boundary else len(text)
        start_line = text.count("\n", 0, match.start()) + 1
        batches.append({
            "id": f"{revision}:{start_line}",
            "recordedAt": match.group(0).split(" ", 2)[-1],
            "source": {"path": "testing/verification.md", "startLine": start_line,
                       "endLine": start_line + len(text[match.start():end].rstrip().splitlines()) - 1},
        })
    if not matches:
        return {"documentRevision": revision, "batches": [], "latestBatchId": None, "selectedBatch": None}

    latest = matches[-1]
    boundary = SECTION_RE.search(text, latest.end())
    record = text[latest.start():boundary.start() if boundary else len(text)].strip()
    check_matches = list(CHECK_HEADER_RE.finditer(record))
    header = record[:check_matches[0].start()].splitlines() if check_matches else record.splitlines()
    issues: list[dict[str, object]] = []
    selected = dict(batches[-1])
    source_line = selected["source"]["startLine"]

    def issue(code: str, message: str, line: int = source_line) -> None:
        issues.append({"code": code, "message": message, "path": "testing/verification.md", "line": line})

    def required(lines: list[str], field: str, line: int) -> str | None:
        value = _field(lines, field)
        if not value:
            issue("VERIFICATION_FIELD_INCOMPLETE", f"字段缺失、为空或重复：{field}", line)
        return value

    def recorded_result(value: str | None) -> str:
        if value == "通过":
            return "passed"
        return "failed" if value in {"失败", "不通过", "未通过"} else "unknown"

    legacy = not BATCH_HEADER_RE.fullmatch(record.splitlines()[0])
    if legacy:
        issue("VERIFICATION_LEGACY_RECORD", "旧格式保留原文，不能作为完整批次核对")
    overall = required(header, "总体结果：", source_line)
    review = required(header, "审查结论：", source_line)
    encoded = required(header, "代码状态：", source_line)
    try:
        code_state = json.loads(encoded) if encoded else None
    except (ValueError, RecursionError):
        code_state = None
    if not _valid_code_state(code_state):
        issue("VERIFICATION_CODE_STATE_INVALID", "未记录合法的仓库代码指纹集合")
    if not check_matches:
        issue("VERIFICATION_CHECKS_MISSING", "批次中没有完整的检查记录")

    checks = []
    for index, match in enumerate(check_matches):
        end = check_matches[index + 1].start() if index + 1 < len(check_matches) else len(record)
        lines = record[match.end():end].splitlines()
        line = source_line + record.count("\n", 0, match.start())
        fields = {name: required(lines, name, line) for name in CHECK_FIELDS}
        test_count = _field(lines, "测试数量：")
        checks.append({
            "id": match.group(0).strip().split(" ")[-1],
            "source": {"path": "testing/verification.md", "startLine": line,
                       "endLine": source_line + len(record[:end].rstrip().splitlines()) - 1},
            "workingDirectory": fields["工作目录："], "command": fields["命令："],
            "exitStatus": fields["退出状态："], "result": fields["结果："],
            "duration": _field(lines, "耗时："),
            "testCount": int(test_count) if test_count and re.fullmatch(r"[0-9]{1,9}", test_count) else None,
        })
    selected.update({
        "raw": record, "recordedResult": recorded_result(overall),
        "recordedReview": recorded_result(review),
        "completeness": "legacy" if legacy else "incomplete" if issues else "complete",
        "issues": issues, "checks": checks,
    })
    return {"documentRevision": revision, "batches": batches, "latestBatchId": selected["id"], "selectedBatch": selected}


def verification_passed(
    record: str | None, current_states: Mapping[str, str] | None
) -> bool:
    if record is None or current_states is None:
        return False
    lines = record.splitlines()
    if not lines or not BATCH_HEADER_RE.fullmatch(lines[0]):
        return False
    checks = list(CHECK_HEADER_RE.finditer(record))
    if not checks:
        return False
    header = record[: checks[0].start()].splitlines()
    if _field(header, "总体结果：") != "通过" or _field(header, "审查结论：") != "通过":
        return False
    encoded_state = _field(header, "代码状态：")
    if encoded_state is None:
        return False
    try:
        recorded_states = json.loads(encoded_state)
    except json.JSONDecodeError:
        return False
    if not _valid_code_state(recorded_states) or not _valid_code_state(dict(current_states)):
        return False
    if recorded_states != dict(current_states):
        return False
    for index, match in enumerate(checks):
        end = checks[index + 1].start() if index + 1 < len(checks) else len(record)
        block = record[match.end() : end].splitlines()
        fields = {name: _field(block, name) for name in CHECK_FIELDS}
        if any(value is None for value in fields.values()) or fields["退出状态："] != "0":
            return False
    return True


def feature_code_state(
    root: Path, mode: str, feature: Mapping[str, object], *, timeout: float | None = None, inspect_budget: bool = False
) -> dict[str, str]:
    root = Path(root).resolve()
    repositories = feature.get("repositories")
    if not isinstance(repositories, list) or not all(
        isinstance(name, str) and name for name in repositories
    ):
        raise ValueError("需求缺少可验证的涉及仓库")
    if mode == "maintenance":
        if repositories != [root.name]:
            raise ValueError("维护需求必须只涉及当前 Kit 仓库")
        relative = feature.get("path")
        if not isinstance(relative, str):
            raise ValueError("维护需求缺少路径")
        feature_path = PurePosixPath(relative)
        if feature_path.is_absolute() or ".." in feature_path.parts:
            raise ValueError("维护需求路径无效")
        excluded = tuple(
            (feature_path / item).as_posix()
            for item in (
                "README.md",
                "plans/implementation.md",
                "testing/verification.md",
                "testing/.evidence.lock",
                "testing/evidence",
                "testing/archive",
            )
        )
        return {root.name: git_fingerprint(root, excluded, timeout=timeout, max_untracked_files=INSPECT_MAX_UNTRACKED_FILES if inspect_budget else None, max_bytes=INSPECT_MAX_FINGERPRINT_BYTES if inspect_budget else None)}

    workspace = load_workspace(root)
    result = {}
    for name in repositories:
        repository = resolve_repository(workspace.repositories, name)
        result[name] = git_fingerprint(repository_path(workspace, repository), timeout=timeout, max_untracked_files=INSPECT_MAX_UNTRACKED_FILES if inspect_budget else None, max_bytes=INSPECT_MAX_FINGERPRINT_BYTES if inspect_budget else None)
    return result


def snapshot_result(root: Path, slug: str) -> dict[str, object]:
    # Import lazily: workspace_status reuses this module for batch validation.
    from workspace_status import status_result

    status = status_result(Path(root).resolve())
    feature = next(
        (item for item in status["features"] if item["featureSlug"] == slug), None
    )
    if feature is None:
        raise ValueError(f"未找到进行中的需求：{slug}")
    return {
        "featureSlug": slug,
        "codeState": feature_code_state(Path(root), str(status["mode"]), feature),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="管理任务证据、验证批次和当前需求的 Git 代码状态。")
    subcommands = parser.add_subparsers(dest="command", required=True)
    snapshot = subcommands.add_parser("snapshot", help="输出当前需求的代码状态指纹")
    snapshot.add_argument("feature")
    snapshot.add_argument("--root", type=Path, default=Path.cwd())
    snapshot.add_argument("--json", action="store_true")
    record_parser = subcommands.add_parser("record", help="记录一条结构化证据")
    record_parser.add_argument("feature")
    record_parser.add_argument("--root", type=Path, default=Path.cwd())
    record_parser.add_argument("--input", type=Path, required=True, help="证据 JSON 文件；- 表示标准输入")
    record_parser.add_argument("--preview", action="store_true")
    record_parser.add_argument("--json", action="store_true")
    evidence = subcommands.add_parser("evidence", help="读取一条任务或批次证据")
    evidence.add_argument("feature", nargs="?")
    evidence.add_argument("--task")
    evidence.add_argument("--batch")
    evidence.add_argument("--id", dest="evidence_id", help="按历史任务证据 ID 读取（需同时指定 --task）")
    evidence.add_argument("--root", type=Path, default=Path.cwd())
    evidence.add_argument("--json", action="store_true")
    history = subcommands.add_parser("history", help="分页读取证据历史")
    history.add_argument("feature", nargs="?")
    history.add_argument("--task")
    history.add_argument("--batch")
    history.add_argument("--cursor")
    history.add_argument("--limit", type=int, default=20)
    history.add_argument("--root", type=Path, default=Path.cwd())
    history.add_argument("--json", action="store_true")
    migrate = subcommands.add_parser("migrate", help="迁移 v1 验证记录")
    migrate.add_argument("feature")
    migrate.add_argument("--root", type=Path, default=Path.cwd())
    migrate.add_argument("--preview", action="store_true")
    migrate.add_argument("--apply", action="store_true")
    migrate.add_argument("--json", action="store_true")
    compact = subcommands.add_parser("compact", help="压缩 v2 活动索引")
    compact.add_argument("feature")
    compact.add_argument("--root", type=Path, default=Path.cwd())
    compact.add_argument("--preview", action="store_true")
    compact.add_argument("--apply", action="store_true")
    compact.add_argument("--json", action="store_true")
    render = subcommands.add_parser("render", help="从结构化证据重建人类摘要")
    render.add_argument("feature")
    render.add_argument("--root", type=Path, default=Path.cwd())
    render.add_argument("--preview", action="store_true")
    render.add_argument("--apply", action="store_true")
    render.add_argument("--json", action="store_true")
    return parser


def _resolve_feature_path(root: Path, slug: str | None) -> tuple[str, Path]:
    from workspace_status import status_result

    root = Path(root).resolve()
    if slug is not None:
        if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug):
            raise EvidenceError("EVIDENCE_FEATURE_NOT_FOUND", "Feature slug 无效")
        base = root / ".workspace" / "docs" / "features" if (root / ".workspace/workspace.json").is_file() else root / "docs/development/features"
        feature = base / slug
        if feature.is_symlink() or not feature.is_dir():
            raise EvidenceError("EVIDENCE_FEATURE_NOT_FOUND", f"未找到需求：{slug}")
        if any(path.is_symlink() for path in (base, base.parent, base.parent.parent)):
            raise EvidenceError("EVIDENCE_UNSAFE_PATH", "Feature 路径包含符号链接")
        try:
            feature.resolve().relative_to(root)
        except (OSError, RuntimeError, ValueError) as exc:
            raise EvidenceError("EVIDENCE_UNSAFE_PATH", "Feature 路径越界") from exc
        return slug, feature
    status = status_result(root)
    features = status.get("features", [])
    if not isinstance(features, list):
        raise EvidenceError("EVIDENCE_FEATURE_NOT_FOUND", "没有可用 Feature")
    if len(features) != 1:
        raise EvidenceError("EVIDENCE_FEATURE_AMBIGUOUS", "未指定 Feature 且无法唯一选择")
    item = features[0]
    return str(item["featureSlug"]), root / str(item["path"])


def summary_text(
    root: Path,
    slug: str,
    feature: Path,
    *,
    action_summaries: list[Mapping[str, object]] | None = None,
) -> str:
    from workspace_status import status_result

    status = status_result(Path(root).resolve())
    tracked = next((item for item in status.get("features", []) if item["featureSlug"] == slug), {})
    return render_status_summary(root, slug, feature, tracked, action_summaries=action_summaries)


def render_status_summary(
    root: Path,
    slug: str,
    feature: Path,
    tracked: Mapping[str, object],
    *,
    action_summaries: list[Mapping[str, object]] | None = None,
) -> str:
    batch = describe_structured_evidence(feature)["latestBatch"]
    if action_summaries is None:
        from workspace_paths import workflow_runs_root
        from workspace_workflow import _load_run

        runs_root = workflow_runs_root(Path(root).resolve())
        actions: list[dict[str, object]] = []
        if runs_root.exists():
            if runs_root.is_symlink() or not runs_root.is_dir():
                raise EvidenceError("EVIDENCE_UNSAFE_PATH", "Workflow Run 目录不安全")
            for path in runs_root.glob("*.json"):
                run = _load_run(Path(root).resolve(), path.stem)
                if run.get("featureSlug") == slug:
                    for stage, row in run["stages"].items():
                        actions.append({"stage": stage, **row})
        action_summaries = sorted(actions, key=lambda row: (str(row["updatedAt"]), str(row["stage"])))
    return render_summary(
        feature,
        state={
            "trustedProgress": tracked.get("trustedProgress"),
            "verificationPassed": tracked.get("verificationPassed"),
            "codeState": batch.get("codeState") if isinstance(batch, Mapping) else None,
            "blockers": tracked.get("documentDiagnostics", []),
            "artifacts": tracked.get("artifacts", []),
        },
        action_summaries=action_summaries,
    )


def refresh_summary(root: Path, slug: str, feature: Path) -> bool:
    path = feature / "testing" / "verification.md"
    text = summary_text(root, slug, feature)
    if path.is_file() and not path.is_symlink() and path.read_text(encoding="utf-8") == text:
        return False
    atomic_write_many(((path, text),))
    return True


def render_preview(root: Path, slug: str, feature: Path) -> dict[str, object]:
    text = summary_text(root, slug, feature)
    path = feature / "testing/verification.md"
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise EvidenceError("EVIDENCE_UNSAFE_PATH", "人类摘要路径不安全")
    return {"featureSlug": slug, "path": "testing/verification.md", "lines": len(text.splitlines()),
            "bytes": len(text.encode("utf-8")), "changed": not path.is_file() or path.read_text(encoding="utf-8") != text}


def _record_input(path: Path) -> dict[str, object]:
    if str(path) == "-":
        data = sys.stdin.buffer.read(MAX_JSON_BYTES + 1)
    else:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_JSON_BYTES:
            raise EvidenceError("EVIDENCE_UNSAFE_PATH", f"证据输入必须是普通且不超过 {MAX_JSON_BYTES} 字节的 JSON 文件")
        data = path.read_bytes()
    if len(data) > MAX_JSON_BYTES:
        raise EvidenceError("EVIDENCE_RECORD_INVALID", "证据输入超过大小限制")
    try:
        raw = json.loads(data.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise EvidenceError("EVIDENCE_RECORD_INVALID", "证据输入 JSON 无效") from exc
    if not isinstance(raw, dict):
        raise EvidenceError("EVIDENCE_RECORD_INVALID", "证据输入必须是 JSON 对象")
    return raw


def _state_signature(status: Mapping[str, object], slug: str) -> dict[str, object]:
    from workspace_status import _single_feature_progress

    feature = next(item for item in status["features"] if item["featureSlug"] == slug)
    stage = _single_feature_progress(feature, mode=str(status["mode"]))
    diagnostics = feature.get("documentDiagnostics", [])
    return {
        "trustedProgress": feature.get("trustedProgress"),
        "verificationPassed": feature.get("verificationPassed"),
        "blockers": stage.get("blockers"),
        "currentStage": stage.get("currentStage"),
        "diagnostics": sorted(
            (item.get("severity"), item.get("code"), item.get("message"))
            for item in diagnostics if isinstance(item, Mapping)
        ),
    }


def _record_trust(root: Path, feature: Path, raw: Mapping[str, object]) -> bool | None:
    if raw.get("kind") != "taskEvidence":
        return None
    from workspace_status import _task_repository_roots, plan_analysis

    root = Path(root).resolve()
    analysis = plan_analysis(feature / "plans" / "implementation.md")
    task = next((value for value in analysis["tasks"] if value.get("id") == raw.get("taskId")), None)
    if task is None:
        return False
    repository = task.get("repository")
    mode = "workspace" if (root / ".workspace/workspace.json").is_file() else "maintenance"
    item = {"repositories": [repository] if isinstance(repository, str) else [root.name]}
    evidence = {
        **raw,
        "deliveryCheck": "通过" if raw.get("deliveryCheck") == "passed" else "失败",
        "checks": [
            {**check, "exitStatus": str(check.get("exitStatus")), "source": {"path": "testing/evidence/index.json", "startLine": 1}}
            for check in raw.get("checks", []) if isinstance(check, Mapping)
        ],
        "issues": [],
        "source": {"path": "testing/evidence/index.json", "startLine": 1},
    }
    return bool(evaluate_task_evidence(
        task,
        evidence,
        _task_repository_roots(root, mode, item),
        workspace_root=root,
        feature_root=feature,
    )["trusted"])


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "record":
            _, feature = _resolve_feature_path(args.root, args.feature)
            from workspace_status import plan_analysis

            if plan_analysis(feature / "plans" / "implementation.md")["completionPolicy"] != "task-evidence-v2":
                raise EvidenceError("EVIDENCE_POLICY_INVALID", "record 只适用于 task-evidence-v2 Feature")
            raw = _record_input(args.input)
            record(feature, raw, preview=True)
            trusted = _record_trust(args.root, feature, raw)
            if args.preview:
                result = record(feature, raw, preview=True, trusted=trusted)
            else:
                with mutation_lock(feature):
                    result = record(feature, raw, preview=False, trusted=trusted, _locked=True)
                    refresh_summary(args.root, args.feature, feature)
            result["preview"] = bool(args.preview)
            print(json.dumps(result, ensure_ascii=False))
            return 0
        if args.command == "evidence":
            _, feature = _resolve_feature_path(args.root, args.feature)
            result = get_evidence(feature, task_id=args.task, batch_id=args.batch, evidence_id=args.evidence_id)
            if args.task is not None:
                result = dict(result)
                if args.evidence_id is not None:
                    result.update({"trusted": None, "diagnostics": result.get("issues", [])})
                else:
                    from workspace_status import status_result

                    status = status_result(Path(args.root).resolve())
                    current = next(item for item in status["features"] if item["featureSlug"] == feature.name)
                    judgement = next((item for item in current.get("taskEvidence", []) if item["taskId"] == args.task), None)
                    result.update({"trusted": judgement["trusted"] if judgement else None,
                                   "diagnostics": judgement["diagnostics"] if judgement else []})
            print(json.dumps(result, ensure_ascii=False))
            return 0
        if args.command == "history":
            _, feature = _resolve_feature_path(args.root, args.feature)
            print(json.dumps(history_page(feature, cursor=args.cursor, task_id=args.task, batch_id=args.batch, limit=args.limit), ensure_ascii=False))
            return 0
        if args.command == "render":
            if args.preview and args.apply:
                raise EvidenceError("EVIDENCE_ARGUMENT_INVALID", "preview 与 apply 互斥")
            _, feature = _resolve_feature_path(args.root, args.feature)
            result = render_preview(args.root, args.feature, feature)
            if args.apply and result["changed"]:
                refresh_summary(args.root, args.feature, feature)
            result["preview"] = not args.apply
            print(json.dumps(result, ensure_ascii=False))
            return 0
        if args.command in {"migrate", "compact"}:
            if args.preview and args.apply:
                raise EvidenceError("EVIDENCE_ARGUMENT_INVALID", "preview 与 apply 互斥")
            from workspace_status import status_result

            _, feature = _resolve_feature_path(args.root, args.feature)
            def run_operation(locked: bool) -> dict[str, object]:
                if args.apply:
                    recover_transaction(feature, args.command, _locked=locked)
                before = _state_signature(status_result(Path(args.root).resolve()), args.feature)
                if args.command == "migrate":
                    operation_result = migrate_feature(feature, preview=not args.apply, _locked=locked)
                    if args.apply:
                        try:
                            after = _state_signature(status_result(Path(args.root).resolve()), args.feature)
                            if before != after:
                                raise EvidenceError("EVIDENCE_STATE_MISMATCH", "迁移前后可信状态不一致")
                            refresh_summary(args.root, args.feature, feature)
                        except BaseException:
                            rollback_migration(feature, str(operation_result["archive"]))
                            raise
                else:
                    operation_result = compact_feature(feature, preview=not args.apply, _locked=locked)
                    if args.apply:
                        try:
                            after = _state_signature(status_result(Path(args.root).resolve()), args.feature)
                            if before != after:
                                raise EvidenceError("EVIDENCE_STATE_MISMATCH", "压缩前后可信状态不一致")
                            operation_result["summaryChanged"] = refresh_summary(args.root, args.feature, feature) if operation_result.get("changed") else False
                        except BaseException:
                            if operation_result.get("changed"):
                                rollback_compact(feature, str(operation_result["archive"]))
                            raise
                return operation_result

            if args.apply:
                with mutation_lock(feature):
                    result = run_operation(True)
            else:
                result = run_operation(False)
            print(json.dumps(result, ensure_ascii=False))
            return 0
        result = snapshot_result(args.root, args.feature)
    except (OSError, RuntimeError, UnicodeError, ValueError, EvidenceError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False) if args.json else json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
