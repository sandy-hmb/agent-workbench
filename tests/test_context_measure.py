from __future__ import annotations

import json
import subprocess
import sys
from types import SimpleNamespace
from pathlib import Path
from unittest import mock

import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import context_measure  # noqa: E402


class EstimateTokensTest(unittest.TestCase):
    def test_empty_string(self):
        result = context_measure.estimate_tokens("")
        self.assertEqual(
            result,
            {"bytes": 0, "chars": 0, "cjkChars": 0, "estTokens": 0},
        )

    def test_pure_ascii_multiple_of_four(self):
        result = context_measure.estimate_tokens("a" * 400)
        self.assertEqual(result["bytes"], 400)
        self.assertEqual(result["cjkChars"], 0)
        self.assertEqual(result["estTokens"], 100)

    def test_pure_ascii_rounds_up(self):
        result = context_measure.estimate_tokens("a" * 401)
        self.assertEqual(result["estTokens"], 101)

    def test_pure_cjk(self):
        result = context_measure.estimate_tokens("中" * 50)
        self.assertEqual(result["bytes"], 150)
        self.assertEqual(result["chars"], 50)
        self.assertEqual(result["cjkChars"], 50)
        self.assertEqual(result["estTokens"], 50)

    def test_mixed(self):
        result = context_measure.estimate_tokens("abc" + "中" * 2)
        self.assertEqual(result["bytes"], 3 + 2 * 3)
        self.assertEqual(result["cjkChars"], 2)
        # ascii "abc" -> 3 bytes -> ceil(3/4) = 1 token; + 2 cjk tokens = 3
        self.assertEqual(result["estTokens"], 3)


class ComponentsAndPathsTest(unittest.TestCase):
    def test_new_observations_use_existing_kit_entrypoint(self):
        self.assertEqual(
            ["kit.py", "describe", "--json"],
            context_measure.COMPONENTS["describe_json"]["argv"],
        )
        self.assertEqual(
            ["kit.py", "brief"],
            context_measure.COMPONENTS["brief_text"]["argv"],
        )
        self.assertIn("lightweight", context_measure.PATHS)
        self.assertIn("standard", context_measure.PATHS)
        self.assertEqual(
            ["status_json", "skill_writing_plan"], context_measure.PATHS["plan"]
        )
        self.assertEqual(
            ["status_json", "skill_execute_plan"], context_measure.PATHS["implement"]
        )
        self.assertIn("resume", context_measure.PATHS)
        self.assertEqual(
            ["kit.py", "brief"],
            context_measure.COMPONENTS["brief_task"]["argv"],
        )
        self.assertEqual(
            ["status_json", "skill_execute_plan", "brief_task"],
            context_measure.PATHS["implement_task"],
        )

    def test_file_components_exist(self):
        for component_id, component in context_measure.COMPONENTS.items():
            if component["kind"] == "file":
                target = ROOT / component["path"]
                self.assertTrue(
                    target.is_file(), f"{component_id} -> {target} 不存在"
                )

    def test_command_components_reference_real_scripts(self):
        for component_id, component in context_measure.COMPONENTS.items():
            if component["kind"] == "command":
                script = ROOT / "scripts" / component["argv"][0]
                self.assertTrue(
                    script.is_file(), f"{component_id} -> {script} 不存在"
                )

    def test_paths_reference_known_components(self):
        for path_name, component_ids in context_measure.PATHS.items():
            for component_id in component_ids:
                self.assertIn(
                    component_id,
                    context_measure.COMPONENTS,
                    f"路径 {path_name} 引用了未知组件 {component_id}",
                )


class BuildReportTest(unittest.TestCase):
    def setUp(self):
        self.report = context_measure.build_report(ROOT)

    def test_top_level_keys(self):
        expected = {
            "schemaVersion",
            "tokenModel",
            "repoState",
            "components",
            "paths",
            "allSkills",
            "scripts",
            "flowScenario",
        }
        self.assertEqual(expected, set(self.report.keys()))

    def test_token_model_values(self):
        self.assertEqual(
            self.report["tokenModel"],
            {"cjkCharsPerToken": 1, "otherBytesPerToken": 4},
        )

    def test_paths_have_bytes_and_tokens(self):
        for name in context_measure.PATHS:
            entry = self.report["paths"][name]
            self.assertIn("bytes", entry)
            self.assertIn("estTokens", entry)
            self.assertGreater(entry["bytes"], 0)

    def test_all_skills_matches_glob_count(self):
        skill_files = sorted(ROOT.glob(".agents/skills/*/SKILL.md"))
        self.assertEqual(len(skill_files), len(self.report["allSkills"]["files"]))
        self.assertGreater(self.report["allSkills"]["bytes"], 0)

    def test_scripts_largest_sorted_descending(self):
        largest = self.report["scripts"]["largest"]
        sizes = [entry["bytes"] for entry in largest]
        self.assertEqual(sizes, sorted(sizes, reverse=True))
        self.assertGreater(self.report["scripts"]["totalBytes"], 0)

    def test_repo_state_active_feature_count_matches_status_component(self):
        status_component = self.report["components"]["status_json"]
        parsed = json.loads(status_component["rawStdout"])
        self.assertEqual(
            self.report["repoState"]["activeFeatureCount"],
            len(parsed.get("features", [])),
        )


