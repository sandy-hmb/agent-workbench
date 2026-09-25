from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path


from tests.support import ROOT
sys.path.insert(0, str(ROOT))

import workbench.extensions.management as workspace_extension  # noqa: E402
import workbench.cli.status as workspace_status  # noqa: E402
import workbench.extensions.runner as workspace_workflow  # noqa: E402
import workbench.work_items.evidence as workspace_evidence  # noqa: E402
import workbench.inspection.api as workspace_inspect  # noqa: E402


ACTION_FIXTURE = ROOT / "tests" / "fixtures" / "action-extension"


class WorkflowFixture:
    def open(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "kit"
        self.root.mkdir()
        (self.root / ".gitignore").write_text("/.workspace/\n", encoding="utf-8")
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        state = self.root / ".workspace"
        (state / "extensions" / ".state").mkdir(parents=True)
        (state / "extensions" / ".state" / "cache").mkdir()
        (state / "items").mkdir(parents=True)
        (state / "docs/repositories").mkdir(parents=True)
        (state / "workspace.json").write_text(
            json.dumps(
                {
                    "version": {"major": 3, "minor": 0},
                    "workspace": {"name": "Demo"},
                    "context": {},
                    "branchPolicy": {},
                    "extensions": {"providers": {}, "config": {}},
                    "repositories": [],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        (state / "workspace.local.json").write_text(
            json.dumps({"branchOwner": "alice", "primaryRole": None, "extensions": {}})
            + "\n",
            encoding="utf-8",
        )
        (state / "extensions" / ".state" / "lock.json").write_text(
            json.dumps({"lockVersion": {"major": 1, "minor": 0}, "kitApi": 1, "extensions": [], "providers": {}})
            + "\n",
            encoding="utf-8",
        )
        (self.root / ".agents/skills").mkdir(parents=True)
        (self.root / ".claude/skills").mkdir(parents=True)
        shutil.copytree(ACTION_FIXTURE, state / "extensions/action-extension")
        self.extension_config = state / "extensions" / ".state" / "input.json"
        self.extension_config.write_text(
            json.dumps(
                {
                    "extensions": [{"id": "action-extension", "version": "1.0.0"}],
                    "providers": {},
                    "config": {"action-extension": {}},
                }
            )
            + "\n",
            encoding="utf-8",
        )
        preview = workspace_extension.preview_result(self.root, self.extension_config)
        workspace_extension.apply(self.root, self.extension_config, preview["previewHash"])
        self.overlay_config = state / "workflow-input.json"

    def close(self) -> None:
        self.temp.cleanup()

    def write_overlay(
        self,
        stages: list[dict[str, object]],
        skip_hints: list[dict[str, object]] | None = None,
    ) -> None:
        payload = {
            "schemaVersion": {"major": 1, "minor": 0},
            "workflow": "item-development",
            "stages": stages,
        }
        if skip_hints is not None:
            payload["skipHints"] = skip_hints
        self.overlay_config.write_text(json.dumps(payload) + "\n", encoding="utf-8")

    def activate_overlay(self) -> dict[str, object]:
        preview = workspace_workflow.preview_result(self.root, self.overlay_config)
        workspace_workflow.apply(self.root, self.overlay_config, preview["previewHash"])
        return preview
