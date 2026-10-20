"""One execution fact can cover multiple independently checked tasks."""
from unittest import TestCase, mock

from tests.support.items import ItemFixture
from workbench.work_items import commands
from workbench.work_items.query import WorkItemQuery
from workbench.work_items.store import WorkItemError


class BatchEvidenceTest(ItemFixture, TestCase):
    def setUp(self):
        self.open()
        self.addCleanup(self.close)
        self.plan(('T01', 'T02'))
        plan = self.item / 'plan.md'
        plan.write_text(plan.read_text().replace('### T02 Behavior\n\n依据：R1\n依赖：无',
                                                '### T02 Behavior\n\n依据：R1\n依赖：T01'))
        self.review()

    def batch(self, scope='item'):
        payload = self.payload()
        payload.update(scope=scope, taskCoverage={'T01': [0], 'T02': [0]})
        return payload

    def test_one_atomic_record_completes_dependencies_and_overall(self):
        payload, revision = self.batch(), self.state()['stateRevision']
        with mock.patch.object(commands, 'render', wraps=commands.render) as render:
            result = commands.record(self.root, 'demo', payload, expected_revision=revision)
        self.assertEqual(1, render.call_count)
        state = self.state()
        self.assertEqual([result['id']], state['evidence'])
        self.assertEqual(result['id'], state['verification'])
        self.assertEqual({result['id']}, {t['evidence'] for t in state['tasks'].values()})
        self.assertTrue(WorkItemQuery(self.root, 'demo').decision(check_code=True)['canComplete'])
        self.assertTrue(commands.record(self.root, 'demo', payload, expected_revision=revision)['replayed'])

    def test_coverage_cannot_skip_dependency_or_checks(self):
        for coverage, code in [({'T02': [0]}, 'TASK_DEPENDENCY_PENDING'),
                               ({'T01': []}, 'EVIDENCE_COVERAGE_INVALID'),
                               ({'T01': [1]}, 'EVIDENCE_COVERAGE_INVALID'),
                               ({'T99': [0]}, 'TASK_UNREGISTERED')]:
            with self.subTest(coverage=coverage):
                payload = self.batch('batch'); payload['taskCoverage'] = coverage
                with self.assertRaisesRegex(WorkItemError, code):
                    commands.record(self.root, 'demo', payload, expected_revision=self.state()['stateRevision'])
        self.assertEqual([], self.state()['evidence'])

    def test_failure_and_state_write_interruption_never_complete(self):
        payload = self.batch(); payload['result'] = 'failed'; payload['checks'][0]['exitStatus'] = 1
        commands.record(self.root, 'demo', payload, expected_revision=self.state()['stateRevision'])
        self.assertFalse(any(t['completed'] for t in WorkItemQuery(self.root, 'demo').task_states()))
        payload = self.batch()
        with mock.patch.object(commands, 'save_state', side_effect=OSError('interrupted')):
            with self.assertRaises(OSError):
                commands.record(self.root, 'demo', payload, expected_revision=self.state()['stateRevision'])
        self.assertEqual('failed', WorkItemQuery(self.root, 'demo').verification()['recordedResult'])

    def test_each_tasks_validation_kind_is_checked(self):
        plan = self.item / 'plan.md'
        plan.write_text(plan.read_text().replace('验证性质：行为', '验证性质：持久化'))
        self.review()
        with self.assertRaisesRegex(WorkItemError, 'EVIDENCE_KIND_INSUFFICIENT'):
            commands.record(self.root, 'demo', self.batch(), expected_revision=self.state()['stateRevision'])

    def test_batch_rejects_unrelated_check_directory(self):
        payload = self.batch()
        payload['checks'][0]['workingDirectory'] = '/unrelated'
        with self.assertRaisesRegex(WorkItemError, 'EVIDENCE_COVERAGE_INVALID'):
            commands.record(self.root, 'demo', payload, expected_revision=self.state()['stateRevision'])

    def test_concurrent_replay_commits_one_execution_fact(self):
        from concurrent.futures import ThreadPoolExecutor
        payload, revision = self.batch(), self.state()['stateRevision']
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: commands.record(self.root, 'demo', payload, expected_revision=revision), range(2)))
        self.assertEqual(1, sum(not row['replayed'] for row in results))
        self.assertEqual(1, len(self.state()['evidence']))

    def test_code_drift_and_blockers_still_reject_batch(self):
        payload = self.batch()
        (self.root / 'new.py').write_text('changed\n')
        with self.assertRaisesRegex(WorkItemError, 'CODE_CHANGED'):
            commands.record(self.root, 'demo', payload, expected_revision=self.state()['stateRevision'])
        commands.block(self.root, 'demo', task_ids=['T01'], reason='wait', owner='owner', condition='ready',
                       expected_revision=self.state()['stateRevision'])
        with self.assertRaisesRegex(WorkItemError, 'EXECUTION_BLOCKED'):
            commands.record(self.root, 'demo', self.batch(), expected_revision=self.state()['stateRevision'])

    def test_explicit_no_affected_tasks_preserves_old_records_on_new_scope(self):
        commands.record(self.root, 'demo', self.batch(), expected_revision=self.state()['stateRevision'])
        previous = self.state()['tasks']['T01']['evidence']
        with (self.item / 'change.md').open('a') as handle:
            handle.write('\n## R2 独立的新范围\n新增独立行为。\n')
        self.plan(('T01', 'T02', 'T03'))
        # Reapply the existing dependency so only T03 is new.
        plan = self.item / 'plan.md'
        plan.write_text(plan.read_text().replace('### T02 Behavior\n\n依据：R1\n依赖：无', '### T02 Behavior\n\n依据：R1\n依赖：T01'))
        commands.review(self.root, 'demo', decision='approved', reason='independent new scope',
                        affected_tasks=[], expected_revision=self.state()['stateRevision'])
        self.assertEqual(previous, self.state()['tasks']['T01']['evidence'])
        self.assertIsNone(self.state()['verification'])
        self.assertIsNone(self.state()['tasks']['T03']['evidence'])

    def test_commit_equivalence_keeps_actual_evidence_current(self):
        import subprocess
        (self.root / 'source.py').write_text('tested = True\n')
        commands.record(self.root, 'demo', self.batch(), expected_revision=self.state()['stateRevision'])
        subprocess.run(['git', '-C', str(self.root), 'add', 'source.py'], check=True)
        subprocess.run(['git', '-C', str(self.root), '-c', 'user.name=Test', '-c', 'user.email=test@example.test', 'commit', '-qm', 'tested'], check=True)
        self.assertTrue(WorkItemQuery(self.root, 'demo').decision(check_code=True)['canComplete'])
        self.assertEqual(1, len(self.state()['evidence']))

    def test_delivery_stdin_and_explicit_no_affected_tasks_cli(self):
        import json
        import subprocess
        import sys
        from tests.support.items import ROOT
        commands.record(self.root, 'demo', self.batch(), expected_revision=self.state()['stateRevision'])
        previous = self.state()['tasks']['T01']['evidence']
        with (self.item / 'change.md').open('a') as handle:
            handle.write('\nIndependent new scope\n')
        result = subprocess.run([sys.executable, str(ROOT / 'scripts/kit.py'), 'item', 'approval', 'demo',
                                 '--decision', 'approved', '--reason', 'independent scope', '--no-affected-tasks',
                                 '--state-revision', self.state()['stateRevision'], '--root', str(self.root), '--json'],
                                capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual(previous, self.state()['tasks']['T01']['evidence'])
        result = subprocess.run([sys.executable, str(ROOT / 'scripts/kit.py'), 'item', 'delivery', 'demo',
                                 '--input', '-', '--state-revision', self.state()['stateRevision'],
                                 '--root', str(self.root), '--json'], input=json.dumps({'repositories': {'kit': {'version': 'tested', 'evidence': 'actual receipt'}}}),
                                capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual('tested', self.state()['delivery']['kit']['version'])
        self.assertFalse((self.item / 'artifacts/delivery-input.json').exists())
