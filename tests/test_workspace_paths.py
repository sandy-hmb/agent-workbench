from __future__ import annotations

import sys
import unittest
import tempfile
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from workspace_paths import (  # noqa: E402
    adapters_root,
    cache_root,
    context_file,
    extension_input_file,
    extension_state_root,
    extensions_root,
    features_root,
    kit_root,
    local_file,
    lock_file,
    migration_marker_file,
    profiles_root,
    state_root,
    workflow_file,
    workflow_run_file,
    workflow_runs_root,
    workspace_file,
)


class WorkspacePathsTest(unittest.TestCase):
    def test_document_roles_preserve_existing_layout_and_detect_conflicts(self):
        import workspace_paths as paths

        with tempfile.TemporaryDirectory() as temp:
            feature = Path(temp)
            (feature / '.work-item.json').write_text(json.dumps({'documentLayout': 'flat-v1'}))
            for role in ('requirements', 'design', 'plan', 'verification'):
                self.assertEqual(feature / (role + '.md'), paths.feature_document_file(feature, role))
            (feature / '.work-item.json').unlink()
            (feature / 'design').mkdir()
            old = feature / 'design/design.md'
            old.write_text('# existing')
            self.assertEqual(old, paths.feature_document_file(feature, 'design'))
            (feature / 'design.md').write_text('# competing')
            with self.assertRaisesRegex(ValueError, 'DOCUMENT_ROLE_CONFLICT'):
                paths.feature_document_file(feature, 'design')

    def test_state_and_governance_paths(self):
        root = Path("relative-root")
        resolved = root.resolve()
        self.assertEqual(resolved, kit_root(root))
        self.assertEqual(resolved / ".workspace", state_root(root))
        self.assertEqual(resolved / ".workspace/workspace.json", workspace_file(root))
        self.assertEqual(
            resolved / ".workspace/workspace.local.json", local_file(root)
        )
        self.assertEqual(resolved / ".workspace/CONTEXT.md", context_file(root))
        self.assertEqual(resolved / ".workspace/docs/features", features_root(root))
        self.assertEqual(resolved / ".workspace/docs/repositories", profiles_root(root))
        self.assertEqual(resolved / ".workspace/extensions", extensions_root(root))
        self.assertEqual(resolved / ".workspace/extensions/.state", extension_state_root(root))
        self.assertEqual(resolved / ".workspace/extensions/.state/input.json", extension_input_file(root))
        self.assertEqual(
            resolved / ".workspace/extensions/.state/lock.json", lock_file(root)
        )
        self.assertEqual(resolved / ".workspace/workflow.json", workflow_file(root))
        self.assertEqual(resolved / ".workspace/runs", workflow_runs_root(root))
        self.assertEqual(
            resolved / ".workspace/runs/demo.json", workflow_run_file(root, "demo")
        )
        self.assertEqual(
            resolved / ".workspace/.migration-marker.json",
            migration_marker_file(root),
        )
        self.assertEqual(resolved / ".workspace/extensions/.state/cache", cache_root(root))

    def test_client_adapter_paths(self):
        self.assertEqual(ROOT / ".agents/skills", adapters_root(ROOT, "agents"))
        self.assertEqual(ROOT / ".claude/skills", adapters_root(ROOT, "claude"))

    def test_unknown_client_is_rejected(self):
        with self.assertRaises(ValueError):
            adapters_root(ROOT, "unknown")
