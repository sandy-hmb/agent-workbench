from __future__ import annotations

import json
import re
import subprocess
import sys
import unittest
from pathlib import Path
from urllib.parse import unquote


ROOT = Path(__file__).resolve().parents[1]
AGENTS_SOFT_LIMIT_BYTES = 7700
sys.path.insert(0, str(ROOT / "scripts"))

import workspace_setup  # noqa: E402
import kit_describe  # noqa: E402


SKILLS = (
    "workspace-init",
    "workspace-repo-onboarding",
    "workspace-cross-repo-analysis",
    "workspace-feature-design",
    "workspace-writing-plan",
    "workspace-execute-plan",
    "workspace-api-contract",
    "workspace-feature-workflow",
    "workspace-verify",
    "workspace-sync-base",
    "workspace-submit-test",
    "workspace-extension",
    "workspace-update",
    "workspace-instruction",
)


def core_skill_files() -> list[Path]:
    root = ROOT / ".agents" / "skills"
    return [
        path
        for path in root.rglob("SKILL.md")
        if not any(part.startswith("local-") for part in path.relative_to(root).parts)
    ]


BEGINNER_DOCS = (
    "docs/getting-started.md",
    "docs/guides/first-feature.md",
    "docs/foundation/README.md",
)
BARE_SCRIPT_RE = re.compile(r"python3 scripts/(?!kit\.py)\w+\.py")


