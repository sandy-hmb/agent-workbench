#!/usr/bin/env python3
"""Run one local Extension Provider through a bounded JSON stdin/stdout protocol."""

from __future__ import annotations

import json
import math
import os
import re
import selectors
import signal
import subprocess
import sys
import time
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping, Sequence

from schema_validation import SchemaValidationError, validate
from workspace_model import WorkspaceError, parse_json_bytes


API_VERSION = 1
DEFAULT_TIMEOUT_SECONDS = 30.0
MAX_OUTPUT_BYTES = 1024 * 1024
POST_EXIT_DRAIN_SECONDS = 0.25
MAX_REDACTION_DEPTH = 128
RESULT_FIELDS = frozenset(
    {"apiVersion", "provider", "status", "result", "diagnostics", "effects"}
)
_PROVIDER_RE = re.compile(
    r"^[a-z0-9]+(?:-[a-z0-9]+)*/[a-z0-9]+(?:-[a-z0-9]+)*$"
)
_ENVIRONMENT_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
_BASE_ENVIRONMENT = ("PATH", "LANG", "LC_ALL")
_SCHEMA_PATH = Path(__file__).resolve().parents[1] / "schemas" / "provider-result.schema.json"


@lru_cache(maxsize=1)
def _schema() -> dict[str, Any]:
    try:
        return json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"无法读取 Provider 结果契约：{exc}") from exc


def _validated_envelope(data: bytes, provider: str) -> dict[str, object]:
    envelope = parse_json_bytes(data, "Provider stdout")
    if set(envelope) != RESULT_FIELDS:
        raise WorkspaceError("Provider 结果字段无效")
    validate(envelope, _schema())
    if envelope["provider"] != provider:
        raise WorkspaceError("Provider 结果引用不匹配")
    if envelope["apiVersion"] != API_VERSION:
        raise WorkspaceError("Provider apiVersion 不匹配")
    if envelope["status"] != "ok":
        diagnostics = envelope["diagnostics"]
        if (
            not isinstance(diagnostics, list)
            or not diagnostics
            or not any(
                isinstance(item, dict) and item.get("level") == "error"
                for item in diagnostics
            )
        ):
            raise WorkspaceError("Provider 非 ok 结果必须包含 error diagnostic")
    return envelope

def _safe_provider(provider: object) -> str:
    return provider if isinstance(provider, str) and _PROVIDER_RE.fullmatch(provider) else "invalid/invalid"


def _failure(provider: object, code: str, message: str, *, blocked: bool = False) -> dict[str, object]:
    return {
        "apiVersion": API_VERSION,
        "provider": _safe_provider(provider),
        "status": "blocked" if blocked else "failed",
        "result": None,
        "diagnostics": [{"level": "error", "code": code, "message": message}],
        "effects": [],
    }


def _redact(
    value: object, declared_values: Sequence[str], depth: int = 0
) -> object:
    if depth > MAX_REDACTION_DEPTH:
        raise WorkspaceError("Provider 结果嵌套过深")
    if isinstance(value, str):
        result = value
        for secret in sorted(set(declared_values), key=len, reverse=True):
            if secret:
                result = result.replace(secret, "[REDACTED]")
        return result
    if isinstance(value, list):
        return [_redact(item, declared_values, depth + 1) for item in value]
    if isinstance(value, dict):
        return {
            str(_redact(key, declared_values, depth + 1)): _redact(
                item, declared_values, depth + 1
            )
            for key, item in value.items()
        }
    return value


def _redacted_envelope(
    envelope: Mapping[str, object], declared_values: Sequence[str]
) -> dict[str, object]:
    diagnostics = envelope["diagnostics"]
    assert isinstance(diagnostics, list)
    safe_diagnostics = []
    for diagnostic in diagnostics:
        assert isinstance(diagnostic, dict)
        safe = dict(diagnostic)
        code = safe["code"]
        assert isinstance(code, str)
        if _redact(code, declared_values) != code:
            safe["code"] = "REDACTED"
        safe["message"] = _redact(safe["message"], declared_values)
        if "evidence" in safe:
            safe["evidence"] = _redact(safe["evidence"], declared_values)
        safe_diagnostics.append(safe)
    return {
        "apiVersion": envelope["apiVersion"],
        "provider": envelope["provider"],
        "status": envelope["status"],
        "result": _redact(envelope["result"], declared_values),
        "diagnostics": safe_diagnostics,
        "effects": _redact(envelope["effects"], declared_values),
    }


