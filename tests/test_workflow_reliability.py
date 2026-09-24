"""Exercise actual dispatch boundaries using isolated workspace fixtures."""
from __future__ import annotations
import json
import os
import unittest
from unittest import mock
import test_workspace_workflow as fixtures
import workspace_workflow as workflow


class ActionReliabilityTest(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.WorkspaceWorkflowTest()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.root = self.fixture.root
        self.stage = 'team-delivery.deploy-test'
        self.fixture.write_overlay([{'id': self.stage, 'after': 'feature.implement', 'uses': 'action-extension/deploy-test'}])
        self.fixture.activate_overlay()
        workflow.start_run(self.root, run_id='reliable')
        self.item = workflow.plan_result(self.root, 'reliable', after='feature.implement')['pending'][0]

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
        self.assertEqual([], workflow.plan_result(self.root, 'reliable', after='feature.implement')['pending'])

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
import workspace_workflow as w
root=Path(sys.argv[1])
def provider(*a,**k):
    (root/'entered').write_text('started')
    time.sleep(30)
w.run_provider=provider
w.run_action(root,'reliable',sys.argv[2],sys.argv[3],request_id='killed')
'''
        process = subprocess.Popen([sys.executable, '-B', '-c', code, str(self.root), self.stage, self.item['planHash']],
                                   env={**os.environ, 'PYTHONPATH': str(workflow.SCRIPT_DIR)}, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
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

    def test_old_running_requires_explicit_reconciliation_without_running_code(self):
        path = self.root / '.workspace/runs/reliable.json'
        run = json.loads(path.read_text())
        run['stages'][self.stage] = {'fingerprint': self.item['fingerprint'], 'status': 'running',
                                    'updatedAt': '2026-09-24T00:00:00Z', 'summary': 'legacy'}
        path.write_text(json.dumps(run))
        with mock.patch.object(workflow, 'run_provider', side_effect=AssertionError('must not execute')):
            with self.assertRaisesRegex(ValueError, 'ACTION_RECONCILIATION_REQUIRED'):
                self.execute()

        fixed = workflow.reconcile_action(self.root, 'reliable', self.item['requestId'], status='succeeded', summary='Deployment confirmed', evidence='fixture:deployment-id')
        self.assertEqual('reconciled', fixed['origin'])
        self.assertTrue(fixed['legacy'])
        self.assertEqual([], workflow.plan_result(self.root, 'reliable', after='feature.implement')['pending'])

    def test_changed_predecessor_does_not_unlock_command(self):
        self.fixture.write_overlay([
            {'id': 'quality.check', 'after': 'feature.implement', 'uses': 'action-extension/integration-test'},
            {'id': self.stage, 'after': 'quality.check', 'uses': 'action-extension/deploy-test'},
        ])
        self.fixture.activate_overlay()
        plan = workflow.plan_result(self.root, 'reliable', after='feature.implement')
        workflow.finish(self.root, 'reliable', 'quality.check', plan['pending'][0]['planHash'], status='succeeded', summary='checked')
        self.fixture.write_overlay([
            {'id': 'quality.check', 'after': 'feature.implement', 'uses': 'action-extension/integration-test', 'with': {'suite': 'changed'}},
            {'id': self.stage, 'after': 'quality.check', 'uses': 'action-extension/deploy-test'},
        ])
        self.fixture.activate_overlay()
        plan = workflow.plan_result(self.root, 'reliable', after='feature.implement')
        item = next(row for row in plan['pending'] if row['stage'] == self.stage)
        with mock.patch.object(workflow, 'run_provider', return_value={'status': 'ok', 'diagnostics': []}) as dispatch:
            with self.assertRaisesRegex(ValueError, 'ACTION_BLOCKED'):
                workflow.run_action(self.root, 'reliable', self.stage, item['planHash'])
            dispatch.assert_not_called()
