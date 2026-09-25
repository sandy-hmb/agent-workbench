"""Bounded read-only Git fingerprint operations, shared by queries and validation."""
from __future__ import annotations
import hashlib
import os
import stat
import subprocess
import threading
import time
from pathlib import Path, PurePosixPath
from typing import Iterable


def branch_fingerprint(repository: Path, branch: str, *, timeout: float = 5) -> str:
    """Fingerprint a local committed branch without checkout or fetching."""
    started = time.monotonic()
    _git(repository, ['check-ref-format', '--branch', branch], timeout, 65536)
    remaining = timeout - (time.monotonic() - started)
    if remaining <= 0:
        raise subprocess.TimeoutExpired('git', timeout)
    head = _git(repository, ['rev-parse', '--verify', 'refs/heads/' + branch + '^{commit}'], remaining, 65536).strip()
    result = hashlib.sha256()
    _digest_part(result, b'HEAD', head)
    _digest_part(result, b'DIFF', b'')
    return 'sha256:' + result.hexdigest()

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