class SurfaceTest(unittest.TestCase):
    def test_skill_frontmatter_and_metadata(self):
        for name in SKILLS:
            with self.subTest(skill=name):
                path = ROOT / ".agents" / "skills" / name
                content = (path / "SKILL.md").read_text(encoding="utf-8")
                match = re.match(r"^---\n(.*?)\n---\n", content, re.DOTALL)
                self.assertIsNotNone(match)
                frontmatter = match.group(1)
                self.assertRegex(frontmatter, rf"(?m)^name:\s*{re.escape(name)}\s*$")
                self.assertRegex(frontmatter, r"(?m)^description:\s*\S")
                metadata = (path / "agents" / "openai.yaml").read_text(encoding="utf-8")
                self.assertIn("interface:", metadata)
                self.assertIn("default_prompt:", metadata)
                self.assertIn(f"${name}", metadata)

    def test_client_adapters_are_exact(self):
        self.assertEqual(b"@AGENTS.md\n", (ROOT / "CLAUDE.md").read_bytes())
        self.assertEqual(b"@./AGENTS.md\n", (ROOT / "GEMINI.md").read_bytes())

    def test_agents_stays_within_doctor_size_budget(self):
        import workspace_doctor

        size = (ROOT / "AGENTS.md").stat().st_size
        self.assertLess(
            size,
            workspace_doctor.AGENTS_MAX_BYTES,
            f"AGENTS.md 已达 {size} 字节，超过 doctor 的 "
            f"{workspace_doctor.AGENTS_MAX_BYTES} 字节硬限；"
            "请把非核心内容迁移到 docs/ 或对应 SKILL.md。",
        )
        # 软上限：为分层规则等后续常驻内容保留余量，超支在硬限前先红
        self.assertLessEqual(
            size,
            AGENTS_SOFT_LIMIT_BYTES,
            f"AGENTS.md 已达 {size} 字节，超过 {AGENTS_SOFT_LIMIT_BYTES} 字节软上限；"
            "根规范只保留全局规则，阶段细节归各 SKILL.md。",
        )

    def test_claude_skill_adapters_are_relative_symlinks(self):
        for name in SKILLS:
            with self.subTest(skill=name):
                adapter = ROOT / ".claude" / "skills" / name
                target = adapter.readlink()
                self.assertFalse(target.is_absolute())
                self.assertEqual(Path("../../.agents/skills") / name, target)
                self.assertEqual(
                    (ROOT / ".agents" / "skills" / name).resolve(),
                    adapter.resolve(),
                )

    def test_operational_skills_use_registry_and_remote_branch_state(self):
        update = (
            ROOT / ".agents/skills/workspace-update/SKILL.md"
        ).read_text(encoding="utf-8")
        self.assertIn("python3 scripts/workspace_update.py plan", update)
        self.assertIn("python3 scripts/workspace_update.py apply", update)
        self.assertIn("Extension", update)

        submit = (
            ROOT / ".agents/skills/workspace-submit-test/SKILL.md"
        ).read_text(encoding="utf-8")
        commands = (
            "python3 scripts/workspace_submit.py plan",
            "--path <business-file>",
            "python3 scripts/workspace_submit.py apply",
            "--plan-hash <planHash>",
        )
        positions = [submit.index(command) for command in commands]
        self.assertEqual(sorted(positions), positions)
        self.assertIn("保持现场", submit)
        self.assertIn("Feature 目录", submit)

        design = (
            ROOT / ".agents/skills/workspace-feature-design/SKILL.md"
        ).read_text(encoding="utf-8")
        self.assertIn("`docs/development/features/<slug>/`", design)
        self.assertIn("不调用依赖 workspace registry", design)
        self.assertIn("`templates/feature/README.md`", design)
        self.assertIn("python3 scripts/feature_context.py create <slug>", design)
        self.assertIn("`artifacts/sql/`", design)
        self.assertIn("只创建", design)
        self.assertIn("用户确认后才创建", design)
        for path in (
            "`.workspace/docs/features/<slug>/README.md`",
            "`requirements/requirements.md`",
            "`design/design.md`",
            "`plans/implementation.md`",
            "`testing/verification.md`",
        ):
            self.assertIn(path, design)
        self.assertIn("python3 scripts/feature_context.py list --json", design)
        self.assertIn("workspace-writing-plan", design)
        self.assertIn("书面设计", design)
        for rule in (
            "每轮最多提出三个问题",
            "二至三个明确选项",
            "选项按 `A`、`B`、`C` 标记",
            "默认使用互斥单选",
            "彼此独立、可以同时成立",
            "`Q1-A,C`",
            "原生交互工具可用",
            "结束当前轮次并等待用户回复",
            "确认生成",
            "审阅实际文件",
            "用户场景",
            "边界情况",
            "关键实体",
            "成功标准",
            "`design/data-model.md`",
            "`design/api-integration.md`",
        ):
            self.assertIn(rule, design)
        self.assertNotIn("实施计划和验证策略，说明修改范围", design)

        writing_plan = (
            ROOT / ".agents/skills/workspace-writing-plan/SKILL.md"
        ).read_text(encoding="utf-8")
        self.assertIn("独立、可验证的交付单元", writing_plan)
        self.assertIn("书面计划", writing_plan)
        self.assertIn("需求覆盖", writing_plan)
        self.assertIn("书面设计获批后直接", writing_plan)
        self.assertIn("不依赖历史对话", writing_plan)
        self.assertIn("启动读取清单", writing_plan)
        self.assertIn("实际计划文件", writing_plan)
        self.assertIn("定向修订", writing_plan)
        self.assertIn("保持设计审阅为“已批准”", writing_plan)
        self.assertIn("不再请求同一修订的设计批准", writing_plan)
        self.assertIn("未披露或超出用户选择", writing_plan)
        self.assertIn("计划审阅仍为“待审阅”", writing_plan)
        self.assertIn(
            "python3 scripts/workspace_registry.py branch <repo> --type <type> --slug <slug> --json",
            writing_plan,
        )
        self.assertIn("`baseBranch`", writing_plan)
        self.assertIn("`branch`", writing_plan)

        execute = (
            ROOT / ".agents/skills/workspace-execute-plan/SKILL.md"
        ).read_text(encoding="utf-8")
        self.assertIn("根因", execute)
        self.assertIn("实际 diff", execute)
        self.assertIn("重要问题", execute)
        self.assertIn("显式指定", execute)
        self.assertIn("读取清单", execute)
        self.assertIn("历史对话", execute)

        onboarding = (
            ROOT / ".agents/skills/workspace-repo-onboarding/SKILL.md"
        ).read_text(encoding="utf-8")
        self.assertIn("clone 之后、preview/apply 之前", onboarding)
        self.assertIn("已存在且是普通文件", onboarding)
        self.assertIn("不得自动修改文件", onboarding)
        self.assertIn("等待用户明确批准后再写入", onboarding)
        self.assertIn("sourceInstruction", onboarding)
        self.assertNotIn("CodeGraph", onboarding)
        self.assertNotIn("codegraph", onboarding)
        self.assertIn("关键目录", onboarding)

        initialization = (
            ROOT / ".agents/skills/workspace-init/SKILL.md"
        ).read_text(encoding="utf-8")
        self.assertIn("explain", initialization)
        self.assertIn("不再重复请求确认", initialization)
        self.assertIn("`instruction` 字段固定为 `docs/repositories/<path>.md`", initialization)
        self.assertIn("相对于 `.workspace` 解析", initialization)

        api = (
            ROOT / ".agents/skills/workspace-api-contract/SKILL.md"
        ).read_text(encoding="utf-8")
        self.assertIn("仅当本次需求满足至少一项时使用", api)
        self.assertIn("`design/api-integration.md`", api)
        self.assertIn("`testing/api-integration.md`", api)
        self.assertIn("`docs/development/features/<slug>/`", api)
        self.assertIn("OpenAPI", api)
        self.assertIn("默认在 `design/design.md`", api)
        self.assertIn("D01-DNN", api)
        self.assertIn("不替代主设计", api)
        self.assertIn("事实、推断和待确认", api)
        self.assertIn("不自动启动服务", api)
        self.assertIn("不自动 curl 真实环境", api)

        verify = (
            ROOT / ".agents/skills/workspace-verify/SKILL.md"
        ).read_text(encoding="utf-8")
        self.assertIn("workspace_status.py", verify)
        self.assertIn("validation", verify)
        self.assertIn("工作目录", verify)
        self.assertIn("退出状态", verify)
        self.assertIn("plans/implementation.md", verify)
        self.assertIn("testing/verification.md", verify)
        self.assertIn("`docs/development/features/<slug>/`", verify)
        self.assertIn("`done` 仍需业务完成确认", verify)
        self.assertIn("不 commit、不 push", verify)
        self.assertIn("已获授权的同范围离线验证直接执行", verify)
        self.assertIn("轻量路径", verify)
        self.assertIn("不调用 feature resolve", verify)
        self.assertIn("## 执行记录 YYYY-MM-DD", verify)
        self.assertIn("## 验证批次", verify)
        self.assertIn("代码状态", verify)
        self.assertIn("审查结论", verify)
        self.assertIn("kit.py verify snapshot", verify)

        agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIn("没有 Skill 发现能力时", agents)
        self.assertIn("artifacts/sql/", agents)
        self.assertIn("同一任务内已授权", agents)
        self.assertIn("未知命令或参数", agents)
        self.assertIn("requirements/requirements.md", agents)
        self.assertIn("design/data-model.md", agents)
        self.assertIn("附件扩展主设计，不替代或抽空主设计", agents)
        self.assertIn("D01-DNN 的规范定义只位于主设计", agents)
        self.assertIn("plans/implementation.md", agents)
        self.assertIn("历史对话", agents)
        self.assertIn("默认使用中文", agents)
        self.assertIn("错误原文", agents)
        self.assertIn("用户可见", agents)
        self.assertIn("当前结果、下一步和需要用户决定的内容", agents)

        self.assertIn("不引用 Skill 名称", design)
        self.assertIn("不解释内部门禁", design)

    def test_feature_stage_templates_and_rules_stay_aligned(self):
        templates = ROOT / "templates/feature"
        requirements = (templates / "requirements.md").read_text(encoding="utf-8")
        design = (templates / "design.md").read_text(encoding="utf-8")
        plan = (templates / "implementation.md").read_text(encoding="utf-8")
        readme = (templates / "README.md").read_text(encoding="utf-8")
        self.assertIn("### R1", requirements)
        self.assertIn("{summary}", requirements)
        self.assertIn("## 背景与目标", requirements)
        self.assertIn("## 范围与边界", requirements)
        self.assertIn("## 功能需求与验收", requirements)
        self.assertIn('<a id="d01"></a>', design)
        self.assertIn("完整、自包含的技术设计和唯一主入口", design)
        self.assertLess(design.index("## D01"), design.index("## 需求覆盖"))
        self.assertIn("角色权限、字段映射或方案比较使用表格", design)
        self.assertIn("审批、事务、迁移或发布顺序使用编号步骤", design)
        for field in (
            "**目标：**",
            "**选定方案：**",
            "**关键约束：**",
            "## 现状与方案选择",
            "## 需求覆盖",
        ):
            self.assertIn(field, design)
        for field in (
            "**目标：**",
            "**方案：**",
            "**技术栈：**",
            "**设计来源：**",
            "### 全局约束",
            "**文件**",
            "**接口**",
            "**实施步骤**",
            "**验证**",
            "完成门禁",
            "目标仓",
            "验证性质",
            "具体失败场景",
            "连续执行",
        ):
            self.assertIn(field, plan)
        self.assertNotRegex(plan, r"(?m)^[ \t]+- \[[ x]\]")
        self.assertIn("data-model.md#d01-字段设计", plan)
        self.assertIn("## 执行概览", plan)
        self.assertIn("### 1. 示例分组", plan)
        self.assertIn("## 整体验证与完成条件", plan)
        data_model = (templates / "data-model.md").read_text(encoding="utf-8")
        api_integration = (templates / "api-integration.md").read_text(encoding="utf-8")
        self.assertIn("扩展主设计中的 [D01]", data_model)
        self.assertIn("扩展主设计中的 [D02]", api_integration)
        self.assertIn("- [ ] T01", plan)
        for field in ("需求审阅：待审阅", "设计审阅：未生成", "计划审阅：未生成"):
            self.assertIn(field, readme)

        design_skill = (ROOT / ".agents/skills/workspace-feature-design/SKILL.md").read_text(
            encoding="utf-8"
        )
        plan_skill = (ROOT / ".agents/skills/workspace-writing-plan/SKILL.md").read_text(
            encoding="utf-8"
        )
        execute_skill = (ROOT / ".agents/skills/workspace-execute-plan/SKILL.md").read_text(
            encoding="utf-8"
        )
        verify_skill = (ROOT / ".agents/skills/workspace-verify/SKILL.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("templates/feature/requirements.md", design_skill)
        self.assertIn("templates/feature/design.md", design_skill)
        self.assertIn("附件只能引用并展开这些决策", design_skill)
        self.assertIn("设计审阅及已有计划审阅都回到“待审阅”", design_skill)
        self.assertIn("唯一例外是尚未生成计划时", design_skill)
        self.assertIn("内容、结构和阅读自审", design_skill)
        self.assertIn("Markdown 预览", design_skill)
        self.assertIn("templates/feature/implementation.md", plan_skill)
        self.assertIn("- [ ] T01", plan_skill)
        self.assertIn("任务骨架", plan_skill)
        self.assertIn("自然业务或交付领域", plan_skill)
        self.assertIn("Markdown 预览", plan_skill)
        self.assertIn("范围缩写", plan_skill)
        self.assertIn("存在真实技术取舍", design_skill)
        self.assertIn("占位内容、内部矛盾、范围和表述歧义", design_skill)
        self.assertIn("全部任务做一次代码事实预检", plan_skill)
        self.assertIn("普通代码漂移", plan_skill)
        self.assertIn("任务边界、验证强度", plan_skill)
        self.assertIn("task-evidence-v1", plan_skill)
        self.assertIn("唯一目标仓", plan_skill)
        self.assertIn("持久化", plan_skill)
        self.assertIn("--task T01", execute_skill)
        self.assertIn("--check", execute_skill)
        self.assertIn("instructionContext", execute_skill)
        self.assertIn("首次编辑", execute_skill)
        self.assertIn("任务证据", execute_skill)
        self.assertIn("trustedProgress", execute_skill)
        self.assertIn("覆盖验收", verify_skill)
        self.assertIn("执行情况", verify_skill)
        self.assertIn("可信完成", verify_skill)
        self.assertIn("待外部验证", verify_skill)
        self.assertNotIn(
            "为非平凡行为增加最小失败检查，并确认失败原因正确",
            plan,
        )

    def test_worktree_requires_explicit_user_request(self):
        paths = (
            ROOT / "AGENTS.md",
            ROOT / "templates/workspace/AGENTS.md",
            ROOT / ".agents/skills/workspace-execute-plan/SKILL.md",
            ROOT / "docs/guides/first-feature.md",
        )
        for path in paths:
            with self.subTest(path=path.relative_to(ROOT)):
                content = path.read_text(encoding="utf-8")
                self.assertIn("未明确要求 worktree 时", content)
                self.assertIn("默认在目标仓当前工作目录开发", content)
                self.assertIn("分支批准不包含 worktree 操作", content)

    def test_execution_plan_runs_until_complete_or_blocked(self):
        paths = (
            ROOT / ".agents/skills/workspace-execute-plan/SKILL.md",
            ROOT / "docs/guides/first-feature.md",
            ROOT / "docs/architecture.md",
        )
        for path in paths:
            with self.subTest(path=path.relative_to(ROOT)):
                content = path.read_text(encoding="utf-8")
                self.assertIn("一次一个可验证任务不是会话边界", content)
                self.assertIn("有下一项依赖满足的未完成任务时直接继续", content)
                self.assertIn("只有全部任务完成或所有剩余任务真实阻塞时", content)

        execute = paths[0].read_text(encoding="utf-8")
        self.assertIn("单个任务受阻", execute)
        self.assertIn("普通代码漂移", execute)
        self.assertIn("executionDecision", execute)
        self.assertIn("confirmationRequired", execute)
        self.assertIn("不得跳过 `currentTask`", execute)
        self.assertIn(
            "当前任务、阻塞类别、实际证据、已完成的安全步骤和最小用户决策",
            execute,
        )

    def test_instruction_context_docs_define_monotonic_narrowing(self):
        paths = (
            ROOT / "AGENTS.md",
            ROOT / ".agents/skills/workspace-execute-plan/SKILL.md",
            ROOT / "docs/guides/first-feature.md",
            ROOT / "templates/feature/implementation.md",
            ROOT / "templates/workspace/AGENTS.md",
        )
        for path in paths:
            with self.subTest(path=path.relative_to(ROOT)):
                content = path.read_text(encoding="utf-8")
                if "instructionContext" in content:
                    self.assertIn("单调收窄", content)

    def test_workspace_template_keeps_category_examples_in_reference_docs(self):
        template = (ROOT / "templates/workspace/AGENTS.md").read_text(encoding="utf-8")
        reference = (ROOT / "docs/reference/instruction-layers.md").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("category=backend", template)
        self.assertIn("category=backend", reference)
        self.assertIn("不会按 category 自动过滤", reference)

    def test_add_repo_apply_regenerates_generated_context_safely(self):
        content = (ROOT / ".agents/skills/workspace-init/SKILL.md").read_text(
            encoding="utf-8"
        )
        self.assertRegex(content, r"(?:重建|更新).*`\.workspace/CONTEXT\.md`")
        self.assertIn("失配", content)
        self.assertIn("拒绝", content)
        self.assertNotRegex(
            content,
            r"(?:不更新|不覆盖|不得覆盖)[^。\n]{0,40}(?:CONTEXT\.md|上下文)",
        )

    def test_setup_documentation_matches_required_cli_sequence(self):
        paths = (
            ROOT / "docs/getting-started.md",
            ROOT / "docs/foundation/README.md",
            ROOT / ".agents/skills/workspace-init/SKILL.md",
        )
        commands = (
            "init plan --config",
            "init clone --config",
            "init preview --config",
            "init apply --config",
        )
        for path in paths:
            with self.subTest(path=path.relative_to(ROOT)):
                content = path.read_text(encoding="utf-8")
                positions = [content.index(command) for command in commands]
                self.assertEqual(sorted(positions), positions)
                self.assertIn("--preview-hash", content)
                self.assertNotIn("[--config", content)
                self.assertNotRegex(
                    content,
                    r"(?:可让[^。\n]*交互|交互收集|交互模式(?:只|自动))",
                )

        getting_started = (ROOT / "docs/getting-started.md").read_text(encoding="utf-8")
        self.assertIn("applyCommand", getting_started)
        self.assertIn("workspace_status.py", getting_started)

        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("workspace-init", readme)
        self.assertIn("workspace_update.py plan", readme)
        self.assertIn(".workspace/", readme)

        parser = workspace_setup.build_parser()
        for operation in ("init", "add-repo"):
            for action, extra in (
                ("plan", []),
                ("clone", []),
                ("preview", ["--json"]),
                ("apply", ["--preview-hash", "reviewed-hash"]),
            ):
                with self.subTest(operation=operation, action=action):
                    args = parser.parse_args(
                        [operation, action, "--config", "input.json", *extra]
                    )
                    self.assertEqual(operation, args.operation)
                    self.assertEqual(action, args.action)

    def test_beginner_docs_lead_with_kit_entrypoint_and_agent_handoff(self):
        for relative in BEGINNER_DOCS:
            path = ROOT / relative
            with self.subTest(path=relative):
                content = path.read_text(encoding="utf-8")
                parts = content.split("## 等价命令", 1)
                self.assertEqual(2, len(parts), "缺少'等价命令'附录小节")
                primary, _ = parts
                self.assertIn("kit.py", primary)
                self.assertNotRegex(primary, BARE_SCRIPT_RE)
        getting_started = (ROOT / "docs/getting-started.md").read_text(encoding="utf-8")
        self.assertIn("git clone https://github.com/sandy-hmb/agent-workbench.git", getting_started)
        first_feature = (ROOT / "docs/guides/first-feature.md").read_text(encoding="utf-8")
        self.assertIn("轻量路径", first_feature)
        self.assertIn("testTarget: null", first_feature)
        foundation = (ROOT / "docs/foundation/README.md").read_text(encoding="utf-8")
        self.assertIn("set-status <slug> done", foundation)

    def test_gitignore_separates_local_inputs_from_governance_state(self):
        for path, ignored in (
            (".workspace/workspace.json", True),
            (".workspace/docs/features/demo/README.md", True),
            (".workspace/extensions-input.json", True),
            (".workspace.migrate-stage/workspace.json", True),
            (".workspace.migrate-before/workspace.json", False),
            (".agents/skills/local-example-rule", True),
            (".agents/skills/local-example-rule/SKILL.md", True),
            (".claude/skills/local-example-rule", True),
            ("workspace-input.json", True),
            ("new-repo.json", True),
            (".idea/workspace.xml", True),
            ("workspace.json", False),
            ("CONTEXT.md", False),
            ("examples/workspace-input.json", False),
            ("docs/repositories/example.md", False),
            ("docs/development/features/example/README.md", True),
        ):
            with self.subTest(path=path):
                result = subprocess.run(
                    [
                        "git",
                        "-c",
                        "core.excludesFile=/dev/null",
                        "check-ignore",
                        "--quiet",
                        "--",
                        path,
                    ],
                    cwd=ROOT,
                    check=False,
                )
                self.assertEqual(0 if ignored else 1, result.returncode)

    def test_development_records_are_ignored(self):
        result = subprocess.run(
            ["git", "check-ignore", "--quiet", "--", "docs/development/features/example/README.md"],
            cwd=ROOT,
            check=False,
        )
        self.assertEqual(0, result.returncode)

    def test_reusable_surface_has_no_product_specific_integrations(self):
        paths = [ROOT / "README.md", ROOT / "AGENTS.md"]
        paths.extend(
            path for path in (ROOT / "docs").rglob("*.md")
            if not path.is_relative_to(ROOT / "docs/development")
        )
        paths.extend(core_skill_files())
        for path in paths:
            with self.subTest(path=path.relative_to(ROOT)):
                content = path.read_text(encoding="utf-8")
                lowered = content.lower()
                for term in ("qbit", "jira", "lark", "maven", "e2e"):
                    self.assertNotIn(term, lowered)
                for line in content.splitlines():
                    if re.search(r"(?i)(?<![a-z])PR(?:/MR)?(?![a-z])", line):
                        self.assertRegex(line, r"(?:不|非目标|边界)")

    def test_extension_surface_stays_local_and_agent_neutral(self):
        paths = (
            ROOT / "scripts/workspace_adapters.py",
            ROOT / "scripts/workspace_extension.py",
            ROOT / ".agents/skills/workspace-extension/SKILL.md",
        )
        for path in paths:
            with self.subTest(path=path.relative_to(ROOT)):
                content = path.read_text(encoding="utf-8").lower()
                self.assertNotIn("plugin", content)
                self.assertNotIn("marketplace", content)
                self.assertNotIn("cache/", content)

    def test_extension_skill_requires_explicit_local_config_cleanup_before_deactivation(self):
        skill = (ROOT / ".agents/skills/workspace-extension/SKILL.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("workspace.local.json", skill)
        self.assertIn("先显式移除", skill)
        self.assertIn("不自动删除", skill)

    def test_extension_guide_explains_single_provider_and_explicit_execution(self):
        guide = (ROOT / "docs/guides/local-extensions.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("每个 capability 只能有一个默认 Provider", guide)
        self.assertIn("workspace_provider.py run <capability>", guide)
        self.assertIn("不是操作系统级权限隔离", guide)

    def test_workspace_update_requires_authorized_fast_forward(self):
        skill = (ROOT / ".agents/skills/workspace-update/SKILL.md").read_text(
            encoding="utf-8"
        )
        for command in (
            "git status --short --branch",
            "git remote -v",
            "python3 scripts/workspace_update.py plan",
            "python3 scripts/workspace_update.py apply",
            "git pull --ff-only",
            "python3 scripts/workspace_migrate.py preview --root . --json",
        ):
            with self.subTest(command=command):
                self.assertIn(command, skill)
        for forbidden in ("git merge", "git rebase", "git stash", "git reset", "git push"):
            with self.subTest(forbidden=forbidden):
                self.assertIn(forbidden, skill)
        self.assertIn("用户明确确认", skill)
        self.assertIn("不得自动解决冲突", skill)

    def test_ci_runs_all_offline_governance_checks(self):
        workflow = (ROOT / ".github/workflows/governance.yml").read_text(
            encoding="utf-8"
        )
        for command in (
            "python -m unittest discover -s tests -p 'test_*.py'",
            "python -m py_compile scripts/*.py migrations/*.py",
            "python scripts/workspace_doctor.py --root .",
            'for schema in schemas/*.json; do python -m json.tool "$schema" >/dev/null; done',
            "python -m json.tool upgrades/manifest.json",
            "python -m json.tool workflows/feature-development.json",
        ):
            self.assertIn(command, workflow)
        self.assertNotRegex(
            workflow,
            r"(?i)\b(?:pip3?|uv|poetry|npm|yarn)\s+(?:install|sync)\b",
        )
        self.assertIn("fail-fast: false", workflow)
        for value in (
            "matrix.os",
            "matrix.python",
            "ubuntu-latest",
            "macos-latest",
            "3.10",
            "3.x",
        ):
            self.assertIn(value, workflow)
        self.assertEqual(
            1,
            workflow.count("python -m unittest discover -s tests -p 'test_*.py'"),
        )
        self.assertNotIn("test_surface.py", workflow)
        self.assertTrue(list((ROOT / "scripts").glob("*.py")))

    def test_public_documentation_surface(self):
        for relative in ("README.md", "LICENSE", "CONTRIBUTING.md", "SECURITY.md"):
            self.assertTrue((ROOT / relative).is_file(), relative)
        for relative in (
            "CHANGELOG.md",
            "docs/getting-started.md",
            "docs/guides/local-extensions.md",
            "docs/guides/custom-workflows.md",
            "docs/reference/local-workspace-layout.md",
        ):
            self.assertTrue((ROOT / relative).is_file(), relative)
        version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
        manifest = json.loads((ROOT / "upgrades/manifest.json").read_text(encoding="utf-8"))
        changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        match = re.search(r"(?m)^## (\d+\.\d+\.\d+)", changelog)
        self.assertIsNotNone(match, "CHANGELOG.md 必须至少有一个已发布版本标题")
        self.assertEqual(version, manifest["kitVersion"], "VERSION 与 upgrades/manifest.json 的 kitVersion 不一致")
        self.assertEqual(version, match.group(1), "VERSION 与 CHANGELOG.md 最新已发布版本号不一致")

        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        for entry in (
            "Python 3.10+",
            "Git 2.23+",
            "git clone https://github.com/sandy-hmb/agent-workbench.git",
            "cd agent-workbench",
            "workspace-init",
            "workspace_update.py plan",
            ".workspace/",
        ):
            with self.subTest(entry=entry):
                self.assertIn(entry, readme)
        self.assertNotIn("Use this template", readme)
        self.assertNotIn("GitHub Template", readme)
        for skill in SKILLS:
            self.assertIn(f"`{skill}`", readme)

        license_text = (ROOT / "LICENSE").read_text(encoding="utf-8")
        self.assertTrue(license_text.startswith("MIT License\n"))
        self.assertIn("Copyright (c) 2026 sandy", license_text)
        self.assertIn("THE SOFTWARE IS PROVIDED \"AS IS\"", license_text)

        security = (ROOT / "SECURITY.md").read_text(encoding="utf-8")
        self.assertIn("1.x", security)
        self.assertIn("Provider", security)
        self.assertNotRegex(
            security,
            r"(?i)(?:password|token|secret|api[_-]?key)\s*[:=]\s*\S+",
        )
        self.assertNotRegex(security, r"https?://[^/\s]+:[^@\s]+@")
        self.assertIn("Private vulnerability reporting", security)
        self.assertIn("如果入口不可用", security)

        migration = (ROOT / "docs/reference/local-workspace-layout.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("状态版本与迁移", migration)
        self.assertIn("previewHash", migration)

    def test_public_docs_have_no_vendor_runtime_dependency(self):
        paths = [
            ROOT / "README.md",
            ROOT / "AGENTS.md",
            ROOT / "SECURITY.md",
            ROOT / "CONTRIBUTING.md",
            ROOT / "CHANGELOG.md",
            ROOT / "docs/getting-started.md",
            ROOT / "docs/foundation/README.md",
            ROOT / "docs/guides/local-extensions.md",
            ROOT / "docs/reference/local-workspace-layout.md",
            ROOT / ".agents/skills/workspace-update/SKILL.md",
        ]
        for path in paths:
            content = path.read_text(encoding="utf-8").lower()
            with self.subTest(path=path.relative_to(ROOT)):
                self.assertNotIn("plugin", content)
                self.assertNotIn("marketplace", content)
                self.assertNotIn("plugins/cache", content)

    def test_public_runtime_has_no_private_or_plugin_dependencies(self):
        paths = [
            ROOT / "README.md",
            ROOT / "AGENTS.md",
            ROOT / "SECURITY.md",
            ROOT / "CONTRIBUTING.md",
            ROOT / "docs/getting-started.md",
        ]
        for directory in (
            ROOT / "docs/foundation",
            ROOT / "docs/guides",
            ROOT / "docs/reference",
            ROOT / "scripts",
            ROOT / "templates",
            ROOT / "schemas",
            ROOT / "examples",
            ROOT / ".agents/skills",
        ):
            paths.extend(
                path
                for path in directory.rglob("*")
                if path.is_file()
                and "__pycache__" not in path.parts
                and not (
                    directory == ROOT / ".agents/skills"
                    and any(
                        part.startswith("local-")
                        for part in path.relative_to(directory).parts
                    )
                )
                and path.suffix in {".md", ".py", ".json", ".yaml"}
            )
        for path in paths:
            with self.subTest(path=path.relative_to(ROOT)):
                content = path.read_text(encoding="utf-8").lower()
                for private_identifier in ("internal.example", "private-token", "qbit"):
                    self.assertNotIn(private_identifier, content)
                self.assertNotRegex(content, r"\.codex-plugin|marketplace|plugins/cache")

    def test_workspace_input_schema_keeps_local_section_optional(self):
        shared = json.loads(
            (ROOT / "schemas/workspace-input.schema.json").read_text(encoding="utf-8")
        )
        self.assertIn("local", shared["properties"])
        self.assertNotIn("local", shared["required"])
        safe_name = "^[A-Za-z0-9][A-Za-z0-9._-]*$"
        local = shared["properties"]["local"]["properties"]
        self.assertEqual(safe_name, local["branchOwner"]["pattern"])
        self.assertEqual(safe_name, local["primaryRole"]["pattern"])
        self.assertEqual(safe_name, local["extensions"]["propertyNames"]["pattern"])
        registry = json.loads(
            (ROOT / "schemas/workspace.schema.json").read_text(encoding="utf-8")
        )
        repository = registry["$defs"]["repository"]["properties"]
        self.assertEqual(safe_name, repository["path"]["pattern"])
        self.assertEqual(safe_name, repository["aliases"]["items"]["pattern"])

    def test_public_docs_have_no_machine_specific_paths(self):
        paths = [ROOT / "README.md", ROOT / "AGENTS.md"]
        paths.extend(
            (
                ROOT / relative
                for relative in (
                    "SECURITY.md",
                    "CONTRIBUTING.md",
                    "docs/getting-started.md",
                    "docs/foundation/README.md",
                    "docs/guides/local-extensions.md",
                    "docs/reference/local-workspace-layout.md",
                )
            )
        )
        for path in paths:
            with self.subTest(path=path.relative_to(ROOT)):
                self.assertNotRegex(
                    path.read_text(encoding="utf-8"),
                    r"(?m)(?:^|[\s(`])/(?:Users|home|private|tmp)/",
                )

    def test_describe_commands_are_covered_by_their_runbook(self):
        for command in kit_describe.describe_result(ROOT)["commands"]:
            name = command["name"]
            if name in kit_describe.EXEMPT:
                continue
            with self.subTest(command=name):
                runbook = ROOT / command["runbook"]
                self.assertTrue(runbook.is_file(), f"{name} 的 runbook 不存在：{runbook}")
                text = runbook.read_text(encoding="utf-8")
                self.assertTrue(
                    command["module"] in text or f"kit.py {name}" in text,
                    f"{name} 未在 {command['runbook']} 中被提及",
                )
        for name, reason in kit_describe.EXEMPT.items():
            with self.subTest(exempt=name):
                self.assertTrue(reason)

    def test_workspace_extension_skill_mentions_scaffold(self):
        skill = (ROOT / ".agents/skills/workspace-extension/SKILL.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("scaffold", skill)

    def test_local_markdown_links_exist(self):
        pattern = re.compile(r"\]\(([^)]+)\)")
        files = [ROOT / "README.md", ROOT / "AGENTS.md"]
        files.extend((ROOT / "docs").rglob("*.md"))
        files.extend(core_skill_files())
        for path in files:
            for raw in pattern.findall(path.read_text(encoding="utf-8")):
                target = unquote(raw.split("#", 1)[0].split("?", 1)[0])
                if not target or re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", target):
                    continue
                self.assertTrue((path.parent / target).exists(), f"{path} -> {raw}")


if __name__ == "__main__":
    unittest.main()
