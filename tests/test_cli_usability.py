"""Real CLI display and template-backed WorkItem creation regressions."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.support.items import ItemFixture, ROOT
from tests.support.workflows import WorkflowFixture
from tests.test_kit import _run
from workbench.cli import brief, status
from workbench.cli.brief import brief_result
from workbench.cli.status import status_result
from workbench.work_items import commands
from workbench.work_items.query import WorkItemQuery
from workbench.work_items.store import WorkItemError, item_path, state_path


class CliUsabilityTest(unittest.TestCase):
    def setUp(self):
        self.fixture = ItemFixture()
        self.fixture.open()
        self.addCleanup(self.fixture.close)
        self.root = self.fixture.root

    def cli(self, *args):
        return subprocess.run(
            [sys.executable, '-B', str(ROOT / 'scripts/kit.py'), *args, '--root', str(self.root)],
            cwd=tempfile.gettempdir(), capture_output=True, text=True,
        )

    def snapshot(self):
        return {path: (path.read_bytes(), path.stat().st_mtime_ns)
                for path in self.fixture.item.rglob('*') if path.is_file()}

    def test_brief_text_and_json_are_read_only_and_keep_api_facts(self):
        before = self.snapshot()
        output = self.cli('brief', 'demo')
        self.assertEqual(0, output.returncode, output.stderr)
        for text in ('工作项：demo', '仓库：kit', '轮次：i01', '活动：develop',
                     '生命周期：active', '阶段：item.design', '执行决策：BLOCKED',
                     '可完成：否', 'REVIEW_REQUIRED', '无开发阻塞', '无待外部验收',
                     '.agents/skills/workspace-item-design/SKILL.md'):
            self.assertIn(text, output.stdout)
        machine = self.cli('brief', 'demo', '--json')
        self.assertEqual(0, machine.returncode, machine.stderr)
        self.assertEqual(brief_result(self.root, 'demo'), json.loads(machine.stdout))
        self.assertEqual(1, len(machine.stdout.splitlines()))
        self.assertEqual(before, self.snapshot())

    def test_status_text_and_json_keep_recorded_progress_and_api_facts(self):
        before = self.snapshot()
        output = self.cli('status')
        self.assertEqual(0, output.returncode, output.stderr)
        for text in ('工作区：kit', '模式：maintenance', 'Extension：无已启用扩展',
                     'Workflow：未启用', '工作项数量：1', 'demo', 'Demo',
                     'active', 'develop', '记录进度：0/0', '代码适用性：未检查'):
            self.assertIn(text, output.stdout)
        machine = self.cli('status', '--json')
        self.assertEqual(0, machine.returncode, machine.stderr)
        self.assertEqual(status_result(self.root), json.loads(machine.stdout))
        self.assertEqual(1, len(machine.stdout.splitlines()))
        self.assertEqual(before, self.snapshot())

    def test_formatting_does_not_check_code_render_or_scan_other_items(self):
        with mock.patch.object(WorkItemQuery, 'code_state', side_effect=AssertionError('unexpected code check')), \
                mock.patch.object(commands, 'render', side_effect=AssertionError('unexpected write')), \
                mock.patch.object(status, 'list_items', side_effect=AssertionError('unexpected item scan')), \
                mock.patch('workbench.extensions.runner.run_action', side_effect=AssertionError('unexpected Action')):
            for main, args in ((brief.main, ['demo']), (status.main, ['--item', 'demo'])):
                code, output, error = _run(main, [*args, '--root', str(self.root)])
                self.assertEqual(0, code, error)
                self.assertIn('阶段：item.design', output)

    def test_configured_status_shows_extension_and_workflow_blockers(self):
        workflow = WorkflowFixture()
        workflow.open()
        self.addCleanup(workflow.close)
        self.root = workflow.root
        workflow.write_overlay([{'id': 'team.check', 'after': 'item.verify',
                                 'uses': 'action-extension/integration-test'}])
        workflow.activate_overlay()
        skill = self.root / '.workspace/extensions/action-extension/skills/integration-test/SKILL.md'
        skill.write_text(skill.read_text() + '\nChanged\n', encoding='utf-8')
        facts = status_result(self.root)
        self.assertTrue(facts['extensions']['blockedCodes'])
        self.assertTrue(facts['workflow']['blockedCodes'])
        output = self.cli('status')
        self.assertEqual(0, output.returncode, output.stderr)
        for text in ('工作区：Demo', '模式：workspace', 'action-extension',
                     'Workflow：已启用', 'Extension 阻塞：', 'Workflow 阻塞：'):
            self.assertIn(text, output.stdout)
        for section in ('extensions', 'workflow'):
            for code in facts[section]['blockedCodes']:
                self.assertIn(code, output.stdout)
        self.assertEqual(facts, json.loads(self.cli('status', '--json').stdout))

    def test_task_text_keeps_body_dependencies_and_branch_mismatch(self):
        self.fixture.plan(('T01', 'T02'))
        plan = self.fixture.item / 'plan.md'
        plan.write_text(plan.read_text().replace(
            '### T02 Behavior\n\n依据：R1\n依赖：无',
            '### T02 Behavior\n\n依据：R1\n依赖：T01'), encoding='utf-8')
        self.fixture.review()
        bindings = self.fixture.state()['bindings']
        bindings[0]['workBranch'] = 'expected-work'
        commands.update(self.root, 'demo', {'bindings': bindings}, reason='调整目标分支',
                        expected_revision=self.fixture.state()['stateRevision'])
        output = self.cli('brief', 'demo', '--task', 'T02')
        self.assertEqual(0, output.returncode, output.stderr)
        for text in ('所选任务：T02', '### T02 Behavior', '依赖：T01',
                     '直接依赖：', 'T01', 'pending', 'expected-work', 'main', 'mismatched'):
            self.assertIn(text, output.stdout)
        machine = self.cli('brief', 'demo', '--task', 'T02', '--json')
        self.assertEqual(0, machine.returncode, machine.stderr)
        self.assertEqual(brief_result(self.root, 'demo', task_id='T02'), json.loads(machine.stdout))

    def test_text_and_json_errors_keep_code_and_nonzero_exit(self):
        for args in (('brief', 'missing'), ('status', '--item', 'missing')):
            with self.subTest(args=args):
                output = self.cli(*args)
                machine = self.cli(*args, '--json')
                self.assertEqual(1, output.returncode)
                self.assertEqual(1, machine.returncode)
                error = json.loads(machine.stdout)
                self.assertEqual('FILE_UNAVAILABLE', error['error'])
                self.assertIn(error['error'], output.stdout)
                self.assertIn(error['message'], output.stdout)

    def test_repository_context_without_item_shows_rules_and_diagnostics(self):
        output = self.cli('brief', '--repo', 'kit', '--path', 'AGENTS.md')
        self.assertEqual(0, output.returncode, output.stderr)
        self.assertIn('规则来源：', output.stdout)
        self.assertIn(str(self.root / 'AGENTS.md'), output.stdout)
        (self.root / 'AGENTS.md').unlink()
        output = self.cli('brief', '--repo', 'kit', '--path', 'AGENTS.md')
        self.assertEqual(0, output.returncode, output.stderr)
        self.assertIn('RULE_SOURCE_MISSING', output.stdout)
        machine = self.cli('brief', '--repo', 'kit', '--path', 'AGENTS.md', '--json')
        self.assertEqual(brief_result(self.root, repository='kit', paths=['AGENTS.md']),
                         json.loads(machine.stdout))

    def test_status_empty_corrupt_records_and_context_sources_are_explicit(self):
        shutil.rmtree(self.fixture.item)
        output = self.cli('status', '--context-sources')
        self.assertEqual(0, output.returncode, output.stderr)
        self.assertIn('工作项数量：0', output.stdout)
        self.assertIn('暂无工作项', output.stdout)
        self.assertIn('上下文来源：', output.stdout)
        self.assertIn('.workspace/CONTEXT.md', output.stdout)
        commands.create(self.root, 'broken', title='Broken', repositories=['kit'])
        state_path(item_path(self.root, 'broken')).write_text('{broken', encoding='utf-8')
        output = self.cli('status')
        self.assertEqual(0, output.returncode, output.stderr)
        machine = json.loads(self.cli('status', '--json').stdout)
        self.assertEqual([], machine['items'])
        self.assertIn(machine['diagnostics'][0]['code'], output.stdout)
        self.assertIn('broken', output.stdout)

    def test_blockers_acceptance_and_selected_status_are_visible(self):
        commands.block(self.root, 'demo', reason='等待环境', owner='环境负责人', condition='环境恢复',
                       expected_revision=self.fixture.state()['stateRevision'])
        commands.delivery(self.root, 'demo', {'externalChecks': [{
            'id': 'contract', 'requirement': 'R1', 'description': '接口联调',
            'owner': '验收负责人', 'status': 'pending', 'evidence': '',
        }]}, expected_revision=self.fixture.state()['stateRevision'])
        commands.create(self.root, 'broken', title='Broken', repositories=['kit'])
        state_path(item_path(self.root, 'broken')).write_text('{broken', encoding='utf-8')
        for args in (('brief', 'demo'), ('status', '--item', 'demo')):
            output = self.cli(*args)
            self.assertEqual(0, output.returncode, output.stderr)
            for text in ('阶段：', '执行决策：BLOCKED', '等待环境', '环境负责人', '环境恢复',
                         'contract', '接口联调', '验收负责人', 'pending', '下一步：'):
                self.assertIn(text, output.stdout)
            self.assertNotIn('broken', output.stdout)
        self.assertEqual(status_result(self.root, item_slug='demo'),
                         json.loads(self.cli('status', '--item', 'demo', '--json').stdout))

    def test_recorded_pass_unchecked_invalid_and_completed_are_distinct(self):
        self.fixture.review()
        self.fixture.record()
        output = self.cli('brief', 'demo')
        self.assertEqual(0, output.returncode, output.stderr)
        self.assertIn('记录结果：通过', output.stdout)
        self.assertIn('代码适用性：未检查', output.stdout)
        self.assertIn('执行决策：RUN', output.stdout)
        self.assertNotIn('已完成', output.stdout)
        (self.root / 'changed.py').write_text('# changed\n', encoding='utf-8')
        output = self.cli('brief', 'demo', '--check-code')
        self.assertEqual(0, output.returncode, output.stderr)
        self.assertIn('代码适用性：失效', output.stdout)
        self.fixture.record()
        commands.complete(self.root, 'demo', expected_revision=self.fixture.state()['stateRevision'])
        output = self.cli('brief', 'demo')
        self.assertEqual(0, output.returncode, output.stderr)
        self.assertIn('生命周期：done（已完成）', output.stdout)
        self.assertIn('执行决策：COMPLETE', output.stdout)
        self.assertIn('无阻塞', output.stdout)
        self.assertIn('无下一步', output.stdout)

    def test_activity_templates_custom_fallback_and_braces_through_real_cli(self):
        headings = {
            'repair': ('症状与复现', '根因证据', '修复和验证'),
            'investigate': ('问题与范围', '事实证据', '结论和后续'),
            'takeover': ('当前事实', '风险与未决', '下一步验收'),
            'acceptance': ('验收标准', '证据', '未关闭事项'),
            'develop': ('已知事实与关键假设', '验收与检查', '方案与工作项'),
            'custom-audit': ('已知事实与关键假设', '验收与检查', '方案与工作项'),
        }
        title = 'Title {summary}'
        summary = '保留 {title}、{summary} 与 {"count": 1}'
        for activity, expected in headings.items():
            with self.subTest(activity=activity):
                output = self.cli('item', 'create', activity, '--repo', 'kit', '--activity', activity,
                                  '--title', title, '--summary', summary, '--json')
                self.assertEqual(0, output.returncode, output.stdout + output.stderr)
                item = item_path(self.root, activity)
                body = (item / 'change.md').read_text()
                self.assertIn(title, body)
                self.assertIn(summary, body)
                for heading in expected:
                    self.assertIn('## ' + heading, body)
                self.assertNotIn('- [', body)
                self.assertFalse((item / 'plan.md').exists())
                self.assertFalse((item / 'design.md').exists())
                state = WorkItemQuery(self.root, activity).state
                self.assertEqual('active', state['lifecycle'])
                self.assertEqual({}, state['reviews'])
                self.assertIsNone(state['verification'])
                commands.review(self.root, activity, decision='approved', reason='审阅实际正文',
                                expected_revision=state['stateRevision'])
                self.assertFalse(WorkItemQuery(self.root, activity).decision()['canComplete'])

    def test_requirements_mode_uses_requirements_even_for_repair(self):
        output = self.cli('item', 'create', 'major-repair', '--repo', 'kit', '--title', 'Repair',
                          '--activity', 'repair', '--document-kind', 'requirements', '--risk-tier', 'major')
        self.assertEqual(0, output.returncode, output.stdout + output.stderr)
        item = item_path(self.root, 'major-repair')
        body = (item / 'requirements.md').read_text()
        self.assertIn('## 本轮变更', body)
        self.assertIn('### R1 可独立验收的行为', body)
        self.assertFalse((item / 'change.md').exists())

    def test_invalid_activity_never_creates_a_directory(self):
        for index, activity in enumerate(('', 'Repair', '../repair', 'bad_name', '修复', None)):
            slug = f'invalid-{index}'
            with self.subTest(activity=activity):
                with self.assertRaises(WorkItemError) as caught:
                    commands.create(self.root, slug, title='Invalid', repositories=['kit'], activity=activity)
                self.assertEqual('ITEM_INVALID', caught.exception.code)
                self.assertFalse(item_path(self.root, slug).exists())
        output = self.cli('item', 'create', 'invalid-cli', '--repo', 'kit', '--title', 'Invalid',
                          '--activity', '../repair', '--json')
        self.assertEqual(1, output.returncode)
        self.assertIn('ITEM_INVALID', output.stdout)
        self.assertFalse(item_path(self.root, 'invalid-cli').exists())

    def test_missing_or_invalid_utf8_template_leaves_no_partial_item(self):
        kit_root = Path(self.fixture.temp.name) / 'template-kit'
        template = kit_root / 'templates/item/change.md'
        with mock.patch.object(commands, 'KIT_ROOT', kit_root):
            for slug in ('missing-template', 'invalid-template'):
                if slug == 'invalid-template':
                    template.parent.mkdir(parents=True)
                    template.write_bytes(b'\xff')
                with self.assertRaises(WorkItemError) as caught:
                    commands.create(self.root, slug, title='Invalid', repositories=['kit'])
                self.assertEqual('ITEM_TEMPLATE_INVALID', caught.exception.code)
                self.assertFalse(item_path(self.root, slug).exists())


if __name__ == '__main__':
    unittest.main()
