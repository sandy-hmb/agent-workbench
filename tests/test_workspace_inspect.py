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
