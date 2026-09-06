from __future__ import annotations

import http.server
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import schema_validation  # noqa: E402
import workspace_extension  # noqa: E402
from provider_protocol import run_provider  # noqa: E402
from test_happy_path import create_public_clone, initialize_workspace  # noqa: E402


EXAMPLES_ROOT = ROOT / "examples/extensions"
BRANCH_NAMING = EXAMPLES_ROOT / "example-branch-naming"
WEBHOOK_NOTIFY = EXAMPLES_ROOT / "example-webhook-notify"
MANIFEST_SCHEMA = json.loads(
    (ROOT / "schemas/workspace-extension.schema.json").read_text(encoding="utf-8")
)
PROVIDER_RESULT_SCHEMA = json.loads(
    (ROOT / "schemas/provider-result.schema.json").read_text(encoding="utf-8")
)


class ExtensionExamplesStructureTest(unittest.TestCase):
    def test_both_examples_pass_manifest_schema_validation(self) -> None:
        for example in (BRANCH_NAMING, WEBHOOK_NOTIFY):
            with self.subTest(example=example.name):
                manifest = json.loads(
                    (example / "workspace-extension.json").read_text(encoding="utf-8")
                )
                schema_validation.validate(manifest, MANIFEST_SCHEMA)

    def test_both_examples_pass_validate_extension(self) -> None:
        result = workspace_extension.validate_result(BRANCH_NAMING)
        self.assertEqual(["branch.naming"], result["capabilities"])
        self.assertEqual([], result["actions"])

        result = workspace_extension.validate_result(WEBHOOK_NOTIFY)
        self.assertEqual([], result["capabilities"])
        self.assertEqual(["notify"], result["actions"])

    def test_both_examples_pass_install_preview_dry_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary).resolve()
            _, _, root = create_public_clone(parent)
            initialize_workspace(root, include_repository=False)

            for example in (BRANCH_NAMING, WEBHOOK_NOTIFY):
                with self.subTest(example=example.name):
                    preview = workspace_extension.install_preview_result(root, example)
                    self.assertIn("previewHash", preview)
                    self.assertFalse(
                        (root / ".workspace/extensions" / example.name).exists()
                    )


class BranchNamingExampleTest(unittest.TestCase):
    def test_naming_script_returns_a_valid_result_that_differs_from_core_default(self) -> None:
        result = run_provider(
            [sys.executable, "commands/naming.py"],
            cwd=BRANCH_NAMING,
            provider="example-branch-naming/naming",
            request={
                "type": "feature",
                "slug": "demo",
                "owner": "alice",
                "baseBranch": "main",
                "parameters": {},
            },
        )
        self.assertEqual("ok", result["status"])
        schema_validation.validate(result, PROVIDER_RESULT_SCHEMA)
        branch = result["result"]["branch"]
        core_default = "alice/feature/demo"
        self.assertNotEqual(core_default, branch)
        self.assertIn("alice", branch)
        self.assertIn("feature", branch)
        self.assertIn("demo", branch)


class WebhookNotifyExampleTest(unittest.TestCase):
    REQUEST = {
        "workflow": "feature-development",
        "run": "demo-feature",
        "stage": "feature.implement",
        "featureSlug": "demo-feature",
        "repository": "service",
        "branch": "owner/feature/demo",
        "with": {},
    }

    def test_notify_script_posts_expected_payload_to_local_loopback_server(self) -> None:
        received: dict[str, object] = {}

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length)
                received["body"] = json.loads(body)
                received["contentType"] = self.headers.get("Content-Type")
                self.send_response(200)
                self.end_headers()

            def log_message(self, *args: object) -> None:
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{server.server_port}/"
            os.environ["WEBHOOK_URL"] = url
            try:
                result = run_provider(
                    [sys.executable, "commands/notify.py"],
                    cwd=WEBHOOK_NOTIFY,
                    provider="example-webhook-notify/notify",
                    request=self.REQUEST,
                    environment=("WEBHOOK_URL",),
                )
            finally:
                os.environ.pop("WEBHOOK_URL", None)
        finally:
            server.shutdown()
            thread.join(timeout=5)

        self.assertEqual("ok", result["status"], result)
        self.assertEqual("application/json", received["contentType"])
        self.assertEqual(
            {
                "workflow": "feature-development",
                "run": "demo-feature",
                "stage": "feature.implement",
                "featureSlug": "demo-feature",
                "repository": "service",
                "branch": "owner/feature/demo",
            },
            received["body"],
        )

    def test_missing_webhook_url_blocks_without_running_the_script(self) -> None:
        os.environ.pop("WEBHOOK_URL", None)
        result = run_provider(
            [sys.executable, "commands/notify.py"],
            cwd=WEBHOOK_NOTIFY,
            provider="example-webhook-notify/notify",
            request=self.REQUEST,
            environment=("WEBHOOK_URL",),
        )
        self.assertEqual("blocked", result["status"])
        self.assertEqual("CREDENTIAL_MISSING", result["diagnostics"][0]["code"])


if __name__ == "__main__":
    unittest.main()
