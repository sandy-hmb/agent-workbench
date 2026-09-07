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
from pathlib import Path, PurePosixPath
from typing import Iterable, Mapping

from workspace_model import load_workspace, repository_path, resolve_repository


DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
BATCH_HEADER_RE = re.compile(
    r"^## 验证批次 \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:Z|[+-]\d{2}:\d{2})\s*$"
)
CHECK_HEADER_RE = re.compile(r"^### 检查 [1-9][0-9]*\s*$", re.MULTILINE)
CHECK_FIELDS = ("工作目录：", "命令：", "退出状态：", "结果：")


def _git(repository: Path, arguments: list[str]) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=False,
        capture_output=True,
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


def git_fingerprint(repository: Path, excluded: Iterable[str] = ()) -> str:
    repository = Path(repository).resolve()
    top = Path(os.fsdecode(_git(repository, ["rev-parse", "--show-toplevel"]).strip())).resolve()
    if top != repository:
        raise ValueError(f"仓库不是独立 Git 根目录：{repository}")
    head = _git(repository, ["rev-parse", "--verify", "HEAD"]).strip()
    excluded_paths = _excluded_paths(excluded)
    pathspecs = [".", *(f":(exclude){path}" for path in excluded_paths)]
    diff = _git(
        repository,
        ["diff", "--binary", "--no-ext-diff", "HEAD", "--", *pathspecs],
    )
    untracked = _git(repository, ["ls-files", "--others", "--exclude-standard", "-z", "--", "."])

    digest = hashlib.sha256()
    _digest_part(digest, b"HEAD", head)
    _digest_part(digest, b"DIFF", diff)
    for raw_path in sorted(path for path in untracked.split(b"\0") if path):
        relative = os.fsdecode(raw_path)
        if PurePosixPath(relative).as_posix() in excluded_paths:
            continue
        path = repository / relative
        file_stat = path.lstat()
        if stat.S_ISLNK(file_stat.st_mode):
            content = os.fsencode(os.readlink(path))
        elif stat.S_ISREG(file_stat.st_mode):
            content = path.read_bytes()
        else:
            raise ValueError(f"未跟踪路径不是普通文件或符号链接：{path}")
        _digest_part(digest, b"PATH", raw_path)
        _digest_part(digest, b"DATA", content)
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
    root: Path, mode: str, feature: Mapping[str, object]
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
        return {root.name: git_fingerprint(root, excluded)}

    workspace = load_workspace(root)
    result = {}
    for name in repositories:
        repository = resolve_repository(workspace.repositories, name)
        result[name] = git_fingerprint(repository_path(workspace, repository))
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
