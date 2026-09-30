"""Required Action evidence must remain valid until a WorkItem is completed."""
from __future__ import annotations

import fcntl
import json
import subprocess
import unittest
from unittest import mock

from tests.support.workflows import WorkflowFixture
from workbench.extensions import attempts, runner
from workbench.extensions.management import extension_lock
from workbench.inspection import api as inspect
from workbench.work_items import commands
from workbench.work_items.query import WorkItemQuery
from workbench.work_items.store import WorkItemError, item_path, load_state


class WorkflowAcceptanceTest(unittest.TestCase):
    def setUp(self):
        self.fixture = WorkflowFixture(); self.fixture.open(); self.addCleanup(self.fixture.close)
        self.root = self.fixture.root.resolve()
        repository = self.root.parent / 'service'; repository.mkdir()
        (repository / 'AGENTS.md').write_text('# Rules\n')
        subprocess.run(['git', 'init', '-q', '--initial-branch=main', str(repository)], check=True)
        subprocess.run(['git', '-C', str(repository), 'add', '.'], check=True)
        subprocess.run(['git', '-C', str(repository), '-c', 'user.name=Fixture',
                        '-c', 'user.email=test@example.test', 'commit', '-qm', 'fixture'], check=True)
        config = self.root / '.workspace/config/workspace.json'
        value = json.loads(config.read_text())
        value['repositories'] = [{'path': 'service', 'aliases': [], 'remote': None, 'category': 'service',
                                 'description': 'fixture', 'instruction': 'repositories/service.md'}]
        config.write_text(json.dumps(value))
        for path in ['AGENTS.md', '.workspace/AGENTS.md', '.workspace/CONTEXT.md', '.workspace/repositories/service.md']:
            (self.root / path).write_text('# Fixture\n')
        commands.create(self.root, 'demo', title='Demo', repositories=['service'])
        self.item = item_path(self.root, 'demo')
        self.verify_item()
        self.stage = {'id': 'team.check', 'after': 'item.verify', 'uses': 'action-extension/deploy-test'}
        self.fixture.write_overlay([self.stage]); self.fixture.activate_overlay()
        runner.start_run(self.root, item_slug='demo')
        self.plan = runner.plan_result(self.root, 'demo-i01', after='item.verify')['pending'][0]
        self.ref = {'kind': 'workflow', 'runId': 'demo-i01', 'requestId': 'first', 'stage': 'team.check'}

    def state(self):
        return load_state(self.item)

    def verify_item(self):
        commands.review(self.root, 'demo', decision='approved', reason='Reviewed scope',
                        expected_revision=self.state()['stateRevision'])
        commands.record(self.root, 'demo', {'scope': 'item', 'taskId': None,
            'recordedAt': '2026-09-30T10:00:00+08:00', 'result': 'passed', 'reviewResult': 'passed',
            'codeState': WorkItemQuery(self.root, 'demo').code_state(),
            'checks': [{'type': '测试', 'workingDirectory': 'service', 'command': 'fixture-check',
                        'target': 'behavior', 'executed': 1, 'skipped': 0, 'exitStatus': 0, 'result': 'passed'}]},
            expected_revision=self.state()['stateRevision'])

    def execute(self, status='succeeded', request='first'):
        latest = attempts.latest(attempts.read(self.root, 'demo-i01'), 'team.check')
        result = {'status': 'ok' if status == 'succeeded' else 'failed',
                  'diagnostics': [] if status == 'succeeded' else [{'code': 'PROVIDER_TIMEOUT' if status == 'unknown' else 'CHECK_FAILED'}]}
        with mock.patch.object(runner, 'run_provider', return_value=result):
            return runner.run_action(self.root, 'demo-i01', 'team.check', self.plan['planHash'], request_id=request,
                                     previous_request_id=latest['requestId'] if latest else None, reason='Recheck')

    def deliver(self, status, *, ref=True):
        check = {'id': 'quality', 'requirement': 'R1', 'description': '接口检查', 'owner': '测试',
                 'status': status, 'evidence': '已核对结果' if status != 'pending' else ''}
        if ref:
            check['evidenceRefs'] = [dict(self.ref)]
        return commands.delivery(self.root, 'demo', {'externalChecks': [check]}, expected_revision=self.state()['stateRevision'])

    def complete(self):
        return commands.complete(self.root, 'demo', expected_revision=self.state()['stateRevision'])

    def test_declared_check_needs_explicit_acceptance_after_success(self):
        self.deliver('pending', ref=False)
        self.execute()
        with self.assertRaisesRegex(WorkItemError, 'ITEM_NOT_READY'):
            self.complete()
        self.deliver('passed')
        # Stored status is a display snapshot; round trips accept it and read the actual attempt.
        checks = self.state()['externalChecks']
        self.assertFalse(commands.delivery(self.root, 'demo', {'externalChecks': checks},
                         expected_revision=self.state()['stateRevision'])['changed'])
        self.assertEqual('done', self.complete()['state']['lifecycle'])

    def test_unrequired_action_failure_does_not_block_completion(self):
        self.execute('failed')
        self.assertEqual('done', self.complete()['state']['lifecycle'])

    def test_failed_or_skipped_action_cannot_be_recorded_as_passed(self):
        self.execute('failed')
        before = self.state()
        with self.assertRaisesRegex(WorkItemError, 'WORKFLOW_CHECK_INVALID'):
            self.deliver('passed')
        self.assertEqual(before, self.state())
        skipped = runner.skip(self.root, 'demo-i01', 'team.check', self.plan['planHash'], reason='用户明确豁免')
        self.ref['requestId'] = skipped['requestId']
        with self.assertRaisesRegex(WorkItemError, 'WORKFLOW_CHECK_INVALID'):
            self.deliver('passed')
        self.deliver('waived')
        self.assertEqual('done', self.complete()['state']['lifecycle'])

    def test_unknown_result_requires_reconciliation_even_for_waiver(self):
        self.execute('unknown')
        for status in ['passed', 'waived']:
            with self.subTest(status=status), self.assertRaisesRegex(WorkItemError, 'WORKFLOW_CHECK_INVALID'):
                self.deliver(status)
        runner.reconcile_action(self.root, 'demo-i01', 'first', status='failed',
                                summary='已确认未完成', evidence='目标系统记录')
        self.deliver('waived')
        self.assertEqual('done', self.complete()['state']['lifecycle'])

    def test_retry_invalidates_accepted_evidence_in_every_completion_view(self):
        self.execute(); self.deliver('passed')
        self.execute('failed', 'second')
        before = self.state()
        view = inspect.projection(self.root, 'demo', 'summary')
        self.assertFalse(view['summary']['completionAction']['available'])
        check = view['summary']['verification']['pendingExternalChecks'][0]
        self.assertEqual('passed', check['recordedStatus'])
        self.assertEqual('pending', check['status'])
        self.assertIn('最新', check['reason'])
        self.assertEqual(before, self.state())
        commands.render(self.root, 'demo')
        self.assertIn('pending', (self.item / 'README.md').read_text())
        with self.assertRaisesRegex(WorkItemError, 'ITEM_NOT_READY'):
            self.complete()
        with self.assertRaisesRegex(WorkItemError, 'WORKFLOW_CHECK_INVALID'):
            self.deliver('passed')
        with self.assertRaisesRegex(WorkItemError, 'WORKFLOW_CHECK_INVALID'):
            self.deliver('passed', ref=False)
        with self.assertRaisesRegex(WorkItemError, 'DELIVERY_INVALID'):
            commands.delivery(self.root, 'demo', {'externalChecks': []}, expected_revision=self.state()['stateRevision'])
        self.execute('succeeded', 'third'); self.ref['requestId'] = 'third'; self.deliver('passed')
        self.assertEqual('done', self.complete()['state']['lifecycle'])

    def test_changed_action_configuration_requires_new_acceptance(self):
        self.execute(); self.deliver('passed')
        self.fixture.write_overlay([{**self.stage, 'with': {'environment': 'other'}}]); self.fixture.activate_overlay()
        self.assertFalse(WorkItemQuery(self.root, 'demo').decision(check_code=True)['canComplete'])
        with self.assertRaisesRegex(WorkItemError, 'ITEM_NOT_READY'):
            self.complete()
        with self.assertRaisesRegex(WorkItemError, 'WORKFLOW_CHECK_INVALID'):
            self.deliver('passed')

    def test_partial_reference_update_cannot_drop_an_unknown_required_stage(self):
        self.fixture.write_overlay([self.stage, {'id': 'team.other', 'after': 'item.implement',
                                               'uses': 'action-extension/integration-test'}])
        self.fixture.activate_overlay()
        self.execute()
        other = runner.plan_result(self.root, 'demo-i01', after='item.implement')['pending'][0]
        result = runner.finish(self.root, 'demo-i01', 'team.other', other['planHash'], status='succeeded', summary='checked')
        other_ref = {**self.ref, 'stage': 'team.other', 'requestId': result['requestId']}
        self.deliver('passed')
        checks = self.state()['externalChecks']; checks[0]['evidenceRefs'].append(other_ref)
        commands.delivery(self.root, 'demo', {'externalChecks': checks}, expected_revision=self.state()['stateRevision'])
        self.execute('unknown', 'second')
        checks[0]['evidenceRefs'] = [other_ref]
        for status in ['passed', 'waived']:
            checks[0]['status'] = status
            with self.subTest(status=status), self.assertRaisesRegex(WorkItemError, 'WORKFLOW_CHECK_INVALID'):
                commands.delivery(self.root, 'demo', {'externalChecks': checks}, expected_revision=self.state()['stateRevision'])
        self.assertEqual(2, len(self.state()['externalChecks'][0]['evidenceRefs']))

    def test_transitive_predecessor_failure_invalidates_acceptance(self):
        self.fixture.write_overlay([self.stage,
            {'id': 'team.middle', 'after': 'team.check', 'uses': 'action-extension/integration-test'},
            {'id': 'team.last', 'after': 'team.middle', 'uses': 'action-extension/integration-test'}])
        self.fixture.activate_overlay()
        self.execute()
        for stage in ['team.middle', 'team.last']:
            plan = runner.plan_result(self.root, 'demo-i01', after='item.verify')['pending'][0]
            result = runner.finish(self.root, 'demo-i01', stage, plan['planHash'], status='succeeded', summary='checked')
        self.ref.update(stage='team.last', requestId=result['requestId'])
        self.deliver('passed')
        for status in ['failed', 'unknown']:
            self.execute(status, status)
            with self.subTest(status=status):
                self.assertFalse(WorkItemQuery(self.root, 'demo').decision(check_code=True)['canComplete'])
                with self.assertRaisesRegex(WorkItemError, 'WORKFLOW_CHECK_INVALID'):
                    self.deliver('passed')
                with self.assertRaisesRegex(WorkItemError, 'ITEM_NOT_READY'):
                    self.complete()
                with self.assertRaisesRegex(ValueError, 'ACTION_BLOCKED'):
                    runner.finish(self.root, 'demo-i01', 'team.last', plan['planHash'], status='succeeded', summary='rechecked')

    def test_new_run_cannot_replace_an_unknown_result(self):
        self.execute(); self.deliver('passed'); self.execute('unknown', 'second')
        runner.start_run(self.root, run_id='replacement', item_slug='demo')
        plan = runner.plan_result(self.root, 'replacement', after='item.verify')['pending'][0]
        with mock.patch.object(runner, 'run_provider', return_value={'status': 'ok', 'diagnostics': []}):
            runner.run_action(self.root, 'replacement', 'team.check', plan['planHash'], request_id='replacement')
        self.ref.update(runId='replacement', requestId='replacement')
        with self.assertRaisesRegex(WorkItemError, 'WORKFLOW_CHECK_INVALID'):
            self.deliver('passed')
        runner.reconcile_action(self.root, 'demo-i01', 'second', status='failed', summary='未完成', evidence='目标回执')
        self.deliver('passed')
        self.assertEqual('done', self.complete()['state']['lifecycle'])

    def test_previous_iteration_cannot_unlock_current_completion(self):
        self.execute(); self.deliver('passed'); self.complete()
        commands.next_iteration(self.root, 'demo', expected_revision=self.state()['stateRevision'])
        self.verify_item()
        with self.assertRaisesRegex(WorkItemError, 'ITEM_NOT_READY'):
            self.complete()
        with self.assertRaisesRegex(WorkItemError, 'WORKFLOW_CHECK_INVALID'):
            self.deliver('passed')
        # A reviewed scope change may waive a terminal result from the previous iteration.
        self.deliver('waived')
        self.assertEqual('done', self.complete()['state']['lifecycle'])

    def test_binding_change_invalidates_action_evidence(self):
        self.execute(); self.deliver('passed')
        bindings = self.state()['bindings']; bindings[0]['baseBranch'] = 'other-base'
        commands.update(self.root, 'demo', {'bindings': bindings}, reason='已调整基线',
                        expected_revision=self.state()['stateRevision'])
        with self.assertRaisesRegex(WorkItemError, 'WORKFLOW_CHECK_INVALID'):
            self.deliver('passed')

    def test_missing_run_blocks_active_check_but_preserves_completed_history(self):
        self.execute(); self.deliver('passed')
        path = self.root / '.workspace/runs/demo-i01.json'; data = path.read_bytes(); path.unlink()
        self.assertFalse(WorkItemQuery(self.root, 'demo').decision(check_code=True)['canComplete'])
        with self.assertRaisesRegex(WorkItemError, 'ITEM_NOT_READY'):
            self.complete()
        path.write_bytes(data); self.complete(); path.unlink()
        self.assertEqual('COMPLETE', WorkItemQuery(self.root, 'demo').decision()['executionDecision'])
        self.assertEqual('passed', WorkItemQuery(self.root, 'demo').verification()['pendingExternalChecks'][0]['status'])

    def test_concurrent_action_cannot_race_acceptance_or_completion(self):
        self.execute(); self.deliver('passed')
        before = self.state()
        with extension_lock(self.root, fcntl.LOCK_EX, create_cache=False):
            for operation in [lambda: self.deliver('passed'), self.complete]:
                with self.assertRaisesRegex(ValueError, 'EXTENSION_BUSY'):
                    operation()
        self.assertEqual(before, self.state())


if __name__ == '__main__':
    unittest.main()
