from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path


from tests.support import ROOT
sys.path.insert(0, str(ROOT / "tests"))

from tests.support.public_clone import (  # noqa: E402
    create_public_clone,
    git_status,
    initialize_workspace,
    run_script,
)


EXTENSION_FIXTURE = ROOT / "tests" / "fixtures" / "example-extension"
ACTION_FIXTURE = ROOT / "tests" / "fixtures" / "action-extension"
BRANCH_PROVIDER = ROOT / "tests" / "fixtures" / "provider" / "branch.py"


def extension_input(path: Path, *, active: bool) -> None:
    path.write_text(
        json.dumps(
            {
                "extensions": (
                    [{"id": "example-extension", "version": "1.0.0"}]
                    if active
                    else []
                ),
                "providers": (
                    {
                        "branch.naming": {
                            "default": "example-extension/team",
                            "repositories": {},
                        }
                    }
                    if active
                    else {}
                ),
                "config": {"example-extension": {}} if active else {},
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def preview(root: Path, config: Path) -> dict[str, object]:
    return json.loads(
        run_script(
            root,
            "workspace_extension.py",
            "preview",
            "--root",
            str(root),
            "--config",
            str(config),
            "--json",
        ).stdout
    )


def apply(root: Path, config: Path, preview_result: dict[str, object]) -> None:
    run_script(
        root,
        "workspace_extension.py",
        "apply",
        "--root",
        str(root),
        "--config",
        str(config),
        "--preview-hash",
        str(preview_result["previewHash"]),
    )
