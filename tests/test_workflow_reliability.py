"""Exercise actual dispatch boundaries using isolated workspace fixtures."""
from __future__ import annotations
import json
import os
import unittest
from unittest import mock
import tests.support.workflows as fixtures
import workbench.extensions.runner as workflow


class ActionReliabilityTest(unittest.TestCase):
    def create_linked_item(self):
        import subprocess
        from pathlib import Path
        from workbench.work_items import commands
        from workbench.work_items.store import item_path, load_state
        repository = self.root.parent / 'service'
        repository.mkdir()
        subprocess.run(['git', 'init', '-q', '--initial-branch=main', str(repository)], check=True)
        (repository/'file.txt').write_text('fixture')
        subprocess.run(['git', '-C', str(repository), 'add', '.'], check=True)
        subprocess.run(['git', '-C', str(repository), '-c', 'user.name=Fixture', '-c', 'user.email=test@example.test', 'commit', '-qm', 'fixture'], check=True)
        config = self.root / '.workspace/config/workspace.json'
        data = json.loads(config.read_text())
        data['repositories'] = [{'path':'service','aliases':[],'remote':None,'category':'service','description':'fixture','instruction':'repositories/service.md'}]
        config.write_text(json.dumps(data))
        self.root = self.root.resolve()
        commands.create(self.root, 'linked', title='Linked', repositories=['service'])
        workflow.start_run(self.root, run_id='linked-run', item_slug='linked', repository='service', branch='main')
        item = workflow.plan_result(self.root, 'linked-run', after='item.implement')['pending'][0]
        return commands, item_path(self.root, 'linked'), item

    def test_paused_cancelled_or_blocked_items_do_not_dispatch(self):
        from workbench.work_items.store import load_state
        commands, directory, item = self.create_linked_item()
        def dispatch():
            return workflow.run_action(self.root, 'linked-run', self.stage, item['planHash'], request_id='not-started')
        with mock.patch.object(workflow, 'run_provider', side_effect=AssertionError('unexpected external action')):
            commands.lifecycle(self.root, 'linked', 'paused', expected_revision=load_state(directory)['stateRevision'])
            with self.assertRaisesRegex(ValueError, 'WORKFLOW_ITEM_INACTIVE'): dispatch()
            commands.lifecycle(self.root, 'linked', 'active', expected_revision=load_state(directory)['stateRevision'])
            blocked = commands.block(self.root, 'linked', reason='等待', owner='测试', condition='环境可用', expected_revision=load_state(directory)['stateRevision'])
            with self.assertRaisesRegex(ValueError, 'WORKFLOW_ITEM_BLOCKED'): dispatch()
            commands.unblock(self.root, 'linked', blocked['blockerId'], reason='可用', expected_revision=load_state(directory)['stateRevision'])
            commands.cancel(self.root, 'linked', reason='取消', expected_revision=load_state(directory)['stateRevision'])
            with self.assertRaisesRegex(ValueError, 'WORKFLOW_ITEM_INACTIVE'): dispatch()

    def test_binding_change_blocks_dispatch_but_keeps_result_readable(self):
        from workbench.work_items.store import load_state
        commands, directory, item = self.create_linked_item()
        with mock.patch.object(workflow, 'run_provider', return_value={'status':'ok','diagnostics':[]}):
            workflow.run_action(self.root, 'linked-run', self.stage, item['planHash'], request_id='completed')
        bindings = load_state(directory)['bindings']; bindings[0]['workBranch']='feature/next'
        commands.update(self.root, 'linked', {'bindings':bindings}, reason='新分支', expected_revision=load_state(directory)['stateRevision'])
        self.assertEqual('succeeded', workflow.action_result(self.root, 'linked-run', 'completed')['status'])
        with self.assertRaisesRegex(ValueError, 'WORKFLOW_BINDING_CHANGED'):
            workflow.run_action(self.root, 'linked-run', self.stage, item['planHash'], request_id='new-request', previous_request_id='completed', reason='retry')

    def setUp(self):
        self.fixture = fixtures.WorkflowFixture()
        self.fixture.open()
        self.addCleanup(self.fixture.close)
        self.root = self.fixture.root
        self.stage = 'team-delivery.deploy-test'
        self.fixture.write_overlay([{'id': self.stage, 'after': 'item.implement', 'uses': 'action-extension/deploy-test'}])
        self.fixture.activate_overlay()
        workflow.start_run(self.root, run_id='reliable')
        self.item = workflow.plan_result(self.root, 'reliable', after='item.implement')['pending'][0]

    def execute(self, **kwargs):
        return workflow.run_action(self.root, 'reliable', self.stage, self.item['planHash'], **kwargs)

    def test_replay_dispatches_only_once_and_survives_extension_removal(self):
        with mock.patch.dict(os.environ, {'DEPLOY_TOKEN': 'secret-fixture'}):
            with mock.patch.object(workflow, 'run_provider', wraps=workflow.run_provider) as dispatch:
                first = self.execute(request_id='request-one')
                second = self.execute(request_id='request-one')
                self.assertEqual(1, dispatch.call_count)
        self.assertEqual('succeeded', first['status'])
        self.assertEqual(first['status'], second['status'])
        self.assertTrue(second['replayed'])
        with mock.patch.object(workflow, '_resolve', side_effect=AssertionError('extension unavailable')):
            result = workflow.action_result(self.root, 'reliable', 'request-one')
        self.assertEqual('succeeded', result['status'])
        self.assertNotIn('secret-fixture', json.dumps(result))

    def test_unknown_outcome_requires_reconciliation_before_retry(self):
        uncertain = {'status': 'failed', 'diagnostics': [{'code': 'PROVIDER_TIMEOUT'}]}
        with mock.patch.object(workflow, 'run_provider', return_value=uncertain) as dispatch:
            result = self.execute(request_id='timed-out')
            self.assertEqual('unknown', result['status'])
            self.execute(request_id='timed-out')
            self.assertEqual(1, dispatch.call_count)
        with self.assertRaisesRegex(ValueError, 'ACTION_RECONCILIATION_REQUIRED'):
            self.execute(request_id='retry-one', previous_request_id='timed-out', reason='retry')
        fixed = workflow.reconcile_action(self.root, 'reliable', 'timed-out', status='failed', summary='No deployment occurred', evidence='fixture:remote-status')
        self.assertEqual('reconciled', fixed['origin'])
        with mock.patch.dict(os.environ, {'DEPLOY_TOKEN': 'fixture'}):
            self.assertEqual('succeeded', self.execute(request_id='retry-one', previous_request_id='timed-out', reason='checked remote')['status'])
        self.assertEqual('failed', workflow.action_result(self.root, 'reliable', 'timed-out')['status'])

    def test_unknown_result_blocks_plan_and_cannot_be_hidden_by_skip(self):
        with mock.patch.object(workflow, 'run_provider', return_value={'status': 'failed', 'diagnostics': [{'code': 'PROVIDER_TIMEOUT'}]}):
            self.execute(request_id='unknown-result')
        plan = workflow.plan_result(self.root, 'reliable', after='item.implement')
        self.assertTrue(plan['blocked'])
        with self.assertRaisesRegex(ValueError, 'ACTION_RECONCILIATION_REQUIRED'):
            workflow.skip(self.root, 'reliable', self.stage, self.item['planHash'], reason='skip')

    def test_new_request_cannot_bypass_current_attempt_and_input_conflict_is_rejected(self):
        self.execute(request_id='first')  # Missing credential: known pre-launch failure.
        with self.assertRaisesRegex(ValueError, 'ACTION_RETRY_REQUIRED'):
            self.execute(request_id='another')
        with self.assertRaisesRegex(ValueError, 'ACTION_REQUEST_CONFLICT'):
            workflow.run_action(self.root, 'reliable', self.stage, '0' * 64, request_id='first')

    def test_result_survives_compatibility_summary_failure(self):
        original = workflow._record_stage
        def fail_final(*args, **kwargs):
            if kwargs['status'] == 'succeeded':
                raise OSError('simulated disk failure')
            return original(*args, **kwargs)
        with mock.patch.dict(os.environ, {'DEPLOY_TOKEN': 'fixture'}):
            with mock.patch.object(workflow, '_record_stage', side_effect=fail_final):
                first = self.execute(request_id='persisted')
        self.assertEqual('succeeded', first['status'])
        with mock.patch.object(workflow, 'run_provider', side_effect=AssertionError('duplicate execution')):
            self.assertEqual('succeeded', self.execute(request_id='persisted')['status'])
        self.assertEqual([], workflow.plan_result(self.root, 'reliable', after='item.implement')['pending'])

    def test_concurrent_duplicate_observes_running_without_second_dispatch(self):
        import concurrent.futures
        import threading
        entered = threading.Event()
        release = threading.Event()
        def slow(*args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise AssertionError('test coordinator failed')
            return {'status': 'ok', 'diagnostics': []}
        with mock.patch.object(workflow, 'run_provider', side_effect=slow) as dispatch:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(self.execute, request_id='concurrent')
                try:
                    self.assertTrue(entered.wait(5))
                    duplicate = self.execute(request_id='concurrent')
                    self.assertEqual('running', duplicate['status'])
                    self.assertTrue(duplicate['replayed'])
                    self.assertEqual(1, workflow.status_result(self.root)['running'])
                finally:
                    release.set()
                self.assertEqual('succeeded', future.result(5)['status'])
            self.assertEqual(1, dispatch.call_count)

    def test_killed_runner_leaves_unknown_and_never_restarts(self):
        import subprocess
        import sys
        import time
        # Synchronize against the mock dispatch, so SIGKILL lands after intent publication.
        marker = self.root / 'entered'
        code = '''import sys,time
from pathlib import Path
import workbench.extensions.runner as w
root=Path(sys.argv[1])
def provider(*a,**k):
    (root/'entered').write_text('started')
    time.sleep(30)
w.run_provider=provider
w.run_action(root,'reliable',sys.argv[2],sys.argv[3],request_id='killed')
'''
        process = subprocess.Popen([sys.executable, '-B', '-c', code, str(self.root), self.stage, self.item['planHash']],
                                   env={**os.environ, 'PYTHONPATH': str(workflow.KIT_ROOT)}, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 5
            while not marker.exists() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertTrue(marker.exists())
            process.kill()
            process.communicate(timeout=5)
            with mock.patch.object(workflow, 'run_provider', side_effect=AssertionError('unexpected restart')):
                self.assertEqual('unknown', self.execute(request_id='killed')['status'])
            self.assertEqual('unknown', workflow.action_result(self.root, 'reliable', 'killed')['status'])
        finally:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=5)


    def test_changed_predecessor_does_not_unlock_command(self):
        self.fixture.write_overlay([
            {'id': 'quality.check', 'after': 'item.implement', 'uses': 'action-extension/integration-test'},
            {'id': self.stage, 'after': 'quality.check', 'uses': 'action-extension/deploy-test'},
        ])
        self.fixture.activate_overlay()
        plan = workflow.plan_result(self.root, 'reliable', after='item.implement')
        workflow.finish(self.root, 'reliable', 'quality.check', plan['pending'][0]['planHash'], status='succeeded', summary='checked')
        self.fixture.write_overlay([
            {'id': 'quality.check', 'after': 'item.implement', 'uses': 'action-extension/integration-test', 'with': {'suite': 'changed'}},
            {'id': self.stage, 'after': 'quality.check', 'uses': 'action-extension/deploy-test'},
        ])
        self.fixture.activate_overlay()
        plan = workflow.plan_result(self.root, 'reliable', after='item.implement')
        item = next(row for row in plan['pending'] if row['stage'] == self.stage)
        with mock.patch.object(workflow, 'run_provider', return_value={'status': 'ok', 'diagnostics': []}) as dispatch:
            with self.assertRaisesRegex(ValueError, 'ACTION_BLOCKED'):
                workflow.run_action(self.root, 'reliable', self.stage, item['planHash'])
            dispatch.assert_not_called()
