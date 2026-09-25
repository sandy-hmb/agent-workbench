"""Behavioral acceptance tests for the single-state workflow."""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import workbench.work_items.commands as actions
from workbench.work_items.query import WorkItemQuery
from workbench.work_items.store import WorkItemError, load_state


from tests.support.items import ItemFixture


class WorkItemV2Test(ItemFixture, unittest.TestCase):
    def setUp(self):
        self.open()
        self.addCleanup(self.close)

    def test_ordinary_feature_finishes_with_one_batch(self):
        self.review()
        self.assertEqual([], WorkItemQuery(self.root, 'demo').tasks())
        self.record()
        result = actions.complete(self.root, 'demo', expected_revision=self.state()['stateRevision'])
        self.assertEqual('done', result['state']['lifecycle'])
        self.assertFalse((self.item / 'plan.md').exists())
        self.assertIn('(README.md)', (self.item / 'verification.md').read_text())

    def test_task_record_updates_progress_without_checkbox(self):
        self.plan()
        self.review()
        self.record('T01')
        self.assertEqual(1, WorkItemQuery(self.root, 'demo').summary()['progress']['completed'])
        self.assertNotIn('[x]', (self.item / 'plan.md').read_text())

    def test_reused_task_id_cannot_complete_new_iteration(self):
        self.plan()
        self.review()
        self.record('T01')
        self.record()
        actions.complete(self.root, 'demo', expected_revision=self.state()['stateRevision'])
        actions.next_iteration(self.root, 'demo', expected_revision=self.state()['stateRevision'])
        with self.assertRaisesRegex(WorkItemError, 'TASK_ID_REUSED'):
            self.review()
        self.plan(('T02',))
        self.review()
        self.assertEqual(0, WorkItemQuery(self.root, 'demo').summary()['progress']['completed'])
        self.assertIsNone(self.state()['verification'])

    def test_failed_latest_record_replaces_previous_success(self):
        self.plan()
        self.review()
        self.record('T01')
        self.record('T01', 'failed')
        self.assertEqual(0, WorkItemQuery(self.root, 'demo').summary()['progress']['completed'])

    def test_zero_tests_cannot_be_recorded_as_passed(self):
        self.review()
        payload = self.payload()
        payload['checks'][0]['executed'] = 0
        with self.assertRaisesRegex(WorkItemError, 'EVIDENCE_CHECK_INVALID'):
            actions.record(self.root, 'demo', payload, expected_revision=self.state()['stateRevision'])

    def test_summary_failure_does_not_undo_record_and_replay_is_idempotent(self):
        self.review()
        payload, revision = self.payload(), self.state()['stateRevision']
        with mock.patch.object(actions, 'render', side_effect=OSError('disk fixture')):
            first = actions.record(self.root, 'demo', payload, expected_revision=revision)
        self.assertTrue(first['committed'])
        self.assertFalse(first['summaryUpdated'])
        second = actions.record(self.root, 'demo', payload, expected_revision=revision)
        self.assertTrue(second['replayed'])
        self.assertEqual(1, len(self.state()['evidence']))

    def test_state_write_failure_leaves_no_completion(self):
        self.review()
        with mock.patch.object(actions, 'save_state', side_effect=OSError('disk fixture')):
            with self.assertRaises(OSError):
                self.record()
        self.assertIsNone(self.state()['verification'])

    def test_editorial_change_preserves_approval_and_task_evidence(self):
        self.plan()
        self.review()
        self.record('T01')
        with (self.item / 'plan.md').open('a') as handle:
            handle.write('\n')
        self.review('unchanged')
        self.assertEqual(1, WorkItemQuery(self.root, 'demo').summary()['progress']['completed'])

    def test_external_checks_block_completion(self):
        self.review()
        self.record()
        actions.delivery(self.root, 'demo', {'externalChecks': [{'id': 'acceptance', 'requirement': 'R1', 'description': '真实联调', 'owner': '测试', 'status': 'pending', 'evidence': ''}]}, expected_revision=self.state()['stateRevision'])
        with self.assertRaisesRegex(WorkItemError, 'ITEM_NOT_READY'):
            actions.complete(self.root, 'demo', expected_revision=self.state()['stateRevision'])

    def test_recorded_pass_is_not_current_code_pass(self):
        self.review()
        self.record()
        (self.root / 'changed.py').write_text('# changed\n')
        reader = WorkItemQuery(self.root, 'demo')
        self.assertEqual('not_checked', reader.verification()['applicability'])
        self.assertEqual('invalid', reader.verification(check_code=True)['applicability'])

    def test_explicit_feature_does_not_read_another_feature(self):
        other = self.item.parent / 'other'
        other.mkdir()
        (other / 'state.json').write_text('broken')
        self.assertEqual('demo', WorkItemQuery(self.root, 'demo').summary()['slug'])

    def test_new_iteration_does_not_expose_old_task_queue(self):
        self.plan(); self.review(); self.record('T01'); self.record()
        actions.complete(self.root, 'demo', expected_revision=self.state()['stateRevision'])
        actions.next_iteration(self.root, 'demo', expected_revision=self.state()['stateRevision'])
        reader = WorkItemQuery(self.root, 'demo')
        self.assertEqual([], reader.task_states())
        self.assertEqual([], reader.decision()['readyTasks'])
        self.assertIn('上次整体验证：unknown', (self.item / 'verification.md').read_text())

    def test_deleting_plan_cannot_hide_unfinished_registered_tasks(self):
        self.plan(); self.review()
        (self.item / 'plan.md').unlink()
        with self.assertRaisesRegex(WorkItemError, 'REVIEW_REQUIRED'):
            self.record()

    def test_task_number_aliases_are_rejected(self):
        self.plan(('T01', 'T001'))
        with self.assertRaisesRegex(WorkItemError, 'TASK_ID_INVALID'):
            self.review()

    def test_reference_change_requires_review_but_delivery_notes_do_not(self):
        references = self.item / 'references'; references.mkdir()
        attachment = references / 'contract.md'; attachment.write_text('# Contract\n')
        (self.item / 'change.md').write_text('# Change\n\n[契约](references/contract.md)\n')
        self.review()
        attachment.write_text('# New contract\n')
        self.assertEqual('changed', WorkItemQuery(self.root, 'demo').review_state()['change'])
        self.review()
        artifacts = self.item / 'artifacts'; artifacts.mkdir()
        (artifacts / 'release.md').write_text('# Release environment\n')
        self.assertEqual('approved', WorkItemQuery(self.root, 'demo').review_state()['change'])

    def test_material_change_invalidates_dependents(self):
        self.plan(('T01', 'T02'))
        path = self.item / 'plan.md'
        text = path.read_text()
        position = text.index('### T02')
        path.write_text(text[:position] + text[position:].replace('依赖：无', '依赖：T01', 1))
        self.review(); self.record('T01'); self.record('T02')
        actions.review(self.root, 'demo', decision='needs-review', reason='验收调整', roles=['change'],
                       affected_tasks=['T01'], expected_revision=self.state()['stateRevision'])
        self.assertTrue(all(not task['completed'] for task in WorkItemQuery(self.root, 'demo').task_states()))

    def test_dependencies_block_premature_completion(self):
        self.plan(('T01', 'T02'))
        path = self.item / 'plan.md'; text = path.read_text(); at = text.index('### T02')
        path.write_text(text[:at] + text[at:].replace('依赖：无', '依赖：T01', 1))
        self.review()
        with self.assertRaisesRegex(WorkItemError, 'TASK_DEPENDENCY_PENDING'):
            self.record('T02')

    def test_evidence_hash_corruption_cannot_look_completed(self):
        self.plan(); self.review(); result = self.record('T01')
        from workbench.work_items.store import evidence_path
        target = evidence_path(self.item, result['id'])
        value = json.loads(target.read_text()); value['result'] = 'failed'
        target.write_text(json.dumps(value))
        with self.assertRaisesRegex(WorkItemError, 'EVIDENCE_HASH_MISMATCH'):
            WorkItemQuery(self.root, 'demo').task_states()

    def test_stale_state_cannot_overwrite_newer_delivery(self):
        revision = self.state()['stateRevision']
        self.review()
        with self.assertRaisesRegex(WorkItemError, 'STATE_CHANGED'):
            actions.delivery(self.root, 'demo', {'externalChecks': []}, expected_revision=revision)

    def test_skipped_tests_and_compile_only_behavior_cannot_pass(self):
        self.plan(); self.review()
        payload = self.payload('T01')
        payload['checks'][0]['skipped'] = 1
        with self.assertRaisesRegex(WorkItemError, 'EVIDENCE_CHECK_INVALID'):
            actions.record(self.root, 'demo', payload, expected_revision=self.state()['stateRevision'])
        payload['checks'][0].update(type='编译', skipped=0)
        with self.assertRaisesRegex(WorkItemError, 'EVIDENCE_KIND_INSUFFICIENT'):
            actions.record(self.root, 'demo', payload, expected_revision=self.state()['stateRevision'])

    def test_inspect_other_local_branch_does_not_checkout(self):
        self.review(); self.record()
        subprocess.run(['git', '-C', str(self.root), 'switch', '-qc', 'unrelated'], check=True)
        (self.root / 'unrelated.py').write_text('# unrelated')
        result = WorkItemQuery(self.root, 'demo').verification(check_code=True, browse=True)
        self.assertEqual('valid', result['applicability'])
        self.assertEqual('local-branch', result['repositoryStates'][0]['source'])
        self.assertEqual('unrelated', subprocess.check_output(['git', '-C', str(self.root), 'branch', '--show-current'], text=True).strip())


if __name__ == '__main__':
    unittest.main()
