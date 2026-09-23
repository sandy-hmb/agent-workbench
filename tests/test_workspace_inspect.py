from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"


def command(root: Path, *args: str) -> tuple[int, dict[str, object], str]:
    result = subprocess.run(
        [sys.executable, str(SCRIPTS / "kit.py"), "inspect", "--root", str(root), "--api-major", "1", "--json", *args],
        capture_output=True, text=True, check=False,
    )
    return result.returncode, json.loads(result.stdout), result.stderr


class WorkspaceInspectTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "kit"; self.root.mkdir()
        feature = self.root / "docs/development/features/demo-feature"
        (feature / "requirements").mkdir(parents=True); (feature / "plans").mkdir()
        (feature / "README.md").write_text("# Demo\n\n- 状态：development\n- 需求短名：`demo-feature`\n- 工作分支：`main`\n- 基线分支：`main`\n- 最后更新：2026-09-08\n\n说明。\n", encoding="utf-8")
        (feature / "requirements/requirements.md").write_text("# Requirements\n\nBody\n", encoding="utf-8")
        (feature / "plans/implementation.md").write_text("- [ ] T01 Demo\n\n  依赖：无\n", encoding="utf-8")
        (feature / "design").mkdir()
        (feature / "design/design.md").write_text("[附件](attachment.md)\n", encoding="utf-8")
        (feature / "design/attachment.md").write_text("附件正文\n", encoding="utf-8")

    def tearDown(self): self.temp.cleanup()

    def test_workspace_is_json_only_and_read_only(self):
        code, value, stderr = command(self.root, "workspace")
        self.assertEqual(0, code)
        self.assertEqual("", stderr)
        self.assertEqual({"apiVersion", "operation", "status", "observedAt", "root", "revision", "data", "diagnostics"}, set(value))
        self.assertEqual("workspace", value["operation"])
        self.assertEqual("maintenance", value["data"]["mode"])
        self.assertEqual({"mode", "identity", "repositories", "localContext", "configuration", "protocol"}, set(value["data"]))

    def test_feature_has_all_tasks_and_document_round_trips(self):
        code, value, _ = command(self.root, "feature", "demo-feature")
        self.assertEqual(0, code)
        self.assertEqual(1, len(value["data"]["tasks"]))
        code, document, _ = command(self.root, "document", "demo-feature", "--path", "requirements/requirements.md")
        self.assertEqual(0, code)
        self.assertEqual("# Requirements\n\nBody\n", document["data"]["content"])

    def test_projection_reads_new_root_plan_and_activity_container(self):
        feature = self.root / "docs/development/features/demo-feature"
        old_plan = feature / "plans/implementation.md"
        old_plan.unlink()
        old_plan.parent.rmdir()
        (feature / "plan.md").write_text("- [ ] T01 Demo\n\n  依赖：无\n", encoding="utf-8")
        (feature / ".work-item.json").write_text(
            json.dumps(
                {
                    "schemaVersion": 1,
                    "currentBatchId": "b01",
                    "batches": [
                        {"id": "b01", "workKind": "handoff", "riskTier": "light", "status": "active"}
                    ],
                }
            ),
            encoding="utf-8",
        )

        code, summary, _ = command(self.root, "projection", "demo-feature", "--view", "summary")
        self.assertEqual(0, code)
        self.assertEqual("handoff", summary["data"]["summary"]["workItem"]["currentBatch"]["workKind"])
        self.assertTrue(summary["data"]["summary"]["planSummary"]["exists"])

        code, task, _ = command(self.root, "projection", "demo-feature", "--view", "task", "--task", "T01")
        self.assertEqual(0, code)
        self.assertEqual("plan.md", task["data"]["tasks"][0]["path"])

        code, change, _ = command(self.root, "projection", "demo-feature", "--view", "change")
        self.assertEqual(0, code)
        self.assertEqual("not_checked", change["data"]["comparison"]["state"])

    def test_maintenance_projection_preserves_each_repository_branch(self):
        from unittest.mock import patch

        feature = self.root / "docs/development/features/demo-feature"
        (feature / "README.md").write_text(
            "# Demo\n\n- 状态：development\n- 需求短名：`demo-feature`\n"
            f"- 涉及仓库：`{self.root.name}`、`plugin`\n"
            f"- 工作分支：`{self.root.name}` -> `main`；`plugin` -> `feature/demo`\n"
            f"- 基线分支：`{self.root.name}` -> `main`；`plugin` -> `main`\n"
            "- 最后更新：2026-09-08\n",
            encoding="utf-8",
        )
        with patch("workspace_status.maintenance_repository_path", side_effect=lambda root, name: root if name == root.name else root.parent / "plugin"):
            from workspace_inspect import projection
            result = projection(self.root.resolve(), "demo-feature", "change")
        self.assertEqual(
            [(self.root.name, "main", "main"), ("plugin", "main", "feature/demo")],
            [(item["repository"], item["baseBranch"], item["workBranch"]) for item in result["repositories"]],
        )

    def test_feature_exposes_trusted_plan_state(self):
        source = self.root / "source.py"
        source.write_text("value = 1\n", encoding="utf-8")
        tests = self.root / "tests"
        tests.mkdir()
        (tests / "test_source.py").write_text("def test_source(): pass\n", encoding="utf-8")
        feature = self.root / "docs/development/features/demo-feature"
        (feature / "plans/implementation.md").write_text(
            "- 完成门禁：`task-evidence-v1`\n\n"
            "- [x] T01 Demo\n\n"
            "  依赖：无\n"
            f"  目标仓：`{self.root.name}`\n"
            "  验证性质：行为\n\n"
            "  **文件**\n\n"
            "  - Modify：`source.py`（`Source#run`）\n"
            "  - Test：`tests/test_source.py`\n",
            encoding="utf-8",
        )
        (feature / "testing").mkdir()
        (feature / "testing/verification.md").write_text(
            "## 任务证据 T01 2026-09-10T10:00:00Z\n\n"
            "- 交付核对：通过\n"
            f'- 代码状态：{{"{self.root.name}":"sha256:{"a" * 64}"}}\n\n'
            "### 检查 1\n\n"
            "- 类型：测试\n"
            f"- 工作目录：`{self.root}`\n"
            "- 命令：`python3 -m unittest`\n"
            "- 目标：`tests/test_source.py`\n"
            "- 执行数：1\n"
            "- 跳过数：0\n"
            "- 退出状态：0\n"
            "- 结果：通过\n",
            encoding="utf-8",
        )

        code, value, _ = command(self.root, "feature", "demo-feature")

        self.assertEqual(0, code)
        summary = value["data"]["summary"]["planSummary"]
        self.assertEqual("task-evidence-v1", summary["completionPolicy"])
        self.assertEqual(
            {"applicable": True, "completed": 1, "total": 1},
            summary["trustedProgress"],
        )
        task = value["data"]["tasks"][0]
        self.assertEqual(self.root.name, task["repository"])
        self.assertEqual("行为", task["validationKind"])
        self.assertEqual("source.py", task["deliverables"][0]["path"])
        self.assertTrue(task["trusted"])

        def git(*args):
            subprocess.run(["git", "-C", str(self.root), *args], check=True, capture_output=True)

        git("init", "-q", "-b", "main")
        git("add", ".")
        git("-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "Feature")
        git("checkout", "-qb", "another-feature")
        git("rm", "source.py", "tests/test_source.py")
        git("-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "Other feature")

        def assert_progress(expected, diagnostic=None):
            for operation in (("feature", "demo-feature"), ("features",), ("verification", "demo-feature")):
                with self.subTest(operation=operation, expected=expected):
                    code, value, _ = command(self.root, *operation)
                    self.assertEqual(0, code)
                    if operation[0] == "verification":
                        self.assertEqual(bool(expected), value["data"]["taskEvidence"][0]["trusted"])
                        continue
                    summary = value["data"]["summary"] if operation[0] == "feature" else value["data"]["items"][0]
                    self.assertEqual(expected, summary["planSummary"]["trustedProgress"]["completed"])
                    if diagnostic:
                        self.assertIn(diagnostic, {d["code"] for d in summary["planSummary"]["diagnostics"]})

        import workspace_evidence
        import workspace_status

        for policy in ("task-evidence-v1", "task-evidence-v2"):
            with self.subTest(policy=policy):
                if policy == "task-evidence-v2":
                    git("checkout", "-q", "main")
                    workspace_evidence.migrate_feature(feature, preview=False)
                    git("checkout", "-q", "another-feature")
                before = {p.relative_to(self.root): p.read_bytes()
                          for p in self.root.rglob("*") if p.is_file() and ".git" not in p.relative_to(self.root).parts}
                assert_progress(1)
                after = {p.relative_to(self.root): p.read_bytes()
                         for p in self.root.rglob("*") if p.is_file() and ".git" not in p.relative_to(self.root).parts}
                self.assertEqual(before, after)
                self.assertFalse(source.exists())
                self.assertEqual("another-feature", subprocess.check_output(
                    ["git", "-C", str(self.root), "branch", "--show-current"], text=True).strip())
                # Execution keeps checking the actual worktree, not the browsing branch.
                self.assertEqual(0, workspace_status.status_result(self.root)["features"][0]["trustedProgress"]["completed"])
                git("checkout", "-q", "--detach")
                assert_progress(1)
                git("checkout", "-q", "main")
                source.unlink()
                assert_progress(0, "TASK_DELIVERABLE_MISSING")
                source.write_text("value = 1\n", encoding="utf-8")
                assert_progress(1)
                git("checkout", "-q", "another-feature")
                readme = feature / "README.md"
                original = readme.read_text(encoding="utf-8")
                readme.write_text(original.replace("- 工作分支：`main`", "- 工作分支：`missing-feature`"), encoding="utf-8")
                assert_progress(0, "TASK_DELIVERABLE_REF_UNAVAILABLE")
                readme.write_text(original, encoding="utf-8")

    def test_maintenance_verification_checks_each_sibling_repository(self):
        import workspace_evidence
        import workspace_verification

        def git(path, *args):
            subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True)

        git(self.root, "init", "-q", "-b", "main")
        sibling = self.root.parent / "plugin"
        sibling.mkdir()
        git(sibling, "init", "-q", "-b", "main")
        (sibling / "app.py").write_text("value = 1\n", encoding="utf-8")
        for path in (self.root, sibling):
            git(path, "add", ".")
            git(path, "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "fixture")
        feature = self.root / "docs/development/features/demo-feature"
        (feature / "README.md").write_text(
            "# Demo\n\n- 状态：development\n- 需求短名：`demo-feature`\n"
            "- 涉及仓库：`kit`、`plugin`\n"
            "- 工作分支：`kit` -> `main`；`plugin` -> `main`\n"
            "- 基线分支：`kit` -> `main`；`plugin` -> `main`\n"
            "- 最后更新：2026-09-08\n", encoding="utf-8"
        )
        (feature / "plans/implementation.md").write_text(
            "- 完成门禁：`task-evidence-v2`\n\n- [ ] T01 Demo\n", encoding="utf-8"
        )
        from workspace_status import maintenance_repository_path
        item = {"repositories": ["kit", "plugin"], "path": feature.relative_to(self.root).as_posix()}
        states = workspace_verification.feature_code_state(self.root, "maintenance", item)
        workspace_evidence.record(feature, {
            "kind": "verificationBatch", "recordedAt": "2026-09-13T10:00:00Z",
            "overallResult": "passed", "reviewResult": "passed", "codeState": states,
            "checks": [{"workingDirectory": str(sibling), "command": "true", "exitStatus": 0, "result": "passed", "duration": "1s", "testCount": 1}],
            "blockers": [], "artifactRefs": [],
        })
        code, response, _ = command(self.root, "verification", "demo-feature", "--check-code")
        self.assertEqual(0, code, response)
        rows = {row["repository"]: row for row in response["data"]["repositoryStates"]}
        self.assertEqual("matched", rows["plugin"]["state"])
        self.assertEqual("valid", response["data"]["applicability"])
        (sibling / "app.py").write_text("value = 2\n", encoding="utf-8")
        code, response, _ = command(self.root, "verification", "demo-feature", "--check-code")
        self.assertEqual(0, code, response)
        rows = {row["repository"]: row for row in response["data"]["repositoryStates"]}
        self.assertEqual("changed", rows["plugin"]["state"])
        self.assertEqual("invalid", response["data"]["applicability"])
        self.assertEqual(sibling.resolve(), maintenance_repository_path(self.root, "plugin"))

    def test_v2_verification_is_projected_to_the_inspect_contract(self):
        import workspace_evidence
        from schema_validation import validate

        feature = self.root / "docs/development/features/demo-feature"
        (feature / "plans/implementation.md").write_text(
            "- 完成门禁：`task-evidence-v2`\n\n- [ ] T01 Demo\n\n  依赖：无\n",
            encoding="utf-8",
        )
        workspace_evidence.record(
            feature,
            {
                "kind": "verificationBatch",
                "recordedAt": "2026-09-13T10:00:00+08:00",
                "overallResult": "passed",
                "reviewResult": "passed",
                "codeState": {self.root.name: "sha256:" + "a" * 64},
                "checks": [{
                    "workingDirectory": str(self.root),
                    "command": "python3 -m unittest",
                    "exitStatus": 0,
                    "result": "passed",
                    "duration": "1s",
                    "testCount": 1,
                }],
                "blockers": [],
                "artifactRefs": [],
            },
        )

        code, response, _ = command(self.root, "verification", "demo-feature")

        self.assertEqual(0, code)
        schema = json.loads((ROOT / "schemas/inspect-result.schema.json").read_text(encoding="utf-8"))
        validate(response["data"], {"$defs": schema["$defs"], "$ref": "#/$defs/verification"})
        selected = response["data"]["selectedBatch"]
        self.assertEqual("passed", selected["recordedResult"])
        self.assertEqual("complete", selected["completeness"])
        self.assertEqual("0", selected["checks"][0]["exitStatus"])
        self.assertTrue(response["data"]["batches"][0]["source"]["path"].startswith("testing/evidence/batches/"))

    def test_v2_feature_without_evidence_index_is_listed(self):
        feature = self.root / "docs/development/features/demo-feature"
        (feature / "plans/implementation.md").write_text(
            "- 完成门禁：`task-evidence-v2`\n\n- [ ] T01 Demo\n\n  依赖：无\n",
            encoding="utf-8",
        )

        code, response, _ = command(self.root, "features")

        self.assertEqual(0, code)
        self.assertEqual("ok", response["status"])
        self.assertEqual(["demo-feature"], [item["slug"] for item in response["data"]["items"]])
        self.assertFalse(response["data"]["items"][0]["verificationSummary"]["exists"])

    def test_new_activity_documents_affect_revisions_handoff_and_linked_search(self):
        feature = self.root / "docs/development/features/demo-feature"
        (feature / "README.md").write_text(
            (feature / "README.md").read_text(encoding="utf-8") + "\n[变更](change.md)\n", encoding="utf-8"
        )
        (feature / "change.md").write_text("# 变更\n\nneedle-in-change\n\n[附件](notes.md)\n", encoding="utf-8")
        (feature / "notes.md").write_text("needle-in-notes\n", encoding="utf-8")
        (feature / ".work-item.json").write_text(json.dumps({
            "schemaVersion": 1, "currentBatchId": "b01",
            "batches": [{"id": "b01", "workKind": "handoff", "riskTier": "normal", "status": "active"}],
        }), encoding="utf-8")
        _, detail, _ = command(self.root, "feature", "demo-feature")
        first = detail["data"]["featureRevision"]
        _, collection, _ = command(self.root, "features")
        collection_revision = collection["revision"]
        self.assertIn("change.md", {row["path"] for row in detail["data"]["files"]})
        for path in ("change.md", "notes.md"):
            with self.subTest(path=path):
                code, response, _ = command(self.root, "document", "demo-feature", "--path", path)
                self.assertEqual(0, code, response)
        code, hidden, _ = command(self.root, "document", "demo-feature", "--path", ".work-item.json")
        self.assertEqual(1, code)
        self.assertEqual("INSPECT_UNSAFE_PATH", hidden["diagnostics"][0]["code"])
        code, handoff, _ = command(self.root, "handoff", "demo-feature")
        self.assertEqual(0, code, handoff)
        self.assertIn("change.md", {source["path"] for source in handoff["data"]["sources"]})
        for term, expected in (("needle-in-change", "change.md"), ("needle-in-notes", "notes.md")):
            code, found, _ = command(self.root, "search", "--query", term)
            self.assertEqual(0, code, found)
            self.assertIn(expected, {row["path"] for row in found["data"]["items"]})
        (feature / "change.md").write_text("# 变更\n\nupdated\n\n[附件](notes.md)\n", encoding="utf-8")
        _, changed, _ = command(self.root, "feature", "demo-feature")
        self.assertNotEqual(first, changed["data"]["featureRevision"])
        _, changed_collection, _ = command(self.root, "features")
        self.assertNotEqual(collection_revision, changed_collection["revision"])
        second = changed["data"]["featureRevision"]
        work_item = feature / ".work-item.json"
        work_item.write_text(work_item.read_text(encoding="utf-8").replace('"normal"', '"light"'), encoding="utf-8")
        _, changed_again, _ = command(self.root, "feature", "demo-feature")
        self.assertNotEqual(second, changed_again["data"]["featureRevision"])
        _, flow, _ = command(self.root, "projection", "demo-feature", "--view", "flow")
        self.assertEqual(changed_again["data"]["featureRevision"], flow["data"]["featureRevision"])

    def test_new_handoff_without_plan_and_unsafe_activity_file(self):
        feature = self.root / "docs/development/features/demo-feature"
        (feature / "plans/implementation.md").unlink()
        (feature / "change.md").write_text("# 接手范围\n", encoding="utf-8")
        (feature / ".work-item.json").write_text(json.dumps({
            "schemaVersion": 1, "currentBatchId": "b01",
            "batches": [{"id": "b01", "workKind": "handoff", "riskTier": "normal", "status": "active"}],
        }), encoding="utf-8")
        code, handoff, _ = command(self.root, "handoff", "demo-feature")
        self.assertEqual(0, code, handoff)
        self.assertEqual("feature.context", handoff["data"]["progression"]["currentStage"])
        self.assertIn("change.md", {item["path"] for item in handoff["data"]["sources"]})
        (feature / "change.md").unlink()
        (feature / "change.md").symlink_to(self.root / "outside.md")
        code, detail, _ = command(self.root, "feature", "demo-feature")
        self.assertEqual(1, code)
        self.assertEqual("INSPECT_UNSAFE_PATH", detail["diagnostics"][0]["code"])

    def test_feature_collection_revision_is_page_independent(self):
        _, first, _ = command(self.root, "features", "--offset", "0", "--limit", "1")
        _, second, _ = command(self.root, "features", "--offset", "1", "--limit", "1")
        self.assertEqual(first["revision"], second["revision"])

    def test_unsafe_document_and_unknown_major_are_structured_errors(self):
        code, value, _ = command(self.root, "document", "demo-feature", "--path", "../AGENTS.md")
        self.assertEqual(1, code)
        self.assertEqual("INSPECT_UNSAFE_PATH", value["diagnostics"][0]["code"])
        result = subprocess.run([sys.executable, str(SCRIPTS / "kit.py"), "inspect", "--root", str(self.root), "--api-major", "9", "--json", "workspace"], capture_output=True, text=True, check=False)
        self.assertEqual(2, result.returncode)
        self.assertEqual("INSPECT_UNSUPPORTED_VERSION", json.loads(result.stdout)["diagnostics"][0]["code"])

    def test_changed_document_revision_is_handled_read_error(self):
        code, document, _ = command(self.root, "document", "demo-feature", "--path", "README.md", "--revision", "sha256:" + "0" * 64)
        self.assertEqual(1, code)
        self.assertEqual(str(self.root.resolve()), document["root"])
        self.assertEqual("INSPECT_REVISION_CHANGED", document["diagnostics"][0]["code"])

    def test_schema_accepts_actual_workspace_response(self):
        from schema_validation import validate
        schema = json.loads((ROOT / "schemas/inspect-result.schema.json").read_text(encoding="utf-8"))
        queries = {
            "workspace": ("workspace",),
            "features": ("features",),
            "feature": ("feature", "demo-feature"),
            "document": ("document", "demo-feature", "--path", "README.md"),
            "verification": ("verification", "demo-feature"),
            "workflow": ("workflow",),
            "runs": ("runs",),
        }
        for operation, args in queries.items():
            with self.subTest(operation=operation):
                code, response, _ = command(self.root, *args)
                self.assertEqual(0, code)
                validate(response, schema)
                definition = "list" if operation in {"features", "runs"} else operation
                validate(response["data"], {"$defs": schema["$defs"], "$ref": f"#/$defs/{definition}"})

    def test_linked_feature_attachment_is_readable(self):
        code, value, _ = command(self.root, "document", "demo-feature", "--path", "design/attachment.md")
        self.assertEqual(0, code)
        self.assertEqual("附件正文\n", value["data"]["content"])

    def test_history_documents_are_discoverable_and_readable_transitively(self):
        feature = self.root / "docs/development/features/demo-feature"
        (feature / "README.md").write_text(
            (feature / "README.md").read_text(encoding="utf-8")
            + "\n[历史迭代](history/index.md)\n",
            encoding="utf-8",
        )
        iteration = feature / "history/i01"
        iteration.mkdir(parents=True)
        (feature / "history/index.md").write_text(
            "# 历史迭代\n\n- [i01](i01/README.md)\n", encoding="utf-8"
        )
        (iteration / "README.md").write_text(
            "# i01\n\n[实施计划](plans/implementation.md)\n", encoding="utf-8"
        )
        (iteration / "plans").mkdir()
        (iteration / "plans/implementation.md").write_text(
            "# i01 实施计划\n\n[设计](../design/design.md)\n\narchived-only-token\n",
            encoding="utf-8",
        )
        (iteration / "design").mkdir()
        (iteration / "design/design.md").write_text("# i01 设计\n", encoding="utf-8")

        code, feature_value, _ = command(self.root, "feature", "demo-feature")
        self.assertEqual(0, code)
        listed = {item["path"] for item in feature_value["data"]["files"]}
        self.assertIn("history/index.md", listed)
        self.assertIn("history/i01/README.md", listed)
        self.assertIn("history/i01/plans/implementation.md", listed)
        self.assertIn("history/i01/design/design.md", listed)

        code, document_value, _ = command(
            self.root,
            "document",
            "demo-feature",
            "--path",
            "history/i01/plans/implementation.md",
        )
        self.assertEqual(0, code)
        self.assertIn("archived-only-token", document_value["data"]["content"])

        code, design_value, _ = command(
            self.root,
            "document",
            "demo-feature",
            "--path",
            "history/i01/design/design.md",
        )
        self.assertEqual(0, code)
        self.assertEqual("# i01 设计\n", design_value["data"]["content"])

        code, search_value, _ = command(
            self.root, "search", "--query", "archived-only-token"
        )
        self.assertEqual(0, code)
        self.assertEqual([], search_value["data"]["items"])

        code, handoff_value, _ = command(self.root, "handoff", "demo-feature")
        self.assertEqual(0, code)
        self.assertNotIn("archived-only-token", handoff_value["data"]["content"])

    def test_unlinked_history_document_is_not_readable(self):
        feature = self.root / "docs/development/features/demo-feature"
        hidden = feature / "history/i01/hidden.md"
        hidden.parent.mkdir(parents=True)
        hidden.write_text("hidden\n", encoding="utf-8")
        (feature / "design/design.md").write_text(
            "[不允许绕过历史索引](../history/i01/hidden.md)\n", encoding="utf-8"
        )

        code, value, _ = command(
            self.root, "document", "demo-feature", "--path", "history/i01/hidden.md"
        )

        self.assertEqual(1, code)
        self.assertEqual("INSPECT_UNSAFE_PATH", value["diagnostics"][0]["code"])

    def test_oversized_run_is_partial_and_read_only(self):
        run = self.root / ".workspace/runs/big-run.json"
        run.parent.mkdir(parents=True)
        run.write_bytes(b"{" + b"x" * (1024 * 1024 + 1))
        before = run.read_bytes()
        code, value, _ = command(self.root, "runs")
        self.assertEqual(0, code)
        self.assertEqual("partial", value["status"])
        self.assertEqual("INSPECT_INVALID_DATA", value["diagnostics"][0]["code"])
        self.assertEqual(before, run.read_bytes())


if __name__ == "__main__":
    unittest.main()
