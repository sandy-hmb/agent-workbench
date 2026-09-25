"""Context discovery must preserve scope while keeping model input small."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from tests.support import ROOT
from tests.support.items import ItemFixture
from workbench.work_items.documents import instructions
from workbench.work_items.query import WorkItemQuery
from workbench.work_items import commands


class RuleScopeTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve() / 'kit'; self.repo = self.root.parent / 'service'
        self.root.mkdir(); self.repo.mkdir()
        (self.root / 'AGENTS.md').write_text('# Kit rules\n')
        (self.repo / 'AGENTS.md').write_text('# Repo rules\n')
        for directory in ['a/deep', 'b']:
            (self.repo / directory).mkdir(parents=True)
            (self.repo / directory / 'AGENTS.md').write_text('# Scoped ' + directory)

    def test_sibling_rules_keep_separate_scope_and_directory_rule_is_included(self):
        context = instructions(self.root, self.repo, paths=['a/deep', 'b/file.py'])
        rules = {row['path']: row for row in context['rules']}
        self.assertEqual(str(self.repo / 'a/deep'), rules[str(self.repo / 'a/deep/AGENTS.md')]['scope'])
        self.assertEqual(str(self.repo / 'b'), rules[str(self.repo / 'b/AGENTS.md')]['scope'])
        self.assertEqual(4, len(rules))
        self.assertFalse(context['diagnostics'])

    def test_required_missing_rule_is_reported_but_optional_directory_is_not(self):
        (self.repo / 'AGENTS.md').unlink()
        context = instructions(self.root, self.repo, paths=['a/file.py'])
        self.assertEqual(['RULE_SOURCE_MISSING'], [row['code'] for row in context['diagnostics']])
        self.assertEqual(str(self.repo / 'AGENTS.md'), context['diagnostics'][0]['path'])

    def test_revision_changes_without_persistent_read_receipts(self):
        before = instructions(self.root, self.repo, paths=['b/file.py'])
        (self.repo / 'AGENTS.md').write_text('# Changed\n')
        after = instructions(self.root, self.repo, paths=['b/file.py'])
        self.assertNotEqual(before['rules'][1]['documentRevision'], after['rules'][1]['documentRevision'])
        self.assertFalse((self.root / '.workspace').exists())

    def test_configured_workspace_facts_are_pointers_not_copied_text(self):
        from workbench.work_items.query import repository_context
        state = self.root / '.workspace'; (state / 'config').mkdir(parents=True); (state / 'repositories').mkdir(parents=True)
        (state / 'AGENTS.md').write_text('# Workspace rule')
        (state / 'CONTEXT.md').write_text('FACT_BODY_MUST_NOT_BE_IN_OUTPUT')
        (state / 'repositories/service.md').write_text('PROFILE_BODY_MUST_NOT_BE_IN_OUTPUT')
        (state / 'config/workspace.json').write_text(json.dumps({'version': {'major': 4, 'minor': 0}, 'workspace': {'name': 'fixture'},
            'context': {}, 'branchPolicy': {}, 'extensions': {'providers': {}, 'config': {}},
            'repositories': [{'path': 'service', 'aliases': [], 'remote': None, 'category': 'backend', 'description': '',
                              'instruction': 'repositories/service.md', 'sourceInstruction': 'AGENTS.md'}]}))
        value = repository_context(self.root, self.repo, paths=['b/file.py'])
        self.assertEqual({'workspace-context', 'repository-profile'}, {row['kind'] for row in value['facts']})
        self.assertNotIn('FACT_BODY_MUST_NOT_BE_IN_OUTPUT', json.dumps(value))
        self.assertNotIn('PROFILE_BODY_MUST_NOT_BE_IN_OUTPUT', json.dumps(value))


class ProgressiveContextTest(ItemFixture, unittest.TestCase):
    def setUp(self):
        self.open(); self.addCleanup(self.close)

    def test_ordinary_and_lightweight_entrypoints_expose_rules(self):
        self.review()
        normal = WorkItemQuery(self.root, 'demo').continuation()
        self.assertTrue(normal['instructionContext']['rules'])
        result = subprocess.run([sys.executable, str(ROOT / 'scripts/kit.py'), 'brief', '--root', str(self.root), '--repo', 'kit', '--path', 'AGENTS.md', '--json'], capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stderr + result.stdout)
        data = json.loads(result.stdout)
        self.assertTrue(data['instructionContext']['rules'])
        self.assertNotIn('itemSlug', data)

    def test_task_body_is_not_repeated_and_requirements_are_locatable(self):
        self.plan()
        (self.item / 'change.md').write_text('# Change\n\n### R1 Observable requirement\n\nActual requirement text.\n')
        self.review()
        value = WorkItemQuery(self.root, 'demo').continuation(task_id='T01')
        selected = value['selectedTask']
        self.assertIn('body', selected)
        self.assertNotIn('deliverables', selected)
        self.assertNotIn('basis', selected)
        reference = next(row for row in value['sources'] if row.get('id') == 'R1')
        self.assertEqual('change.md', reference['path'])
        self.assertGreater(reference['startLine'], 1)
        self.assertNotIn('Actual requirement text.', json.dumps(value))

    def test_missing_required_rule_blocks_task_but_read_query_still_works(self):
        self.plan(); self.review()
        (self.root / 'AGENTS.md').unlink()
        value = WorkItemQuery(self.root, 'demo').continuation(task_id='T01')
        self.assertEqual('BLOCKED', value['executionDecision'])
        self.assertIn('RULE_SOURCE_MISSING', value['blockers'])

    def test_explicit_repository_resume_ignores_missing_other_repository(self):
        other = self.root.parent / 'other'; other.mkdir()
        subprocess.run(['git', 'init', '-q', '--initial-branch=main', str(other)], check=True)
        bindings = [*self.state()['bindings'], {'repository': 'other', 'workBranch': 'main', 'baseBranch': 'main'}]
        commands.update(self.root, 'demo', {'bindings': bindings}, reason='scope', expected_revision=self.state()['stateRevision'])
        import shutil
        shutil.rmtree(other)
        result = WorkItemQuery(self.root, 'demo').continuation(repository='kit', paths=['AGENTS.md'])
        self.assertEqual(['kit'], [row['repository'] for row in result['repositoryContext']])

    def test_record_returns_small_next_step_without_repeating_context(self):
        self.plan(('T01', 'T02')); self.review()
        result = commands.record(self.root, 'demo', self.payload('T01'), expected_revision=self.state()['stateRevision'])
        self.assertEqual('T02', result['nextStep']['taskId'])
        self.assertNotIn('body', json.dumps(result['nextStep']))


if __name__ == '__main__': unittest.main()
