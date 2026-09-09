"""八项操作的真实 CLI 契约样例；仅使用临时合成工作区。"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "scripts"), str(ROOT / "tests")]
import test_workspace_workflow as workflow_tests
import workspace_workflow
from schema_validation import validate


class InspectExamplesTest(unittest.TestCase):
    def setUp(self):
        self.fixture = workflow_tests.WorkspaceWorkflowTest()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.root = self.fixture.root.resolve()
        self.fixture.write_overlay([{"id": "quality.integration", "after": "feature.implement",
            "uses": "action-extension/integration-test", "trigger": "manual"}])
        self.fixture.activate_overlay()
        state = self.root / ".workspace"
        feature = state / "docs/features/demo"
        for path, text in {
            "README.md": "# 合成需求\n\n- 状态：development\n- 涉及仓库：service\n- 工作分支：service -> feature/demo\n- 基线分支：service -> main\n- 需求审阅：已批准\n- 设计审阅：已批准\n- 计划审阅：已批准\n- 最后更新：2026-09-08\n\n这是可重复生成的公开样例。\n",
            "requirements/requirements.md": "# 需求\n\n展示现有工作流记录。\n",
            "design/design.md": "# 设计\n\n[附件](details.md)\n",
            "design/details.md": "# 设计细节\n",
            "plans/implementation.md": "# 实施计划\n\n- [x] T01 创建样例\n\n  依赖：无\n\n- [ ] T02 读取结果\n\n  依赖：T01\n",
            "artifacts/example.sql": "SELECT 1;\n",
            "testing/verification.md": "## 验证批次 2026-09-08T10:00:00Z\n- 总体结果：通过\n- 审查结论：通过\n- 代码状态：{\"service\":\"sha256:" + "a" * 64 + "\"}\n\n### 检查 1\n- 工作目录：../service\n- 命令：python3 -m unittest\n- 退出状态：0\n- 结果：通过\n- 测试数量：3\n",
        }.items():
            target = feature / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text)
        (self.root.parent / "service").mkdir()
        config_path = state / "workspace.json"
        config = json.loads(config_path.read_text())
        config["repositories"] = [{"path": "service", "aliases": [], "remote": None,
            "category": "backend", "description": "合成业务仓", "instruction": "docs/repositories/service.md"}]
        config_path.write_text(json.dumps(config))
        (state / "docs/repositories/service.md").write_text("# 合成业务仓\n")
        raw = {"schemaVersion": 1, "id": "demo-run", "workflow": "feature-development",
            "featureSlug": "demo", "repository": None, "branch": None, "stages": {}}
        _, overlay, _, actions = workspace_workflow._resolve(self.root)
        stage = overlay.stages[0]
        raw["stages"][stage.id] = {"fingerprint": workspace_workflow._stage_fingerprint(stage, actions[stage.uses], raw),
            "status": "succeeded", "updatedAt": "2026-09-08T10:00:00Z", "summary": "样例检查通过"}
        (state / "runs").mkdir(exist_ok=True)
        (state / "runs/demo-run.json").write_text(json.dumps(raw))

    def responses(self):
        def snapshot():
            return {p.relative_to(self.root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in self.root.rglob("*") if p.is_file() and not p.is_symlink()}
        before = snapshot()
        values = {}
        for args in [("workspace",), ("features",), ("feature", "demo"),
            ("document", "demo", "--path", "plans/implementation.md"),
            ("verification", "demo"), ("workflow",), ("runs",), ("run", "demo-run")]:
            result = subprocess.run([sys.executable, "-B", str(ROOT / "scripts/kit.py"), "inspect",
                "--root", str(self.root), "--json", *args], text=True, capture_output=True,
                timeout=15, env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1", GIT_OPTIONAL_LOCKS="0"))
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertEqual("", result.stderr)
            values[args[0]] = json.loads(result.stdout)
        self.assertEqual(before, snapshot(), "Inspect 修改了样例文件或 Git 状态")
        return values

    def test_all_actual_operations_and_published_examples_satisfy_contract(self):
        schema = json.loads((ROOT / "schemas/inspect-result.schema.json").read_text())
        for operation, response in self.responses().items():
            with self.subTest(operation=operation):
                validate(response, schema)
                validate(response["data"], {"$defs": schema["$defs"], "$ref": f"#/$defs/{operation}"})
                example = json.loads((ROOT / f"tests/fixtures/inspect-v1/{operation}.json").read_text())
                validate(example, schema)
                validate(example["data"], {"$defs": schema["$defs"], "$ref": f"#/$defs/{operation}"})

    def test_invalid_overlay_keeps_safe_stage_declarations(self):
        workflow = self.root / ".workspace/workflow.json"
        raw = json.loads(workflow.read_text())
        raw["stages"] = [
            {"id": "quality.a", "after": "quality.b", "uses": "action-extension/integration-test"},
            {"id": "quality.b", "after": "quality.a", "uses": "action-extension/integration-test"},
        ]
        workflow.write_text(json.dumps(raw))
        result = subprocess.run([sys.executable, "-B", str(ROOT / "scripts/kit.py"), "inspect", "--root", str(self.root), "--json", "workflow"], capture_output=True, text=True, timeout=15)
        value = json.loads(result.stdout)["data"]
        self.assertEqual(0, result.returncode)
        self.assertEqual("invalid", value["configState"])
        self.assertIsNone(value["orderedStages"])
        self.assertEqual({"quality.a", "quality.b"}, {item["id"] for item in value["configuredStages"]})

    def test_locked_extension_content_drift_is_reported(self):
        manifest = self.root / ".workspace/extensions/action-extension/workspace-extension.json"
        manifest.write_text(manifest.read_text() + "\n")
        result = subprocess.run([sys.executable, "-B", str(ROOT / "scripts/kit.py"), "inspect", "--root", str(self.root), "--json", "workflow"], capture_output=True, text=True, timeout=15)
        value = json.loads(result.stdout)["data"]
        self.assertEqual(0, result.returncode)
        card = next(item for item in value["extensions"] if item["id"] == "action-extension")
        self.assertEqual("drifted", card["activation"])
        self.assertEqual("1.0.0", card["lockedVersion"])
        self.assertEqual("1.0.0", card["declaredVersion"])


def update_examples():
    fixture = InspectExamplesTest()
    fixture.setUp()
    try:
        destination = ROOT / "tests/fixtures/inspect-v1"
        for operation, response in fixture.responses().items():
            response["observedAt"] = "2026-09-08T10:00:00Z"
            text = json.dumps(response, ensure_ascii=False, indent=2).replace(str(fixture.root.parent), "/synthetic")
            (destination / f"{operation}.json").write_text(text + "\n")
        manifest = {"apiVersion": {"major": 1, "minor": 0}, "source": "真实 CLI 对临时合成工作区的响应",
            "regenerate": "python3 -B tests/test_inspect_examples.py --update-examples",
            "normalization": ["临时根路径替换为 /synthetic", "observedAt 固定为样例时间"],
            "operations": ["workspace", "features", "feature", "document", "verification", "workflow", "runs", "run"]}
        (destination / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    finally:
        fixture.doCleanups()


if __name__ == "__main__":
    if sys.argv[1:] == ["--update-examples"]:
        update_examples()
    else:
        unittest.main()
