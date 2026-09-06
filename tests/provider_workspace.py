"""Small temporary workspace factory for local provider tests."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import workspace_extension  # noqa: E402


EXTENSION_FIXTURE = ROOT / "tests" / "fixtures" / "example-extension"
PROVIDER_FIXTURE = ROOT / "tests" / "fixtures" / "provider"


def make_provider_workspace(
    root: Path,
    *,
    providers: list[dict[str, Any]] | None = None,
    bindings: dict[str, object] | None = None,
    shared_config: dict[str, object] | None = None,
    local_config: dict[str, object] | None = None,
    context: dict[str, object] | None = None,
    repository: bool = True,
) -> Path:
    root.mkdir()
    (root / ".gitignore").write_text(
        "/.workspace/\n/.agents/skills/local-*\n/.claude/skills/local-*\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    state = root / ".workspace"
    (state / "docs/features").mkdir(parents=True)
    (state / "docs/repositories").mkdir()
    (state / "extensions" / ".state").mkdir(parents=True)
    (state / "extensions" / ".state" / "cache").mkdir()
    extension = state / "extensions/example-extension"
    shutil.copytree(EXTENSION_FIXTURE, extension)
    shutil.copytree(PROVIDER_FIXTURE, extension / "provider")
    manifest_path = extension / "workspace-extension.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    default_providers = [
        {
            "capability": "branch.naming",
            "provider": "team",
            "apiVersion": 1,
            "skill": "example-branching",
            "command": ["python3", "provider/echo.py"],
        }
    ]
    manifest["provides"] = providers if providers is not None else default_providers
    if shared_config or local_config:
        manifest["configSchema"] = "config-schema.json"
        (extension / "config-schema.json").write_text(
            json.dumps({"type": "object", "additionalProperties": True}),
            encoding="utf-8",
        )
    manifest_path.write_text(json.dumps(manifest) + "\n", encoding="utf-8")
    repositories: list[dict[str, object]] = []
    if repository:
        repositories.append(
            {
                "path": "service",
                "aliases": ["svc"],
                "remote": None,
                "category": "backend",
                "description": "Service",
                "instruction": "docs/repositories/service.md",
            }
        )
        (state / "docs/repositories/service.md").write_text("# Service\n", encoding="utf-8")
    workspace = {
        "version": {"major": 1, "minor": 0},
        "workspace": {"name": "Provider Test"},
        "context": context or {},
        "branchPolicy": {
            "workBase": "develop",
            "testTarget": "test",
            "hotfixBase": "main",
            "namePattern": "{owner}/{type}/{slug}",
        },
        "extensions": {"providers": {}, "config": {}},
        "repositories": repositories,
    }
    (state / "workspace.json").write_text(json.dumps(workspace) + "\n", encoding="utf-8")
    (state / "workspace.local.json").write_text(
        json.dumps(
            {
                "branchOwner": "alice",
                "primaryRole": None,
                "extensions": {"example-extension": local_config or {}},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (state / "extensions" / ".state" / "lock.json").write_text(
        json.dumps({"lockVersion": {"major": 1, "minor": 0}, "kitApi": 1, "extensions": [], "providers": {}})
        + "\n",
        encoding="utf-8",
    )
    (root / ".agents/skills").mkdir(parents=True)
    (root / ".claude/skills").mkdir(parents=True)
    desired = {
        "extensions": [{"id": "example-extension", "version": "1.0.0"}],
        "providers": bindings
        if bindings is not None
        else {
            "branch.naming": {
                "default": "example-extension/team",
                "repositories": {},
            }
        },
        "config": {"example-extension": shared_config or {}},
    }
    config = state / "extensions" / ".state" / "input.json"
    config.write_text(json.dumps(desired) + "\n", encoding="utf-8")
    preview = workspace_extension.preview_result(root, config)
    workspace_extension.apply(root, config, str(preview["previewHash"]))
    return root
