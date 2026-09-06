#!/usr/bin/env python3
"""Agent-neutral local extension manifest model and filesystem integrity checks."""
from __future__ import annotations

import hashlib
import json
import re
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Sequence

from core_capabilities import CORE_CAPABILITIES, REMOVED_CAPABILITIES
from workspace_model import WorkspaceError, parse_json_bytes

# Compatibility import for existing callers. The source of truth lives in
# core_capabilities.py, not in the manifest parser.
CAPABILITIES = CORE_CAPABILITIES
EFFECTS = frozenset({
    "read", "write", "network", "command", "filesystem.read", "filesystem.write",
    "process.exec", "git.read", "git.write", "policy.read", "policy.write",
})
ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
SEMVER_RE = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
ENV_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
NETWORK_URI_RE = re.compile(r"^(?:https|ssh)://")
URI_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")
_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:")
_LONG_PATH_OPTIONS = frozenset({"config", "file", "path", "script", "cwd", "dir", "directory"})
_SHORT_PATH_OPTIONS = frozenset({"C", "f"})
_SEPARATED_PATH_OPTIONS = frozenset(
    {f"--{name}" for name in _LONG_PATH_OPTIONS} | {f"-{name}" for name in _SHORT_PATH_OPTIONS}
)


class ExtensionError(WorkspaceError):
    pass


@dataclass(frozen=True)
class AdapterSpec:
    agent_path: str
    claude_path: str
    skill: str
    installed_digest: str

    def as_dict(self) -> dict[str, str]:
        return {
            "agentPath": self.agent_path,
            "claudePath": self.claude_path,
            "skill": self.skill,
            "installedDigest": self.installed_digest,
        }

    @classmethod
    def from_dict(cls, value: object) -> "AdapterSpec":
        fields = {"agentPath", "claudePath", "skill", "installedDigest"}
        if not isinstance(value, dict) or set(value) != fields:
            raise ExtensionError("adapter must contain agentPath, claudePath, skill, installedDigest")
        agent_path = value["agentPath"]
        claude_path = value["claudePath"]
        skill = value["skill"]
        digest = value["installedDigest"]
        if not _safe_relative(agent_path) or not _safe_relative(claude_path):
            raise ExtensionError("adapter paths must be safe relative paths")
        if not isinstance(skill, str) or not ID_RE.fullmatch(skill):
            raise ExtensionError("adapter skill must be kebab-case")
        if not isinstance(digest, str) or not DIGEST_RE.fullmatch(digest):
            raise ExtensionError("adapter installedDigest must be sha256")
        return cls(agent_path, claude_path, skill, digest)


@dataclass(frozen=True)
class ProviderDeclaration:
    capability: str
    provider: str
    api_version: int
    skill: str
    command: tuple[str, ...] | None = None
    environment: tuple[str, ...] | None = None
    extension_id: str = ""

    @property
    def ref(self) -> str:
        return f"{self.extension_id}/{self.provider}"

    def as_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "capability": self.capability,
            "provider": self.provider,
            "apiVersion": self.api_version,
            "skill": self.skill,
        }
        if self.command is not None:
            result["command"] = list(self.command)
        if self.environment is not None:
            result["environment"] = list(self.environment)
        return result


@dataclass(frozen=True)
class ActionDeclaration:
    action: str
    api_version: int
    skill: str
    effects: tuple[str, ...]
    confirmation_title: str | None = None
    confirmation_summary: str | None = None
    command: tuple[str, ...] | None = None
    environment: tuple[str, ...] | None = None
    extension_id: str = ""

    @property
    def ref(self) -> str:
        return f"{self.extension_id}/{self.action}"

    def as_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "id": self.action,
            "apiVersion": self.api_version,
            "skill": self.skill,
            "effects": list(self.effects),
        }
        if self.confirmation_title is not None:
            result["confirmation"] = {
                "title": self.confirmation_title,
                "summary": self.confirmation_summary,
            }
        if self.command is not None:
            result["command"] = list(self.command)
        if self.environment is not None:
            result["environment"] = list(self.environment)
        return result


