from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import workspace_registry  # noqa: E402


class WorkspaceRegistryTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.parent = Path(self.temp.name).resolve()
        self.root = self.parent / "kit"
        self.root.mkdir()
        state = self.root / ".workspace"
        state.mkdir()
        (state / "workspace.json").write_text(
            json.dumps(
                {
                    "version": {"major": 1, "minor": 0},
                    "workspace": {"name": "Demo Workspace"},
                    "context": {},
                    "branchPolicy": {"workBase": "trunk", "testTarget": "qa"},
                    "extensions": {"providers": {}, "config": {}},
                    "repositories": [
                        {
                            "path": "service",
                            "aliases": ["svc"],
                            "remote": "https://example.test/service.git",
                            "category": "backend",
                            "description": "Service",
                            "instruction": "docs/repositories/service.md",
                            "branchPolicy": {"hotfixBase": "stable"},
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        (state / "workspace.local.json").write_text(
            json.dumps({"branchOwner": "alice", "primaryRole": None, "extensions": {}}),
            encoding="utf-8",
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_resolve_alias_returns_absolute_sibling_and_effective_policy(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = workspace_registry.main(
                ["--root", str(self.root), "resolve", "svc", "--json"]
            )
        payload = json.loads(output.getvalue())
        self.assertEqual(0, code)
        self.assertEqual(str(self.parent / "service"), payload["path"])
        self.assertEqual("service", payload["name"])
        self.assertFalse(payload["present"])
        self.assertIn(str(self.parent / "service"), payload["cloneCommand"])
        self.assertEqual(
            {"workBase": "trunk", "testTarget": "qa", "hotfixBase": "stable", "namePattern": "{owner}/{type}/{slug}"},
            payload["effectiveBranchPolicy"],
        )

    def test_list_only_returns_registered_repositories(self):
        (self.parent / "unregistered").mkdir()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = workspace_registry.main(
                ["--root", str(self.root), "list", "--json"]
            )
        self.assertEqual(0, code)
        self.assertEqual(["service"], [item["name"] for item in json.loads(output.getvalue())])

    def test_unknown_name_has_distinct_exit_code(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(
                2,
                workspace_registry.main(
                    ["--root", str(self.root), "resolve", "missing", "--json"]
                ),
            )

    def test_branch_uses_canonical_repository_default_owner_and_effective_policy(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = workspace_registry.main(
                [
                    "--root",
                    str(self.root),
                    "branch",
                    "svc",
                    "--type",
                    "feature",
                    "--slug",
                    "payments",
                    "--json",
                ]
            )
        self.assertEqual(0, code)
        self.assertEqual(
            {
                "repository": "service",
                "type": "feature",
                "slug": "payments",
                "owner": "alice",
                "baseBranch": "trunk",
                "branch": "alice/feature/payments",
            },
            json.loads(output.getvalue()),
        )

    def test_branch_uses_owner_override_and_hotfix_base(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = workspace_registry.main(
                [
                    "--root",
                    str(self.root),
                    "branch",
                    "service",
                    "--type",
                    "hotfix",
                    "--slug",
                    "incident-42",
                    "--owner",
                    "bob",
                ]
            )
        self.assertEqual(0, code)
        self.assertEqual("bob/hotfix/incident-42\n", output.getvalue())

    def test_branch_provider_is_optional_and_provider_input_cannot_override_standard_fields(self):
        provider_input = self.root / "provider-input.json"
        provider_input.write_text(
            json.dumps(
                {
                    "repository": "attacker",
                    "type": "hotfix",
                    "slug": "attacker",
                    "owner": "attacker",
                    "baseBranch": "attacker",
                    "format": "team",
                }
            ),
            encoding="utf-8",
        )
        with mock.patch.object(
            workspace_registry,
            "bound_provider_ref",
            return_value="example-extension/team",
        ), mock.patch.object(
            workspace_registry,
            "run_bound_provider",
            return_value={
                "status": "ok",
                "result": {"branch": "alice/provider/payments"},
                "diagnostics": [],
            },
        ) as provider:
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(
                    0,
                    workspace_registry.main(
                        [
                            "--root",
                            str(self.root),
                            "branch",
                            "svc",
                            "--type",
                            "feature",
                            "--slug",
                            "payments",
                            "--provider-input",
                            str(provider_input),
                            "--json",
                        ]
                    ),
                )
        request = provider.call_args.kwargs["request"]
        self.assertEqual("feature", request["type"])
        self.assertEqual("payments", request["slug"])
        self.assertEqual("alice", request["owner"])
        self.assertEqual("trunk", request["baseBranch"])
        self.assertEqual("attacker", request["parameters"]["repository"])
        self.assertEqual("alice/provider/payments", json.loads(output.getvalue())["branch"])

    def test_branch_provider_selects_first_error_from_multiple_diagnostics(self):
        with mock.patch.object(
            workspace_registry,
            "bound_provider_ref",
            return_value="example-extension/team",
        ), mock.patch.object(
            workspace_registry,
            "run_bound_provider",
            return_value={
                "status": "blocked",
                "result": None,
                "diagnostics": [
                    {"level": "warning", "code": "NOTICE", "message": "notice"},
                    {"level": "error", "code": "BLOCKED", "message": "blocked"},
                ],
            },
        ):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(
                    1,
                    workspace_registry.main(
                        [
                            "--root", str(self.root), "branch", "service",
                            "--type", "feature", "--slug", "payments",
                        ]
                    ),
                )
            self.assertIn("BLOCKED", output.getvalue())

    def test_branch_provider_empty_diagnostics_fails_closed_without_crashing(self):
        with mock.patch.object(
            workspace_registry,
            "bound_provider_ref",
            return_value="example-extension/team",
        ), mock.patch.object(
            workspace_registry,
            "run_bound_provider",
            return_value={"status": "failed", "result": None, "diagnostics": []},
        ):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(
                    1,
                    workspace_registry.main(
                        [
                            "--root", str(self.root), "branch", "service",
                            "--type", "feature", "--slug", "payments",
                        ]
                    ),
                )
            self.assertIn("PROVIDER_PROTOCOL_ERROR", output.getvalue())

    def test_branch_rejects_invalid_type_ref_and_disabled_hotfix(self):
        cases = (
            ["branch", "service", "--type", "release", "--slug", "change"],
            ["branch", "service", "--type", "feature", "--slug", "bad ref"],
            [
                "branch",
                "service",
                "--type",
                "feature",
                "--slug",
                "change",
                "--owner",
                "",
            ],
        )
        for arguments in cases:
            with self.subTest(arguments=arguments), contextlib.redirect_stdout(
                io.StringIO()
            ):
                self.assertEqual(
                    1,
                    workspace_registry.main(["--root", str(self.root), *arguments]),
                )

        raw = json.loads((self.root / ".workspace/workspace.json").read_text(encoding="utf-8"))
        raw["repositories"][0]["branchPolicy"]["hotfixBase"] = None
        (self.root / ".workspace/workspace.json").write_text(json.dumps(raw), encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(
                1,
                workspace_registry.main(
                    [
                        "--root",
                        str(self.root),
                        "branch",
                        "service",
                        "--type",
                        "hotfix",
                        "--slug",
                        "incident",
                    ]
                ),
            )

    def test_help_exposes_root_and_commands(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            with self.assertRaises(SystemExit) as raised:
                workspace_registry.main(["--help"])
        self.assertEqual(0, raised.exception.code)
        self.assertIn("--root", output.getvalue())
        self.assertIn("resolve", output.getvalue())
        self.assertIn("list", output.getvalue())
        self.assertIn("branch", output.getvalue())

    def test_default_load_rejects_state_symlink_without_reading_outside_registry(self):
        outside = self.parent / "outside"
        self.root.joinpath(".workspace").rename(outside)
        self.root.joinpath(".workspace").symlink_to(outside, target_is_directory=True)
        registry = outside / "workspace.json"
        original = registry.read_bytes()
        for command in ("list", "resolve"):
            arguments = ["--root", str(self.root), command]
            if command == "resolve":
                arguments.append("svc")
            output = io.StringIO()
            with self.subTest(command=command), contextlib.redirect_stdout(output):
                self.assertEqual(1, workspace_registry.main(arguments))
            self.assertIn(".workspace", output.getvalue())
        self.assertEqual(original, registry.read_bytes())


if __name__ == "__main__":
    unittest.main()