class ImplementTaskObservationTest(unittest.TestCase):
    """implement_task 路径必须覆盖任务展开与规范链的真实读取成本。"""

    def _report(self, *, current_task, instruction_context):
        brief_task_payload = json.dumps(
            {
                "selectedTask": {"id": "T01", "body": "任务正文" * 20},
                "instructionContext": instruction_context,
            },
            ensure_ascii=False,
        )

        def readonly(argv, root):
            if argv == ["workspace_status.py", "--root", ".", "--json"]:
                return subprocess.CompletedProcess(
                    argv, 0,
                    json.dumps({"mode": "maintenance", "features": [{"featureSlug": "demo"}]}),
                    "",
                )
            if argv[:2] == ["kit.py", "brief"] and "--task" in argv:
                return subprocess.CompletedProcess(argv, 0, brief_task_payload, "")
            if argv[:2] == ["kit.py", "brief"] and "--execution" in argv:
                return subprocess.CompletedProcess(
                    argv, 0, json.dumps({"currentTask": current_task}), ""
                )
            if argv[:2] == ["kit.py", "brief"]:
                return subprocess.CompletedProcess(argv, 0, "demo（development）\n", "")
            return subprocess.CompletedProcess(argv, 0, "{}", "")

        with mock.patch.object(context_measure, "_run_readonly", side_effect=readonly) as run:
            report = context_measure.build_report(ROOT)
        return report, run

    def test_expands_current_task_and_adds_instruction_source_tokens(self):
        report, run = self._report(
            current_task={"id": "T01"},
            instruction_context={
                "rules": [
                    {"level": 1, "scope": "kit", "path": "AGENTS.md"},
                    {"level": 2, "scope": "workspace", "path": "CONTRIBUTING.md"},
                ]
            },
        )

        component = report["components"]["brief_task"]
        self.assertTrue(component["applicable"])
        self.assertTrue(
            any("--task" in call.args[0] for call in run.call_args_list),
            "必须实际展开当前任务",
        )
        task_calls = [
            call.args[0]
            for call in run.call_args_list
            if "--task" in call.args[0]
        ]
        self.assertTrue(any(["--projection", "execution"] == call[-2:] for call in task_calls))
        self.assertTrue(any("--projection" not in call for call in task_calls))

        # 规范链条目的 token 必须计入，而不是只算 brief 自身输出
        sources = component["instructionSources"]
        self.assertEqual(["AGENTS.md", "CONTRIBUTING.md"], [item["path"] for item in sources])
        self.assertTrue(all(item["estTokens"] > 0 for item in sources))
        self.assertEqual(
            component["estTokens"],
            component["briefEstTokens"] + sum(item["estTokens"] for item in sources),
        )
        self.assertIn("implement_task", report["paths"])
        self.assertEqual(3, report["flowScenario"]["taskCount"])
        self.assertEqual("static estimate", report["flowScenario"]["kind"])
        self.assertGreaterEqual(
            component["fullBriefEstTokens"], component["briefEstTokens"]
        )

    def test_without_current_task_is_not_applicable(self):
        report, _ = self._report(current_task=None, instruction_context=None)

        component = report["components"]["brief_task"]
        self.assertFalse(component["applicable"])
        self.assertEqual(0, component["estTokens"])
        self.assertEqual(
            ["brief_task"], report["paths"]["implement_task"]["notApplicable"]
        )