@dataclass(frozen=True)
class ExtensionManifest:
    schema_version: int
    id: str
    version: str
    kit_api: int
    provides: tuple[ProviderDeclaration, ...]
    actions: tuple[ActionDeclaration, ...]
    requires: tuple[str, ...]
    config_schema: str | None
    effects: tuple[str, ...]
    root: Path

    @property
    def providers(self) -> tuple[ProviderDeclaration, ...]:
        return self.provides

    @property
    def provider_refs(self) -> tuple[str, ...]:
        return tuple(f"{self.id}/{provider.provider}" for provider in self.provides)

    @property
    def action_refs(self) -> tuple[str, ...]:
        return tuple(action.ref for action in self.actions)

    def provider_ref(self, provider: str) -> str:
        if not any(item.provider == provider for item in self.provides):
            raise ExtensionError(f"unknown provider: {self.id}/{provider}")
        return f"{self.id}/{provider}"

    def action_ref(self, action: str) -> str:
        if not any(item.action == action for item in self.actions):
            raise ExtensionError(f"unknown action: {self.id}/{action}")
        return f"{self.id}/{action}"


def _safe_relative(value: object) -> bool:
    if not isinstance(value, str) or not value or "\\" in value or "\0" in value or "//" in value or any(c.isspace() for c in value):
        return False
    path = PurePosixPath(value)
    return not path.is_absolute() and all(part not in {"", ".", ".."} for part in path.parts)


def _string(value: object, label: str, *, pattern: re.Pattern[str] | None = None) -> str:
    if not isinstance(value, str) or not value or any(c in value for c in "\r\n"):
        raise ExtensionError(f"{label} must be non-empty string")
    if pattern and not pattern.fullmatch(value):
        raise ExtensionError(f"{label} has invalid format")
    return value


def _confirmation(value: object) -> tuple[str, str]:
    if not isinstance(value, dict) or set(value) != {"title", "summary"}:
        raise ExtensionError("action confirmation must contain title and summary")
    title = _string(value["title"], "action confirmation title")
    summary = _string(value["summary"], "action confirmation summary")
    if len(title.encode("utf-8")) > 160 or len(summary.encode("utf-8")) > 1024:
        raise ExtensionError("action confirmation is too long")
    return title, summary


def _looks_path(value: str) -> bool:
    return (
        bool(value)
        and not NETWORK_URI_RE.match(value)
        and (
            value.startswith(("/", ".", "\\"))
            or _WINDOWS_DRIVE_RE.match(value)
            or "/" in value
            or "\\" in value
        )
    )


def _at_path(value: str) -> str | None:
    """Normalize a tool's @file argument to the file path it actually names."""
    if value.startswith("@"):
        return value[1:]
    if value.startswith("=@"):
        return value[2:]
    return None


