from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import workspace_setup  # noqa: E402
import workspace_model  # noqa: E402


def repository(path="service", remote="https://example.test/service.git", **overrides):
    value = {
        "path": path,
        "aliases": [],
        "remote": remote,
        "category": "backend",
        "description": path,
        "instruction": f"docs/repositories/{path}.md",
    }
    value.update(overrides)
    return value


def config(path: Path, repositories, **overrides):
    value = {
        "version": {"major": 1, "minor": 0},
        "workspace": {"name": "Demo Workspace"},
        "local": {"branchOwner": "alice", "primaryRole": None, "extensions": {}},
        "context": {},
        "branchPolicy": {},
        "extensions": {"providers": {}, "config": {}},
        "repositories": repositories,
    }
    value.update(overrides)
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def snapshot(path: Path):
    return {
        item.relative_to(path).as_posix(): hashlib.sha256(item.read_bytes()).hexdigest()
        for item in path.rglob("*")
        if item.is_file() and ".git" not in item.parts
    }


def bare_remote(path: Path):
    subprocess.run(["git", "init", "-q", "--bare", str(path)], check=True)


class WorkspaceSetupTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.parent = Path(self.temp.name).resolve()
        self.root = self.parent / "kit"
        self.root.mkdir()
        (self.root / ".gitignore").write_text(".workspace/\ninput.json\n*.json\n", encoding="utf-8")
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        subprocess.run(["git", "-C", str(self.root), "add", ".gitignore"], check=True)
        subprocess.run(
            ["git", "-C", str(self.root), "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "fixture"],
            check=True,
        )

    def tearDown(self):
        self.temp.cleanup()

    def args(self, *values):
        return ["--root", str(self.root), *values]

    def preview(self, operation: str, source: Path, include_diff: bool = False):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            arguments = self.args(
                operation, "preview", "--config", str(source), "--json"
            )
            if include_diff:
                arguments.append("--diff")
            code = workspace_setup.main(arguments)
        self.assertEqual(0, code, stderr.getvalue())
        self.assertEqual("", stderr.getvalue())
        return json.loads(stdout.getvalue())

    def apply_config(self, operation: str, source: Path):
        preview = self.preview(operation, source)
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = workspace_setup.main(
                self.args(
                    operation,
                    "apply",
                    "--config",
                    str(source),
                    "--preview-hash",
                    preview["previewHash"],
                )
            )
        self.assertEqual(0, code, stderr.getvalue())
        self.assertEqual("", stderr.getvalue())
        return preview

    def git_url_environment(self, remotes: Path):
        return mock.patch.dict(
            os.environ,
            {
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "url.file://" + str(remotes) + "/.insteadOf",
                "GIT_CONFIG_VALUE_0": "https://example.test/",
            },
        )

    def test_explain_reports_meanings_defaults_and_choices(self):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = workspace_setup.main(self.args("init", "explain", "--json"))
        self.assertEqual(0, code)
        value = json.loads(stdout.getvalue())
        self.assertEqual(1, value["schemaVersion"])
        fields = {item["path"]: item for item in value["fields"]}
        self.assertIn("workspace.name", fields)
        self.assertTrue(fields["workspace.name"]["required"])
        self.assertIn("branchPolicy.testTarget", fields)
        self.assertIn(None, fields["branchPolicy.testTarget"]["choices"])
        self.assertEqual("{owner}/{type}/{slug}", fields["branchPolicy.namePattern"]["default"])
        self.assertIn("repositories[].instruction", value["derivedFields"])

    def draft(self, output: Path, *extra: str):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = workspace_setup.main(
                self.args("init", "draft", "--output", str(output), "--json", *extra)
            )
        return code, stdout.getvalue(), stderr.getvalue()

    def test_draft_fills_detected_values_and_marks_field_sources(self):
        sibling = self.parent / "service"
        subprocess.run(["git", "init", "-q", str(sibling)], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(sibling),
                "remote",
                "add",
                "origin",
                "https://example.test/service.git",
            ],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(self.root), "config", "user.name", "Detected Owner"],
            check=True,
        )
        output = self.root / "workspace-input.json"

        code, stdout, stderr = self.draft(output)

        self.assertEqual(0, code, stderr)
        self.assertTrue(output.is_file())
        written = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual("Detected Owner", written["local"]["branchOwner"])
        paths = [item["path"] for item in written["repositories"]]
        self.assertIn("service", paths)
        service = next(item for item in written["repositories"] if item["path"] == "service")
        self.assertEqual("https://example.test/service.git", service["remote"])

        report = json.loads(stdout)
        sources = {item["path"]: item["source"] for item in report["fields"]}
        self.assertEqual("detected", sources["local.branchOwner"])
        self.assertEqual("detected", sources["repositories[].remote"])
        self.assertEqual("required", sources["workspace.name"])

    def test_draft_refuses_to_overwrite_existing_output(self):
        output = self.root / "workspace-input.json"
        output.write_text('{"keep": true}\n', encoding="utf-8")

        code, _, stderr = self.draft(output)

        self.assertEqual(1, code)
        self.assertIn("已存在", stderr)
        self.assertEqual('{"keep": true}\n', output.read_text(encoding="utf-8"))

    def test_draft_refuses_output_path_not_ignored_by_git(self):
        output = self.root / "tracked-input.txt"

        code, _, stderr = self.draft(output)

        self.assertEqual(1, code)
        self.assertIn("忽略", stderr)
        self.assertFalse(output.exists())

    def test_draft_output_passes_existing_plan_validation_after_filling_required(self):
        sibling = self.parent / "service"
        subprocess.run(["git", "init", "-q", str(sibling)], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(sibling),
                "remote",
                "add",
                "origin",
                "https://example.test/service.git",
            ],
            check=True,
        )
        output = self.root / "workspace-input.json"
        code, _, stderr = self.draft(output)
        self.assertEqual(0, code, stderr)

        written = json.loads(output.read_text(encoding="utf-8"))
        written["workspace"]["name"] = "Filled Workspace"
        output.write_text(json.dumps(written), encoding="utf-8")

        stdout = io.StringIO()
        errors = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(errors):
            plan_code = workspace_setup.main(
                self.args("init", "plan", "--config", str(output), "--json")
            )
        self.assertEqual(0, plan_code, errors.getvalue())

    def test_draft_does_not_write_workspace_state_or_clone(self):
        output = self.root / "workspace-input.json"
        before = {
            item.relative_to(self.root).as_posix()
            for item in self.root.rglob("*")
            if ".git" not in item.parts
        }

        code, _, stderr = self.draft(output)

        self.assertEqual(0, code, stderr)
        after = {
            item.relative_to(self.root).as_posix()
            for item in self.root.rglob("*")
            if ".git" not in item.parts
        }
        self.assertEqual({"workspace-input.json"}, after - before)
        self.assertFalse((self.root / ".workspace").exists())

    def test_minimal_init_input_uses_safe_defaults_and_derives_instruction(self):
        source = self.root / "minimal.json"
        source.write_text(
            json.dumps(
                {
                    "workspace": {"name": "Minimal Workspace"},
                    "repositories": [{"path": "service", "remote": "https://example.test/service.git"}],
                }
            ),
            encoding="utf-8",
        )

        _, additions, combined, local = workspace_setup.operation_inputs(
            self.root, "init", source
        )

        self.assertEqual("Minimal Workspace", combined.identity.name)
        self.assertEqual("develop", combined.branch_policy.work_base)
        self.assertEqual("test", combined.branch_policy.test_target)
        self.assertIsNone(local.branch_owner)
        self.assertEqual("service", additions.repositories[0].path)
        self.assertEqual("docs/repositories/service.md", additions.repositories[0].instruction)
        self.assertEqual("repository", additions.repositories[0].category)
        self.assertEqual("", additions.repositories[0].description)

    def test_add_repo_accepts_minimal_repository_input_and_inherits_workspace(self):
        initial = config(self.root / "init.json", [repository()])
        self.apply_config("init", initial)
        source = self.root / "add.json"
        source.write_text(
            json.dumps(
                {
                    "repositories": [{"path": "web", "remote": "https://example.test/web.git"}]
                }
            ),
            encoding="utf-8",
        )

        existing, additions, combined = workspace_setup.operation_workspaces(
            self.root, "add-repo", source
        )

        self.assertEqual("Demo Workspace", existing.identity.name)
        self.assertEqual("alice", workspace_setup.load_local_settings(self.root).branch_owner)
        self.assertEqual(["web"], [item.path for item in additions.repositories])
        self.assertEqual(["service", "web"], [item.path for item in combined.repositories])

    def test_config_is_required_for_every_action_and_apply_hash_is_required(self):
        source = config(self.root / "input.json", [repository()])
        for operation in ("init", "add-repo"):
            for action in ("plan", "clone", "preview", "apply"):
                with self.subTest(operation=operation, action=action):
                    stderr = io.StringIO()
                    with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as raised:
                        workspace_setup.main(self.args(operation, action))
                    self.assertEqual(2, raised.exception.code)
                    self.assertIn("--config", stderr.getvalue())

        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as raised:
            workspace_setup.main(
                self.args("init", "apply", "--config", str(source))
            )
        self.assertEqual(2, raised.exception.code)
        self.assertIn("--preview-hash", stderr.getvalue())

    def test_plan_text_and_json_are_read_only_and_json_has_no_noise(self):
        sibling = self.parent / "existing"
        subprocess.run(["git", "init", "-q", str(sibling)], check=True)
        source = config(self.root / "input.json", [repository()])
        before_root = snapshot(self.root)
        before_parent = set(self.parent.iterdir())

        text_output = io.StringIO()
        with contextlib.redirect_stdout(text_output):
            self.assertEqual(
                0,
                workspace_setup.main(
                    self.args("init", "plan", "--config", str(source))
                ),
            )
        self.assertIn("工作区：Demo Workspace", text_output.getvalue())
        self.assertIn("分支 owner：alice", text_output.getvalue())
        self.assertIn(str(sibling), text_output.getvalue())
        self.assertIn("git clone", text_output.getvalue())

        json_output = io.StringIO()
        with contextlib.redirect_stdout(json_output):
            self.assertEqual(
                0,
                workspace_setup.main(
                    self.args("init", "plan", "--config", str(source), "--json")
                ),
            )
        value = json.loads(json_output.getvalue())
        self.assertEqual(
            {
                "schemaVersion",
                "operation",
                "workspace",
                "registeredRepositories",
                "candidateSiblingRepositories",
                "cloneCommands",
            },
            set(value),
        )
        self.assertEqual("init", value["operation"])
        self.assertEqual(
            {"name": "Demo Workspace", "branchOwner": "alice", "primaryRole": None},
            value["workspace"],
        )
        self.assertEqual(["service"], value["registeredRepositories"])
        self.assertEqual([str(sibling)], value["candidateSiblingRepositories"])
        self.assertEqual(
            [
                "git clone --quiet -- https://example.test/service.git "
                f"{self.parent / 'service'}"
            ],
            value["cloneCommands"],
        )
        self.assertEqual(before_root, snapshot(self.root))
        self.assertEqual(before_parent, set(self.parent.iterdir()))

    def test_preview_is_read_only_reports_summary_optional_diff_and_stable_hash(self):
        source = config(self.root / "input.json", [repository()])
        before = snapshot(self.root)

        first = self.preview("init", source)
        second = self.preview("init", source)

        self.assertEqual(before, snapshot(self.root))
        self.assertEqual(first, second)
        self.assertEqual("init", first["operation"])
        self.assertEqual(
            [".workspace/extensions/.state/cache/", ".workspace/docs/features/", ".workspace/extensions/"],
            first["directories"],
        )
        self.assertEqual([".workspace/docs/features/"], first["preservedPaths"])
        self.assertEqual(
            [
                ".workspace/AGENTS.md",
                ".workspace/CONTEXT.md",
                ".workspace/docs/repositories/service.md",
                ".workspace/extensions/.state/lock.json",
                ".workspace/workspace.json",
                ".workspace/workspace.local.json",
            ],
            sorted(change["path"] for change in first["changes"]),
        )
        self.assertEqual({"create"}, {change["action"] for change in first["changes"]})
        self.assertTrue(
            all(
                set(change) == {"path", "action", "additions", "deletions"}
                for change in first["changes"]
            )
        )
        self.assertTrue(all(change["additions"] > 0 for change in first["changes"]))
        self.assertTrue(all(change["deletions"] == 0 for change in first["changes"]))

        _, additions, combined, local = workspace_setup.operation_inputs(
            self.root, "init", source
        )
        outputs = workspace_setup._prepare_outputs(
            self.root, "init", additions, combined, local
        )
        payload = [
            {"path": path.relative_to(self.root).as_posix(), "content": content}
            for path, content in sorted(
                outputs,
                key=lambda item: item[0].relative_to(self.root).as_posix(),
            )
        ]
        expected_hash = hashlib.sha256(
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        self.assertEqual(expected_hash, first["previewHash"])
        expected_command = shlex.join(
            [
                "python3",
                "scripts/workspace_setup.py",
                "init",
                "apply",
                "--config",
                str(source),
                "--preview-hash",
                expected_hash,
            ]
        )
        self.assertEqual(expected_command, first["applyCommand"])

        full = self.preview("init", source, include_diff=True)
        self.assertEqual(expected_hash, full["previewHash"])
        self.assertEqual(expected_command, full["applyCommand"])
        self.assertTrue(
            all(change["diff"].startswith("--- /dev/null\n") for change in full["changes"])
        )

        text_output = io.StringIO()
        with contextlib.redirect_stdout(text_output):
            self.assertEqual(
                0,
                workspace_setup.main(
                    self.args("init", "preview", "--config", str(source))
                ),
            )
        self.assertIn("--- /dev/null", text_output.getvalue())
        self.assertIn(f"预览哈希：{expected_hash}", text_output.getvalue())
        self.assertIn(f"应用命令：{expected_command}", text_output.getvalue())

    def test_add_preview_reports_updates_and_skips_unchanged_output(self):
        initial = config(self.root / "init.json", [repository()])
        self.apply_config("init", initial)
        addition = config(self.root / "add.json", [repository("web")])
        _, additions, combined = workspace_setup.operation_workspaces(
            self.root, "add-repo", addition
        )
        profile, content = workspace_setup._prepare_outputs(
            self.root, "add-repo", additions, combined
        )[2]
        profile.parent.mkdir(parents=True, exist_ok=True)
        profile.write_text(content, encoding="utf-8")
        before = snapshot(self.root)

        value = self.preview("add-repo", addition, include_diff=True)

        self.assertEqual(before, snapshot(self.root))
        self.assertEqual(
            {".workspace/CONTEXT.md", ".workspace/workspace.json"},
            {change["path"] for change in value["changes"]},
        )
        self.assertEqual({"update"}, {change["action"] for change in value["changes"]})
        for change in value["changes"]:
            self.assertTrue(change["diff"].startswith(f"--- {change['path']}\n"))
            self.assertIn(f"+++ {change['path']}\n", change["diff"])

    def test_apply_accepts_matching_hash_and_rejects_stale_hash_without_writes(self):
        source = config(self.root / "input.json", [repository()])
        preview = self.preview("init", source)
        before = snapshot(self.root)
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            self.assertEqual(
                1,
                workspace_setup.main(
                    self.args(
                        "init",
                        "apply",
                        "--config",
                        str(source),
                        "--preview-hash",
                        "0" * 64,
                    )
                ),
            )
        self.assertEqual("", stdout.getvalue())
        self.assertIn("预览哈希", stderr.getvalue())
        self.assertEqual(before, snapshot(self.root))
        self.assertFalse((self.root / "docs").exists())

        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(
                0,
                workspace_setup.main(
                    self.args(
                        "init",
                        "apply",
                        "--config",
                        str(source),
                        "--preview-hash",
                        preview["previewHash"],
                    )
                ),
            )
        self.assertTrue((self.root / ".workspace/workspace.json").is_file())
        self.assertTrue((self.root / ".workspace/docs/features").is_dir())

    def test_json_error_uses_stderr_and_leaves_stdout_empty(self):
        source = self.root / "bad.json"
        source.write_text("{", encoding="utf-8")
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = workspace_setup.main(
                self.args("init", "preview", "--config", str(source), "--json")
            )
        self.assertEqual(1, code)
        self.assertEqual("", stdout.getvalue())
        error = json.loads(stderr.getvalue())
        self.assertEqual("WORKSPACE_SETUP_INVALID", error["error"]["code"])
        self.assertIn("JSON", error["error"]["message"])
        self.assertIn("hint", error["error"])

    def test_clone_success_conflict_and_partial_failure_remain_offline(self):
        remotes = self.parent / "remotes"
        remotes.mkdir()
        bare_remote(remotes / "one.git")
        source = config(
            self.root / "input.json",
            [
                repository("one", "https://example.test/one.git"),
                repository("missing", "https://example.test/missing.git"),
            ],
        )
        with self.git_url_environment(remotes), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(
                1,
                workspace_setup.main(
                    self.args("init", "clone", "--config", str(source))
                ),
            )
        self.assertTrue((self.parent / "one" / ".git").is_dir())
        self.assertFalse((self.parent / "missing").exists())
        self.assertFalse((self.root / ".workspace/workspace.json").exists())

        conflict = self.parent / "conflict"
        subprocess.run(["git", "init", "-q", str(conflict)], check=True)
        conflict_config = config(
            self.root / "conflict.json",
            [repository("conflict", "https://example.test/one.git")],
        )
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(
                1,
                workspace_setup.main(
                    self.args("init", "clone", "--config", str(conflict_config))
                ),
            )

    def test_init_and_add_apply_preserve_profiles_features_and_role_inheritance(self):
        source = config(
            self.root / "init.json",
            [repository()],
            workspace={
                "name": "Demo",
            },
            local={"branchOwner": "alice", "primaryRole": "backend", "extensions": {}},
            context={"description": "Context"},
            branchPolicy={"workBase": "trunk", "testTarget": "qa", "hotfixBase": "stable"},
        )
        self.apply_config("init", source)
        existing_profile = (self.root / ".workspace/docs/repositories/service.md").read_bytes()
        existing_agents = (self.root / ".workspace/AGENTS.md").read_bytes()
        existing_local = (self.root / ".workspace/workspace.local.json").read_bytes()
        feature = self.root / ".workspace/docs/features/demo/README.md"
        feature.parent.mkdir()
        feature.write_text("feature\n", encoding="utf-8")

        addition = config(
            self.root / "add.json",
            [repository("web", aliases=["frontend"])],
            workspace={"name": "Demo"},
            local={"branchOwner": "alice", "primaryRole": "backend", "extensions": {}},
        )
        addition_value = json.loads(addition.read_text(encoding="utf-8"))
        del addition_value["local"]
        addition.write_text(json.dumps(addition_value), encoding="utf-8")
        self.apply_config("add-repo", addition)

        registry = json.loads((self.root / ".workspace/workspace.json").read_text(encoding="utf-8"))
        self.assertEqual(["service", "web"], [item["path"] for item in registry["repositories"]])
        self.assertEqual(existing_profile, (self.root / ".workspace/docs/repositories/service.md").read_bytes())
        self.assertEqual(existing_agents, (self.root / ".workspace/AGENTS.md").read_bytes())
        self.assertEqual(existing_local, (self.root / ".workspace/workspace.local.json").read_bytes())
        self.assertEqual("feature\n", feature.read_text(encoding="utf-8"))
        self.assertIn("trunk", (self.root / ".workspace/docs/repositories/web.md").read_text(encoding="utf-8"))

    def test_add_repo_explicit_primary_role_values_must_match_without_writes(self):
        initial = config(
            self.root / "init.json",
            [repository()],
            workspace={
                "name": "Demo Workspace",
            },
            local={"branchOwner": "alice", "primaryRole": "backend", "extensions": {}},
        )
        self.apply_config("init", initial)
        explicit_null = config(
            self.root / "null.json",
            [repository("jobs")],
            workspace={
                "name": "Demo Workspace",
            },
            local={"branchOwner": "alice", "primaryRole": None, "extensions": {}},
        )
        explicit_other = config(
            self.root / "other.json",
            [repository("admin")],
            workspace={
                "name": "Demo Workspace",
            },
            local={"branchOwner": "alice", "primaryRole": "frontend", "extensions": {}},
        )

        for source in (explicit_null, explicit_other):
            for action, extra in (
                ("preview", []),
                ("apply", ["--preview-hash", "0" * 64]),
            ):
                before = snapshot(self.root)
                stdout = io.StringIO()
                stderr = io.StringIO()
                with self.subTest(source=source.name, action=action), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    self.assertEqual(
                        1,
                        workspace_setup.main(
                            self.args(
                                "add-repo",
                                action,
                                "--config",
                                str(source),
                                *extra,
                            )
                        ),
                    )
                self.assertEqual("", stdout.getvalue())
                self.assertIn("local 必须与现有本地配置一致", stderr.getvalue())
                self.assertEqual(before, snapshot(self.root))
        registry = json.loads((self.root / ".workspace/workspace.json").read_text(encoding="utf-8"))
        self.assertEqual(["service"], [item["path"] for item in registry["repositories"]])

    def test_add_preview_and_apply_refuse_noncanonical_registry_without_changes(self):
        initial = config(self.root / "init.json", [repository()])
        self.apply_config("init", initial)
        addition = config(self.root / "add.json", [repository("web")])
        registry = self.root / ".workspace/workspace.json"
        parsed_registry = json.loads(registry.read_text(encoding="utf-8"))
        registry.write_text(
            json.dumps(parsed_registry, ensure_ascii=False), encoding="utf-8"
        )
        drifted = snapshot(self.root)

        for action, extra in (
            ("preview", []),
            ("apply", ["--preview-hash", "0" * 64]),
        ):
            with self.subTest(action=action):
                stdout = io.StringIO()
                stderr = io.StringIO()
                with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    self.assertEqual(
                        1,
                        workspace_setup.main(
                            self.args(
                                "add-repo",
                                action,
                                "--config",
                                str(addition),
                                *extra,
                            )
                        ),
                    )
                self.assertEqual("", stdout.getvalue())
                self.assertIn("漂移", stderr.getvalue())
                self.assertIn("--- current/", stderr.getvalue())
                self.assertIn("+++ expected/", stderr.getvalue())
                self.assertEqual(drifted, snapshot(self.root))
        self.assertFalse((self.root / ".workspace/docs/repositories/web.md").exists())

    def test_add_preview_and_apply_refuse_drifted_context_and_profile(self):
        initial = config(self.root / "init.json", [repository()])
        self.apply_config("init", initial)
        addition = config(self.root / "add.json", [repository("web")])
        registry_before = (self.root / ".workspace/workspace.json").read_bytes()
        context = self.root / ".workspace/CONTEXT.md"
        expected_context = context.read_text(encoding="utf-8")
        profile = self.root / ".workspace/docs/repositories/service.md"
        expected_profile = profile.read_text(encoding="utf-8")

        for path, manual in (
            (context, "manual context\n"),
            (profile, "manual profile\n"),
        ):
            path.write_text(manual, encoding="utf-8")
            drifted = snapshot(self.root)
            for action, extra in (
                ("preview", []),
                ("apply", ["--preview-hash", "0" * 64]),
            ):
                with self.subTest(path=path.name, action=action):
                    stdout = io.StringIO()
                    stderr = io.StringIO()
                    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                        self.assertEqual(
                            1,
                            workspace_setup.main(
                                self.args(
                                    "add-repo",
                                    action,
                                    "--config",
                                    str(addition),
                                    *extra,
                                )
                            ),
                        )
                    self.assertEqual("", stdout.getvalue())
                    self.assertIn("漂移", stderr.getvalue())
                    self.assertIn("--- current/", stderr.getvalue())
                    self.assertIn("+++ expected/", stderr.getvalue())
                    self.assertEqual(drifted, snapshot(self.root))
            path.write_text(
                expected_context if path == context else expected_profile,
                encoding="utf-8",
            )

        self.assertEqual(registry_before, (self.root / ".workspace/workspace.json").read_bytes())
        self.assertFalse((self.root / ".workspace/docs/repositories/web.md").exists())

    def test_init_refuses_existing_outputs_and_add_conflicts_without_changes(self):
        source = config(self.root / "init.json", [repository()])
        preview_hash = self.apply_config("init", source)["previewHash"]
        before = snapshot(self.root)
        stderr = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(stderr):
            self.assertEqual(
                1,
                workspace_setup.main(
                    self.args(
                        "init",
                        "apply",
                        "--config",
                        str(source),
                        "--preview-hash",
                        preview_hash,
                    )
                ),
            )
        self.assertIn("拒绝覆盖", stderr.getvalue())
        self.assertEqual(before, snapshot(self.root))

        conflict = config(
            self.root / "add.json", [repository("other", aliases=["service"])]
        )
        conflict_before = snapshot(self.root)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(
                1,
                workspace_setup.main(
                    self.args(
                        "add-repo",
                        "apply",
                        "--config",
                        str(conflict),
                        "--preview-hash",
                        "0" * 64,
                    )
                ),
            )
        self.assertEqual(conflict_before, snapshot(self.root))

    def test_init_apply_requires_entire_state_tree_ignored_before_writing(self):
        source = config(self.root / "input.json", [repository()])
        preview_hash = self.preview("init", source)["previewHash"]
        (self.root / ".gitignore").write_text(
            ".workspace/workspace.json\ninput.json\n", encoding="utf-8"
        )
        subprocess.run(["git", "-C", str(self.root), "add", ".gitignore"], check=True)
        subprocess.run(
            ["git", "-C", str(self.root), "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "narrow ignore"],
            check=True,
        )
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            self.assertEqual(
                1,
                workspace_setup.main(
                    self.args(
                        "init",
                        "apply",
                        "--config",
                        str(source),
                        "--preview-hash",
                        preview_hash,
                    )
                ),
            )
        self.assertIn("完整", stderr.getvalue())
        self.assertFalse((self.root / ".workspace").exists())

    def test_init_apply_preserves_state_when_post_write_status_changes(self):
        source = config(self.root / "input.json", [repository()])
        preview_hash = self.preview("init", source)["previewHash"]
        stderr = io.StringIO()
        with mock.patch(
            "workspace_setup._git_status", side_effect=("", "?? unexpected\n")
        ), contextlib.redirect_stderr(stderr):
            self.assertEqual(
                1,
                workspace_setup.main(
                    self.args(
                        "init",
                        "apply",
                        "--config",
                        str(source),
                        "--preview-hash",
                        preview_hash,
                    )
                ),
            )
        self.assertIn("init apply 后治理仓 Git 状态发生变化", stderr.getvalue())
        for relative in (
            "AGENTS.md",
            "CONTEXT.md",
            "workspace.json",
            "workspace.local.json",
            "extensions/.state/lock.json",
            "docs/repositories/service.md",
            "docs/features",
            "extensions",
            "extensions/.state/cache",
        ):
            self.assertTrue((self.root / ".workspace" / relative).exists(), relative)

    def test_git_info_exclude_cannot_authorize_init_state_writes(self):
        source = config(self.root / "input.json", [repository()])
        preview_hash = self.preview("init", source)["previewHash"]
        (self.root / ".gitignore").write_text("input.json\n*.json\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.root), "add", ".gitignore"], check=True)
        subprocess.run(
            ["git", "-C", str(self.root), "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "remove state ignore"],
            check=True,
        )
        (self.root / ".git/info/exclude").write_text(".workspace/\n", encoding="utf-8")
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            self.assertEqual(
                1,
                workspace_setup.main(
                    self.args(
                        "init",
                        "apply",
                        "--config",
                        str(source),
                        "--preview-hash",
                        preview_hash,
                    )
                ),
            )
        self.assertIn("完整 Git ignore", stderr.getvalue())
        self.assertFalse((self.root / ".workspace").exists())

    def test_sensitive_shared_extension_config_rejects_before_state_write(self):
        source = config(
            self.root / "input.json",
            [repository()],
            extensions={
                "providers": {},
                "config": {"clientSecretValue": "must-not-appear"},
            },
        )
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            self.assertEqual(
                1,
                workspace_setup.main(
                    self.args("init", "preview", "--config", str(source))
                ),
            )
        self.assertIn("clientSecretValue", stderr.getvalue())
        self.assertNotIn("must-not-appear", stderr.getvalue())
        self.assertFalse((self.root / ".workspace").exists())

    def test_init_preview_and_apply_refuse_existing_context_or_profile(self):
        source = config(self.root / "init.json", [repository()])
        context = self.root / ".workspace/CONTEXT.md"
        context.parent.mkdir()
        context.write_text("owned\n", encoding="utf-8")

        for action, extra in (
            ("preview", []),
            ("apply", ["--preview-hash", "0" * 64]),
        ):
            before = snapshot(self.root)
            with self.subTest(path="CONTEXT.md", action=action), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(
                    1,
                    workspace_setup.main(
                        self.args("init", action, "--config", str(source), *extra)
                    ),
                )
            self.assertEqual(before, snapshot(self.root))
        self.assertEqual("owned\n", context.read_text(encoding="utf-8"))
        self.assertFalse((self.root / ".workspace/workspace.json").exists())
        self.assertFalse((self.root / ".workspace/docs").exists())

        context.unlink()
        profile = self.root / ".workspace/docs/repositories/service.md"
        profile.parent.mkdir(parents=True)
        profile.write_text("owned\n", encoding="utf-8")
        for action, extra in (
            ("preview", []),
            ("apply", ["--preview-hash", "0" * 64]),
        ):
            before = snapshot(self.root)
            with self.subTest(path="profile", action=action), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(
                    1,
                    workspace_setup.main(
                        self.args("init", action, "--config", str(source), *extra)
                    ),
                )
            self.assertEqual(before, snapshot(self.root))
        self.assertEqual("owned\n", profile.read_text(encoding="utf-8"))
        self.assertFalse((self.root / ".workspace/workspace.json").exists())
        self.assertFalse((self.root / ".workspace/CONTEXT.md").exists())
        self.assertFalse((self.root / ".workspace/docs/features").exists())

    def test_preview_and_apply_preflight_parent_types_before_any_write(self):
        source = config(self.root / "init.json", [repository()])
        docs = self.root / ".workspace/docs"
        docs.mkdir(parents=True)
        repositories = docs / "repositories"
        repositories.write_text("not a directory\n", encoding="utf-8")

        for action, extra in (
            ("preview", []),
            ("apply", ["--preview-hash", "0" * 64]),
        ):
            before = snapshot(self.root)
            with self.subTest(action=action), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(
                    1,
                    workspace_setup.main(
                        self.args("init", action, "--config", str(source), *extra)
                    ),
                )
            self.assertEqual(before, snapshot(self.root))
        self.assertFalse((self.root / ".workspace/workspace.json").exists())
        self.assertFalse((self.root / ".workspace/CONTEXT.md").exists())
        self.assertFalse((docs / "features").exists())

    def test_add_repo_plan_and_clone_are_offline_and_do_not_register(self):
        initial = config(self.root / "init.json", [repository()])
        self.apply_config("init", initial)
        remotes = self.parent / "remotes"
        remotes.mkdir()
        bare_remote(remotes / "web.git")
        addition = config(
            self.root / "add.json",
            [repository("web", "https://example.test/web.git", aliases=["frontend"])],
        )
        registry_before = (self.root / ".workspace/workspace.json").read_bytes()
        root_before = snapshot(self.root)
        plan_output = io.StringIO()
        with contextlib.redirect_stdout(plan_output):
            self.assertEqual(
                0,
                workspace_setup.main(
                    self.args("add-repo", "plan", "--config", str(addition))
                ),
            )
        self.assertIn("web.git", plan_output.getvalue())
        self.assertEqual(root_before, snapshot(self.root))

        with self.git_url_environment(remotes), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(
                0,
                workspace_setup.main(
                    self.args("add-repo", "clone", "--config", str(addition))
                ),
            )
        self.assertTrue((self.parent / "web" / ".git").is_dir())
        self.assertEqual(registry_before, (self.root / ".workspace/workspace.json").read_bytes())
        self.assertFalse((self.root / ".workspace/docs/repositories/web.md").exists())

    def test_atomic_failure_removes_only_directories_created_by_apply(self):
        source = config(self.root / "init.json", [repository()])
        preview_hash = self.preview("init", source)["previewHash"]
        with mock.patch(
            "workspace_setup.atomic_write_many",
            side_effect=OSError("injected atomic failure"),
        ), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(
                1,
                workspace_setup.main(
                    self.args(
                        "init",
                        "apply",
                        "--config",
                        str(source),
                        "--preview-hash",
                        preview_hash,
                    )
                ),
            )
        self.assertFalse((self.root / "docs").exists())
        self.assertFalse((self.root / ".workspace/workspace.json").exists())
        self.assertFalse((self.root / "CONTEXT.md").exists())

        docs = self.root / "docs"
        docs.mkdir()
        with mock.patch(
            "workspace_setup.atomic_write_many",
            side_effect=OSError("injected atomic failure"),
        ), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(
                1,
                workspace_setup.main(
                    self.args(
                        "init",
                        "apply",
                        "--config",
                        str(source),
                        "--preview-hash",
                        preview_hash,
                    )
                ),
            )
        self.assertTrue(docs.is_dir())
        self.assertFalse((docs / "repositories").exists())
        self.assertFalse((docs / "features").exists())

    def test_remote_null_stops_every_stateful_action_without_writes(self):
        source = config(self.root / "input.json", [repository(remote=None)])
        for action, extra in (
            ("plan", []),
            ("clone", []),
            ("preview", []),
            ("apply", ["--preview-hash", "0" * 64]),
        ):
            with self.subTest(action=action), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(
                    1,
                    workspace_setup.main(
                        self.args("init", action, "--config", str(source), *extra)
                    ),
                )
            self.assertFalse((self.root / ".workspace/workspace.json").exists())

    def test_help_and_example_expose_complete_interface(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            with self.assertRaises(SystemExit) as raised:
                workspace_setup.main(self.args("add-repo", "--help"))
        self.assertEqual(0, raised.exception.code)
        for action in ("plan", "clone", "preview", "apply"):
            self.assertIn(action, output.getvalue())
        self.assertIn("explain", output.getvalue())
        self.assertTrue((ROOT / "examples/new-repo.json").is_file())

        example = json.loads(
            (ROOT / "examples/workspace-input.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            {"workspace", "local", "repositories"},
            set(example),
        )
        self.assertEqual(
            {"name"}, set(example["workspace"])
        )
        self.assertEqual({"branchOwner"}, set(example["local"]))
        item = example["repositories"][0]
        self.assertEqual(
            {"path", "remote", "category"},
            set(item),
        )
        self.assertEqual("https://github.com/example/backend.git", item["remote"])
        parsed = workspace_setup.load_workspace_input(
            self.root, ROOT / "examples/workspace-input.json", require_local=True
        ).workspace
        self.assertEqual("backend", parsed.repositories[0].path)


if __name__ == "__main__":
    unittest.main()