class OptionalBriefObservationTest(unittest.TestCase):
    def _report_for_status(self, status: dict[str, object]):
        def readonly(argv, root):
            if argv == ["workspace_status.py", "--root", ".", "--json"]:
                return subprocess.CompletedProcess(argv, 0, json.dumps(status), "")
            if argv[:2] == ["kit.py", "brief"]:
                return subprocess.CompletedProcess(argv, 0, "demo-feature（development）\n", "")
            return subprocess.CompletedProcess(argv, 0, "{}", "")

        with mock.patch.object(context_measure, "_run_readonly", side_effect=readonly) as run:
            report = context_measure.build_report(ROOT)
        return report, run

    def test_no_feature_marks_brief_observation_not_applicable(self):
        report, run = self._report_for_status({"mode": "maintenance", "features": []})

        brief = report["components"]["brief_text"]
        self.assertFalse(brief["applicable"])
        self.assertEqual(0, brief["bytes"])
        self.assertEqual("", brief["rawStdout"])
        self.assertFalse(any(call.args[0][:2] == ["kit.py", "brief"] for call in run.call_args_list))
        self.assertEqual(["brief_text"], report["paths"]["resume"]["notApplicable"])

    def test_single_feature_measures_explicit_brief(self):
        report, run = self._report_for_status(
            {"mode": "maintenance", "features": [{"featureSlug": "demo-feature"}]}
        )

        brief = report["components"]["brief_text"]
        self.assertTrue(brief["applicable"])
        self.assertEqual("demo-feature（development）\n", brief["rawStdout"])
        self.assertTrue(any(call.args[0] == ["kit.py", "brief", "demo-feature"] for call in run.call_args_list))
        self.assertEqual(2, report["paths"]["resume"]["commandCalls"])

    def test_multiple_features_without_pointer_skips_brief(self):
        report, run = self._report_for_status(
            {
                "mode": "maintenance",
                "features": [{"featureSlug": "one"}, {"featureSlug": "two"}],
            }
        )

        self.assertFalse(report["components"]["brief_text"]["applicable"])
        self.assertFalse(any(call.args[0][:2] == ["kit.py", "brief"] for call in run.call_args_list))

    def test_workspace_active_feature_measures_selected_brief(self):
        status = {
            "mode": "workspace",
            "features": [{"featureSlug": "one"}, {"featureSlug": "two"}],
        }
        with mock.patch.object(
            context_measure,
            "load_local_settings",
            return_value=SimpleNamespace(active_feature="two"),
        ):
            report, run = self._report_for_status(status)

        self.assertTrue(report["components"]["brief_text"]["applicable"])
        self.assertTrue(any(call.args[0] == ["kit.py", "brief", "two"] for call in run.call_args_list))


class TaskScopedContextTest(unittest.TestCase):
    def test_simple_feature_keeps_required_facts_while_skipping_completed_task_detail(self):
        sections = {
            "requirements": "R1 当前账户只能查询自己的记录。",
            "design": "D1 身份从认证上下文取得。",
            "task": "T02 修改查询服务。依赖：T01。验证：运行定向测试。",
            "completed": "T01 已完成的建表细节与测试输出。" * 20,
            "history": "历史验证批次与过期日志。" * 20,
        }
        full = "\n".join(sections.values())
        scoped = "\n".join(sections[key] for key in ("requirements", "design", "task"))

        self.assertLess(
            context_measure.estimate_tokens(scoped)["estTokens"],
            context_measure.estimate_tokens(full)["estTokens"],
        )
        for required in ("R1", "D1", "T02", "T01", "定向测试"):
            self.assertIn(required, scoped)

    def test_complex_feature_loads_only_the_attachment_required_by_current_task(self):
        sections = {
            "requirements": "R2 Webhook 重复投递不得重复写入。",
            "design": "D1 共享幂等约束。",
            "api": "D2 事件 ID 是本任务消费的幂等键。",
            "task": "T03 接收事件；依赖：T01、T02；验证：回调测试。",
            "data_model": "数据迁移回填、批量校验和回退 SQL。" * 20,
            "rollout": "发布窗口、灰度和人工回退值守。" * 20,
            "history": "已完成任务的详细日志。" * 20,
        }
        full = "\n".join(sections.values())
        scoped = "\n".join(sections[key] for key in ("requirements", "design", "api", "task"))

        self.assertLess(
            context_measure.estimate_tokens(scoped)["estTokens"],
            context_measure.estimate_tokens(full)["estTokens"],
        )
        for required in ("R2", "D1", "D2", "T03", "T01", "T02", "回调测试"):
            self.assertIn(required, scoped)

    def test_progressive_instruction_loading_reuses_session_sources(self):
        sources = {
            "workspace": "工作区共享约束：数据安全、授权边界和跨仓规则。\n" * 20,
            "repository": "目标仓规范入口：目录、测试命令和专项规范索引。\n" * 20,
            "entity": "持久化实体规范：字段、审计信息和业务注释。\n" * 10,
            "openapi": "接口规范：参数校验、契约文档和错误响应。\n" * 10,
            "notice": "通知规范：业务上下文、防重和失败处理。\n" * 10,
            "job": "调度规范：分页、并发和重试边界。\n" * 10,
        }
        tasks = [
            {"id": "T01", "required": ["workspace", "repository", "entity"]},
            {"id": "T02", "required": ["workspace", "repository", "openapi"]},
            {"id": "T03", "required": ["workspace", "repository", "notice", "job"]},
            {"id": "T04", "required": ["workspace", "repository", "entity"]},
        ]
        all_sources = list(sources)
        repeated_reads = [name for _ in tasks for name in all_sources]
        loaded = set()
        progressive_reads = []
        for task in tasks:
            additions = [name for name in task["required"] if name not in loaded]
            progressive_reads.extend(additions)
            loaded.update(additions)
            self.assertTrue(set(task["required"]).issubset(loaded))

        repeated_text = "\n".join(sources[name] for name in repeated_reads)
        progressive_text = "\n".join(sources[name] for name in progressive_reads)
        repeated = context_measure.estimate_tokens(repeated_text)
        progressive = context_measure.estimate_tokens(progressive_text)

        self.assertEqual(all_sources * len(tasks), repeated_reads)
        self.assertEqual(all_sources, progressive_reads)
        self.assertLess(progressive["estTokens"], repeated["estTokens"])


