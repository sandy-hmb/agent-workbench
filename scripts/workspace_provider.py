#!/usr/bin/env python3
"""Run the one activated local Extension Provider bound to a capability."""

from __future__ import annotations

import argparse
import fcntl
import json
import re
import sys
from pathlib import Path, PurePosixPath
from typing import Mapping, Optional, Sequence


sys.dont_write_bytecode = True


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from extension_model import (  # noqa: E402
    ExtensionError,
    ExtensionManifest,
    ProviderDeclaration,
    command_path_is_safe,
    command_path_values,
    extension_digest,
)
from extension_registry import discover_extensions  # noqa: E402
from provider_protocol import API_VERSION, run_provider  # noqa: E402
from workspace_extension import (  # noqa: E402
    ExtensionCommandError,
    _read_lock,
    extension_findings,
    extension_lock,
    validate_manifest_config,
)
from workspace_local import load_local_settings  # noqa: E402
from workspace_model import (  # noqa: E402
    Repository,
    Workspace,
    WorkspaceError,
    load_workspace,
    read_json,
    resolve_repository,
)
_REF_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*/[a-z0-9]+(?:-[a-z0-9]+)*$")
_RESULT_CODE_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
_FALLBACK_PROVIDER = "workspace/provider"
_RESERVED_REQUEST_FIELDS = frozenset({"apiVersion", "provider", "capability", "operation", "config"})


class ProviderStateError(ExtensionError):
    """Raised when a locked Provider's Extension snapshot changed."""


def _result(
    provider: object,
    code: object,
    message: object,
    *,
    blocked: bool = True,
) -> dict[str, object]:
    safe_provider = (
        provider
        if isinstance(provider, str) and _REF_RE.fullmatch(provider)
        else _FALLBACK_PROVIDER
    )
    safe_code = (
        code
        if isinstance(code, str) and _RESULT_CODE_RE.fullmatch(code)
        else "PROVIDER_INCOMPATIBLE"
    )
    try:
        safe_message = str(message).replace("\r", " ").replace("\n", " ").strip()
    except Exception:
        safe_message = ""
    if not safe_message:
        safe_message = "Provider 请求失败"
    return {
        "apiVersion": API_VERSION,
        "provider": safe_provider,
        "status": "blocked" if blocked else "failed",
        "result": None,
        "diagnostics": [{"level": "error", "code": safe_code, "message": safe_message}],
        "effects": [],
    }


def bound_provider_ref(
    workspace: Workspace,
    capability: str,
    repository: Repository | None = None,
) -> str | None:
    """Return a configured binding without checking Extension health or running it."""
    binding = workspace.extensions["providers"].get(capability)
    if not isinstance(binding, Mapping):
        return None
    repositories = binding.get("repositories")
    if repository is not None and isinstance(repositories, Mapping):
        override = repositories.get(repository.path)
        if isinstance(override, str):
            return override
    default = binding.get("default")
    return default if isinstance(default, str) else None


def _resolve_repository(workspace: Workspace, value: str | Repository | None) -> Repository | None:
    if value is None:
        return value
    if isinstance(value, Repository):
        if value not in workspace.repositories:
            raise WorkspaceError(f"未登记仓库：{value.path}")
        return value
    return resolve_repository(workspace.repositories, value)


def _health_failure(root: Path, provider: str) -> dict[str, object] | None:
    try:
        findings = extension_findings(root)
    except (ExtensionError, WorkspaceError, OSError, RuntimeError) as exc:
        return _result(provider, "EXTENSION_DRIFT", str(exc))
    if not findings:
        return None
    finding = findings[0]
    return _result(provider, finding.code, finding.message)


