from __future__ import annotations

import contextlib
import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import workspace_doctor  # noqa: E402
import workspace_model  # noqa: E402
import workspace_setup  # noqa: E402


SKILLS = (
    "workspace-init",
    "workspace-repo-onboarding",
    "workspace-cross-repo-analysis",
    "workspace-feature-design",
    "workspace-api-contract",
    "workspace-feature-workflow",
    "workspace-verify",
    "workspace-sync-base",
    "workspace-submit-test",
    "workspace-extension",
    "workspace-update",
)


def repository(remote="https://example.test/service.git"):
    return {
        "path": "service",
        "aliases": ["svc"],
        "remote": remote,
        "category": "backend",
        "description": "Service",
        "instruction": "docs/repositories/service.md",
    }


class WorkspaceDoctorTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.parent = Path(self.temp.name).resolve()
        self.root = self.parent / "kit"
        self.root.mkdir()
        self.initialize_git()

    def initialize_git(self):
        (self.root / ".gitignore").write_text(".workspace/\ninput.json\n", encoding="utf-8")
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        subprocess.run(["git", "-C", str(self.root), "add", ".gitignore"], check=True)
        subprocess.run(
            ["git", "-C", str(self.root), "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "fixture"],
            check=True,
        )

    def tearDown(self):
        self.temp.cleanup()

    def install_core(self):
        (self.root / "AGENTS.md").write_text("# Rules\n", encoding="utf-8")
        (self.root / "CLAUDE.md").write_text("@AGENTS.md\n", encoding="utf-8")
        (self.root / "GEMINI.md").write_text("@./AGENTS.md\n", encoding="utf-8")
        skills_root = self.root / ".agents" / "skills"
        claude_root = self.root / ".claude" / "skills"
        skills_root.mkdir(parents=True)
        claude_root.mkdir(parents=True)
        for name in SKILLS:
            skill = skills_root / name
            skill.mkdir()
            (skill / "SKILL.md").write_text(
                f"---\nname: {name}\n---\n", encoding="utf-8"
            )
            (claude_root / name).symlink_to(
                Path("../../.agents/skills") / name,
                target_is_directory=True,
            )

    def initialize(self, remote="https://example.test/service.git", repo=None):
        config = self.root / "input.json"
        config.write_text(
            json.dumps(
                {
                    "version": {"major": 1, "minor": 0},
                    "workspace": {"name": "Demo Workspace"},
                    "local": {"branchOwner": "alice", "primaryRole": None, "extensions": {}},
                    "context": {},
                    "branchPolicy": {},
                    "extensions": {"providers": {}, "config": {}},
                    "repositories": [repo or repository(remote)],
                }
            ),
            encoding="utf-8",
        )
        preview_output = io.StringIO()
        with contextlib.redirect_stdout(preview_output):
            self.assertEqual(
                0,
                workspace_setup.main(
                    [
                        "--root",
                        str(self.root),
                        "init",
                        "preview",
                        "--config",
                        str(config),
                        "--json",
                    ]
                ),
            )
        preview_hash = json.loads(preview_output.getvalue())["previewHash"]
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(
                0,
                workspace_setup.main(
                    [
                        "--root",
                        str(self.root),
                        "init",
                        "apply",
                        "--config",
                        str(config),
                        "--preview-hash",
                        preview_hash,
                    ]
                ),
            )
        self.install_core()

    def codes(self, **kwargs):
        return {item.code for item in workspace_doctor.audit(self.root, **kwargs)}

    def write_feature(self, slug="demo-feature", status="planning"):
        feature = self.root / ".workspace/docs/features" / slug
        feature.mkdir(parents=True, exist_ok=True)
        readme = feature / "README.md"
        readme.write_text(
            f"# Demo\n\n- 状态：{status}\n"
            "- 涉及仓库：service\n"
            "- 工作分支：service -> owner/feature/demo\n"
            "- 基线分支：service -> trunk\n"
            "- 最后更新：2026-09-01\n",
            encoding="utf-8",
        )
        return feature

    def test_clean_clone_is_healthy_and_instructive(self):
        self.install_core()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = workspace_doctor.main(["--root", str(self.root)])
        self.assertEqual(0, code)
        self.assertIn("WORKSPACE_UNINITIALIZED", output.getvalue())
        self.assertIn("init plan --config ./workspace-input.json", output.getvalue())
        self.assertIn("clone -> preview --json", output.getvalue())
        self.assertIn("previewHash", output.getvalue())
        self.assertIn("applyCommand", output.getvalue())
        self.assertIn("SUMMARY ERROR=0 WARN=0 INFO=1", output.getvalue())
        self.assertEqual(
            [("INFO", "WORKSPACE_UNINITIALIZED")],
            [
                (item.level, item.code)
                for item in workspace_doctor.audit(self.root, repository="service")
            ],
        )

    def test_empty_residual_root_directory_warns(self):
        self.install_core()
        (self.root / "design").mkdir()
        self.assertIn("ROOT_RESIDUAL_DIRECTORY", self.codes())

    def test_nonempty_directory_with_residual_name_is_not_flagged(self):
        self.install_core()
        design = self.root / "design"
        design.mkdir()
        (design / "note.md").write_text("keep\n", encoding="utf-8")
        self.assertNotIn("ROOT_RESIDUAL_DIRECTORY", self.codes())

    def test_clean_clone_has_no_residual_directories(self):
        self.install_core()
        self.assertNotIn("ROOT_RESIDUAL_DIRECTORY", self.codes())

    def test_initialized_workspace_checks_ignore_tracking_and_local_config(self):
        self.initialize()
        gitignore = self.root / ".gitignore"
        gitignore.write_text("input.json\n", encoding="utf-8")
        self.assertIn("WORKSPACE_NOT_IGNORED", self.codes())
        gitignore.write_text(".workspace/\ninput.json\n", encoding="utf-8")

        subprocess.run(
            ["git", "-C", str(self.root), "add", "-f", ".workspace/workspace.json"],
            check=True,
        )
        self.assertIn("WORKSPACE_TRACKED", self.codes())

        local = self.root / ".workspace/workspace.local.json"
        local.write_text(
            json.dumps(
                {
                    "branchOwner": "alice",
                    "primaryRole": None,
                    "extensions": {"demo": {"api_token": "nope"}},
                }
            ),
            encoding="utf-8",
        )
        self.assertIn("LOCAL_CONFIG_INVALID", self.codes())

    def test_ignore_rule_for_registry_only_does_not_protect_workspace_tree(self):
        self.initialize()
        (self.root / ".gitignore").write_text(
            ".workspace/workspace.json\ninput.json\n", encoding="utf-8"
        )
        self.assertIn("WORKSPACE_NOT_IGNORED", self.codes())

    def test_git_info_exclude_does_not_satisfy_root_ignore_contract(self):
        self.initialize()
        (self.root / ".gitignore").write_text("input.json\n", encoding="utf-8")
        (self.root / ".git/info/exclude").write_text(".workspace/\n", encoding="utf-8")
        self.assertIn("WORKSPACE_NOT_IGNORED", self.codes())

    def test_managed_state_requires_safe_agents_extensions_and_cache(self):
        self.initialize()
        agents = self.root / ".workspace/AGENTS.md"
        target = self.parent / "agents.md"
        target.write_text("# outside\n", encoding="utf-8")
        agents.unlink()
        agents.symlink_to(target)
        self.assertIn("WORKSPACE_PATH_UNSAFE", self.codes())

        agents.unlink()
        agents.write_text("# restored\n", encoding="utf-8")
        extensions = self.root / ".workspace/extensions"
        shutil.rmtree(extensions / ".state")
        extensions.rmdir()
        self.assertIn("WORKSPACE_ARTIFACT_MISSING", self.codes())
        extensions.mkdir()
        (extensions / ".state").mkdir()
        (extensions / ".state" / "lock.json").write_text('{"lockVersion":{"major":1,"minor":0},"kitApi":1,"extensions":[],"providers":{}}\n')

        cache = self.root / ".workspace/extensions/.state/cache"
        cache.mkdir(parents=True)
        cache.rmdir()
        cache_findings = workspace_doctor.audit(self.root)
        cache_codes = {item.code for item in cache_findings}
        self.assertNotIn("WORKSPACE_ARTIFACT_MISSING", cache_codes)
        self.assertNotIn("WORKSPACE_PATH_UNSAFE", cache_codes)
        self.assertFalse([item for item in cache_findings if item.level == "ERROR"])
        outside_cache = self.parent / "outside-cache"
        outside_cache.mkdir()
        cache.symlink_to(outside_cache, target_is_directory=True)
        self.assertIn("WORKSPACE_PATH_UNSAFE", self.codes())

    def test_workflow_overlay_is_optional_but_invalid_overlay_is_reported(self):
        self.initialize()
        self.assertNotIn("WORKFLOW_INVALID", self.codes())
        overlay = self.root / ".workspace/workflow.json"
        overlay.write_text(
            json.dumps(
                {
                    "schemaVersion": {"major": 1, "minor": 0},
                    "workflow": "feature-development",
                    "stages": [
                        {
                            "id": "team-check.run",
                            "after": "feature.implement",
                            "uses": "missing/run",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        self.assertIn("ACTION_MISSING", self.codes())

        overlay.unlink()
        overlay.symlink_to(self.parent / "outside.json")
        self.assertIn("WORKFLOW_INVALID", self.codes())

    def test_empty_directory_is_unhealthy_but_still_reports_uninitialized(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = workspace_doctor.main(["--root", str(self.root)])

        self.assertEqual(1, code)
        self.assertIn("[INFO] WORKSPACE_UNINITIALIZED", output.getvalue())
        self.assertIn("AGENTS_INVALID", output.getvalue())

    def test_uninitialized_workspace_still_checks_core_markdown_links(self):
        self.install_core()
        (self.root / "README.md").write_text("[missing](docs/missing.md)\n", encoding="utf-8")

        self.assertIn("MARKDOWN_LINK_BROKEN", self.codes())

    def test_uninitialized_workspace_rejects_extra_client_skill_adapter(self):
        self.install_core()
        (self.root / ".claude/skills/extra").symlink_to(
            Path("../../.agents/skills/workspace-init"), target_is_directory=True
        )

        self.assertIn("CLIENT_SKILL_ADAPTER_UNEXPECTED", self.codes())

    def test_uninitialized_workspace_does_not_validate_kit_maintenance_features(self):
        self.install_core()
        readme = self.root / "docs/development/features/kit-change/README.md"
        readme.parent.mkdir(parents=True)
        readme.write_text("- 状态：invalid\n", encoding="utf-8")

        self.assertNotIn("FEATURE_METADATA_INVALID", self.codes())

    def test_initialized_workspace_accepts_valid_feature_metadata(self):
        self.initialize()
        self.write_feature()

        self.assertNotIn("FEATURE_METADATA_INVALID", self.codes())

    def test_initialized_workspace_requires_feature_readme(self):
        self.initialize()
        self.write_feature()
        (self.root / ".workspace/docs/features/demo-feature/README.md").unlink()

        findings = workspace_doctor.audit(self.root)

        invalid = [item for item in findings if item.code == "FEATURE_METADATA_INVALID"]
        self.assertEqual(1, len(invalid))
        self.assertIn("docs/features/demo-feature", invalid[0].message)
        self.assertIn("README.md", invalid[0].message)

    def test_initialized_workspace_rejects_symlinked_feature_readme(self):
        self.initialize()
        feature = self.write_feature()
        readme = feature / "README.md"
        target = self.parent / "feature-readme.md"
        readme.replace(target)
        readme.symlink_to(target)

        self.assertIn("FEATURE_METADATA_INVALID", self.codes())

    def test_initialized_workspace_rejects_invalid_or_incomplete_feature_metadata(self):
        mutations = {
            "invalid-status": ("状态：planning", "状态：invalid", "状态无效"),
            "missing-updated": (
                "- 最后更新：2026-09-01\n",
                "",
                "最后更新日期必须为 YYYY-MM-DD",
            ),
            "empty-repositories": (
                "- 涉及仓库：service",
                "- 涉及仓库：",
                "涉及仓库不能为空",
            ),
            "empty-branches": (
                "- 工作分支：service -> owner/feature/demo",
                "- 工作分支：",
                "工作分支不能为空",
            ),
            "empty-bases": (
                "- 基线分支：service -> trunk",
                "- 基线分支：",
                "基线分支不能为空",
            ),
        }
        for mutation, (old, new, error) in mutations.items():
            with self.subTest(mutation=mutation):
                self.initialize()
                feature = self.write_feature()
                readme = feature / "README.md"
                readme.write_text(
                    readme.read_text(encoding="utf-8").replace(old, new),
                    encoding="utf-8",
                )

                findings = workspace_doctor.audit(self.root)
                invalid = [
                    item for item in findings if item.code == "FEATURE_METADATA_INVALID"
                ]
                self.assertEqual(1, len(invalid))
                self.assertIn("docs/features/demo-feature", invalid[0].message)
                self.assertIn(error, invalid[0].message)

                shutil.rmtree(self.root)
                self.root.mkdir()
                self.initialize_git()

    def test_initialized_workspace_rejects_mismatched_feature_repositories(self):
        self.initialize()
        registry_path = self.root / ".workspace/workspace.json"
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
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
        registry_path.write_text(json.dumps(registry), encoding="utf-8")
        feature = self.write_feature()
        readme = feature / "README.md"
        readme.write_text(
            readme.read_text(encoding="utf-8").replace(
                "- 基线分支：service -> trunk",
                "- 基线分支：worker -> trunk",
            ),
            encoding="utf-8",
        )

        findings = workspace_doctor.audit(self.root)

        invalid = [item for item in findings if item.code == "FEATURE_METADATA_INVALID"]
        self.assertEqual(1, len(invalid))
        self.assertIn("仓库必须完全一致", invalid[0].message)

    def test_initialized_workspace_rejects_invalid_feature_slug(self):
        for slug in ("Bad_Name", ".hidden"):
            with self.subTest(slug=slug):
                self.initialize()
                self.write_feature(slug)

                findings = workspace_doctor.audit(self.root)

                invalid = [
                    item for item in findings if item.code == "FEATURE_METADATA_INVALID"
                ]
                self.assertEqual(1, len(invalid))
                self.assertIn("小写 kebab-case", invalid[0].message)

                shutil.rmtree(self.root)
                self.root.mkdir()
                self.initialize_git()

    def test_initialized_workspace_rejects_symlinked_feature_directory(self):
        self.initialize()
        outside = self.parent / "outside-feature"
        outside.mkdir()
        (self.root / ".workspace/docs/features/linked-feature").symlink_to(
            outside, target_is_directory=True
        )

        findings = workspace_doctor.audit(self.root)

        invalid = [item for item in findings if item.code == "FEATURE_METADATA_INVALID"]
        self.assertEqual(1, len(invalid))
        self.assertIn("linked-feature", invalid[0].message)
        self.assertIn("符号链接", invalid[0].message)

    def test_missing_repository_is_optional_unless_targeted(self):
        self.initialize()
        self.assertIn("REPO_NOT_INSTALLED", self.codes())
        self.assertFalse(
            [item for item in workspace_doctor.audit(self.root) if item.level == "ERROR"]
        )
        findings = workspace_doctor.audit(self.root, repository="svc")
        self.assertIn("TARGET_REPO_MISSING", {item.code for item in findings})
        self.assertTrue(any("git clone" in item.message for item in findings))

    def test_present_repository_requires_independent_git_origin_and_instruction(self):
        self.initialize()
        service = self.parent / "service"
        subprocess.run(["git", "init", "-q", str(service)], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(service),
                "remote",
                "add",
                "origin",
                "https://example.test/service.git",
            ],
            check=True,
        )
        (service / "AGENTS.md").write_text("# Rules\n", encoding="utf-8")
        self.assertNotIn("REPO_INVALID", self.codes())
        subprocess.run(
            [
                "git",
                "-C",
                str(service),
                "remote",
                "set-url",
                "origin",
                "https://example.test/other.git",
            ],
            check=True,
        )
        self.assertIn("REPO_INVALID", self.codes())

    def test_verbose_reports_but_never_registers_sibling(self):
        self.initialize()
        sibling = self.parent / "unregistered"
        subprocess.run(["git", "init", "-q", str(sibling)], check=True)
        before = (self.root / ".workspace/workspace.json").read_bytes()
        self.assertNotIn("UNREGISTERED_SIBLING", self.codes())
        self.assertIn("UNREGISTERED_SIBLING", self.codes(verbose=True))
        self.assertEqual(before, (self.root / ".workspace/workspace.json").read_bytes())

    def test_generated_files_markdown_agents_and_optional_adapters_are_checked(self):
        self.initialize()
        (self.root / ".workspace/CONTEXT.md").write_text("stale\n", encoding="utf-8")
        self.assertIn("CONTEXT_INVALID", self.codes())

        (self.root / "AGENTS.md").write_text("x" * 8192, encoding="utf-8")
        self.assertIn("AGENTS_TOO_LARGE", self.codes())
        (self.root / "AGENTS.md").write_text("[missing](docs/nope.md)\n", encoding="utf-8")
        self.assertIn("MARKDOWN_LINK_BROKEN", self.codes())

        skill = self.root / ".agents/skills/demo/SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text("---\nname: wrong\n---\n", encoding="utf-8")
        self.assertIn("SKILL_INVALID", self.codes())

    def test_dangling_and_absolute_symlinks_are_reported(self):
        self.initialize()
        agents = self.root / "AGENTS.md"
        agents.unlink()
        agents.symlink_to(self.parent / "missing-agents.md")
        self.assertIn("AGENTS_INVALID", self.codes())
        agents.unlink()

        skills_root = self.root / ".agents" / "skills"
        real_skills = self.root / ".agents" / "real-skills"
        skills_root.rename(real_skills)
        skills_root.symlink_to(self.parent / "missing-skills", target_is_directory=True)
        self.assertIn("SKILLS_INVALID", self.codes())
        skills_root.unlink()
        real_skills.rename(skills_root)

        claude_root = self.root / ".claude/skills"
        adapter = claude_root / "workspace-init"
        adapter.unlink()
        adapter.symlink_to(
            (skills_root / "workspace-init").resolve(), target_is_directory=True
        )
        self.assertIn("CLIENT_SKILL_ADAPTER_INVALID", self.codes())
        adapter.unlink()
        adapter.symlink_to(self.parent / "missing-adapter", target_is_directory=True)
        self.assertIn("CLIENT_SKILL_ADAPTER_INVALID", self.codes())
        adapter.unlink()
        adapter.symlink_to(
            Path("../../.agents/skills/workspace-init"), target_is_directory=True
        )

        real_claude = self.root / ".claude" / "real-skills"
        claude_root.rename(real_claude)
        dangling_claude = self.root / ".claude" / "skills"
        dangling_claude.symlink_to(self.parent / "missing-claude", target_is_directory=True)
        self.assertIn("CLIENT_SKILL_ROOT_INVALID", self.codes())

    def test_client_skill_adapter_invalid_remediation_is_an_actionable_command(self):
        remediation = workspace_doctor.REMEDIATIONS["CLIENT_SKILL_ADAPTER_INVALID"]
        self.assertEqual("command", remediation.kind)
        self.assertIn("ln -s", remediation.detail)
        for name in SKILLS:
            resolved = (ROOT / ".claude/skills" / "../../.agents/skills" / name / "SKILL.md").resolve()
            self.assertTrue(resolved.is_file(), name)

    def test_initialized_workspace_requires_all_core_assets(self):
        self.initialize()

        agents = self.root / "AGENTS.md"
        agents.unlink()
        self.assertIn("AGENTS_INVALID", self.codes())
        agents.write_text("# Rules\n", encoding="utf-8")

        claude = self.root / "CLAUDE.md"
        claude.unlink()
        self.assertIn("CLIENT_ADAPTER_INVALID", self.codes())
        claude.write_text("@AGENTS.md\n", encoding="utf-8")

        skill = self.root / ".agents/skills/workspace-init"
        hidden_skill = self.root / ".agents/skills/.workspace-init"
        skill.rename(hidden_skill)
        self.assertIn("SKILL_MISSING", self.codes())
        self.assertIn("CLIENT_SKILL_ADAPTER_INVALID", self.codes())
        hidden_skill.rename(skill)

        adapter = self.root / ".claude/skills/workspace-init"
        adapter.unlink()
        self.assertIn("CLIENT_SKILL_ADAPTER_INVALID", self.codes())

    def test_repository_instruction_is_only_resolved_from_governance_root(self):
        repo = repository()
        repo["instruction"] = "docs/repositories/service.md"
        self.initialize(repo=repo)
        service = self.parent / "service"
        subprocess.run(["git", "init", "-q", str(service)], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(service),
                "remote",
                "add",
                "origin",
                "https://example.test/service.git",
            ],
            check=True,
        )
        business_instruction = service / "docs/repositories/service.md"
        business_instruction.parent.mkdir(parents=True)
        business_instruction.write_text("# Business copy\n", encoding="utf-8")
        (self.root / ".workspace/docs/repositories/service.md").unlink()

        codes = self.codes()

        self.assertIn("INSTRUCTION_MISSING", codes)
        self.assertIn("REPOSITORY_PROFILE_INVALID", codes)

    def test_markdown_absolute_local_links_are_unsafe_even_when_they_exist(self):
        self.initialize()
        note = self.root / "docs" / "absolute.md"
        note.parent.mkdir()
        note.write_text(f"[rules]({self.root / 'AGENTS.md'})\n", encoding="utf-8")

        self.assertIn("MARKDOWN_LINK_UNSAFE", self.codes())

    def test_docs_symlink_inside_root_is_reported_for_generated_and_markdown_paths(self):
        self.initialize()
        docs = self.root / ".workspace/docs"
        real_docs = self.root / "real-docs"
        docs.rename(real_docs)
        docs.symlink_to("../real-docs", target_is_directory=True)

        codes = self.codes()

        self.assertIn("DOCS_DIRECTORY_INVALID", codes)
        self.assertIn("MARKDOWN_ROOT_INVALID", codes)

    def test_docs_symlink_outside_root_is_reported_for_generated_and_markdown_paths(self):
        self.initialize()
        docs = self.root / ".workspace/docs"
        outside_docs = self.parent / "outside-docs"
        shutil.move(str(docs), str(outside_docs))
        docs.symlink_to(outside_docs, target_is_directory=True)

        codes = self.codes()

        self.assertIn("DOCS_DIRECTORY_INVALID", codes)
        self.assertIn("MARKDOWN_ROOT_INVALID", codes)

    def test_remote_mismatch_cli_does_not_echo_credentials(self):
        self.initialize()
        service = self.parent / "service"
        subprocess.run(["git", "init", "-q", str(service)], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(service),
                "remote",
                "add",
                "origin",
                "https://user:doctor-secret@example.test/service.git",
            ],
            check=True,
        )
        (service / "AGENTS.md").write_text("# Rules\n", encoding="utf-8")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = workspace_doctor.main(["--root", str(self.root)])
        self.assertEqual(1, code)
        self.assertNotIn("doctor-secret", output.getvalue())
        self.assertIn("remote 不匹配", output.getvalue())

    def test_doctor_help_exposes_root_and_options(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            with self.assertRaises(SystemExit) as raised:
                workspace_doctor.main(["--help"])
        self.assertEqual(0, raised.exception.code)
        self.assertIn("--root", output.getvalue())
        self.assertIn("--repo", output.getvalue())
        self.assertIn("--verbose", output.getvalue())

    def test_remote_null_missing_is_info_by_default_and_error_when_targeted(self):
        self.initialize()
        registry_path = self.root / ".workspace/workspace.json"
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
        registry["repositories"][0]["remote"] = None
        registry_path.write_text(json.dumps(registry), encoding="utf-8")
        default_findings = workspace_doctor.audit(self.root)
        self.assertIn("REPO_NOT_INSTALLED", {item.code for item in default_findings})
        self.assertFalse([item for item in default_findings if item.level == "ERROR"])
        targeted = workspace_doctor.audit(self.root, repository="svc")
        self.assertIn("TARGET_REPO_MISSING", {item.code for item in targeted})
        self.assertTrue(any("无 clone remote" in item.message for item in targeted))
        self.assertIn(
            "REPOSITORY_UNKNOWN",
            self.codes(repository="not-registered"),
        )

    def test_text_mode_prints_remediation_for_covered_code(self):
        self.initialize()
        subprocess.run(
            ["git", "-C", str(self.root), "add", "-f", ".workspace/workspace.json"],
            check=True,
        )
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            workspace_doctor.main(["--root", str(self.root)])
        text = output.getvalue()
        self.assertIn("WORKSPACE_TRACKED", text)
        self.assertIn("git rm -r --cached .workspace", text)

    def test_text_mode_adds_no_extra_line_for_uncovered_code(self):
        self.install_core()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            workspace_doctor.main(["--root", str(self.root)])
        lines = output.getvalue().splitlines()
        uninitialized_index = next(
            index for index, line in enumerate(lines) if "WORKSPACE_UNINITIALIZED" in line
        )
        self.assertTrue(lines[uninitialized_index + 1].startswith("SUMMARY"))

    def test_json_mode_reports_remediation_and_matching_summary(self):
        self.initialize()
        subprocess.run(
            ["git", "-C", str(self.root), "add", "-f", ".workspace/workspace.json"],
            check=True,
        )
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = workspace_doctor.main(["--root", str(self.root), "--json"])
        payload = json.loads(output.getvalue())
        tracked = next(item for item in payload["findings"] if item["code"] == "WORKSPACE_TRACKED")
        self.assertEqual(
            {"kind": "command", "detail": "git rm -r --cached .workspace"},
            tracked["remediation"],
        )
        uninitialized_free = [
            item for item in payload["findings"] if item["code"] == "REPO_NOT_INSTALLED"
        ]
        self.assertEqual(1, len(uninitialized_free))
        self.assertIsNone(uninitialized_free[0]["remediation"])
        counts = payload["summary"]
        text_output = io.StringIO()
        with contextlib.redirect_stdout(text_output):
            workspace_doctor.main(["--root", str(self.root)])
        summary_line = next(
            line for line in text_output.getvalue().splitlines() if line.startswith("SUMMARY")
        )
        self.assertEqual(
            f"SUMMARY ERROR={counts['errors']} WARN={counts['warnings']} INFO={counts['info']}",
            summary_line,
        )
        self.assertEqual(1, code)

    def test_json_mode_null_remediation_for_uncovered_code(self):
        self.install_core()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            workspace_doctor.main(["--root", str(self.root), "--json"])
        payload = json.loads(output.getvalue())
        uninitialized = next(
            item for item in payload["findings"] if item["code"] == "WORKSPACE_UNINITIALIZED"
        )
        self.assertIsNone(uninitialized["remediation"])

    def test_remediation_details_have_no_unfilled_placeholders(self):
        for remediation in workspace_doctor.REMEDIATIONS.values():
            self.assertIn(remediation.kind, ("command", "manual"))
            self.assertNotRegex(remediation.detail, r"[<>{}]")


if __name__ == "__main__":
    unittest.main()