class DocumentLayoutTest(unittest.TestCase):
    def test_simple_plan_keeps_a_shallow_structure(self):
        simple = (
            "## 执行概览\n\n目标仓：service\n\n"
            "## 任务\n\n- [ ] T01 修正查询\n\n"
            "  返回当前账户可见的数据。\n\n"
            "  依据：R1、D01\n  依赖：无\n\n"
            "  **验证**\n\n  工作目录：service\n\n"
            "  ```bash\n  pytest tests/test_query.py\n  ```\n\n"
            "  通过条件：目标测试通过。\n"
        )

        self.assertNotIn("### ", simple)
        for fact in ("T01", "R1", "D01", "service", "pytest", "目标测试通过"):
            self.assertIn(fact, simple)

    def test_complex_plan_improves_blocks_without_losing_facts(self):
        old = (
            "- [ ] T02 交付查询\n"
            "  依据：R1、D01\n"
            "  结果：返回当前账户可见的数据\n"
            "  落点：service/query.py、tests/test_query.py\n"
            "  依赖：T01\n"
            "  Red：越权查询先失败\n"
            "  Green：实现作用域过滤\n"
            "  回归：原分页语义不变\n"
            "  验证：pytest tests/test_query.py\n"
        )
        new = (
            "### 1. 查询权限\n\n"
            "- [ ] T02 交付查询\n\n"
            "  返回当前账户可见的数据。\n\n"
            "  依据：R1、D01\n  依赖：T01\n\n"
            "  **改动位置**\n\n"
            "  - service/query.py\n  - tests/test_query.py\n\n"
            "  **实施步骤**\n\n"
            "  1. 越权查询先失败。\n"
            "  2. 实现作用域过滤，回归原分页语义。\n\n"
            "  **验证**\n\n"
            "  ```bash\n  pytest \\\n+    tests/test_query.py\n  ```\n\n"
            "  通过条件：目标测试通过。\n"
        )

        for fact in (
            "T02",
            "R1",
            "D01",
            "T01",
            "service/query.py",
            "tests/test_query.py",
            "越权查询",
            "作用域过滤",
            "分页语义",
            "pytest",
        ):
            self.assertIn(fact, old)
            self.assertIn(fact, new)
        self.assertIn("**改动位置**\n\n", new)
        self.assertIn("**实施步骤**\n\n", new)
        self.assertIn("**验证**\n\n", new)
        self.assertGreater(
            context_measure.estimate_tokens(new)["estTokens"],
            context_measure.estimate_tokens(old)["estTokens"],
        )


class DeterminismTest(unittest.TestCase):
    def test_two_builds_are_byte_identical_json(self):
        first = json.dumps(context_measure.build_report(ROOT), sort_keys=True)
        second = json.dumps(context_measure.build_report(ROOT), sort_keys=True)
        self.assertEqual(first, second)


class CliTest(unittest.TestCase):
    def _run(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "context_measure.py"), *args],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_json_mode_exit_zero_and_valid_json(self):
        result = self._run("--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertIn("paths", payload)

    def test_text_mode_exit_zero(self):
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotEqual(result.stdout.strip(), "")
        with self.assertRaises(json.JSONDecodeError):
            json.loads(result.stdout)


if __name__ == "__main__":
    unittest.main()
