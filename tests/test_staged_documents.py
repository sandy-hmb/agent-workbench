"""Major documents are prepared and approved through separate gates."""
import json
import subprocess
import sys
import unittest

from tests.support import ROOT
from tests.support.items import ItemFixture
from workbench.cli.brief import brief_result, brief_text
from workbench.cli.status import status_result
from workbench.inspection.api import projection
from workbench.work_items import commands
from workbench.work_items.query import WorkItemQuery
from workbench.work_items.store import WorkItemError, load_state


class StagedDocumentsTest(ItemFixture, unittest.TestCase):
    def setUp(self):
        self.open()
        self.addCleanup(self.close)
        result = commands.create(self.root, 'major', title='Major', repositories=['kit'],
                                 document_kind='requirements', risk='major')
        self.major = self.root / result['path']

    def major_state(self):
        return load_state(self.major)

    def approve(self, roles=None, decision='approved'):
        return commands.review(self.root, 'major', roles=roles, decision=decision,
                               reason='审阅实际内容', expected_revision=self.major_state()['stateRevision'])

    def prepare(self, role):
        return commands.prepare_document(self.root, 'major', role,
                                         expected_revision=self.major_state()['stateRevision'])

    def approved_package(self):
        self.approve()
        self.prepare('design')
        self.approve()
        self.prepare('plan')
        self.plan()
        (self.major / 'plan.md').write_bytes((self.item / 'plan.md').read_bytes())
        self.approve()

    def test_prepare_gates_and_readonly_stage_queries(self):
        self.assertFalse((self.major / 'design.md').exists())
        self.assertFalse((self.major / 'plan.md').exists())
        initial = self.major_state()
        for role in ('design', 'plan'):
            with self.assertRaisesRegex(WorkItemError, 'DOCUMENT_ORDER_INVALID'):
                self.prepare(role)
        self.assertEqual(initial, self.major_state())
        brief = brief_result(self.root, 'major')
        self.assertEqual({'requirements': 'pending', 'design': 'not_ready', 'plan': 'not_ready'}, brief['reviews'])
        self.assertEqual({'currentDocument': 'requirements', 'nextDocument': None}, brief['documentProgress'])
        self.assertIn('文档阶段：requirements', brief_text(brief))
        self.approve()
        before = self.major_state()
        for facts in (brief_result(self.root, 'major'), status_result(self.root, item_slug='major')['item'],
                      projection(self.root, 'major', 'summary')['summary']):
            self.assertEqual({'currentDocument': 'design', 'nextDocument': 'design'}, facts['documentProgress'])
            self.assertEqual('missing', facts['reviews']['design'])
            self.assertEqual('not_ready', facts['reviews']['plan'])
            self.assertEqual('item.design', facts['currentStage'])
        self.assertEqual(before, self.major_state())
        self.prepare('design')
        self.assertTrue((self.major / 'design.md').exists())
        self.assertFalse((self.major / 'plan.md').exists())
        with self.assertRaisesRegex(WorkItemError, 'DOCUMENT_ORDER_INVALID'):
            self.prepare('plan')
        self.approve()
        self.assertEqual('plan', brief_result(self.root, 'major')['documentProgress']['nextDocument'])
        self.prepare('plan')
        self.assertEqual('plan', brief_result(self.root, 'major')['documentProgress']['currentDocument'])

    def test_prepare_cli_never_overwrites_and_checks_revision(self):
        self.approve()
        args = [sys.executable, str(ROOT / 'scripts/kit.py'), 'item', 'prepare', 'major',
                '--root', str(self.root), '--role', 'design', '--state-revision',
                self.major_state()['stateRevision'], '--json']
        result = subprocess.run(args, capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertTrue(json.loads(result.stdout)['created'])
        path = self.major / 'design.md'
        path.write_text('# User content\n')
        result = subprocess.run(args, capture_output=True, text=True)
        self.assertEqual(1, result.returncode)
        self.assertEqual('DOCUMENT_EXISTS', json.loads(result.stdout)['error']['code'])
        self.assertEqual('# User content\n', path.read_text())
        with self.assertRaisesRegex(WorkItemError, 'STATE_CHANGED'):
            commands.prepare_document(self.root, 'major', 'plan', expected_revision='sha256:' + '0' * 64)
        self.assertFalse((self.major / 'plan.md').exists())

    def test_manual_markdown_does_not_bypass_approval_order_or_multi_role_gate(self):
        # Editors can write files directly; only Kit preparation and approval
        # are controlled.  Premature files cannot unlock downstream approval.
        (self.major / 'design.md').write_text('# Design\n')
        (self.major / 'plan.md').write_text('# Plan\n')
        before = self.major_state()
        for roles in (['design'], ['plan']):
            with self.subTest(roles=roles):
                with self.assertRaisesRegex(WorkItemError, 'REVIEW_ORDER_INVALID'):
                    self.approve(roles)
                self.assertEqual(before, self.major_state())
        for roles in (['requirements', 'design', 'plan'], ['design', 'requirements']):
            with self.subTest(roles=roles):
                with self.assertRaisesRegex(WorkItemError, 'REVIEW_INVALID'):
                    self.approve(roles)
                self.assertEqual(before, self.major_state())
        self.approve()
        self.assertEqual({'requirements'}, self.major_state()['reviews'].keys())
        for roles in (['plan'],):
            with self.assertRaisesRegex(WorkItemError, 'REVIEW_ORDER_INVALID'):
                self.approve(roles)
        with self.assertRaisesRegex(WorkItemError, 'REVIEW_INVALID'):
            self.approve(['design', 'plan'])
        self.approve()
        self.assertEqual({'requirements', 'design'}, self.major_state()['reviews'].keys())
        self.approve()
        self.assertTrue(all(value == 'approved' for value in WorkItemQuery(self.root, 'major').review_state().values()))

    def test_major_explicit_approval_accepts_only_one_role(self):
        self.approve()
        self.prepare('design')
        with self.assertRaisesRegex(WorkItemError, 'REVIEW_INVALID'):
            self.approve(['requirements', 'design'])
        self.assertEqual({'requirements'}, self.major_state()['reviews'].keys())

    def test_substantive_requirements_change_cascades_even_after_reapproval(self):
        self.approved_package()
        path = self.major / 'requirements.md'
        path.write_text(path.read_text() + '\nNew requirement\n')
        self.approve()
        self.assertEqual('approved', self.major_state()['reviews']['requirements']['decision'])
        for role in ('design', 'plan'):
            self.assertEqual('needs-review', self.major_state()['reviews'][role]['decision'])
        with self.assertRaisesRegex(WorkItemError, 'REVIEW_ORDER_INVALID'):
            self.approve(['plan'])
        self.approve()
        self.assertEqual('pending', WorkItemQuery(self.root, 'major').review_state()['plan'])
        self.approve()
        path = self.major / 'design.md'
        path.write_text(path.read_text() + '\nNew design\n')
        self.approve(['design'])
        self.assertEqual('needs-review', self.major_state()['reviews']['plan']['decision'])

    def test_needs_review_cascades_and_editorial_unchanged_preserves_approval(self):
        self.approved_package()
        for role in ('requirements', 'design'):
            path = self.major / (role + '.md')
            path.write_text(path.read_text() + '\n')
            self.approve(decision='unchanged')
            self.assertTrue(all(value == 'approved' for value in WorkItemQuery(self.root, 'major').review_state().values()))
        self.approve(['requirements'], decision='needs-review')
        self.approve()
        self.assertEqual('needs-review', self.major_state()['reviews']['design']['decision'])
        self.assertEqual('needs-review', self.major_state()['reviews']['plan']['decision'])

    def test_ordinary_change_stays_light_and_approves_optional_plan_together(self):
        self.assertFalse((self.item / 'plan.md').exists())
        self.assertNotIn('documentProgress', brief_result(self.root, 'demo'))
        commands.prepare_document(self.root, 'demo', 'plan', expected_revision=self.state()['stateRevision'])
        self.plan()
        self.review()
        self.assertEqual({'change': 'approved', 'plan': 'approved'}, WorkItemQuery(self.root, 'demo').review_state())
        self.assertEqual(['T01'], self.state()['currentTasks'])


if __name__ == '__main__':
    unittest.main()