def command_path_values(command: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    """Return normalized argv values that explicitly name local paths."""
    values: list[str] = []

    def add(value: str, *, force: bool = False) -> None:
        at_path = _at_path(value)
        if at_path is not None:
            values.append(at_path)
        elif force or _looks_path(value):
            values.append(value)

    for index, argument in enumerate(command):
        previous = command[index - 1] if index else None
        if previous == "-c":
            continue
        if previous in _SEPARATED_PATH_OPTIONS:
            add(argument, force=True)
            continue
        if index == 0:
            add(argument)
            continue
        if NETWORK_URI_RE.match(argument):
            continue
        if _at_path(argument) is not None:
            add(argument)
            continue
        if argument.startswith("--"):
            name, separator, value = argument[2:].partition("=")
            if separator and (name in _LONG_PATH_OPTIONS or _looks_path(value) or _at_path(value) is not None):
                add(value, force=name in _LONG_PATH_OPTIONS)
            continue
        if argument.startswith("-") and len(argument) > 2:
            option = argument[1]
            value = argument[2:]
            if option in _SHORT_PATH_OPTIONS or _looks_path(value) or _at_path(value) is not None:
                add(value, force=option in _SHORT_PATH_OPTIONS)
            continue
        add(argument)
    return tuple(values)


def command_path_is_safe(value: str) -> bool:
    """Apply the same lexical path policy at manifest and execution time."""
    relative = value[2:] if value.startswith("./") else value
    path = PurePosixPath(relative)
    return (
        bool(value)
        and not value.startswith("@")
        and not URI_RE.match(value)
        and not _WINDOWS_DRIVE_RE.match(value)
        and "\\" not in value
        and "\0" not in value
        and not path.is_absolute()
        and not any(part in {"", ".", ".."} for part in path.parts)
    )


def validate_command_argv(command: tuple[str, ...] | list[str]) -> None:
    """Reject lexical argv path escapes before an Extension can be activated."""
    for value in command_path_values(command):
        if not command_path_is_safe(value):
            raise ExtensionError("command paths must stay inside extension root")


def _command(value: object) -> tuple[str, ...] | None:
    if value is None:
        return None
    if (
        not isinstance(value, list)
        or not value
        or any(
            not isinstance(item, str)
            or not item
            or not item.strip()
            or any(character in item for character in "\0\r\n")
            for item in value
        )
    ):
        raise ExtensionError("command must be non-empty argv strings")
    command = tuple(value)
    validate_command_argv(command)
    return command


def _environment(value: object) -> tuple[str, ...] | None:
    if value is None:
        return None
    if (
        not isinstance(value, list)
        or len(set(value)) != len(value)
        or any(not isinstance(item, str) or not ENV_RE.fullmatch(item) for item in value)
    ):
        raise ExtensionError("environment must contain unique safe names")
    return tuple(value)


def _effects(value: object, label: str) -> tuple[str, ...]:
    if (
        not isinstance(value, list)
        or len(set(value)) != len(value)
        or any(not isinstance(item, str) or item not in EFFECTS for item in value)
    ):
        raise ExtensionError(f"{label} must contain known effect names")
    return tuple(value)


def _capability(value: object, label: str) -> str:
    capability = _string(value, label)
    if capability in REMOVED_CAPABILITIES:
        raise ExtensionError(
            f"EXTENSION_CAPABILITY_REMOVED: {capability} must migrate to a workflow action"
        )
    if capability not in CAPABILITIES:
        raise ExtensionError(f"unknown capability: {capability}")
    return capability


def load_manifest(path: Path) -> ExtensionManifest:
    root_path = path.parent
    if root_path.is_symlink() or not root_path.is_dir():
        raise ExtensionError(f"extension root must be regular directory: {root_path}")
    root = root_path.resolve()
    if path.name != "workspace-extension.json" or path.is_symlink() or not path.is_file():
        raise ExtensionError(f"manifest must be a regular workspace-extension.json: {path}")
    raw = parse_json_bytes(path.read_bytes(), str(path))
    schema_version = raw.get("schemaVersion")
    if schema_version != 1 or type(schema_version) is not int:
        raise ExtensionError("schemaVersion must be 1")
    required = {"schemaVersion", "id", "version", "kitApi", "provides", "requires", "effects"}
    required = required | {"actions"}
    allowed = required | {"configSchema"}
    if set(raw) - allowed or required - set(raw):
        raise ExtensionError("manifest fields are invalid")
    kit_api = raw["kitApi"]
    if kit_api != schema_version or type(kit_api) is not int:
        raise ExtensionError("kitApi must match schemaVersion")
    extension_id = _string(raw["id"], "id", pattern=ID_RE)
    if root.name != extension_id:
        raise ExtensionError("extension root name must match manifest id")
    version = _string(raw["version"], "version")
    if not SEMVER_RE.fullmatch(version):
        raise ExtensionError("version must be X.Y.Z semver")
    effects = _effects(raw["effects"], "effects")
    provides_raw = raw["provides"]
    if not isinstance(provides_raw, list):
        raise ExtensionError("provides must be array")
    providers: list[ProviderDeclaration] = []
    seen_refs: set[str] = set()
    for item in provides_raw:
        if not isinstance(item, dict):
            raise ExtensionError("provider declaration must be object")
        allowed_provider = {
            "capability",
            "provider",
            "apiVersion",
            "skill",
            "command",
            "environment",
        }
        if set(item) - allowed_provider or not {
            "capability",
            "provider",
            "apiVersion",
            "skill",
        } <= set(item):
            raise ExtensionError("provider declaration fields are invalid")
        capability = _capability(item["capability"], "capability")
        provider = _string(item["provider"], "provider", pattern=ID_RE)
        if provider in seen_refs:
            raise ExtensionError(f"duplicate provider or action: {extension_id}/{provider}")
        seen_refs.add(provider)
        if type(item["apiVersion"]) is not int or item["apiVersion"] != 1:
            raise ExtensionError("provider apiVersion must be 1")
        skill = _string(item["skill"], "skill", pattern=ID_RE)
        providers.append(
            ProviderDeclaration(
                capability,
                provider,
                1,
                skill,
                _command(item.get("command")),
                _environment(item.get("environment")),
                extension_id,
            )
        )
    actions_raw = raw["actions"]
    if not isinstance(actions_raw, list):
        raise ExtensionError("actions must be array")
    actions: list[ActionDeclaration] = []
    for item in actions_raw:
        if not isinstance(item, dict):
            raise ExtensionError("action declaration must be object")
        allowed_action = {
            "id", "apiVersion", "skill", "confirmation", "command", "environment", "effects",
        }
        if set(item) - allowed_action or not {"id", "apiVersion", "skill", "effects"} <= set(item):
            raise ExtensionError("action declaration fields are invalid")
        action = _string(item["id"], "action id", pattern=ID_RE)
        if action in seen_refs:
            raise ExtensionError(f"duplicate provider or action: {extension_id}/{action}")
        seen_refs.add(action)
        api_version = item["apiVersion"]
        if type(api_version) is not int or api_version != 1:
            raise ExtensionError("action apiVersion must be 1")
        skill = _string(item["skill"], "skill", pattern=ID_RE)
        action_effects = _effects(item["effects"], "action effects")
        if not set(action_effects) <= set(effects):
            raise ExtensionError("action effects must be contained by extension effects")
        confirmation_title, confirmation_summary = _confirmation(item.get("confirmation"))
        actions.append(
            ActionDeclaration(
                action,
                api_version,
                skill,
                action_effects,
                confirmation_title,
                confirmation_summary,
                _command(item.get("command")),
                _environment(item.get("environment")),
                extension_id,
            )
        )
    requires_raw = raw["requires"]
    if not isinstance(requires_raw, list) or len(set(requires_raw)) != len(requires_raw):
        raise ExtensionError("requires must be unique capability names")
    requires = tuple(_capability(value, "requires") for value in requires_raw)
    config_schema = raw.get("configSchema")
    if config_schema is not None and not _safe_relative(config_schema):
        raise ExtensionError("configSchema must be safe relative path")
    _validate_skills(root, (*providers, *actions))
    return ExtensionManifest(
        schema_version,
        extension_id,
        version,
        kit_api,
        tuple(providers),
        tuple(actions),
        requires,
        config_schema,
        effects,
        root,
    )


def _validate_skills(
    root: Path, declarations: Sequence[ProviderDeclaration | ActionDeclaration]
) -> None:
    for declaration in declarations:
        skill_file = root / "skills" / declaration.skill / "SKILL.md"
        for parent in (root / "skills", root / "skills" / declaration.skill):
            if parent.is_symlink():
                raise ExtensionError(f"skill path may not contain symlink: {parent}")
        if skill_file.is_symlink() or not skill_file.is_file():
            raise ExtensionError(f"missing extension skill: {declaration.skill}")
        try:
            lines = skill_file.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError) as exc:
            raise ExtensionError(f"invalid skill: {skill_file}") from exc
        if not lines or lines[0].strip() != "---":
            raise ExtensionError(f"skill frontmatter missing: {skill_file}")
        frontmatter = []
        for line in lines[1:]:
            if line.strip() == "---":
                break
            frontmatter.append(line)
        name = next((line.split(":", 1)[1].strip() for line in frontmatter if line.startswith("name:")), None)
        if name != declaration.skill:
            raise ExtensionError(f"skill frontmatter name mismatch: {declaration.skill}")