def _environment(names: Sequence[str]) -> tuple[dict[str, str] | None, tuple[str, ...]]:
    if (
        len(set(names)) != len(names)
        or any(not isinstance(name, str) or not _ENVIRONMENT_RE.fullmatch(name) for name in names)
    ):
        return None, ()
    missing = tuple(name for name in names if not os.environ.get(name))
    if missing:
        return None, missing
    environment = {
        name: os.environ[name]
        for name in (*_BASE_ENVIRONMENT, *names)
        if name in os.environ
    }
    return environment, ()


def _close_stream(selector: selectors.BaseSelector, stream: Any) -> None:
    try:
        selector.unregister(stream)
    except (KeyError, ValueError, OSError):
        pass
    try:
        stream.close()
    except OSError:
        pass


def _read_output(stream: Any, output: bytearray) -> tuple[bool, bool]:
    """Return (closed, over_limit) after draining a nonblocking output pipe."""
    closed = False
    over_limit = False
    while True:
        try:
            chunk = os.read(stream.fileno(), 64 * 1024)
        except BlockingIOError:
            return closed, over_limit
        except OSError:
            return True, over_limit
        if not chunk:
            return True, over_limit
        remaining = MAX_OUTPUT_BYTES - len(output)
        if remaining > 0:
            output.extend(chunk[:remaining])
        if len(chunk) > remaining:
            over_limit = True
            return closed, over_limit


def _signal_group(group_id: int, signal_number: int) -> bool:
    try:
        os.killpg(group_id, signal_number)
        return True
    except ProcessLookupError:
        return False
    except OSError:
        return False


def _provider_group(process: subprocess.Popen[bytes]) -> int | None:
    if os.name != "posix":
        return None
    try:
        if os.getpgid(process.pid) != process.pid or os.getsid(process.pid) != process.pid:
            return None
    except OSError:
        return None
    return process.pid


def _stop(
    process: subprocess.Popen[bytes],
    group_id: int | None,
    *,
    leader_reaped: bool = False,
) -> None:
    """Stop the verified Provider process group and, when needed, its leader."""
    if os.name != "posix":
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        return
    if group_id is None:
        return
    # The group was verified at launch.  When poll has just reaped the leader,
    # killpg either reaches residual members of that same group or fails.
    _signal_group(group_id, signal.SIGTERM)
    time.sleep(0.05)
    _signal_group(group_id, signal.SIGKILL)
    if leader_reaped:
        return
    try:
        process.wait(timeout=0.5)
    except subprocess.TimeoutExpired:
        pass


