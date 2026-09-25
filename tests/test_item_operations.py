"""Work-item change, interruption, and no-op behavior through public commands."""
import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock
from tests.support import ROOT
from tests.support.items import ItemFixture
from workbench.work_items import commands
from workbench.work_items.query import WorkItemQuery
from workbench.work_items.store import WorkItemError


class ItemOperationsTest(ItemFixture, unittest.TestCase):
    def setUp(self):
        self.open()
        self.addCleanup(self.close)

    def update(self, changes, preview=False):
        return commands.update(self.root, 'demo', changes, reason='已确认本轮调整', expected_revision=self.state()['stateRevision'], preview=preview)

    def test_noop_update_and_render_leave_versions_and_mtimes_unchanged(self):
        self.review(); self.record()
        paths = [self.item / 'state.json', self.item / 'README.md', self.item / 'verification.md']
        before = [(p.read_bytes(), p.stat().st_mtime_ns) for p in paths]
        self.assertFalse(self.update({'title': self.state()['title']})['changed'])
        commands.render(self.root, 'demo')
        self.assertEqual(before, [(p.read_bytes(), p.stat().st_mtime_ns) for p in paths])
        revision = self.state()['stateRevision']
        result = commands.delivery(self.root, 'demo', {'externalChecks': []}, expected_revision=revision)
        self.assertFalse(result['changed'])
        self.assertEqual(revision, self.state()['stateRevision'])

    def test_preview_is_readonly_and_metadata_keeps_approval(self):
        self.review()
        before = self.state()
        preview = self.update({'title': 'Renamed'}, preview=True)
        self.assertTrue(preview['changed'])
        self.assertEqual(before, self.state())
        self.update({'title': 'Renamed'})
        self.assertEqual('approved', WorkItemQuery(self.root, 'demo').review_state()['change'])

    def test_title_can_change_while_plan_is_an_incomplete_draft(self):
        (self.item / 'plan.md').write_text('### T01 尚在编写的计划\n')
        result = self.update({'title':'新的展示标题'})
        self.assertTrue(result['committed'])
        self.assertEqual('新的展示标题',self.state()['title'])

    def test_risk_upgrade_requires_document_package(self):
        self.review()
        with self.assertRaises(ValueError):
            self.update({'risk': 'major'})
        self.update({'risk': 'major', 'documentKind': 'requirements'})
        self.assertEqual('missing', WorkItemQuery(self.root, 'demo').review_state()['requirements'])
        self.assertTrue((self.item / 'change.md').exists())

    def test_block_excludes_task_and_dependency_but_allows_independent_task(self):
        self.plan(('T01', 'T02', 'T03'))
        plan = self.item / 'plan.md'; text = plan.read_text(); at = text.index('### T02')
        plan.write_text(text[:at] + text[at:].replace('依赖：无', '依赖：T01', 1))
        self.review()
        blocked = commands.block(self.root, 'demo', reason='等待环境', owner='测试', condition='环境可访问', task_ids=['T01'], expected_revision=self.state()['stateRevision'])
        self.assertEqual(['T03'], WorkItemQuery(self.root, 'demo').decision()['readyTasks'])
        selected = WorkItemQuery(self.root, 'demo').continuation(task_id='T01')
        self.assertEqual('BLOCKED', selected['executionDecision'])
        self.assertTrue(selected['selectedTask']['executionBlocked'])
        self.assertEqual(['T03'], selected['readyTasks'])
        self.assertEqual([], self.state()['evidence'])
        commands.unblock(self.root, 'demo', blocked['blockerId'], reason='环境已恢复', expected_revision=self.state()['stateRevision'])
        self.assertEqual(['T01', 'T03'], WorkItemQuery(self.root, 'demo').decision()['readyTasks'])
        self.assertEqual(0, WorkItemQuery(self.root, 'demo').summary()['progress']['completed'])

    def test_item_block_prevents_completion(self):
        self.review(); self.record()
        commands.block(self.root, 'demo', reason='等待决策', owner='用户', condition='决策确认', expected_revision=self.state()['stateRevision'])
        with self.assertRaisesRegex(WorkItemError, 'ITEM_NOT_READY'):
            commands.complete(self.root, 'demo', expected_revision=self.state()['stateRevision'])

    def test_cancel_preserves_facts_and_restarts_only_as_new_iteration(self):
        self.plan(); self.review(); self.record('T01')
        previous = list(self.state()['evidence'])
        commands.cancel(self.root, 'demo', reason='目标被替代', expected_revision=self.state()['stateRevision'])
        self.assertEqual('cancelled', self.state()['lifecycle'])
        self.assertEqual(previous, self.state()['evidence'])
        with self.assertRaises(WorkItemError):
            commands.lifecycle(self.root, 'demo', 'active', expected_revision=self.state()['stateRevision'])
        commands.next_iteration(self.root, 'demo', expected_revision=self.state()['stateRevision'])
        self.assertEqual('i02', self.state()['iteration'])
        self.assertEqual(1, self.state()['maxTaskNumber'])
        self.assertIsNone(self.state()['cancellation'])

    def test_bad_cli_inputs_are_structured_and_do_not_write(self):
        before = self.state()
        for payload in [None, [], {'externalChecks': None}, {'repositories': []}]:
            input_file = self.root / 'input.json'; input_file.write_text(json.dumps(payload))
            result = subprocess.run([sys.executable, str(ROOT / 'scripts/kit.py'), 'item', 'delivery', 'demo', '--root', str(self.root), '--input', str(input_file), '--state-revision', before['stateRevision'], '--json'], capture_output=True, text=True)
            value = json.loads(result.stdout)
            self.assertNotEqual(0, result.returncode)
            self.assertIn('field', value['error'])
            self.assertNotIn('Traceback', result.stderr)
            self.assertEqual(before, self.state())

    def test_old_command_is_rejected_without_reading_old_directory(self):
        result = subprocess.run([sys.executable,str(ROOT/'scripts/kit.py'),'feature','list','--json'],capture_output=True,text=True)
        self.assertEqual(2,result.returncode)
        old = self.root / 'docs/development/features/old-item'; old.mkdir(parents=True)
        (old/'state.json').write_text('broken old format')
        from workbench.work_items.query import list_items
        self.assertEqual(['demo'],[row['slug'] for row in list_items(self.root)['items']])

    def test_empty_stdin_is_reported_with_input_location(self):
        result = subprocess.run([sys.executable,str(ROOT/'scripts/kit.py'),'verify','record','demo','--root',str(self.root),
            '--input','-','--state-revision',self.state()['stateRevision'],'--json'],input='',capture_output=True,text=True)
        self.assertEqual(1,result.returncode)
        self.assertEqual('$',json.loads(result.stdout)['error']['field'])
        self.assertNotIn('Traceback',result.stderr)

    def test_summary_prioritizes_open_checks_and_explains_truncation(self):
        self.review(); self.record()
        checks = [{'id': f'C{i}', 'requirement': 'R1', 'description': f'check-{i}', 'owner': '测试', 'status': 'passed' if i < 22 else 'pending', 'evidence': 'result' if i < 22 else ''} for i in range(25)]
        commands.delivery(self.root, 'demo', {'externalChecks': checks}, expected_revision=self.state()['stateRevision'])
        summary = (self.item / 'verification.md').read_text()
        self.assertIn('check-24', summary)
        self.assertIn('共 25', summary)
        self.assertIn('其余 5', summary)

    def test_delivery_can_reference_current_workflow_attempt_without_completing_acceptance(self):
        from workbench.extensions import attempts
        run = self.root / '.workspace' / 'runs' / 'demo-i01.json'
        run.parent.mkdir(parents=True)
        run.write_text(json.dumps({'schemaVersion': 3, 'id': 'demo-i01', 'workflow': 'item-development',
                                   'itemSlug': 'demo', 'iteration': 'i01', 'bindingRevision': None,
                                   'repository': 'kit', 'branch': 'main'}))
        attempts.save(self.root, 'demo-i01', {'schemaVersion': 1, 'run': 'demo-i01', 'requests': {
            'request-deploy': {'requestId': 'request-deploy', 'run': 'demo-i01', 'stage': 'team.deploy',
                               'planHash': 'a' * 64, 'fingerprint': 'sha256:' + 'b' * 64,
                               'sequence': 1, 'action': 'team/deploy', 'status': 'succeeded', 'origin': 'runner',
                               'updatedAt': '2026-09-25T10:00:00Z', 'summary': 'deployed'}}})
        result = commands.delivery(
            self.root, 'demo',
            {'repositories': {'kit': {
                'deployment': '已部署', 'evidence': '测试环境部署回执',
                'evidenceRefs': [{'kind': 'workflow', 'runId': 'demo-i01', 'requestId': 'request-deploy', 'stage': 'team.deploy'}],
            }}},
            expected_revision=self.state()['stateRevision'],
        )
        ref = result['state']['delivery']['kit']['evidenceRefs'][0]
        self.assertEqual('succeeded', ref['status'])
        self.assertEqual('已部署', result['state']['delivery']['kit']['deployment'])

    def test_delivery_rejects_workflow_reference_from_other_item(self):
        run = self.root / '.workspace' / 'runs' / 'other-i01.json'
        run.parent.mkdir(parents=True)
        run.write_text(json.dumps({'schemaVersion': 3, 'id': 'other-i01', 'workflow': 'item-development',
                                   'itemSlug': 'other', 'iteration': 'i01', 'bindingRevision': None,
                                   'repository': 'kit', 'branch': 'main'}))
        with self.assertRaisesRegex(WorkItemError, 'EVIDENCE_REFERENCE_SUBJECT_MISMATCH'):
            commands.delivery(
                self.root, 'demo',
                {'repositories': {'kit': {
                    'evidence': 'wrong item',
                    'evidenceRefs': [{'kind': 'workflow', 'runId': 'other-i01', 'requestId': 'missing', 'stage': 'team.deploy'}],
                }}},
                expected_revision=self.state()['stateRevision'],
            )

    def add_second_repository(self):
        repository = self.root.parent / 'other'
        repository.mkdir()
        subprocess.run(['git', 'init', '-q', '--initial-branch=main', str(repository)], check=True)
        (repository / 'file.txt').write_text('other')
        subprocess.run(['git', '-C', str(repository), 'add', '.'], check=True)
        subprocess.run(['git', '-C', str(repository), '-c', 'user.name=Fixture', '-c', 'user.email=test@example.test', 'commit', '-qm', 'fixture'], check=True)
        self.update({'bindings': [*self.state()['bindings'], {'repository': 'other', 'workBranch': 'main', 'baseBranch': 'main'}]})
        return repository

    def test_task_snapshot_and_record_do_not_touch_unrelated_repository(self):
        from workbench.cli.verify import snapshot_result
        self.add_second_repository()
        self.plan(); self.review()
        from workbench.work_items import query
        original = query.git_fingerprint
        calls = []
        def guarded(repository, *args, **kwargs):
            calls.append(repository.name)
            self.assertEqual('kit', repository.name)
            return original(repository, *args, **kwargs)
        with mock.patch.object(query, 'git_fingerprint', side_effect=guarded):
            snapshot = snapshot_result(self.root, 'demo', 'T01')
            payload = {'scope':'task','taskId':'T01','recordedAt':'2026-09-25T10:00:00+08:00','result':'passed','codeState':snapshot['codeState'],
                'checks':[{'type':'测试','workingDirectory':'kit','command':'fixture-check','target':'behavior','executed':1,'skipped':0,'exitStatus':0,'result':'passed'}]}
            commands.record(self.root, 'demo', payload, expected_revision=self.state()['stateRevision'])
        self.assertEqual(['kit', 'kit'], calls)

    def test_branch_rebinding_invalidates_only_affected_tasks(self):
        self.add_second_repository(); self.plan(('T01','T02'))
        plan = self.item / 'plan.md'; content = plan.read_text(); at = content.index('### T02')
        plan.write_text(content[:at] + content[at:].replace('目标仓：kit','目标仓：other').replace('`AGENTS.md`','`file.txt`'))
        self.review()
        for task_id, repository in [('T01','kit'),('T02','other')]:
            payload = self.payload(task_id); payload['codeState'] = {repository:payload['codeState'][repository]}
            commands.record(self.root, 'demo', payload, expected_revision=self.state()['stateRevision'])
        bindings = self.state()['bindings']; bindings[1]['workBranch'] = 'feature/new-scope'
        self.update({'bindings':bindings})
        self.assertIsNotNone(self.state()['tasks']['T01']['evidence'])
        self.assertIsNone(self.state()['tasks']['T02']['evidence'])
        with self.assertRaisesRegex(WorkItemError,'ITEM_REPOSITORY_REFERENCED'):
            self.update({'bindings':[bindings[0]]})

if __name__ == '__main__':
    unittest.main()
