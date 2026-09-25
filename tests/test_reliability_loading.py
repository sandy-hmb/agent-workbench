"""Regression scenarios for approval, artifacts and progressive context."""
import json
import subprocess
import sys
import unittest
from pathlib import Path
from tests.support import ROOT
from tests.support.items import ItemFixture
from workbench.work_items import commands
from workbench.work_items.query import WorkItemQuery
from workbench.work_items.store import WorkItemError, text_digest
from workbench.inspection import api


class ReliabilityLoadingTest(ItemFixture, unittest.TestCase):
    def setUp(self):
        self.open()
        self.addCleanup(self.close)

    def artifact(self):
        path = self.item / 'artifacts/migration.sql'
        path.parent.mkdir(exist_ok=True)
        path.write_text('select 1;\n')
        return path

    def record_artifact(self, task=None):
        path = self.artifact()
        payload = self.payload(task)
        payload['artifactRefs'] = [{'path': 'artifacts/migration.sql', 'sha256': text_digest(path.read_bytes()), 'bytes': path.stat().st_size, 'type': 'sql'}]
        commands.record(self.root, 'demo', payload, expected_revision=self.state()['stateRevision'])
        return path

    def test_changed_or_missing_batch_artifact_invalidates_completion(self):
        self.review()
        path = self.record_artifact()
        self.assertEqual('valid', WorkItemQuery(self.root, 'demo').verification(check_code=True)['applicability'])
        path.write_text('select 2;\n')
        self.assertEqual('invalid', WorkItemQuery(self.root, 'demo').verification(check_code=True)['applicability'])
        path.unlink()
        with self.assertRaisesRegex(WorkItemError, 'ITEM_NOT_READY'):
            commands.complete(self.root, 'demo', expected_revision=self.state()['stateRevision'])

    def test_task_artifact_is_checked_even_if_final_batch_has_no_artifact_refs(self):
        self.plan(); self.review()
        path = self.record_artifact('T01')
        self.record()
        path.write_text('select 3;\n')
        self.assertEqual('invalid', WorkItemQuery(self.root, 'demo').verification(check_code=True)['applicability'])

    def test_later_task_can_replace_earlier_artifact_version(self):
        self.plan(('T01', 'T02')); self.review()
        path = self.record_artifact('T01')
        path.write_text('select 2;\n')
        payload = self.payload('T02')
        payload['artifactRefs'] = [{'path': 'artifacts/migration.sql', 'sha256': text_digest(path.read_bytes()), 'bytes': path.stat().st_size, 'type': 'sql'}]
        commands.record(self.root, 'demo', payload, expected_revision=self.state()['stateRevision'])
        self.record()
        result = WorkItemQuery(self.root, 'demo').verification(check_code=True)
        self.assertEqual('valid', result['applicability'])
        self.assertEqual(1, len(result['artifactStates']))

    def test_unreferenced_large_artifact_does_not_block_resume_or_navigation(self):
        self.review()
        directory = self.item / 'artifacts'; directory.mkdir()
        (directory / 'fixture.json').write_text('a' * (1024 * 1024 + 1))
        self.assertEqual('demo', WorkItemQuery(self.root, 'demo').continuation()['itemSlug'])
        document = next(row for row in api.projection(self.root, 'demo', 'task')['documents'] if row['path'] == 'artifacts/fixture.json')
        self.assertFalse(document['readable'])

    def test_needs_review_handles_unregistered_dependent_draft(self):
        self.plan(); self.review(); self.record('T01')
        self.plan(('T01', 'T02'))
        plan = self.item / 'plan.md'; value = plan.read_text(); at = value.index('### T02')
        plan.write_text(value[:at] + value[at:].replace('依赖：无', '依赖：T01', 1))
        with (self.item / 'change.md').open('a') as handle: handle.write('\nChanged acceptance\n')
        commands.review(self.root, 'demo', decision='needs-review', reason='scope changed', roles=['change'], expected_revision=self.state()['stateRevision'])
        self.assertIsNone(self.state()['tasks']['T01']['evidence'])
        self.assertNotIn('T02', self.state()['tasks'])

    def test_needs_review_before_edit_invalidates_default_scope(self):
        self.plan(); self.review(); self.record('T01')
        commands.review(self.root, 'demo', decision='needs-review', reason='acceptance was mistaken', roles=['change'], expected_revision=self.state()['stateRevision'])
        self.assertIsNone(self.state()['tasks']['T01']['evidence'])

    def test_closed_item_rejects_new_pending_acceptance_but_allows_delivery_facts(self):
        self.review(); self.record()
        commands.complete(self.root, 'demo', expected_revision=self.state()['stateRevision'])
        with self.assertRaisesRegex(WorkItemError, 'ITERATION_REQUIRED'):
            commands.delivery(self.root, 'demo', {'externalChecks': [{'id': 'new', 'requirement': 'R1', 'description': 'new finding', 'owner': 'QA', 'status': 'failed', 'evidence': 'observed'}]}, expected_revision=self.state()['stateRevision'])
        commands.delivery(self.root, 'demo', {'repositories': {'kit': {'version': 'confirmed-version', 'evidence': 'release receipt'}}}, expected_revision=self.state()['stateRevision'])
        self.assertEqual('done', self.state()['lifecycle'])

    def test_handoff_contains_blocker_and_external_check_without_history(self):
        self.review()
        commands.block(self.root, 'demo', reason='WAIT_FOR_FIXTURE_ENV', owner='QA_OWNER', condition='ENV_READY', expected_revision=self.state()['stateRevision'])
        pack = api.handoff(self.root, 'demo')
        for required in ('WAIT_FOR_FIXTURE_ENV', 'QA_OWNER', 'ENV_READY'):
            self.assertIn(required, pack['content'])

    def test_delivery_document_is_in_current_navigation(self):
        self.artifact()
        (self.item / 'artifacts/frontend.md').write_text('Integration guide')
        value = api.projection(self.root, 'demo', 'task')
        self.assertIn('artifacts/frontend.md', [row['path'] for row in value['documents']])

    def test_collection_revision_is_stable_across_pages_and_changes_with_membership(self):
        values = [{'slug': f'item-{i}'} for i in range(201)]
        first = api._page(values, 0, 200); second = api._page(values, 200, 200)
        self.assertEqual(first['collectionRevision'], second['collectionRevision'])
        self.assertNotEqual(first['collectionRevision'], api._page(values + [{'slug': 'new'}], 0, 200)['collectionRevision'])


if __name__ == '__main__':
    unittest.main()