def extension_digest(root: Path) -> str:
    if root.is_symlink() or not root.is_dir():
        raise ExtensionError(f"extension root must be regular directory: {root}")
    root = root.resolve()
    digest = hashlib.sha256()
    entries: list[tuple[str, Path]] = []
    for path in root.rglob("*"):
        if path.is_symlink():
            raise ExtensionError(f"symlink not allowed in extension: {path}")
        rel = path.relative_to(root)
        if "__pycache__" in rel.parts or path.name == ".DS_Store" or path.suffix == ".pyc":
            continue
        mode = path.stat().st_mode
        if stat.S_ISREG(mode):
            entries.append(("file", path))
        elif stat.S_ISDIR(mode):
            entries.append(("directory", path))
        else:
            raise ExtensionError(f"special file not allowed in extension: {path}")
    for kind, path in sorted(entries, key=lambda item: item[1].relative_to(root).as_posix()):
        rel = path.relative_to(root).as_posix().encode()
        digest.update(b"F" if kind == "file" else b"D")
        digest.update(len(rel).to_bytes(8, "big")); digest.update(rel)
        if kind == "file":
            data = path.read_bytes(); digest.update(len(data).to_bytes(8, "big")); digest.update(data)
    return f"sha256:{digest.hexdigest()}"


def lock_version_major(value: object) -> int:
    if (
        isinstance(value, dict)
        and type(value.get("major")) is int
        and type(value.get("minor")) is int
        and value["minor"] >= 0
    ):
        return value["major"]
    raise ExtensionError("extension lock version must be {major, minor}")


