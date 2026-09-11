"""消费者依赖的查询语义：仓库身份、来源、版本和阅读定位。"""
from __future__ import annotations

import copy
import json
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from schema_validation import SchemaValidationError, validate


class InspectContractReviewTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve() / "kit"
        self.root.mkdir()
        (self.root / "VERSION").write_text("7.8.9\n")
        self.config = {
            "version": {"major": 1, "minor": 0}, "workspace": {"name": "样例"},
            "context": {}, "branchPolicy": {"workBase": "main", "testTarget": "testing"},
            "extensions": {"providers": {}, "config": {}},
            "repositories": [{"path": "service", "aliases": [], "remote": None,
                "category": "backend", "description": "样例服务",
                "instruction": "docs/repositories/service.md", "branchPolicy": {"testTarget": None}}],
        }
        for path in (self.root, self.root.parent / "service"):
            path.mkdir(exist_ok=True)
            subprocess.run(["git", "init", "-q", str(path)], check=True, capture_output=True)
        self.write(".workspace/workspace.json", json.dumps(self.config))
        self.write(".workspace/docs/repositories/service.md", "# 样例服务\n")
        self.feature = ".workspace/docs/features/demo"
        self.write(f"{self.feature}/README.md", "# 样例需求\n\n- 状态：development\n- 涉及仓库：service\n- 工作分支：service -> feature/demo\n- 基线分支：service -> main\n- 最后更新：2026-09-08\n\n需求说明。\n")
        self.write(f"{self.feature}/plans/implementation.md", "# 实施计划\n\n- [ ] T01 首项任务\n")
        self.write(f"{self.feature}/design/design.md", "# 设计\n\n[附件](attachment.md)\n")
        self.write(f"{self.feature}/design/attachment.md", "# 附件\n\n附件正文。\n")

    def write(self, relative, text):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def query(self, *args, exitcode=0):
        result = subprocess.run([sys.executable, "-B", str(ROOT / "scripts/kit.py"),
            "inspect", "--root", str(self.root), "--json", *args],
            text=True, capture_output=True, timeout=15)
        self.assertEqual(exitcode, result.returncode, result.stdout)
        self.assertEqual("", result.stderr)
        return json.loads(result.stdout)

    def test_workspace_keeps_kit_identity_and_field_level_policy_provenance(self):
        data = self.query("workspace")["data"]
        kit = [r for r in data["repositories"] if r["role"] == "kit"]
        self.assertEqual(1, len(kit))
        self.assertEqual(str(self.root), kit[0]["absolutePath"])
        service = next(r for r in data["repositories"] if r["id"] == "service")
        self.assertIsNone(service["effectiveBranchPolicy"]["testTarget"])
        sources = service["policySources"]
        self.assertEqual("repository", sources["testTarget"]["source"])
        self.assertEqual("/repositories/0/branchPolicy/testTarget", sources["testTarget"]["pointer"])
        self.assertEqual("workspace", sources["workBase"]["source"])
        self.assertEqual("kit-default", sources["namePattern"]["source"])
        # 实际提供 Inspect 的 Kit 版本来自 VERSION；不能回传 workspace schema 主版本。
        self.assertIn(data["protocol"]["kitVersion"], {"7.8.9", (ROOT / "VERSION").read_text().strip()})

    def test_pagination_defaults_and_source_changes_are_observable(self):
        first = self.query("features")
        self.assertEqual(100, first["data"]["page"]["limit"])
        self.assertEqual(100, self.query("runs")["data"]["page"]["limit"])
        self.write(f"{self.feature}/plans/implementation.md", "# 实施计划\n\n- [ ] T01 修改后的任务标题\n")
        second = self.query("features")
        self.assertNotEqual(first["revision"], second["revision"])

    def test_linked_document_is_listed_and_tasks_reference_its_plan_revision(self):
        feature = self.query("feature", "demo")["data"]
        files = {f["path"]: f for f in feature["files"]}
        self.assertIn("design/attachment.md", files)
        plan = self.query("document", "demo", "--path", "plans/implementation.md")["data"]
        task = feature["tasks"][0]
        self.assertIn(plan["revision"], json.dumps(task))
        self.assertEqual("text/markdown", plan["mediaType"])

    def test_inspect_preserves_claimed_and_trusted_progress(self):
        service = self.root.parent / "service"
        (service / "source.py").write_text("value = 1\n")
        tests = service / "tests"
        tests.mkdir()
        (tests / "test_source.py").write_text("def test_source(): pass\n")
        self.write(
            f"{self.feature}/plans/implementation.md",
            "- 完成门禁：`task-evidence-v1`\n\n"
            "- [x] T01 已声明完成\n\n"
            "  依赖：无\n"
            "  目标仓：`service`\n"
            "  验证性质：行为\n\n"
            "  **文件**\n\n"
            "  - Modify：`source.py`\n"
            "  - Test：`tests/test_source.py`\n",
        )
        self.write(
            f"{self.feature}/testing/verification.md",
            "## 任务证据 T01 2026-09-10T10:00:00Z\n\n"
            "- 交付核对：通过\n"
            f'- 代码状态：{{"service":"sha256:{"a" * 64}"}}\n\n'
            "### 检查 1\n\n"
            "- 类型：测试\n"
            f"- 工作目录：`{service}`\n"
            "- 命令：`python3 -m unittest`\n"
            "- 目标：`tests/test_source.py`\n"
            "- 执行数：1\n"
            "- 跳过数：0\n"
            "- 退出状态：0\n"
            "- 结果：通过\n",
        )

        feature = self.query("feature", "demo")["data"]
        verification = self.query("verification", "demo")["data"]

        self.assertEqual(1, feature["summary"]["planSummary"]["completed"])
        self.assertEqual(
            1,
            feature["summary"]["planSummary"]["trustedProgress"]["completed"],
        )
        self.assertTrue(feature["tasks"][0]["trusted"])
        self.assertTrue(verification["taskEvidence"][0]["trusted"])
        schema = json.loads((ROOT / "schemas/inspect-result.schema.json").read_text())
        validate(feature, {"$defs": schema["$defs"], "$ref": "#/$defs/feature"})
        validate(
            verification,
            {"$defs": schema["$defs"], "$ref": "#/$defs/verification"},
        )

    def test_schema_rejects_invalid_repository_and_verification_states(self):
        schema = json.loads((ROOT / "schemas/inspect-result.schema.json").read_text())
        for operation, args, mutate in [
            ("workspace", ("workspace",), lambda d: d["repositories"][0].update(role="anything")),
            ("verification", ("verification", "demo"), lambda d: d.update(applicability="anything")),
        ]:
            with self.subTest(operation=operation):
                data = self.query(*args)["data"]
                shape = {"$defs": schema["$defs"], "$ref": f"#/$defs/{operation}"}
                validate(data, shape)
                wrong = copy.deepcopy(data)
                mutate(wrong)
                with self.assertRaises(SchemaValidationError):
                    validate(wrong, shape)

    def test_argument_error_envelope_also_satisfies_schema(self):
        data = self.query("unsupported-operation", exitcode=2)
        schema = json.loads((ROOT / "schemas/inspect-result.schema.json").read_text())
        self.assertEqual("error", data["status"])
        validate(data, schema)

    def test_registered_bad_or_oversized_readme_is_partial_without_hiding_sibling(self):
        self.write(".workspace/docs/features/large/README.md", "x" * (1024 * 1024 + 1))
        self.write(".workspace/docs/features/bad/README.md", "# Bad\n\n- 状态：unknown\n")
        value = self.query("features")
        self.assertEqual("partial", value["status"])
        self.assertIn("demo", {item["slug"] for item in value["data"]["items"]})
        self.assertGreaterEqual(value["data"]["counts"]["diagnostics"], 2)

    def test_run_rejects_unvalidated_fields_instead_of_echoing_them(self):
        raw = {"schemaVersion": 1, "id": "unsafe", "workflow": "feature-development",
               "featureSlug": None, "repository": None, "branch": None, "stages": {},
               "environment": {"API_TOKEN": "synthetic-value-must-not-be-echoed"}}
        self.write(".workspace/runs/unsafe.json", json.dumps(raw))
        run = self.query("run", "unsafe", exitcode=1)
        self.assertEqual("error", run["status"])
        self.assertNotIn("synthetic-value-must-not-be-echoed", json.dumps(run))
        listing = self.query("runs")
        self.assertEqual("partial", listing["status"])
        self.assertEqual([], listing["data"]["items"])

    def test_workspace_file_limit_applies_before_configuration_parsing(self):
        self.write(".workspace/workspace.json", json.dumps(self.config) + " " * (1024 * 1024))
        response = self.query("workspace", exitcode=1)
        self.assertEqual("error", response["status"])
        self.assertIn("INSPECT_LIMIT_EXCEEDED", [d["code"] for d in response["diagnostics"]])

    def test_handoff_returns_current_task_progression_and_versioned_sources(self):
        self.write(
            f"{self.feature}/requirements/requirements.md",
            "# 需求\n\n## R1 可观察行为\n\n订单查询必须隔离租户。\n",
        )
        self.write(
            f"{self.feature}/design/design.md",
            "# 设计\n\n<a id=\"d1\"></a>\n## D1 查询方案\n\n按租户过滤。\n",
        )
        self.write(
            f"{self.feature}/plans/implementation.md",
            "# 计划\n\n- [ ] T01 实现隔离查询\n\n"
            "  依据：R1、[D1](../design/design.md#d1)\n"
            "  依赖：无\n  目标仓：`service`\n  验证性质：行为\n\n"
            "  **文件**\n\n  - Modify：`source.py`（`query`）\n",
        )

        response = self.query("handoff", "demo")
        data = response["data"]

        self.assertEqual("demo", data["slug"])
        self.assertEqual("T01", data["currentTask"]["id"])
        self.assertIn("T01 实现隔离查询", data["currentTask"]["body"])
        self.assertEqual("feature.implement", data["progression"]["currentStage"])
        sources = {item["path"]: item for item in data["sources"]}
        for path in (
            "README.md",
            "requirements/requirements.md",
            "design/design.md",
            "plans/implementation.md",
        ):
            self.assertRegex(sources[path]["revision"], r"^sha256:[0-9a-f]{64}$")
        self.assertGreater(data["estimatedTokens"], 0)
        schema = json.loads((ROOT / "schemas/inspect-result.schema.json").read_text())
        validate(data, {"$defs": schema["$defs"], "$ref": "#/$defs/handoff"})

    def test_search_filters_history_and_reports_bad_documents_as_partial(self):
        self.write(
            f"{self.feature}/requirements/requirements.md",
            "# 月度佣金\n\n## R1 租户核算\n\nPartner Commission 必须按租户隔离。\n",
        )
        history = ".workspace/docs/features/history"
        self.write(
            f"{history}/README.md",
            "# 历史佣金\n\n- 状态：done\n- 涉及仓库：service\n"
            "- 工作分支：service -> feature/history\n- 基线分支：service -> main\n"
            "- 最后更新：2026-09-01\n",
        )
        self.write(
            f"{history}/design/design.md",
            "# 历史设计\n\n## Partner Commission\n\n使用旧佣金规则。\n",
        )
        bad = self.root / f"{history}/requirements/requirements.md"
        bad.parent.mkdir(parents=True, exist_ok=True)
        bad.write_bytes(b"\xff")

        response = self.query(
            "search", "--query", "partner commission", "--repo", "service",
            "--offset", "0", "--limit", "1",
        )

        self.assertEqual("partial", response["status"])
        self.assertEqual(1, len(response["data"]["items"]))
        self.assertTrue(response["data"]["page"]["hasMore"])
        self.assertEqual("heading", response["data"]["items"][0]["matchKind"])
        self.assertIn("INSPECT_INVALID_DATA", {d["code"] for d in response["diagnostics"]})

        done = self.query("search", "--query", "partner", "--status", "done")
        self.assertEqual(["history"], sorted({item["slug"] for item in done["data"]["items"]}))
        self.assertNotEqual(response["revision"], done["revision"])
        schema = json.loads((ROOT / "schemas/inspect-result.schema.json").read_text())
        validate(done["data"], {"$defs": schema["$defs"], "$ref": "#/$defs/search"})

    def test_scan_stops_at_the_entry_limit_without_consuming_the_directory(self):
        import workspace_inspect
        yielded = []
        def entries():
            for index in range(100):
                yielded.append(index)
                yield Path(str(index))
        with mock.patch.object(workspace_inspect, "MAX_SCAN", 2):
            with self.assertRaises(workspace_inspect.InspectError):
                workspace_inspect._scan(entries(), workspace_inspect.Deadline(1))
        self.assertEqual([0, 1, 2], yielded)

    def test_verification_preserves_each_repository_and_original_success_rule(self):
        from workspace_verification import git_fingerprint
        repositories = [self.root.parent / name for name in ("service", "web")]
        for path in repositories:
            path.mkdir(exist_ok=True)
            commands = [["init", "-q"], ["checkout", "-qb", "feature/demo"],
                        ["add", "."], ["-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "fixture"]]
            (path / "source.txt").write_text("initial\n")
            for args in commands:
                subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True)
        self.config["repositories"].append({**self.config["repositories"][0], "path": "web", "instruction": "docs/repositories/web.md"})
        self.write(".workspace/workspace.json", json.dumps(self.config))
        self.write(".workspace/docs/repositories/web.md", "# Web\n")
        readme = (self.root / self.feature / "README.md").read_text().replace("涉及仓库：service", "涉及仓库：service, web").replace("service -> feature/demo", "service -> feature/demo；web -> feature/demo").replace("service -> main", "service -> main；web -> main")
        self.write(f"{self.feature}/README.md", readme)
        recorded = {p.name: git_fingerprint(p) for p in repositories}
        record = "## 验证批次 2026-09-08T10:00:00Z\n- 总体结果：通过\n- 审查结论：通过\n- 代码状态：" + json.dumps(recorded) + "\n\n### 检查 1\n- 工作目录：../service\n- 命令：true\n- 退出状态：0\n- 结果：通过\n"
        self.write(f"{self.feature}/testing/verification.md", record)
        initial = self.query("verification", "demo", "--check-code")["data"]
        self.assertEqual("valid", initial["applicability"])
        self.write(f"{self.feature}/testing/verification.md", record.replace("审查结论：通过", "审查结论：失败"))
        failed_review = self.query("verification", "demo", "--check-code")["data"]
        self.assertEqual("invalid", failed_review["applicability"])
        self.write(f"{self.feature}/testing/verification.md", record)
        (repositories[0] / "source.txt").write_text("changed\n")
        changed = self.query("verification", "demo", "--check-code")["data"]
        self.assertEqual({"service": "changed", "web": "matched"}, {r["repository"]: r["state"] for r in changed["repositoryStates"]})
        # 第二仓不可读时不能丢失第一仓已确定的 changed，也不能把整体降为未知。
        repositories[1].rename(repositories[1].with_name("web-unavailable"))
        missing = self.query("verification", "demo", "--check-code")["data"]
        self.assertEqual("invalid", missing["applicability"])
        self.assertEqual({"service": "changed", "web": "unknown"}, {r["repository"]: r["state"] for r in missing["repositoryStates"]})


if __name__ == "__main__":
    unittest.main()
