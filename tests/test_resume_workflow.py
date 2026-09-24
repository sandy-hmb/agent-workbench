"""New-layout and host-neutral continuation acceptance scenarios."""
from __future__ import annotations
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import kit_feature_brief as brief
import workspace_status as status
import workspace_inspect as inspect
import workspace_verification as verification
import workspace_evidence as evidence
import workspace_model as model


class ResumeWorkflowTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve() / 'kit'
        self.root.mkdir()
        self.feature = self.root / 'docs/development/features/demo'
        self.feature.mkdir(parents=True)
        (self.root / 'AGENTS.md').write_text('# Rules\n')
        (self.root / '.gitignore').write_text('/docs/development/\n/.workspace/\n')
        subprocess.run(['git', 'init', '-q', '--initial-branch=main', str(self.root)], check=True)
        subprocess.run(['git', '-C', str(self.root), 'add', '.'], check=True)
        subprocess.run(['git', '-C', str(self.root), '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.test', 'commit', '-qm', 'initial'], check=True)
        (self.feature / 'README.md').write_text('# Demo\n\n- 状态：development\n- 需求短名：`demo`\n- 工作分支：`main`\n- 基线分支：`main`\n- 最后更新：2026-09-24\n- 需求审阅：已批准\n- 设计审阅：未生成\n- 计划审阅：未生成\n\n[变更](change.md)\n')
        (self.feature / '.work-item.json').write_text(model.initial_work_item('demo'))
        (self.feature / 'change.md').write_text('# 本轮目标\n\n- [ ] 修复排序\n')

    def test_ordinary_activity_without_plan_can_complete_and_verify(self):
        result = brief.resume_result(self.root, 'demo')
        self.assertFalse(result['taskExecution']['applicable'])
        self.assertEqual('feature.implement', result['currentStage'])
        (self.feature / 'change.md').write_text('# 本轮目标\n\n- [x] 修复排序\n')
        self.assertEqual('feature.verify', brief.resume_result(self.root, 'demo')['currentStage'])
        state = status.status_result(self.root, feature_slug='demo')
        code_state = verification.feature_code_state(self.root, 'maintenance', state['features'][0])
        evidence.record(self.feature, {'kind': 'verificationBatch', 'recordedAt': '2026-09-24T10:00:00+08:00',
            'overallResult': 'passed', 'reviewResult': 'passed', 'codeState': code_state,
            'checks': [{'workingDirectory': '.', 'command': 'fixture-check', 'exitStatus': 0, 'result': 'fixture passed'}],
            'blockers': [], 'artifactRefs': []})
        verification.refresh_summary(self.root, 'demo', self.feature)
        self.assertTrue((self.feature / 'verification.md').is_file())
        self.assertFalse((self.feature / 'testing/verification.md').exists())
        unchecked = brief.resume_result(self.root, 'demo')
        self.assertEqual('not_checked', unchecked['verification']['applicability'])
        checked = brief.resume_result(self.root, 'demo', check_code=True)
        self.assertEqual('valid', checked['verification']['applicability'])
        self.assertEqual('feature.complete', checked['currentStage'])
        (self.root / 'changed.py').write_text('# new code\n')
        changed = brief.resume_result(self.root, 'demo', check_code=True)
        self.assertEqual('invalid', changed['verification']['applicability'])
        self.assertEqual('feature.verify', changed['currentStage'])
        self.assertFalse((self.feature / 'plan.md').exists())

    def test_flat_complex_documents_are_read_and_searched_with_revision(self):
        (self.feature / 'requirements.md').write_text('# R1 排序规则\n')
        (self.feature / 'design.md').write_text('# D01 排序方案\n')
        (self.feature / 'plan.md').write_text('- [ ] T01 排序任务\n\n  依赖：无\n')
        first = inspect.handoff(self.root, 'demo')
        sources = {row['path'] for row in first['sources']}
        self.assertTrue({'requirements.md', 'design.md', 'plan.md'}.issubset(sources))
        text = inspect.document(self.root, 'demo', 'design.md', None)
        self.assertEqual('# D01 排序方案\n', text['content'])
        self.assertTrue(inspect.search(self.root, '排序规则', None, None, 0, 20)['items'])
        (self.feature / 'design.md').write_text('# D01 新排序方案\n')
        self.assertNotEqual(first['featureRevision'], inspect.handoff(self.root, 'demo')['featureRevision'])

    def test_cli_works_without_session_history_and_is_read_only(self):
        before = {p: p.read_bytes() for p in self.feature.rglob('*') if p.is_file()}
        process = subprocess.run([sys.executable, '-B', str(ROOT / 'scripts/kit.py'), 'brief', 'demo',
            '--root', str(self.root), '--projection', 'resume', '--json'], capture_output=True, text=True)
        self.assertEqual(0, process.returncode, process.stderr)
        self.assertEqual('demo', json.loads(process.stdout)['featureSlug'])
        self.assertEqual(before, {p: p.read_bytes() for p in self.feature.rglob('*') if p.is_file()})
        with mock.patch.object(status, '_maintenance_features', side_effect=AssertionError('unrelated scan')):
            self.assertEqual('demo', status.status_result(self.root, feature_slug='demo')['features'][0]['featureSlug'])
        with self.assertRaises(ValueError):
            brief.resume_result(self.root, 'missing')

    def test_branch_mismatch_is_explicit_and_does_not_switch_branch(self):
        subprocess.run(['git', '-C', str(self.root), 'checkout', '-qb', 'another'], check=True)
        result = brief.resume_result(self.root, 'demo')
        self.assertIn('WORKING_BRANCH_MISMATCH', result['blockers'])
        self.assertEqual('another', result['repositoryContext'][0]['currentBranch'])
        self.assertEqual('another', subprocess.check_output(['git', '-C', str(self.root), 'branch', '--show-current'], text=True).strip())

    def test_completed_feature_remains_historical_in_resume(self):
        readme = self.feature / 'README.md'
        readme.write_text(readme.read_text().replace('development', 'done'))
        historical = brief.resume_result(self.root, 'demo')
        self.assertEqual('historical', historical['state'])
        self.assertFalse(historical['taskExecution']['applicable'])
        self.assertEqual([], historical['nextActions'])
        self.assertIsNone(status.status_result(self.root, feature_slug='demo')['currentStage'])
        with self.assertRaises(ValueError):
            brief.brief_result(self.root, 'demo')
