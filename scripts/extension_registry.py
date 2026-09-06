#!/usr/bin/env python3
"""Discovery and workspace binding validation for local extensions."""
from __future__ import annotations

from pathlib import Path
from typing import Mapping

from extension_model import CAPABILITIES, ExtensionError, ExtensionManifest, load_manifest
from workspace_model import reject_sensitive_fields
from workspace_paths import extensions_root, state_root


def discover_extensions(root: Path) -> dict[str, ExtensionManifest]:
    if Path(root).is_symlink():
        raise ExtensionError(f"workspace root must be regular directory: {root}")
    state = state_root(root)
    if state.is_symlink():
        raise ExtensionError(f"workspace state must be regular directory: {state}")
    base = extensions_root(root)
    if base.is_symlink():
        raise ExtensionError(f"extensions root must be regular directory: {base}")
    if not base.exists():
        return {}
    if base.is_symlink() or not base.is_dir():
        raise ExtensionError(f"extensions root must be regular directory: {base}")
    result: dict[str, ExtensionManifest] = {}
    for child in sorted(base.iterdir(), key=lambda p: p.name):
        if child.name == ".state":
            continue
        if child.is_symlink() or not child.is_dir():
            raise ExtensionError(f"extension entry must be regular directory: {child}")
        manifest = load_manifest(child / "workspace-extension.json")
        if manifest.id in result:
            raise ExtensionError(f"duplicate extension id: {manifest.id}")
        result[manifest.id] = manifest
    return result


discover_local_extensions = discover_extensions


def normalize_workspace_extensions(value: object, discovered: Mapping[str, ExtensionManifest] | None = None) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != {"providers", "config"}:
        raise ExtensionError("extensions must contain providers and config")
    providers = value["providers"]
    config = value["config"]
    if not isinstance(providers, dict) or not isinstance(config, dict):
        raise ExtensionError("extensions.providers and extensions.config must be objects")
    reject_sensitive_fields(config, "extensions.config")
    normalized: dict[str, object] = {"providers": {}, "config": {}}
    provider_out: dict[str, object] = {}
    for capability, binding in providers.items():
        if capability not in CAPABILITIES:
            raise ExtensionError(f"unknown capability binding: {capability}")
        if not isinstance(binding, dict) or set(binding) != {"default", "repositories"}:
            raise ExtensionError(f"binding for {capability} must contain default and repositories")
        default = binding["default"]
        if default is not None and (not isinstance(default, str) or not _valid_ref(default)):
            raise ExtensionError(f"invalid default provider ref: {capability}")
        repositories = binding["repositories"]
        if not isinstance(repositories, dict) or any(not isinstance(name, str) or not name or any(c in name for c in "\r\n") or not isinstance(ref, str) or not _valid_ref(ref) for name, ref in repositories.items()):
            raise ExtensionError(f"invalid repository provider refs: {capability}")
        provider_out[capability] = {"default": default, "repositories": dict(repositories)}
    config_out: dict[str, object] = {}
    for extension_id, item in config.items():
        if not isinstance(extension_id, str) or not _valid_id(extension_id) or not isinstance(item, dict):
            raise ExtensionError("extension config keys must be safe ids mapped to objects")
        config_out[extension_id] = dict(item)
    if discovered is not None:
        unknown_config = set(config_out) - set(discovered)
        if unknown_config:
            raise ExtensionError(f"unknown extension config: {sorted(unknown_config)[0]}")
        refs = {
            f"{manifest.id}/{provider.provider}": (manifest, provider)
            for manifest in discovered.values()
            for provider in manifest.provides
        }
        for binding in provider_out.values():
            assert isinstance(binding, dict)
            candidates = [binding["default"], *binding["repositories"].values()]
            for ref in candidates:
                if ref is not None:
                    if ref not in refs:
                        raise ExtensionError(f"unknown provider ref: {ref}")
                    if refs[ref][1].capability != next(cap for cap, candidate in provider_out.items() if candidate is binding):
                        raise ExtensionError(f"provider capability mismatch: {ref}")
    normalized["providers"] = provider_out
    normalized["config"] = config_out
    return normalized


def validate_workspace_extensions(value: object, discovered: Mapping[str, ExtensionManifest]) -> dict[str, object]:
    return normalize_workspace_extensions(value, discovered)


def _valid_id(value: str) -> bool:
    import re
    return bool(re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", value))


def _valid_ref(value: str) -> bool:
    parts = value.split("/")
    return len(parts) == 2 and all(_valid_id(part) for part in parts)
