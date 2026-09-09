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

from workspace_model import load_workspace, repository_path, resolve_repository


DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
BATCH_HEADER_RE = re.compile(
    r"^## 验证批次 \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:Z|[+-]\d{2}:\d{2})\s*$"
)
CHECK_HEADER_RE = re.compile(r"^### 检查 [1-9][0-9]*\s*$", re.MULTILINE)
CHECK_FIELDS = ("工作目录：", "命令：", "退出状态：", "结果：")
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
        if PurePosixPath(relative).as_posix() in excluded_paths:
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
            for item in ("README.md", "plans/implementation.md", "testing/verification.md")
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
    parser = argparse.ArgumentParser(description="读取当前需求的 Git 代码状态（只读）。")
    subcommands = parser.add_subparsers(dest="command", required=True)
    snapshot = subcommands.add_parser("snapshot", help="输出当前需求的代码状态指纹")
    snapshot.add_argument("feature")
    snapshot.add_argument("--root", type=Path, default=Path.cwd())
    snapshot.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = snapshot_result(args.root, args.feature)
    except (OSError, RuntimeError, UnicodeError, ValueError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False) if args.json else json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
