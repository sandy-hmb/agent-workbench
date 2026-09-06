from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from workspace_model import (  # noqa: E402
    JSON_MAX_NESTING,
    VERSION_VALUE,
    atomic_write_many,
    WorkspaceError,
    effective_branch_policy,
    parse_workspace,
    parse_json_bytes,
    render_branch_name,
    repository_path,
    valid_remote,
)


def repository(**overrides):
    path = overrides.get("path", "service")
    value = {
        "path": path,
        "aliases": ["svc"],
        "remote": "https://git.example.com/team/service.git",
        "category": "backend",
        "description": "Service",
        "instruction": f"docs/repositories/{path}.md",
    }
    value.update(overrides)
    return value


def workspace(root: Path, repositories=None, **overrides):
    value = {
        "version": dict(VERSION_VALUE),
        "workspace": {"name": "Demo Workspace"},
        "context": {},
        "branchPolicy": {
            "workBase": "develop",
            "testTarget": "test",
            "hotfixBase": "main",
            "namePattern": "{owner}/{type}/{slug}",
        },
        "extensions": {"providers": {}, "config": {}},
        "repositories": repositories if repositories is not None else [repository()],
    }
    value.update(overrides)
    return parse_workspace(value, root)


class WorkspaceModelTest(unittest.TestCase):
    def test_deep_json_is_wrapped_as_workspace_error(self):
        payload = (
            b'{"nested":' * (JSON_MAX_NESTING + 1)
            + b"null"
            + b"}" * (JSON_MAX_NESTING + 1)
        )
        with self.assertRaises(WorkspaceError):
            parse_json_bytes(payload, "deep.json")

    def test_json_braces_inside_strings_do_not_count_as_nesting(self):
        payload = json.dumps({"value": "{" * (JSON_MAX_NESTING + 1)}).encode()
        self.assertEqual(
            {"value": "{" * (JSON_MAX_NESTING + 1)},
            parse_json_bytes(payload, "string.json"),
        )

    def test_repository_instruction_is_the_generated_profile_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "kit"
            root.mkdir()
            for instruction in ("AGENTS.md", "docs/repositories/other.md"):
                with self.subTest(instruction=instruction), self.assertRaisesRegex(
                    WorkspaceError, "docs/repositories/service.md"
                ):
                    workspace(root, [repository(instruction=instruction)])

            model = workspace(root, [repository(sourceInstruction="AGENTS.md")])
            self.assertEqual("AGENTS.md", model.repositories[0].source_instruction)

    def test_workspace_identity_only_serializes_name(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "kit"
            root.mkdir()
            model = workspace(
                root,
                workspace={"name": "Payments"},
            )
            self.assertEqual(
                {"name": "Payments"},
                model.as_dict()["workspace"],
            )

            invalid = (
                None,
                {},
                {"name": ""},
                {"name": "Demo", "unknown": "field"},
            )
            for identity in invalid:
                payload = {
                    "version": {"major": 1, "minor": 0},
                    "workspace": identity,
                    "context": {},
                    "branchPolicy": {},
                    "extensions": {"providers": {}, "config": {}},
                    "repositories": [],
                }
                with self.subTest(identity=identity), self.assertRaises(WorkspaceError):
                    parse_workspace(payload, root)

    def test_term_router_runtime_validation_matches_schema_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "kit"
            root.mkdir()
            context = {
                "description": "still allowed",
                "termRouter": {
                    "products": [{"term": "payments", "repository": "service"}],
                    "capabilities": [{"term": "refund", "capability": "refund"}],
                    "actions": [{"term": "review", "action": "review"}],
                    "repositories": [{"term": "svc", "repository": "service"}],
                },
            }
            self.assertEqual(context, workspace(root, context=context).context)
            for invalid in (
                {**context, "termRouter": {"products": []}},
                {
                    **context,
                    "termRouter": {
                        **context["termRouter"],
                        "products": [{"term": "payments", "repository": "missing"}],
                    },
                },
            ):
                with self.subTest(invalid=invalid), self.assertRaises(WorkspaceError):
                    workspace(root, context=invalid)

    def test_repository_path_is_sibling_of_governance_root(self):
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            root = parent / "kit"
            root.mkdir()
            model = workspace(root)

            self.assertEqual(
                parent.resolve() / "service", repository_path(model, model.repositories[0])
            )

    def test_rejects_path_escape_and_symlink(self):
        invalid = (
            "/absolute",
            ".",
            "..",
            "nested/repo",
            "nested\\repo",
            "bad\0name",
            "with space",
            "colon:name",
            "_leading",
            "-leading",
            "é",
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "kit"
            root.mkdir()
            for path in invalid:
                with self.subTest(path=path), self.assertRaises(WorkspaceError):
                    workspace(root, [repository(path=path, aliases=[])])

            outside = Path(directory) / "outside"
            outside.mkdir()
            (Path(directory) / "escape").symlink_to(outside, target_is_directory=True)
            with self.assertRaisesRegex(WorkspaceError, "符号链接"):
                workspace(root, [repository(path="escape", aliases=[])])

    def test_remote_protocols_and_credentials(self):
        accepted = (
            "git@example.com:team/repo.git",
            "ssh://git@example.com/team/repo.git",
            "https://example.com/team/repo.git",
            None,
        )
        rejected = (
            "http://example.com/repo.git",
            "https://user@example.com/repo.git",
            "https://user:secret@example.com/repo.git",
            "ssh://git:secret@example.com/repo.git",
            "https://example.com:not-a-port/repo.git",
            "/tmp/repo.git",
        )
        self.assertTrue(all(valid_remote(value) for value in accepted))
        self.assertFalse(any(valid_remote(value) for value in rejected))

    def test_paths_and_aliases_have_one_global_namespace(self):
        cases = (
            [repository(), repository(path="other", aliases=["svc"])],
            [repository(), repository(path="svc", aliases=[])],
            [repository(aliases=["svc", "svc"])],
            [repository(), repository(path="SERVICE", aliases=[])],
            [repository(aliases=["svc", "SVC"])],
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "kit"
            root.mkdir()
            for repositories in cases:
                with self.subTest(repositories=repositories), self.assertRaisesRegex(
                    WorkspaceError, "重复"
                ):
                    workspace(root, repositories)

    def test_atomic_write_many_rolls_back_and_cleans_staging_on_replace_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "first.txt"
            second = root / "second.txt"
            first.write_text("old first\n", encoding="utf-8")
            second.write_text("old second\n", encoding="utf-8")
            original_replace = os.replace
            calls = 0

            def fail_second(source, target):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("injected replace failure")
                return original_replace(source, target)

            with mock.patch("workspace_model.os.replace", side_effect=fail_second):
                with self.assertRaises(OSError):
                    atomic_write_many(((first, "new first\n"), (second, "new second\n")))

            self.assertEqual("old first\n", first.read_text(encoding="utf-8"))
            self.assertEqual("old second\n", second.read_text(encoding="utf-8"))
            self.assertEqual({first, second}, set(root.iterdir()))

    def test_atomic_write_many_uses_readable_mode_for_new_files(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "new.txt"
            atomic_write_many(((path, "content\n"),))
            self.assertEqual(0o644, stat.S_IMODE(path.stat().st_mode))

    def test_atomic_write_many_copy_failure_preserves_original_and_cleans_staging(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "owned.txt"
            path.write_text("original\n", encoding="utf-8")
            path.chmod(0o640)

            with mock.patch(
                "workspace_model.shutil.copy2",
                side_effect=OSError("injected copy failure"),
            ):
                with self.assertRaises(OSError):
                    atomic_write_many(((path, "replacement\n"),))

            self.assertEqual("original\n", path.read_text(encoding="utf-8"))
            self.assertEqual(0o640, stat.S_IMODE(path.stat().st_mode))
            self.assertEqual({path}, set(root.iterdir()))

    def test_atomic_write_many_second_copy_failure_does_not_replace_destinations(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "first.txt"
            first_link = root / "first-link.txt"
            second = root / "second.txt"
            first.write_text("old first\n", encoding="utf-8")
            second.write_text("old second\n", encoding="utf-8")
            first.chmod(0o640)
            second.chmod(0o600)
            os.link(first, first_link)
            first_inode = first.stat().st_ino
            second_inode = second.stat().st_ino
            original_copy2 = shutil.copy2
            calls = 0

            def fail_second(source, target):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("injected second copy failure")
                return original_copy2(source, target)

            with mock.patch("workspace_model.shutil.copy2", side_effect=fail_second):
                with self.assertRaises(OSError):
                    atomic_write_many(((first, "new first\n"), (second, "new second\n")))

            self.assertEqual("old first\n", first.read_text(encoding="utf-8"))
            self.assertEqual("old second\n", second.read_text(encoding="utf-8"))
            self.assertEqual(0o640, stat.S_IMODE(first.stat().st_mode))
            self.assertEqual(0o600, stat.S_IMODE(second.stat().st_mode))
            self.assertEqual(first_inode, first.stat().st_ino)
            self.assertEqual(second_inode, second.stat().st_ino)
            self.assertEqual(first.stat().st_ino, first_link.stat().st_ino)
            self.assertEqual({first, first_link, second}, set(root.iterdir()))

    def test_origin_mismatch_error_does_not_expose_remote_secret(self):
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory).resolve()
            root = parent / "kit"
            root.mkdir()
            model = workspace(root, [repository(remote="https://safe.example/repo.git")])
            repo_path = parent / "service"
            subprocess.run(["git", "init", "-q", str(repo_path)], check=True)
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(repo_path),
                    "remote",
                    "add",
                    "origin",
                    "https://user:super-secret@example.test/repo.git",
                ],
                check=True,
            )
            with self.assertRaises(WorkspaceError) as raised:
                from workspace_model import validate_repository_state

                validate_repository_state(model, model.repositories[0])
            self.assertNotIn("super-secret", str(raised.exception))

    def test_global_and_repository_branch_policy_are_merged(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "kit"
            root.mkdir()
            model = workspace(
                root,
                [repository(branchPolicy={"testTarget": "qa", "hotfixBase": "stable"})],
                branchPolicy={"workBase": "trunk", "namePattern": "{owner}/{type}/{slug}"},
            )

            policy = effective_branch_policy(model, model.repositories[0])

            self.assertEqual("trunk", policy.work_base)
            self.assertEqual("qa", policy.test_target)
            self.assertEqual("stable", policy.hotfix_base)
            self.assertEqual(
                "alice/feature/payment",
                render_branch_name(
                    policy, owner="alice", branch_type="feature", slug="payment"
                ),
            )

    def test_extensions_are_required_objects_and_test_target_can_be_null(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "kit"
            root.mkdir()
            model = workspace(
                root,
                extensions={"providers": {}, "config": {}},
                branchPolicy={"testTarget": None},
            )
            self.assertIsNone(model.branch_policy.test_target)
            self.assertEqual(
                {"providers": {}, "config": {}},
                model.as_dict()["extensions"],
            )
            for extensions in ({}, {"providers": []}, {"providers": {}, "config": []}):
                with self.subTest(extensions=extensions), self.assertRaises(WorkspaceError):
                    workspace(root, extensions=extensions)

    def test_shared_extension_config_rejects_sensitive_fields_anywhere(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "kit"
            root.mkdir()
            for config in (
                {"apiToken": "hidden"},
                {"nested": [{"tokenEndpoint": "hidden"}]},
            ):
                with self.subTest(config=config):
                    with self.assertRaisesRegex(WorkspaceError, "敏感字段") as raised:
                        workspace(
                            root,
                            extensions={"providers": {}, "config": config},
                        )
                    self.assertNotIn("hidden", str(raised.exception))
            fixture = Path(__file__).resolve().parent / "fixtures/action-extension"
            (root / ".workspace/extensions").mkdir(parents=True)
            shutil.copytree(fixture, root / ".workspace/extensions/action-extension")
            model = workspace(
                root,
                extensions={"providers": {}, "config": {"action-extension": {}}},
            )
            self.assertEqual({}, model.extensions["config"]["action-extension"])

    def test_extensions_binding_and_config_are_strict(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "kit"
            root.mkdir()
            for extensions in (
                {"providers": {"unknown": {"default": None, "repositories": {}}}, "config": {}},
                {"providers": {}, "config": {"example-extension": True}},
                {"providers": {"branch.naming": {"default": "missing/team", "repositories": {}}}, "config": {}},
                {"providers": {}, "config": {"missing": {}}},
            ):
                with self.subTest(extensions=extensions), self.assertRaises(WorkspaceError):
                    workspace(root, extensions=extensions)

    def test_branch_pattern_only_accepts_declared_tokens_and_valid_git_result(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "kit"
            root.mkdir()
            with self.assertRaisesRegex(WorkspaceError, "仅允许"):
                workspace(root, branchPolicy={"namePattern": "{owner}/{year}/{slug}"})
            with self.assertRaisesRegex(WorkspaceError, "有效 Git"):
                workspace(root, branchPolicy={"namePattern": "{owner}//{type}/{slug}"})
            for field_name in ("workBase",):
                with self.subTest(field=field_name), self.assertRaisesRegex(
                    WorkspaceError, "有效分支名"
                ):
                    workspace(root, branchPolicy={field_name: None})
            self.assertIsNone(
                workspace(root, branchPolicy={"hotfixBase": None}).branch_policy.hotfix_base
            )
            model = workspace(root)
            with self.assertRaisesRegex(WorkspaceError, "有效 Git"):
                render_branch_name(
                    model.branch_policy,
                    owner="bad owner",
                    branch_type="feature",
                    slug="slug",
                )
            with self.assertRaisesRegex(WorkspaceError, "不支持"):
                render_branch_name(
                    model.branch_policy, owner="alice", branch_type="release", slug="slug"
                )


if __name__ == "__main__":
    unittest.main()
