"""Focused items keep constraints, while old records remain untouched."""
import json
import subprocess
import sys
import unittest

from tests.support import ROOT
from tests.support.items import ItemFixture
from workbench.cli.brief import brief_result, brief_text
from workbench.work_items import commands, focused
from workbench.work_items.query import WorkItemQuery
from workbench.work_items.store import WorkItemError, save_state


class FocusedDocumentsTest(ItemFixture, unittest.TestCase):
    def setUp(self):
        self.open(); self.addCleanup(self.close)

    def test_new_policy_and_small_layout_without_sidecars(self):
        self.assertEqual('focused-v1', self.state()['documentPolicy'])
        self.assertEqual({'README.md', 'change.md'}, {p.name for p in self.item.glob('*.md')})
        before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.item.rglob('*') if p.is_file()}
        self.assertEqual('focused-v1', brief_result(self.root, 'demo')['documentPolicy'])
        self.assertEqual(before, {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in before})

    def test_legacy_is_not_upgraded_by_read_render_prepare_or_next_iteration(self):
        state = self.state(); state.pop('documentPolicy'); save_state(self.item, state)
        self.assertEqual('legacy', brief_result(self.root, 'demo')['documentPolicy'])
        self.review(); self.record()
        commands.complete(self.root, 'demo', expected_revision=self.state()['stateRevision'])
        commands.next_iteration(self.root, 'demo', expected_revision=self.state()['stateRevision'])
        commands.prepare_document(self.root, 'demo', 'plan', expected_revision=self.state()['stateRevision'])
        self.assertNotIn('documentPolicy', self.state())
        self.assertEqual((ROOT / 'templates/item/plan.md').read_text().replace('{title}', 'Demo').replace('目标仓：repository', '目标仓：kit'),
                         (self.item / 'plan.md').read_text())

    def test_ranges_subsections_and_globals_are_precise_and_merged(self):
        (self.item / 'change.md').write_text('# Change\n\n## 目标与边界\nGoal\n\n## 关键约束\nFail closed\n\n'
            '### R1 One\nA\n### R2 Two\nB\n### R3 Three\nC\n')
        (self.item / 'design.md').write_text('# Design\n\n## 关键约束\nNo resend\n\n## D01 Data\n'
            '### D01.1 Read\nREAD_ONLY\n### D01.2 Write\nWRITE_BOUNDARY\n\n## D02 Other\nNOT_NEEDED\n')
        self.plan()
        p = self.item / 'plan.md'; p.write_text(p.read_text().replace('依据：R1', '依据：R1-R3、D01.2'))
        self.review()
        value = brief_result(self.root, 'demo', task_id='T01')
        snippets = []
        for row in value['sources']:
            self.assertTrue(row['fragmentRevision'].startswith('sha256:'))
            snippets.append('\n'.join((self.item / row['path']).read_text().splitlines()[row['startLine']-1:row['endLine']]))
        text = '\n'.join(snippets)
        for expected in ('Goal', 'Fail closed', 'No resend', 'R1', 'R2', 'R3', 'WRITE_BOUNDARY'):
            self.assertIn(expected, text)
        self.assertNotIn('READ_ONLY', text)
        self.assertNotIn('NOT_NEEDED', text)
        self.assertEqual(1, text.count('R1'))
        self.assertIn('依据来源', brief_text(value))

    def test_bad_basis_blocks_approval_without_full_document_fallback(self):
        self.plan()
        for basis in ('R1-R3', 'R3-R1', 'R1-D03', 'R1-R99', 'R9', 'no-reference'):
            with self.subTest(basis=basis):
                p = self.item / 'plan.md'; self.plan(); p.write_text(p.read_text().replace('依据：R1', '依据：' + basis))
                before = self.state()
                with self.assertRaises(WorkItemError): self.review()
                self.assertEqual(before, self.state())

    def test_fenced_heading_does_not_satisfy_reference_and_duplicates_fail(self):
        self.plan()
        for text in ('# Change\n```md\n### R1 fake\n```\n', '### R1 A\nA\n### R1 B\nB\n'):
            (self.item / 'change.md').write_text(text)
            with self.assertRaisesRegex(WorkItemError, 'BASIS_REFERENCE_INVALID'): self.review()

    def test_multiple_explicit_attachments_remain_locatable(self):
        self.plan()
        (self.item / 'references').mkdir()
        for name in ('one.md', 'two.md'):
            (self.item / 'references' / name).write_text('# Contract\n')
        plan = self.item / 'plan.md'
        plan.write_text(plan.read_text() + '\n[One](references/one.md) [Two](references/two.md)\n')
        self.review()
        value = brief_result(self.root, 'demo', task_id='T01')
        self.assertEqual(['references/one.md', 'references/two.md'],
                         [row['reference'] for row in value['sources'] if 'reference' in row])

    def test_readme_only_current_navigation_and_unclosed_checks(self):
        (self.item / 'artifacts').mkdir()
        (self.item / 'artifacts/current.md').write_text('# Current')
        (self.item / 'artifacts/old-final.md').write_text('# History')
        (self.item / 'change.md').write_text('# Change\n\n## 目标与边界\nGoal\n\n## 交付入口\n[Front](artifacts/current.md)\n')
        self.review()
        commands.render(self.root, 'demo')
        text = (self.item / 'README.md').read_text()
        self.assertIn('artifacts/current.md', text)
        self.assertNotIn('old-final.md', text)
        self.assertIn('未关闭 0/0', text)

    def test_check_docs_readonly_and_broken_links_diagnostic(self):
        (self.item / 'change.md').write_text('# Change\n[bad](references/missing.md)\n')
        before = self.state()
        result = subprocess.run([sys.executable, '-B', str(ROOT/'scripts/kit.py'), 'item', 'check-docs', 'demo',
                                 '--root', str(self.root), '--json'], capture_output=True, text=True)
        value = json.loads(result.stdout)
        self.assertFalse(value['valid'])
        self.assertEqual('DOCUMENT_LINK_INVALID', value['diagnostics'][0]['code'])
        self.assertEqual(before, self.state())

    def test_large_item_precise_subsection_reduces_resume_without_truncation(self):
        from workbench.context_measure import _sources
        (self.item/'change.md').write_text('# Change\n\n## 目标与边界\nStable goal\n\n## 关键约束\nNever release unknown\n\n### R1 Behavior\nObservable\n')
        (self.item/'design.md').write_text('# Design\n\n## D01 Data\n### D01.1 Needed\nExact condition\n'
                                         '### D01.2 Dictionary\n' + ('field: definition\n' * 3000))
        self.plan(('T01','T02','T03'))
        p=self.item/'plan.md'; p.write_text(p.read_text().replace('依据：R1', '依据：R1、D01.1'))
        self.review()
        value=brief_result(self.root,'demo',task_id='T01')
        new=sum(row['estTokens'] for row in _sources(self.root,'demo',value,'workspace-execute-plan'))
        p.write_text(p.read_text().replace('D01.1','D01')); self.review()
        broad=brief_result(self.root,'demo',task_id='T01')
        old=sum(row['estTokens'] for row in _sources(self.root,'demo',broad,'workspace-execute-plan'))
        self.assertLess(new, old / 5)
        self.assertTrue(broad['contextCost']['overBudget'])
        rows=WorkItemQuery(self.root,'demo').requirement_sources(WorkItemQuery(self.root,'demo').task('T01'))
        self.assertTrue(any(row.get('endLine',0)>3000 for row in rows))


if __name__ == '__main__': unittest.main()