def _locked_provider(
    root: Path,
    workspace: Workspace,
    provider_ref: str,
    capability: str,
) -> tuple[ExtensionManifest, ProviderDeclaration] | None:
    extension_id, provider_name = provider_ref.split("/", 1)
    lock = _read_lock(root)
    if lock["providers"] != workspace.extensions["providers"]:
        raise WorkspaceError("workspace.json 与 extensions/.state/lock.json 的 Provider 绑定不一致")
    locked = {
        item["id"]: item
        for item in lock["extensions"]
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    if extension_id not in locked:
        return None
    manifest = discover_extensions(root).get(extension_id)
    if manifest is None:
        return None
    item = locked[extension_id]
    expected_rows = [
        {
            "capability": provider.capability,
            "provider": provider.provider,
            "apiVersion": provider.api_version,
        }
        for provider in sorted(
            manifest.provides, key=lambda provider: (provider.capability, provider.provider)
        )
    ]
    try:
        snapshot_drift = (
            item.get("version") != manifest.version
            or item.get("path") != f"extensions/{extension_id}"
            or item.get("digest") != extension_digest(manifest.root)
            or item.get("providers") != expected_rows
        )
    except ExtensionError as exc:
        raise ProviderStateError(f"PROVIDER_EXTENSION_DRIFT: {exc}") from exc
    if snapshot_drift:
        raise ProviderStateError(
            f"PROVIDER_EXTENSION_DRIFT: 已激活 Extension 已漂移：{extension_id}"
        )
    declaration = next(
        (
            item
            for item in manifest.provides
            if item.provider == provider_name and item.capability == capability
        ),
        None,
    )
    if declaration is None:
        return None
    lock_rows = locked[extension_id].get("providers")
    if not any(
        isinstance(item, dict)
        and item == {
            "capability": declaration.capability,
            "provider": declaration.provider,
            "apiVersion": declaration.api_version,
        }
        for item in (lock_rows if isinstance(lock_rows, list) else ())
    ):
        return None
    return manifest, declaration


def _regular_command_path(root: Path, value: str) -> bool:
    if not command_path_is_safe(value):
        return False
    relative = value[2:] if value.startswith("./") else value
    path = PurePosixPath(relative)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        return False
    target = root.joinpath(*path.parts)
    current = root
    try:
        for part in path.parts:
            current /= part
            if current.is_symlink():
                return False
        return target.is_file() and not target.is_symlink() and target.resolve().is_relative_to(root.resolve())
    except (OSError, RuntimeError, ValueError):
        return False


def _command(manifest: ExtensionManifest, declaration: ProviderDeclaration) -> list[str] | None:
    if declaration.command is None:
        return None
    command = list(declaration.command)
    for path in command_path_values(command):
        if not _regular_command_path(manifest.root, path):
            return None
    return command


def _request(
    provider_ref: str,
    capability: str,
    operation: str,
    request: Mapping[str, object],
    config: Mapping[str, object],
    repository: Repository | None,
) -> dict[str, object] | None:
    if not isinstance(request, Mapping) or _RESERVED_REQUEST_FIELDS & set(request):
        return None
    payload = dict(request)
    if repository is not None:
        value = payload.get("repository")
        if value is not None and value != repository.path:
            return None
        payload["repository"] = repository.path
    payload.update(
        {
            "apiVersion": API_VERSION,
            "provider": provider_ref,
            "capability": capability,
            "operation": operation,
            "config": dict(config),
        }
    )
    return payload


def run_bound_provider(
    root: Path,
    *,
    capability: str,
    operation: str,
    request: Mapping[str, object],
    repository: str | Repository | None = None,
) -> dict[str, object]:
    """Run an active command Provider under a shared Extension state lock."""
    try:
        bootstrap = load_workspace(Path(root))
        bootstrap_repository = _resolve_repository(bootstrap, repository)
    except WorkspaceError as exc:
        return _result(_FALLBACK_PROVIDER, "PROVIDER_WORKSPACE_INVALID", str(exc))
    provider_ref = bound_provider_ref(bootstrap, capability, bootstrap_repository)
    if provider_ref is None:
        return _result(_FALLBACK_PROVIDER, "PROVIDER_NOT_BOUND", f"Capability 未绑定 Provider：{capability}")
    try:
        with extension_lock(bootstrap.root, fcntl.LOCK_SH, create_cache=True):
            return _run_bound_provider_locked(
                bootstrap.root,
                expected_provider_ref=provider_ref,
                capability=capability,
                operation=operation,
                request=request,
                repository=repository,
            )
    except ExtensionCommandError as exc:
        code, _, message = str(exc).partition(": ")
        if code == "EXTENSION_BUSY":
            return _result(provider_ref, "PROVIDER_BUSY", message or str(exc))
        if code == "EXTENSION_CONFIG_INVALID":
            return _result(provider_ref, "PROVIDER_CONFIG_INVALID", message or str(exc))
        return _result(provider_ref, "PROVIDER_INCOMPATIBLE", str(exc))
    except ProviderStateError as exc:
        code, _, message = str(exc).partition(": ")
        return _result(provider_ref, code, message or str(exc))
    except (ExtensionError, WorkspaceError, OSError) as exc:
        return _result(provider_ref, "PROVIDER_INCOMPATIBLE", str(exc))


def _run_bound_provider_locked(
    root: Path,
    *,
    expected_provider_ref: str,
    capability: str,
    operation: str,
    request: Mapping[str, object],
    repository: str | Repository | None,
) -> dict[str, object]:
    workspace = load_workspace(root)
    resolved_repository = _resolve_repository(workspace, repository)
    provider_ref = bound_provider_ref(workspace, capability, resolved_repository)
    if provider_ref != expected_provider_ref:
        return _result(
            expected_provider_ref,
            "PROVIDER_BINDING_CHANGED",
            f"Provider 绑定在执行前已变化：{capability}",
        )
    health = _health_failure(workspace.root, provider_ref)
    if health is not None:
        return health
    health = _health_failure(workspace.root, provider_ref)
    if health is not None:
        return health
    final_workspace = load_workspace(workspace.root)
    final_repository = _resolve_repository(final_workspace, repository)
    final_ref = bound_provider_ref(final_workspace, capability, final_repository)
    if final_ref != expected_provider_ref:
        return _result(
            provider_ref,
            "PROVIDER_BINDING_CHANGED",
            f"Provider 绑定在执行前已变化：{capability}",
        )
    locked = _locked_provider(
        final_workspace.root, final_workspace, provider_ref, capability
    )
    if locked is None:
        return _result(provider_ref, "PROVIDER_NOT_LOCKED", f"Provider 未被当前 Extension lock：{provider_ref}")
    manifest, declaration = locked
    if declaration.command is None:
        return _result(provider_ref, "PROVIDER_COMMAND_MISSING", f"Provider 未声明可执行 command：{provider_ref}")
    command = _command(manifest, declaration)
    if command is None:
        return _result(provider_ref, "PROVIDER_COMMAND_INVALID", f"Provider command 不安全或不可执行：{provider_ref}")
    try:
        local = load_local_settings(final_workspace.root, required=True)
    except WorkspaceError as exc:
        return _result(provider_ref, "PROVIDER_CONFIG_INVALID", str(exc))
    shared = final_workspace.extensions["config"].get(manifest.id, {})
    local_config = local.extensions.get(manifest.id, {})
    if not isinstance(shared, Mapping) or not isinstance(local_config, Mapping):
        return _result(provider_ref, "PROVIDER_CONFIG_INVALID", "Provider 配置必须是对象")
    config = validate_manifest_config(
        final_workspace.root, manifest, shared, local_config
    )
    payload = _request(
        provider_ref,
        capability,
        operation,
        request,
        config,
        final_repository,
    )
    if payload is None:
        return _result(provider_ref, "PROVIDER_INPUT_INVALID", "Provider 请求覆盖了受保护字段")
    return run_provider(
        command,
        cwd=manifest.root,
        provider=provider_ref,
        request=payload,
        environment=declaration.environment or (),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="调用一个已激活的 Provider")
    run.add_argument("capability")
    run.add_argument("--root", type=Path, default=Path("."))
    run.add_argument("--operation", required=True)
    run.add_argument("--input", type=Path, required=True)
    run.add_argument("--repo")
    run.add_argument("--json", action="store_true")
    return parser


def _print(result: Mapping[str, object], json_output: bool) -> None:
    if json_output:
        print(json.dumps(result, ensure_ascii=False))
        return
    diagnostics = result["diagnostics"]
    assert isinstance(diagnostics, list)
    if diagnostics:
        item = diagnostics[0]
        assert isinstance(item, Mapping)
        print(f"{item['code']}: {item['message']}")
    else:
        print(json.dumps(result["result"], ensure_ascii=False))


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        request = read_json(args.input)
        result = run_bound_provider(
            args.root,
            capability=args.capability,
            operation=args.operation,
            request={"parameters": request},
            repository=args.repo,
        )
    except WorkspaceError as exc:
        result = _result(_FALLBACK_PROVIDER, "PROVIDER_INPUT_INVALID", str(exc))
    _print(result, args.json)
    return 0 if result["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
