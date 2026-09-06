from __future__ import annotations

import contextlib
from dataclasses import replace
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import workspace_provider  # noqa: E402
import workspace_registry  # noqa: E402
from extension_model import load_manifest  # noqa: E402
from provider_workspace import make_provider_workspace  # noqa: E402
from schema_validation import validate  # noqa: E402
from workspace_model import Repository  # noqa: E402


class WorkspaceProviderTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = make_provider_workspace(
            Path(self.temp.name) / "kit",
            providers=[
                {
                    "capability": "branch.naming",
                    "provider": "team",
                    "apiVersion": 1,
                    "skill": "example-branching",
                    "command": ["python3", "provider/echo.py"],
                },
                {
                    "capability": "branch.naming",
                    "provider": "repository",
                    "apiVersion": 1,
                    "skill": "example-branching",
                    "command": ["python3", "provider/echo.py"],
                },
            ],
            bindings={
                "branch.naming": {
                    "default": "example-extension/team",
                    "repositories": {"service": "example-extension/repository"},
                }
            },
            shared_config={"shared": "value", "override": "shared"},
            local_config={"local": "value", "override": "local"},
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_uses_repository_override_and_merges_shared_local_config(self):
        result = workspace_provider.run_bound_provider(
            self.root,
            capability="branch.naming",
            operation="name",
            request={"value": "input"},
            repository="service",
        )
        self.assertEqual("ok", result["status"])
        request = result["result"]["request"]
        self.assertEqual("example-extension/repository", request["provider"])
        self.assertEqual({"shared": "value", "local": "value", "override": "local"}, request["config"])

    def test_cli_wraps_file_input_in_parameters_and_returns_structured_blocked(self):
        request_path = self.root / ".workspace/request.json"
        request_path.write_text(json.dumps({"hello": "world"}) + "\n", encoding="utf-8")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = workspace_provider.main(
                [
                    "run",
                    "branch.naming",
                    "--root",
                    str(self.root),
                    "--operation",
                    "name",
                    "--input",
                    str(request_path),
                    "--repo",
                    "svc",
                    "--json",
                ]
            )
        self.assertEqual(0, code)
        payload = json.loads(output.getvalue())
        self.assertEqual("world", payload["result"]["request"]["parameters"]["hello"])

        unbound = workspace_provider.run_bound_provider(
            self.root,
            capability="scm.review",
            operation="review",
            request={},
        )
        self.assertEqual("blocked", unbound["status"])
        self.assertEqual("PROVIDER_NOT_BOUND", unbound["diagnostics"][0]["code"])

    def test_extension_or_adapter_drift_blocks_before_command_execution(self):
        adapter = self.root / ".agents/skills/local-example-extension-example-branching/SKILL.md"
        adapter.write_text("drift\n", encoding="utf-8")
        result = workspace_provider.run_bound_provider(
            self.root,
            capability="branch.naming",
            operation="name",
            request={},
        )
        self.assertEqual("blocked", result["status"])
        self.assertEqual("ADAPTER_DRIFT", result["diagnostics"][0]["code"])

    def test_health_check_exception_fails_closed_before_provider_execution(self):
        with mock.patch.object(workspace_provider, "extension_findings", side_effect=OSError("unsafe state")):
            result = workspace_provider.run_bound_provider(
                self.root,
                capability="branch.naming",
                operation="name",
                request={},
            )
        self.assertEqual("blocked", result["status"])
        self.assertEqual("EXTENSION_DRIFT", result["diagnostics"][0]["code"])

    def test_rechecks_lock_safely_after_health_before_running_provider(self):
        lock = self.root / ".workspace/extensions/.state/lock.json"
        outside = Path(self.temp.name) / "outside-lock.json"
        outside.write_text('{"not":"a lock"}', encoding="utf-8")
        original_findings = workspace_provider.extension_findings
        replaced = False

        def replace_after_health(root):
            nonlocal replaced
            if not replaced:
                replaced = True
                lock.unlink()
                lock.symlink_to(outside)
            return original_findings(root) if not replaced else []

        with mock.patch.object(workspace_provider, "extension_findings", side_effect=replace_after_health):
            result = workspace_provider.run_bound_provider(
                self.root,
                capability="branch.naming",
                operation="name",
                request={},
            )
        self.assertEqual("blocked", result["status"])
        self.assertEqual("PROVIDER_INCOMPATIBLE", result["diagnostics"][0]["code"])
        self.assertEqual('{"not":"a lock"}', outside.read_text(encoding="utf-8"))

    def test_binding_change_after_health_blocks_before_old_provider_can_run(self):
        workspace_path = self.root / ".workspace/workspace.json"
        lock_path = self.root / ".workspace/extensions/.state/lock.json"
        original_health = workspace_provider._health_failure
        calls = 0

        def switch_binding(root, provider):
            nonlocal calls
            calls += 1
            if calls == 2:
                workspace = json.loads(workspace_path.read_text(encoding="utf-8"))
                lock = json.loads(lock_path.read_text(encoding="utf-8"))
                workspace["extensions"]["providers"]["branch.naming"]["default"] = (
                    "example-extension/repository"
                )
                lock["providers"]["branch.naming"]["default"] = (
                    "example-extension/repository"
                )
                workspace_path.write_text(json.dumps(workspace) + "\n", encoding="utf-8")
                lock_path.write_text(json.dumps(lock) + "\n", encoding="utf-8")
            return original_health(root, provider)

        with mock.patch.object(workspace_provider, "_health_failure", side_effect=switch_binding), mock.patch.object(
            workspace_provider, "run_provider"
        ) as runner:
            result = workspace_provider.run_bound_provider(
                self.root,
                capability="branch.naming",
                operation="name",
                request={},
            )
        self.assertEqual("blocked", result["status"])
        self.assertEqual("PROVIDER_BINDING_CHANGED", result["diagnostics"][0]["code"])
        runner.assert_not_called()

    def test_exclusive_extension_lock_blocks_provider_before_command_start(self):
        lock_path = self.root / ".workspace/extensions/.state/cache/extension.apply.lock"
        holder = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import fcntl, os, sys; "
                "fd=os.open(sys.argv[1], os.O_RDWR|os.O_NOFOLLOW); "
                "fcntl.flock(fd, fcntl.LOCK_EX); print('locked', flush=True); "
                "sys.stdin.buffer.read(1); fcntl.flock(fd, fcntl.LOCK_UN); os.close(fd)",
                str(lock_path),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            assert holder.stdout is not None
            self.assertEqual(b"locked\n", holder.stdout.readline())
            with mock.patch.object(workspace_provider, "run_provider") as runner:
                result = workspace_provider.run_bound_provider(
                    self.root,
                    capability="branch.naming",
                    operation="name",
                    request={},
                )
            self.assertEqual("blocked", result["status"])
            self.assertEqual("PROVIDER_BUSY", result["diagnostics"][0]["code"])
            runner.assert_not_called()
        finally:
            if holder.stdin is not None:
                holder.stdin.write(b"x")
                holder.stdin.flush()
            holder.wait(timeout=5)
            for stream in (holder.stdin, holder.stdout, holder.stderr):
                if stream is not None:
                    stream.close()

    def test_provider_recreates_deleted_cache_with_a_safe_lock(self):
        cache = self.root / ".workspace/extensions/.state/cache"
        lock = cache / "extension.apply.lock"
        lock.unlink()
        cache.rmdir()
        result = workspace_provider.run_bound_provider(
            self.root,
            capability="branch.naming",
            operation="name",
            request={},
        )
        self.assertEqual("ok", result["status"])
        self.assertEqual(0o700, cache.stat().st_mode & 0o777)
        self.assertTrue((cache / "extension.apply.lock").is_file())

    def test_final_local_config_is_revalidated_after_second_health(self):
        root = make_provider_workspace(
            Path(self.temp.name) / "config-race",
            providers=[
                {
                    "capability": "branch.naming",
                    "provider": "team",
                    "apiVersion": 1,
                    "skill": "example-branching",
                    "command": ["python3", "provider/echo.py"],
                }
            ],
            local_config={"team": "blue"},
        )
        extension = root / ".workspace/extensions/example-extension"
        manifest_path = extension / "workspace-extension.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["configSchema"] = "config-schema.json"
        manifest_path.write_text(json.dumps(manifest) + "\n", encoding="utf-8")
        (extension / "config-schema.json").write_text(
            json.dumps(
                {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["team"],
                    "properties": {"team": {"type": "string"}},
                }
            ),
            encoding="utf-8",
        )
        import workspace_extension

        config = root / ".workspace/extensions/.state/input.json"
        preview = workspace_extension.preview_result(root, config)
        workspace_extension.apply(root, config, str(preview["previewHash"]))
        local_path = root / ".workspace/workspace.local.json"
        original_health = workspace_provider._health_failure
        calls = 0

        def mutate_after_second_health(path, provider):
            nonlocal calls
            calls += 1
            result = original_health(path, provider)
            if calls == 2:
                local = json.loads(local_path.read_text(encoding="utf-8"))
                local["extensions"]["example-extension"]["team"] = 7
                local_path.write_text(json.dumps(local) + "\n", encoding="utf-8")
            return result

        with mock.patch.object(
            workspace_provider, "_health_failure", side_effect=mutate_after_second_health
        ), mock.patch.object(workspace_provider, "run_provider") as runner:
            result = workspace_provider.run_bound_provider(
                root,
                capability="branch.naming",
                operation="name",
                request={},
            )
        self.assertEqual("blocked", result["status"])
        self.assertEqual("PROVIDER_CONFIG_INVALID", result["diagnostics"][0]["code"])
        runner.assert_not_called()

    def test_final_manifest_snapshot_drift_blocks_before_command_start(self):
        manifest_path = self.root / ".workspace/extensions/example-extension/workspace-extension.json"
        original_health = workspace_provider._health_failure
        calls = 0

        def mutate_after_second_health(path, provider):
            nonlocal calls
            calls += 1
            result = original_health(path, provider)
            if calls == 2:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                manifest["provides"][0]["command"] = ["python3", "provider/branch.py"]
                manifest_path.write_text(json.dumps(manifest) + "\n", encoding="utf-8")
            return result

        with mock.patch.object(
            workspace_provider, "_health_failure", side_effect=mutate_after_second_health
        ), mock.patch.object(workspace_provider, "run_provider") as runner:
            result = workspace_provider.run_bound_provider(
                self.root,
                capability="branch.naming",
                operation="name",
                request={},
            )
        self.assertEqual("blocked", result["status"])
        self.assertEqual("PROVIDER_EXTENSION_DRIFT", result["diagnostics"][0]["code"])
        runner.assert_not_called()

    def test_deep_json_cli_input_returns_structured_error_without_traceback(self):
        request_path = self.root / ".workspace/deep-request.json"
        request_path.write_bytes(b'{"nested":' * 1_100 + b"null" + b"}" * 1_100)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = workspace_provider.main(
                [
                    "run", "branch.naming", "--root", str(self.root),
                    "--operation", "name", "--input", str(request_path), "--json",
                ]
            )
        self.assertEqual(1, code)
        self.assertEqual(
            "PROVIDER_INPUT_INVALID", json.loads(output.getvalue())["diagnostics"][0]["code"]
        )

    def test_rejects_repository_instance_outside_current_workspace(self):
        result = workspace_provider.run_bound_provider(
            self.root,
            capability="branch.naming",
            operation="name",
            request={},
            repository=Repository("outside", (), None, "backend", "Outside", "docs/repositories/outside.md"),
        )
        self.assertEqual("blocked", result["status"])
        self.assertEqual("PROVIDER_WORKSPACE_INVALID", result["diagnostics"][0]["code"])

    def test_bound_provider_without_command_is_blocked(self):
        root = make_provider_workspace(
            Path(self.temp.name) / "without-command",
            providers=[
                {
                    "capability": "branch.naming",
                    "provider": "team",
                    "apiVersion": 1,
                    "skill": "example-branching",
                }
            ],
        )
        result = workspace_provider.run_bound_provider(
            root,
            capability="branch.naming",
            operation="name",
            request={},
        )
        self.assertEqual("blocked", result["status"])
        self.assertEqual("PROVIDER_COMMAND_MISSING", result["diagnostics"][0]["code"])

    def test_bound_provider_with_missing_relative_command_is_blocked(self):
        root = make_provider_workspace(
            Path(self.temp.name) / "invalid-command",
            providers=[
                {
                    "capability": "branch.naming",
                    "provider": "team",
                    "apiVersion": 1,
                    "skill": "example-branching",
                    "command": ["python3", "provider/missing.py"],
                }
            ],
        )
        result = workspace_provider.run_bound_provider(
            root,
            capability="branch.naming",
            operation="name",
            request={},
        )
        self.assertEqual("blocked", result["status"])
        self.assertEqual("PROVIDER_COMMAND_INVALID", result["diagnostics"][0]["code"])

    def test_command_arguments_can_contain_url_slashes(self):
        extension_root = self.root / ".workspace/extensions/example-extension"
        manifest_path = extension_root / "workspace-extension.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["provides"][0]["command"] = [
            "python3",
            "-c",
            "import json,sys; print(json.dumps({'apiVersion':1,'provider':json.load(sys.stdin)['provider'],'status':'ok','result':{},'diagnostics':[],'effects':[]}))",
            "https://example.test/v1",
        ]
        manifest_path.write_text(json.dumps(manifest) + "\n", encoding="utf-8")
        config = self.root / ".workspace/extensions/.state/input.json"
        import workspace_extension

        preview = workspace_extension.preview_result(self.root, config)
        workspace_extension.apply(self.root, config, str(preview["previewHash"]))
        result = workspace_provider.run_bound_provider(
            self.root, capability="branch.naming", operation="name", request={}
        )
        self.assertEqual("ok", result["status"])

    def test_runtime_rejects_non_network_uri_command_value(self):
        extension_root = self.root / ".workspace/extensions/example-extension"
        declaration = load_manifest(extension_root / "workspace-extension.json").provides[0]
        self.assertIsNone(
            workspace_provider._command(
                load_manifest(extension_root / "workspace-extension.json"),
                replace(declaration, command=("curl", "--input=file:///etc/passwd")),
            )
        )

    def test_runtime_rejects_separated_short_option_path_escape(self):
        extension_root = self.root / ".workspace/extensions/example-extension"
        declaration = load_manifest(extension_root / "workspace-extension.json").provides[0]
        self.assertIsNone(
            workspace_provider._command(
                load_manifest(extension_root / "workspace-extension.json"),
                replace(declaration, command=("git", "-C", "/etc")),
            )
        )

    def test_runtime_strips_at_file_prefix_before_validating_paths(self):
        extension_root = self.root / ".workspace/extensions/example-extension"
        declaration = load_manifest(extension_root / "workspace-extension.json").provides[0]
        for command in (
            ("curl", "--data-binary=@/tmp/secret"),
            ("curl", "--data=@../outside"),
            ("curl", "@/tmp/secret"),
            ("curl", "C:\\tmp\\secret"),
            ("curl", "--data=@C:/tmp/secret"),
        ):
            with self.subTest(command=command):
                self.assertIsNone(
                    workspace_provider._command(
                        load_manifest(extension_root / "workspace-extension.json"),
                        replace(declaration, command=command),
                    )
                )
        shadow = extension_root / "@/tmp"
        shadow.mkdir(parents=True)
        (shadow / "secret").write_text("shadow", encoding="utf-8")
        self.assertIsNone(
            workspace_provider._command(
                load_manifest(extension_root / "workspace-extension.json"),
                replace(declaration, command=("curl", "@/tmp/secret")),
            )
        )
        data = extension_root / "provider/data.json"
        data.write_text("{}", encoding="utf-8")
        self.assertIsNotNone(
            workspace_provider._command(
                load_manifest(extension_root / "workspace-extension.json"),
                replace(declaration, command=("curl", "--data-binary=@provider/data.json")),
            )
        )

    def test_failure_envelope_sanitizes_newlines_and_matches_schema(self):
        schema = json.loads(
            (ROOT / "schemas/provider-result.schema.json").read_text(encoding="utf-8")
        )
        cases = (
            self.root,
            Path(str(self.root) + "\nroot"),
        )
        results = [
            workspace_provider.run_bound_provider(
                self.root, capability="missing\ncap", operation="name", request={}
            ),
            workspace_provider.run_bound_provider(
                self.root, capability="branch.naming", operation="name", request={},
                repository="missing\nrepo",
            ),
            workspace_provider.run_bound_provider(
                cases[1], capability="branch.naming", operation="name", request={}
            ),
            workspace_provider._result("bad\nprovider", "bad\ncode", "line1\r\nline2"),
        ]
        for result in results:
            with self.subTest(result=result):
                validate(result, schema)
                message = result["diagnostics"][0]["message"]
                self.assertNotRegex(message, r"[\r\n]")

    def test_command_relative_script_argument_must_be_regular_inside_extension(self):
        extension_root = self.root / ".workspace/extensions/example-extension"
        manifest_path = extension_root / "workspace-extension.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["provides"][0]["command"] = ["curl", "provider/missing.py"]
        manifest_path.write_text(json.dumps(manifest) + "\n", encoding="utf-8")
        config = self.root / ".workspace/extensions/.state/input.json"
        import workspace_extension

        preview = workspace_extension.preview_result(self.root, config)
        workspace_extension.apply(self.root, config, str(preview["previewHash"]))
        result = workspace_provider.run_bound_provider(
            self.root, capability="branch.naming", operation="name", request={}
        )
        self.assertEqual("blocked", result["status"])
        self.assertEqual("PROVIDER_COMMAND_INVALID", result["diagnostics"][0]["code"])

    def test_rejects_provider_command_path_that_escapes_or_uses_symlink(self):
        extension_root = self.root / ".workspace/extensions/example-extension"
        declaration = load_manifest(extension_root / "workspace-extension.json").provides[0]
        self.assertIsNone(
            workspace_provider._command(
                load_manifest(extension_root / "workspace-extension.json"),
                replace(declaration, command=("python3", "../outside.py")),
            )
        )
        manifest_path = self.root / ".workspace/extensions/example-extension/workspace-extension.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["provides"][0]["command"] = ["python3", "../outside.py"]
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        escaped = workspace_provider.run_bound_provider(
            self.root, capability="branch.naming", operation="name", request={}
        )
        self.assertEqual("blocked", escaped["status"])

        manifest["provides"][0]["command"] = ["./provider/linked.py"]
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        linked = self.root / ".workspace/extensions/example-extension/provider/linked.py"
        linked.symlink_to(self.root / ".workspace/extensions/example-extension/provider/echo.py")
        symlinked = workspace_provider.run_bound_provider(
            self.root, capability="branch.naming", operation="name", request={}
        )
        self.assertEqual("blocked", symlinked["status"])

    def test_registry_uses_bound_provider_but_preserves_standard_request_fields(self):
        manifest_path = self.root / ".workspace/extensions/example-extension/workspace-extension.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        for provider in manifest["provides"]:
            provider["command"] = ["python3", "provider/branch.py"]
        manifest_path.write_text(json.dumps(manifest) + "\n", encoding="utf-8")
        config = self.root / ".workspace/extensions/.state/input.json"
        desired = json.loads(config.read_text(encoding="utf-8"))
        desired["config"] = {"example-extension": {}}
        config.write_text(json.dumps(desired) + "\n", encoding="utf-8")
        # Re-apply records the changed manifest snapshot and keeps the test's bindings.
        import workspace_extension

        preview = workspace_extension.preview_result(self.root, config)
        workspace_extension.apply(self.root, config, str(preview["previewHash"]))
        provider_input = self.root / ".workspace/provider-input.json"
        provider_input.write_text(json.dumps({"owner": "attacker", "note": "ok"}), encoding="utf-8")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = workspace_registry.main(
                [
                    "--root", str(self.root), "branch", "svc", "--type", "feature",
                    "--slug", "payments", "--provider-input", str(provider_input), "--json",
                ]
            )
        self.assertEqual(0, code)
        payload = json.loads(output.getvalue())
        self.assertEqual("alice/provider/payments", payload["branch"])
        self.assertEqual("alice", payload["owner"])
        self.assertEqual("develop", payload["baseBranch"])


if __name__ == "__main__":
    unittest.main()
