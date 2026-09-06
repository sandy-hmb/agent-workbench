from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import workspace_context  # noqa: E402
from provider_workspace import make_provider_workspace  # noqa: E402


TERM_ROUTER = {
    "products": [{"term": "payments", "repository": "service"}],
    "capabilities": [
        {"term": "refund", "capability": "refund"},
        {"term": "pay", "capability": "pay"},
    ],
    "actions": [{"term": "review", "action": "review"}],
    "repositories": [{"term": "svc", "repository": "service"}],
}


class WorkspaceContextTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = make_provider_workspace(
            Path(self.temp.name) / "kit",
            context={"termRouter": TERM_ROUTER},
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_routes_exact_declared_term_and_blocks_zero_or_multiple_matches(self):
        exact = workspace_context.route(self.root, "payments")
        self.assertEqual("ok", exact["status"])
        self.assertEqual("products", exact["route"]["kind"])
        self.assertEqual("service", exact["route"]["repository"])
        for text, code in (
            ("unknown", "CONTEXT_NO_MATCH"),
            ("repayment", "CONTEXT_NO_MATCH"),
            ("payments refund", "CONTEXT_AMBIGUOUS"),
        ):
            with self.subTest(text=text):
                result = workspace_context.route(self.root, text)
                self.assertEqual("blocked", result["status"])
                self.assertEqual(code, result["diagnostics"][0]["code"])

    def test_bound_context_provider_uses_the_restricted_provider_path(self):
        root = make_provider_workspace(
            Path(self.temp.name) / "provider-kit",
            providers=[
                {
                    "capability": "context.term-router",
                    "provider": "team",
                    "apiVersion": 1,
                    "skill": "example-branching",
                    "command": ["python3", "provider/route.py"],
                }
            ],
            bindings={
                "context.term-router": {
                    "default": "example-extension/team",
                    "repositories": {},
                }
            },
        )
        result = workspace_context.route(root, "anything")
        self.assertEqual("ok", result["status"])
        self.assertEqual("actions", result["route"]["kind"])
        self.assertEqual("review", result["route"]["action"])
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(
                0,
                workspace_context.main(["route", "--root", str(root), "--text", "anything"]),
            )
        self.assertIn('"kind": "actions"', output.getvalue())

    def test_context_provider_rejects_invalid_route_and_converts_blocked_status(self):
        for script, code in (
            ("invalid-route.py", "CONTEXT_PROVIDER_RESULT_INVALID"),
            ("blocked-route.py", "ROUTE_BLOCKED"),
            ("failed-route.py", "ROUTE_FAILED"),
        ):
            with self.subTest(script=script):
                root = make_provider_workspace(
                    Path(self.temp.name) / script,
                    providers=[
                        {
                            "capability": "context.term-router",
                            "provider": "team",
                            "apiVersion": 1,
                            "skill": "example-branching",
                            "command": ["python3", f"provider/{script}"],
                        }
                    ],
                    bindings={
                        "context.term-router": {
                            "default": "example-extension/team",
                            "repositories": {},
                        }
                    },
                )
                result = workspace_context.route(root, "anything")
                self.assertEqual("blocked", result["status"])
                self.assertIsNone(result["route"])
                self.assertEqual(code, result["diagnostics"][0]["code"])
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    self.assertEqual(
                        1,
                        workspace_context.main(
                            ["route", "--root", str(root), "--text", "anything"]
                        ),
                    )
                self.assertIn(code, output.getvalue())

    def test_context_provider_accepts_null_route(self):
        root = make_provider_workspace(
            Path(self.temp.name) / "null-route",
            providers=[
                {
                    "capability": "context.term-router",
                    "provider": "team",
                    "apiVersion": 1,
                    "skill": "example-branching",
                    "command": ["python3", "provider/null-route.py"],
                }
            ],
            bindings={
                "context.term-router": {
                    "default": "example-extension/team",
                    "repositories": {},
                }
            },
        )
        result = workspace_context.route(root, "anything")
        self.assertEqual("ok", result["status"])
        self.assertIsNone(result["route"])

    def test_cli_returns_json_route(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = workspace_context.main(
                ["route", "--root", str(self.root), "--text", "payments", "--json"]
            )
        self.assertEqual(0, code)
        self.assertEqual("payments", json.loads(output.getvalue())["route"]["term"])


if __name__ == "__main__":
    unittest.main()