def run_provider(
    command: Sequence[str],
    *,
    cwd: Path,
    provider: str,
    request: Mapping[str, object],
    environment: Sequence[str] = (),
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, object]:
    """Return a validated Provider result or a fixed structured failure envelope."""
    expected_provider = _safe_provider(provider)
    if expected_provider != provider:
        return _failure(provider, "PROVIDER_PROTOCOL_ERROR", "Provider 引用无效")
    if (
        not isinstance(command, Sequence)
        or isinstance(command, (str, bytes))
        or not command
        or any(not isinstance(item, str) or not item or "\0" in item for item in command)
    ):
        return _failure(provider, "PROVIDER_PROTOCOL_ERROR", "Provider command 无效")
    if not isinstance(request, Mapping):
        return _failure(provider, "PROVIDER_PROTOCOL_ERROR", "Provider 输入必须是对象")
    if (
        not isinstance(timeout, (int, float))
        or isinstance(timeout, bool)
        or not math.isfinite(timeout)
        or timeout <= 0
    ):
        return _failure(provider, "PROVIDER_PROTOCOL_ERROR", "Provider timeout 无效")
    env, missing = _environment(tuple(environment))
    if env is None:
        if missing:
            return _failure(
                provider,
                "CREDENTIAL_MISSING",
                "Provider 缺少声明环境变量：" + ", ".join(missing),
                blocked=True,
            )
        return _failure(provider, "PROVIDER_PROTOCOL_ERROR", "Provider environment 声明无效")
    try:
        payload = json.dumps(
            dict(request), ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    except (RecursionError, TypeError, ValueError, UnicodeError):
        return _failure(provider, "PROVIDER_PROTOCOL_ERROR", "Provider 输入不是有效 JSON 对象")
    declared_values = tuple(env[name] for name in environment)
    try:
        process = subprocess.Popen(
            list(command),
            cwd=str(cwd),
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=os.name == "posix",
        )
    except OSError:
        return _failure(provider, "PROVIDER_PROCESS_ERROR", "Provider 进程无法启动")
    assert process.stdin is not None and process.stdout is not None and process.stderr is not None
    group_id = _provider_group(process)
    if os.name == "posix" and group_id is None:
        for stream in (process.stdin, process.stdout, process.stderr):
            try:
                stream.close()
            except OSError:
                pass
        try:
            process.terminate()
            process.wait(timeout=0.25)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        except OSError:
            pass
        return _failure(provider, "PROVIDER_PROCESS_ERROR", "Provider 进程组身份无法验证")
    stdout = bytearray()
    stderr = bytearray()
    reason: str | None = None
    deadline = time.monotonic() + float(timeout)
    selector = selectors.DefaultSelector()
    stdout_open = True
    stderr_open = True
    offset = 0
    leader_reaped = False
    group_stopped = False
    post_exit_deadline: float | None = None
    try:
        for stream in (process.stdin, process.stdout, process.stderr):
            os.set_blocking(stream.fileno(), False)
        if payload:
            selector.register(process.stdin, selectors.EVENT_WRITE, "stdin")
        else:
            _close_stream(selector, process.stdin)
        selector.register(process.stdout, selectors.EVENT_READ, "stdout")
        selector.register(process.stderr, selectors.EVENT_READ, "stderr")
        while True:
            now = time.monotonic()
            if not leader_reaped and process.poll() is not None:
                leader_reaped = True
                _stop(process, group_id, leader_reaped=True)
                group_stopped = True
                post_exit_deadline = min(deadline, now + POST_EXIT_DRAIN_SECONDS)
            if leader_reaped and post_exit_deadline is not None and now >= post_exit_deadline:
                break
            remaining = deadline - now
            if remaining <= 0:
                reason = "PROVIDER_TIMEOUT"
                break
            if post_exit_deadline is not None:
                remaining = min(remaining, post_exit_deadline - now)
            events = selector.select(min(remaining, 0.05))
            for key, _ in events:
                stream = key.fileobj
                if key.data == "stdin":
                    try:
                        written = os.write(stream.fileno(), payload[offset : offset + 64 * 1024])
                    except BlockingIOError:
                        continue
                    except OSError:
                        _close_stream(selector, stream)
                        continue
                    offset += written
                    if offset == len(payload):
                        _close_stream(selector, stream)
                else:
                    output = stdout if key.data == "stdout" else stderr
                    closed, over_limit = _read_output(stream, output)
                    if closed:
                        _close_stream(selector, stream)
                        if key.data == "stdout":
                            stdout_open = False
                        else:
                            stderr_open = False
                    if over_limit:
                        reason = "PROVIDER_OUTPUT_LIMIT"
                        break
            if reason is not None:
                break
            if leader_reaped and not (stdout_open or stderr_open):
                break
    except OSError:
        reason = "PROVIDER_PROCESS_ERROR"
    finally:
        if not group_stopped:
            _stop(process, group_id, leader_reaped=leader_reaped)
        for stream in (process.stdin, process.stdout, process.stderr):
            _close_stream(selector, stream)
        selector.close()
    if reason == "PROVIDER_TIMEOUT":
        return _failure(provider, reason, "Provider 执行超时")
    if reason == "PROVIDER_OUTPUT_LIMIT":
        return _failure(provider, "PROVIDER_OUTPUT_LIMIT", "Provider 输出超过 1 MiB 限制")
    if reason == "PROVIDER_PROCESS_ERROR":
        return _failure(provider, reason, "Provider I/O 处理失败")
    if process.returncode != 0:
        return _failure(provider, "PROVIDER_PROCESS_ERROR", "Provider 进程异常退出")
    try:
        envelope = _validated_envelope(bytes(stdout), provider)
    except (RecursionError, RuntimeError, WorkspaceError, SchemaValidationError):
        return _failure(provider, "PROVIDER_PROTOCOL_ERROR", "Provider 输出不符合结果契约")
    try:
        return _redacted_envelope(envelope, declared_values)
    except (RecursionError, WorkspaceError):
        return _failure(provider, "PROVIDER_PROTOCOL_ERROR", "Provider 结果无法安全脱敏")


__all__ = [
    "API_VERSION",
    "DEFAULT_TIMEOUT_SECONDS",
    "MAX_OUTPUT_BYTES",
    "RESULT_FIELDS",
    "run_provider",
]
