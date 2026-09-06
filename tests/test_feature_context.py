from __future__ import annotations

import contextlib
import io
import json
import stat
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import feature_context  # noqa: E402


def write_workspace(root: Path):
    state = root / ".workspace"
    state.mkdir(exist_ok=True)
    (state / "workspace.json").write_text(
        json.dumps(
            {
                "version": {"major": 1, "minor": 0},
                "workspace": {"name": "Demo"},
                "context": {},
                "branchPolicy": {"workBase": "develop", "testTarget": "test", "hotfixBase": "main", "namePattern": "{owner}/{type}/{slug}"},
                "extensions": {"providers": {}, "config": {}},
                "repositories": [
                    {
                        "path": "service",
                        "aliases": ["svc"],
                        "remote": None,
                        "category": "backend",
                        "description": "Service",
                        "instruction": "docs/repositories/service.md",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )


def write_feature(
    root: Path,
    slug: str,
    branch="owner/feature/demo",
    base="trunk",
    repository="service",
):
    if not (root / ".workspace/workspace.json").exists():
        write_workspace(root)
    readme = root / ".workspace" / "docs" / "features" / slug / "README.md"
    readme.parent.mkdir(parents=True, exist_ok=True)
    readme.write_text(
        f"# Demo\n\n- 状态：development\n- 需求短名：`{slug}`\n"
        f"- 涉及仓库：`{repository}`\n"
        f"- 工作分支：`{repository}` -> `{branch}`\n"
        f"- 基线分支：`{repository}` -> `{base}`\n"
        "- 最后更新：2026-09-01\n",
        encoding="utf-8",
    )
    return readme


class FeatureContextTest(unittest.TestCase):
    def test_feature_template_renders_parseable_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_workspace(root)
            feature = root / ".workspace/docs/features/demo-feature"
            feature.mkdir(parents=True)
            template = (ROOT / "templates/feature/README.md").read_text(
                encoding="utf-8"
            )
            for line in (
                "# {title}",
                "- 状态：planning",
                "- 涉及仓库：{repositories}",
                "- 工作分支：{branches}",
                "- 基线分支：{base_branches}",
                "- 最后更新：{updated}",
            ):
                self.assertIn(line, template)
            (feature / "README.md").write_text(
                template.format(
                    title="Demo",
                    repositories="service",
                    branches="service -> owner/feature/demo",
                    base_branches="service -> trunk",
                    updated="2026-09-01",
                ),
                encoding="utf-8",
            )

            summary = feature_context.feature_summary(
                feature_context.load_workspace(root), feature
            )

            self.assertEqual("planning", summary.status)
            self.assertEqual(("service",), summary.repositories)
            self.assertEqual((("service", "owner/feature/demo"),), summary.branches)
            self.assertEqual((("service", "trunk"),), summary.base_branches)

    def test_feature_summary_requires_complete_matching_repository_metadata(self):
        cases = (
            (
                "empty-repositories",
                "- 涉及仓库：`service`",
                "- 涉及仓库：",
                "涉及仓库不能为空",
            ),
            (
                "empty-branches",
                "- 工作分支：`service` -> `owner/feature/demo`",
                "- 工作分支：",
                "工作分支不能为空",
            ),
            (
                "empty-bases",
                "- 基线分支：`service` -> `trunk`",
                "- 基线分支：",
                "基线分支不能为空",
            ),
            (
                "duplicate-repositories",
                "- 涉及仓库：`service`",
                "- 涉及仓库：`service`, `service`",
                "涉及仓库重复",
            ),
        )
        for slug, old, new, message in cases:
            with self.subTest(slug=slug), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                readme = write_feature(root, slug)
                readme.write_text(
                    readme.read_text(encoding="utf-8").replace(old, new),
                    encoding="utf-8",
                )

                with self.assertRaisesRegex(ValueError, message):
                    feature_context.feature_summary(
                        feature_context.load_workspace(root), readme.parent
                    )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            readme = write_feature(root, "mismatched-repositories")
            registry = json.loads((root / ".workspace/workspace.json").read_text(encoding="utf-8"))
            registry["repositories"].append(
                {
                    "path": "worker",
                    "aliases": [],
                    "remote": None,
                    "category": "backend",
                    "description": "Worker",
                    "instruction": "docs/repositories/worker.md",
                }
            )
            (root / ".workspace/workspace.json").write_text(json.dumps(registry), encoding="utf-8")
            readme.write_text(
                readme.read_text(encoding="utf-8").replace(
                    "- 基线分支：`service` -> `trunk`",
                    "- 基线分支：`worker` -> `trunk`",
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "仓库必须完全一致"):
                feature_context.feature_summary(
                    feature_context.load_workspace(root), readme.parent
                )

    def test_feature_summary_rejects_invalid_slug_and_unsafe_paths(self):
        for slug in ("Bad_Name", ".hidden"):
            with self.subTest(slug=slug), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                readme = write_feature(root, slug)
                with self.assertRaisesRegex(ValueError, "小写 kebab-case"):
                    feature_context.feature_summary(
                        feature_context.load_workspace(root), readme.parent
                    )

        for case in ("outside", "nested", "feature-symlink", "readme-symlink"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                root = Path(directory) / "kit"
                root.mkdir()
                valid_readme = write_feature(root, "valid")
                content = valid_readme.read_text(encoding="utf-8")
                if case == "outside":
                    feature = root.parent / "outside"
                    feature.mkdir()
                    (feature / "README.md").write_text(content, encoding="utf-8")
                elif case == "nested":
                    feature = root / ".workspace/docs/features/outer/nested"
                    feature.mkdir(parents=True)
                    (feature / "README.md").write_text(content, encoding="utf-8")
                elif case == "feature-symlink":
                    target = root.parent / "target"
                    target.mkdir()
                    (target / "README.md").write_text(content, encoding="utf-8")
                    feature = root / ".workspace/docs/features/linked"
                    feature.symlink_to(target, target_is_directory=True)
                else:
                    feature = valid_readme.parent
                    target = root.parent / "README.md"
                    target.write_text(content, encoding="utf-8")
                    valid_readme.unlink()
                    valid_readme.symlink_to(target)

                with self.assertRaises(ValueError):
                    feature_context.feature_summary(
                        feature_context.load_workspace(root), feature
                    )

    def test_list_and_resolve_reject_missing_or_symlinked_readme(self):
        for readme_state in ("missing", "symlink"):
            for action in ("list", "resolve"):
                with (
                    self.subTest(readme_state=readme_state, action=action),
                    tempfile.TemporaryDirectory() as directory,
                ):
                    root = Path(directory)
                    readme = write_feature(root, "demo-feature")
                    if readme_state == "missing":
                        readme.unlink()
                    else:
                        target = root / "outside-readme.md"
                        readme.replace(target)
                        readme.symlink_to(target)
                    argv = ["--root", str(root), action]
                    if action == "resolve":
                        argv.extend(
                            [
                                "--repo",
                                "service",
                                "--branch",
                                "owner/feature/demo",
                            ]
                        )
                    stdout = io.StringIO()
                    stderr = io.StringIO()

                    with (
                        contextlib.redirect_stdout(stdout),
                        contextlib.redirect_stderr(stderr),
                    ):
                        code = feature_context.main(argv)

                    self.assertEqual(1, code)
                    self.assertEqual("", stdout.getvalue())
                    self.assertIn("需求 README 不存在或不安全", stderr.getvalue())

    def test_resolve_json_returns_repository_base_branch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_feature(root, "demo-feature", base="stable")
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = feature_context.main(
                    [
                        "--root",
                        str(root),
                        "resolve",
                        "--repo",
                        "service",
                        "--branch",
                        "owner/feature/demo",
                        "--json",
                    ]
                )
            self.assertEqual(0, code)
            self.assertEqual("stable", json.loads(output.getvalue())["baseBranch"])

    def test_resolve_canonicalizes_input_alias_but_readme_requires_canonical_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_feature(root, "demo-feature")
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(
                    0,
                    feature_context.main(
                        [
                            "--root",
                            str(root),
                            "resolve",
                            "--repo",
                            "svc",
                            "--branch",
                            "owner/feature/demo",
                            "--json",
                        ]
                    ),
                )
            self.assertEqual("service", json.loads(output.getvalue())["repository"])

            write_feature(root, "alias-feature", repository="svc")
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(
                    1,
                    feature_context.main(["--root", str(root), "list"]),
                )

    def test_matching_feature_without_base_is_an_error(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            readme = write_feature(root, "demo-feature")
            readme.write_text(
                readme.read_text(encoding="utf-8").replace(
                    "- 基线分支：`service` -> `trunk`\n", ""
                ),
                encoding="utf-8",
            )
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(
                    1,
                    feature_context.main(
                        [
                            "--root",
                            str(root),
                            "resolve",
                            "--repo",
                            "service",
                            "--branch",
                            "owner/feature/demo",
                            "--json",
                        ]
                    ),
                )
                self.assertEqual(
                    1,
                    feature_context.main(["--root", str(root), "list"]),
                )

    def test_list_rejects_unregistered_and_duplicate_repository_mappings(self):
        cases = (
            (
                "unknown",
                "- 基线分支：`service` -> `trunk`",
                "- 基线分支：`missing` -> `trunk`",
            ),
            (
                "duplicate-work",
                "- 工作分支：`service` -> `owner/feature/demo`",
                "- 工作分支：`service` -> `owner/feature/demo`; `service` -> `other`",
            ),
            (
                "duplicate-base",
                "- 基线分支：`service` -> `trunk`",
                "- 基线分支：`service` -> `trunk`; `service` -> `stable`",
            ),
            (
                "duplicate-field",
                "- 基线分支：`service` -> `trunk`",
                "- 基线分支：`service` -> `trunk`\n"
                "- 基线分支：`service` -> `stable`",
            ),
        )
        for slug, old, new in cases:
            with self.subTest(slug=slug), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                readme = write_feature(root, slug)
                readme.write_text(
                    readme.read_text(encoding="utf-8").replace(old, new),
                    encoding="utf-8",
                )
                with contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(
                        1,
                        feature_context.main(["--root", str(root), "list"]),
                    )

    def test_resolve_and_list_require_strict_last_updated_date(self):
        for updated in ("", "2026-9-1", "2026-02-30"):
            with self.subTest(updated=updated), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                readme = write_feature(root, "demo-feature")
                if updated:
                    content = readme.read_text(encoding="utf-8").replace(
                        "2026-09-01", updated
                    )
                else:
                    content = readme.read_text(encoding="utf-8").replace(
                        "- 最后更新：2026-09-01\n", ""
                    )
                readme.write_text(content, encoding="utf-8")
                with contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(
                        1,
                        feature_context.main(["--root", str(root), "list"]),
                    )
                    self.assertEqual(
                        1,
                        feature_context.main(
                            [
                                "--root",
                                str(root),
                                "resolve",
                                "--repo",
                                "service",
                                "--branch",
                                "owner/feature/demo",
                            ]
                        ),
                    )

    def test_list_and_set_status(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            readme = write_feature(root, "demo-feature")
            readme.chmod(0o640)
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(
                    0,
                    feature_context.main(["--root", str(root), "list", "--json"]),
                )
            payload = json.loads(output.getvalue())
            self.assertEqual([["service", "trunk"]], payload[0]["baseBranches"])
            feature_context.update_status(root, "demo-feature", "testing", "2026-09-02")
            self.assertIn("- 状态：testing", readme.read_text(encoding="utf-8"))
            self.assertEqual(0o640, stat.S_IMODE(readme.stat().st_mode))

    def test_set_status_requires_strict_iso_date_without_newlines(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            readme = write_feature(root, "demo-feature")
            original = readme.read_bytes()

            for invalid in (
                "2026-9-1",
                "20260901",
                "not-a-date",
                "2026-09-01\n- 状态：done",
            ):
                with self.subTest(updated=invalid), self.assertRaises(ValueError):
                    feature_context.update_status(
                        root, "demo-feature", "testing", invalid
                    )

            self.assertEqual(original, readme.read_bytes())

    def test_set_status_rejects_symlinked_feature_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_workspace(root)
            features = root / ".workspace/docs/features"
            features.mkdir(parents=True)
            outside = Path(directory) / "outside"
            outside.mkdir()
            outside_readme = outside / "README.md"
            outside_readme.write_text(
                "- 状态：development\n- 最后更新：2026-09-01\n", encoding="utf-8"
            )
            (features / "link").symlink_to(outside, target_is_directory=True)
            with self.assertRaises(ValueError):
                feature_context.update_status(root, "link", "testing", "2026-09-02")
            self.assertIn("状态：development", outside_readme.read_text(encoding="utf-8"))

            # Listing and resolving must reject the same symlink, too.
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(
                    1,
                    feature_context.main(["--root", str(root), "list"]),
                )
                self.assertEqual(
                    1,
                    feature_context.main(
                        [
                            "--root",
                            str(root),
                            "resolve",
                            "--repo",
                            "service",
                            "--branch",
                            "owner/feature/demo",
                        ]
                    ),
                )

    def test_all_feature_operations_reject_symlinked_docs_parent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_workspace(root)
            outside = Path(directory) / "outside"
            (outside / "features").mkdir(parents=True)
            write_feature(outside, "demo-feature")
            (root / "docs").symlink_to(outside, target_is_directory=True)

            with self.assertRaises(ValueError):
                feature_context.list_features(root)
            with self.assertRaises(ValueError):
                feature_context.resolve_features(
                    root, "service", "owner/feature/demo"
                )
            with self.assertRaises(ValueError):
                feature_context.update_status(
                    root, "demo-feature", "testing", "2026-09-02"
                )

    def test_create_feature_generates_metadata_and_keeps_artifacts_on_feature_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_workspace(root)
            (root / ".workspace/docs/features").mkdir(parents=True)
            (root / ".workspace/workspace.local.json").write_text(
                json.dumps({"branchOwner": "owner", "primaryRole": None, "extensions": {}}),
                encoding="utf-8",
            )

            feature = feature_context.create_feature(
                root,
                "payment-feature",
                ["svc"],
                title="Payment Feature",
                summary="Store payment records.",
            )

            self.assertEqual(root.resolve() / ".workspace/docs/features/payment-feature", feature)
            self.assertTrue((feature / "requirements/requirements.md").is_file())
            for directory in ("design", "plans", "testing", "artifacts"):
                self.assertFalse((feature / directory).exists())
            readme = (feature / "README.md").read_text(encoding="utf-8")
            self.assertIn("owner/feature/payment-feature", readme)
            self.assertIn("Store payment records.", readme)
            for directory in ("design", "plans", "testing"):
                self.assertNotIn(f"]({directory}/", readme)

            sql = feature / "artifacts/sql/001-create-payment.sql"
            sql.parent.mkdir(parents=True)
            sql.write_text("create table payment;\n", encoding="utf-8")
            summary = feature_context.feature_summary(
                feature_context.load_workspace(root), feature
            )
            self.assertEqual("payment-feature", summary.slug)

    def test_feature_commands_allow_unactivated_valid_action(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_workspace(root)
            (root / ".workspace/docs/features").mkdir(parents=True)
            (root / ".workspace/workspace.local.json").write_text(
                json.dumps({"branchOwner": "owner", "primaryRole": None, "extensions": {}}),
                encoding="utf-8",
            )
            extension = root / ".workspace/extensions/legacy-action"
            extension.mkdir(parents=True)
            (extension / "workspace-extension.json").write_text(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "id": "legacy-action",
                        "version": "1.0.0",
                        "kitApi": 1,
                        "provides": [],
                        "actions": [
                            {
                                "id": "legacy",
                                "apiVersion": 1,
                                "skill": "legacy",
                                "effects": [],
                                "confirmation": {
                                    "title": "Run",
                                    "summary": "Run the action.",
                                },
                            }
                        ],
                        "requires": [],
                        "effects": [],
                    }
                ),
                encoding="utf-8",
            )
            skill = extension / "skills/legacy/SKILL.md"
            skill.parent.mkdir(parents=True)
            skill.write_text("---\nname: legacy\n---\n", encoding="utf-8")

            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = feature_context.main(
                    [
                        "--root", str(root), "create", "isolated-feature", "--repo", "svc",
                        "--title", "Isolated Feature", "--summary", "Create despite stale Action.",
                        "--date", "2026-09-04", "--json",
                    ]
                )

            self.assertEqual(0, code)
            self.assertEqual("isolated-feature", json.loads(output.getvalue())["featureSlug"])
            self.assertEqual(["isolated-feature"], [item.slug for item in feature_context.list_features(root)])
            self.assertEqual(
                ["isolated-feature"],
                [
                    item.slug
                    for item in feature_context.resolve_features(
                        root, "svc", "owner/feature/isolated-feature"
                    )
                ],
            )

    def test_help_exposes_root_and_actions(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            with self.assertRaises(SystemExit) as raised:
                feature_context.main(["--help"])
        self.assertEqual(0, raised.exception.code)
        self.assertIn("--root", output.getvalue())
        self.assertIn("resolve", output.getvalue())
        self.assertIn("list", output.getvalue())
        self.assertIn("set-status", output.getvalue())
        self.assertIn("create", output.getvalue())
        self.assertIn("set-active", output.getvalue())

    def _write_local(self, root: Path) -> None:
        (root / ".workspace/extensions/.state/cache").mkdir(parents=True, exist_ok=True)
        (root / ".workspace/workspace.local.json").write_text(
            json.dumps({"branchOwner": "alice", "primaryRole": None, "extensions": {}}),
            encoding="utf-8",
        )
        (root / ".workspace/cache").mkdir(parents=True, exist_ok=True)

    def test_set_active_feature_writes_pointer_and_preserves_other_local_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_feature(root, "alpha")
            self._write_local(root)

            result = feature_context.set_active_feature(root, "alpha")

            self.assertEqual({"activeFeature": "alpha"}, result)
            saved = json.loads(
                (root / ".workspace/workspace.local.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                {
                    "branchOwner": "alice",
                    "primaryRole": None,
                    "extensions": {},
                    "activeFeature": "alpha",
                    "extensionSources": {},
                },
                saved,
            )

    def test_set_active_feature_clear_resets_pointer_to_none(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_feature(root, "alpha")
            self._write_local(root)
            feature_context.set_active_feature(root, "alpha")

            result = feature_context.set_active_feature(root, None)

            self.assertEqual({"activeFeature": None}, result)
            saved = json.loads(
                (root / ".workspace/workspace.local.json").read_text(encoding="utf-8")
            )
            self.assertIsNone(saved["activeFeature"])

    def test_set_active_feature_rejects_unknown_slug(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_feature(root, "alpha")
            self._write_local(root)

            with self.assertRaisesRegex(ValueError, "未完成的需求"):
                feature_context.set_active_feature(root, "does-not-exist")

    def test_set_active_feature_rejects_done_feature(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            readme = write_feature(root, "alpha")
            readme.write_text(
                readme.read_text(encoding="utf-8").replace("状态：development", "状态：done"),
                encoding="utf-8",
            )
            self._write_local(root)

            with self.assertRaisesRegex(ValueError, "未完成的需求"):
                feature_context.set_active_feature(root, "alpha")

    def test_set_active_feature_rejects_invalid_slug_format(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_feature(root, "alpha")
            self._write_local(root)

            with self.assertRaisesRegex(ValueError, "kebab-case"):
                feature_context.set_active_feature(root, "Not_Valid")

    def test_cli_set_active_requires_exactly_one_of_slug_or_clear(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_feature(root, "alpha")
            self._write_local(root)

            output = io.StringIO()
            with contextlib.redirect_stderr(output):
                code = feature_context.main(["--root", str(root), "set-active"])
            self.assertNotEqual(0, code)

            code = feature_context.main(
                ["--root", str(root), "set-active", "alpha", "--clear"]
            )
            self.assertNotEqual(0, code)

    def test_cli_set_active_switches_and_reports_json(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_feature(root, "alpha")
            self._write_local(root)

            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = feature_context.main(
                    ["--root", str(root), "set-active", "alpha", "--json"]
                )
            self.assertEqual(0, code)
            self.assertEqual({"activeFeature": "alpha"}, json.loads(output.getvalue()))

            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = feature_context.main(
                    ["--root", str(root), "set-active", "--clear", "--json"]
                )
            self.assertEqual(0, code)
            self.assertEqual({"activeFeature": None}, json.loads(output.getvalue()))


if __name__ == "__main__":
    unittest.main()