def normalize_extensions_lock(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or not {"lockVersion", "kitApi", "extensions", "providers"} <= set(value):
        raise ExtensionError("extension lock fields are invalid")
    lock_version = value["lockVersion"]
    kit_api = value["kitApi"]
    if (
        type(kit_api) is not int
        or lock_version_major(lock_version) != 1
        or kit_api != 1
    ):
        raise ExtensionError("extension lock requires major version 1 and kitApi 1")
    extensions = value["extensions"]
    if not isinstance(extensions, list):
        raise ExtensionError("extension lock extensions must be array")
    normalized_extensions = []
    seen: set[str] = set()
    provider_refs: dict[str, str] = {}
    for item in extensions:
        fields = {"id", "version", "path", "digest", "providers", "adapters"}
        fields = fields | {"actions"}
        if not isinstance(item, dict) or set(item) != fields:
            raise ExtensionError("locked extension fields are invalid")
        extension_id = item["id"]
        if not isinstance(extension_id, str) or not ID_RE.fullmatch(extension_id) or extension_id in seen:
            raise ExtensionError("locked extension id is invalid or duplicate")
        seen.add(extension_id)
        if not isinstance(item["version"], str) or not SEMVER_RE.fullmatch(item["version"]):
            raise ExtensionError("locked extension version is invalid")
        if item["path"] != f"extensions/{extension_id}" or not isinstance(item["digest"], str) or not DIGEST_RE.fullmatch(item["digest"]):
            raise ExtensionError("locked extension path or digest is invalid")
        if not isinstance(item["providers"], list) or not isinstance(item["adapters"], list):
            raise ExtensionError("locked providers and adapters must be arrays")
        if not isinstance(item["actions"], list):
            raise ExtensionError("locked actions must be array")
        names: set[str] = set()
        for provider in item["providers"]:
            if not isinstance(provider, dict) or set(provider) != {"capability", "provider", "apiVersion"}:
                raise ExtensionError("locked provider fields are invalid")
            if provider["capability"] not in CAPABILITIES or not isinstance(provider["provider"], str) or not ID_RE.fullmatch(provider["provider"]):
                raise ExtensionError("locked provider is invalid")
            if type(provider["apiVersion"]) is not int or provider["apiVersion"] != 1:
                raise ExtensionError("locked provider apiVersion must be 1")
            ref = f"{extension_id}/{provider['provider']}"
            if ref in provider_refs or provider["provider"] in names:
                raise ExtensionError(f"duplicate locked provider ref: {ref}")
            provider_refs[ref] = provider["capability"]
            names.add(provider["provider"])
        actions: list[dict[str, object]] = []
        for action in item["actions"]:
            if not isinstance(action, dict) or not {
                "id", "apiVersion", "skill", "confirmation",
            } <= set(action) or set(action) - {
                "id", "apiVersion", "skill", "confirmation",
            }:
                raise ExtensionError("locked action fields are invalid")
            if (
                not isinstance(action["id"], str)
                or not ID_RE.fullmatch(action["id"])
                or action["id"] in names
                or type(action["apiVersion"]) is not int
                or action["apiVersion"] != 1
                or not isinstance(action["skill"], str)
                or not ID_RE.fullmatch(action["skill"])
            ):
                raise ExtensionError("locked action is invalid")
            _confirmation(action["confirmation"])
            names.add(action["id"])
            actions.append(dict(action))
        adapters = []
        for raw_adapter in item["adapters"]:
            adapter = AdapterSpec.from_dict(raw_adapter)
            name = f"local-{extension_id}-{adapter.skill}"
            if adapter.agent_path != f".agents/skills/{name}" or adapter.claude_path != f".claude/skills/{name}":
                raise ExtensionError(f"adapter paths do not match locked extension: {extension_id}/{adapter.skill}")
            adapters.append(adapter.as_dict())
        normalized = {**item, "adapters": adapters}
        normalized["actions"] = actions
        normalized_extensions.append(normalized)
    from extension_registry import normalize_workspace_extensions

    providers = normalize_workspace_extensions(
        {"providers": value["providers"], "config": {}}
    )["providers"]
    for capability, binding in providers.items():
        assert isinstance(binding, dict)
        references = [binding["default"], *binding["repositories"].values()]
        for ref in references:
            if ref is None:
                continue
            if ref not in provider_refs:
                raise ExtensionError(f"locked provider ref is missing: {ref}")
            if provider_refs[ref] != capability:
                raise ExtensionError(f"locked provider capability mismatch: {ref}")
    return {**value, "extensions": normalized_extensions, "providers": providers}
