"""Real CLI collection, XML validation, and the existing record boundary."""
from __future__ import annotations

import copy
import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

from tests.support.items import ItemFixture, ROOT
from workbench.work_items import collector, commands
from workbench.work_items.store import MAX_BYTES, WorkItemError, read_bytes, text_digest

PASSED = '<testsuite tests="1" failures="0" errors="0" skipped="0"><testcase name="ok"/></testsuite>'


class VerificationCollectTest(unittest.TestCase):
    def setUp(self):
        self.fixture = ItemFixture()
        self.fixture.open()
        self.addCleanup(self.fixture.close)
        self.root, self.item = self.fixture.root, self.fixture.item
        self.fixture.review()
        self.report = self.item / 'artifacts/report.xml'
        self.report.parent.mkdir()
        self.report.write_text(PASSED, encoding='utf-8')
        self.snapshot = Path(self.fixture.temp.name) / 'snapshot.json'
        self.save_snapshot()

    def cli(self, *args, input_text=None):
        return subprocess.run(
            [sys.executable, '-B', str(ROOT / 'scripts/kit.py'), *args, '--root', str(self.root)],
            capture_output=True, text=True, input=input_text,
        )

    def save_snapshot(self, task=None):
        args = ['verify', 'snapshot', 'demo', '--json']
        if task:
            args += ['--task', task]
        result = self.cli(*args)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.snapshot.write_text(result.stdout, encoding='utf-8')
        return json.loads(result.stdout)

    def collect(self, *, reports=None, artifacts=(), exit_status=0, task=None, json_flag=True,
                command='fixture-check', working_directory=None):
        args = ['verify', 'collect', 'demo', '--snapshot', str(self.snapshot), '--command', command,
                '--working-directory', working_directory or str(self.root), '--exit-status', str(exit_status)]
        for path in reports if reports is not None else ['artifacts/report.xml']:
            args += ['--junit', path]
        for path in artifacts:
            args += ['--artifact', path]
        if task:
            args += ['--task', task]
        if json_flag:
            args += ['--json']
        result = self.cli(*args)
        return result.returncode, json.loads(result.stdout)

    def files(self):
        return {path: (path.read_bytes(), path.stat().st_mtime_ns)
                for path in self.item.rglob('*') if path.is_file()}

    def assert_collect_error(self, code, **kwargs):
        before = self.files()
        status, value = self.collect(**kwargs)
        self.assertNotEqual(0, status, value)
        self.assertEqual(code, value['error']['code'])
        self.assertEqual(before, self.files())

    def test_nested_namespaced_reports_counts_hashes_and_no_report_bodies(self):
        self.report.write_text('''<testsuites xmlns="urn:junit" tests="3" failures="0" errors="0" skipped="0">
          <testsuite tests="2"><properties><property name="mode" value="fixture"/></properties>
            <testcase status="passed"><system-out>PRIVATE_STDOUT</system-out></testcase>
            <testsuite tests="1"><testcase result="success"><system-err>PRIVATE_STDERR</system-err></testcase></testsuite>
          </testsuite><testsuite tests="1"><testcase/></testsuite>
        </testsuites>''', encoding='utf-8')
        extra = self.report.parent / 'extra.xml'
        extra.write_text(PASSED, encoding='utf-8')
        before = self.files()
        status, value = self.collect(reports=['artifacts/report.xml', 'artifacts/extra.xml'])
        self.assertEqual(0, status, value)
        self.assertEqual({'total': 4, 'executed': 4, 'failed': 0, 'errors': 0, 'skipped': 0}, value['summary'])
        self.assertEqual('passed', value['input']['result'])
        self.assertEqual('item', value['input']['scope'])
        self.assertIsNone(value['input']['taskId'])
        self.assertNotIn('reviewResult', value['input'])
        self.assertIn('需人工审阅', value['input']['verificationScope'])
        self.assertEqual(json.loads(self.snapshot.read_text())['codeState'], value['input']['codeState'])
        self.assertEqual([3, 1], [check['executed'] for check in value['input']['checks']])
        for row in value['input']['artifactRefs']:
            data = (self.item / row['path']).read_bytes()
            self.assertEqual(text_digest(data), row['sha256'])
            self.assertEqual(len(data), row['bytes'])
            self.assertEqual('test-report', row['type'])
        self.assertNotIn('PRIVATE_STDOUT', json.dumps(value))
        self.assertNotIn('PRIVATE_STDERR', json.dumps(value))
        self.assertEqual(before, self.files())

    def test_failures_errors_skips_exit_status_and_empty_report_never_pass(self):
        self.report.write_text('''<testsuite tests="4" failures="1" errors="1" skipped="1">
          <testcase/><testcase><failure>PRIVATE_FAILURE</failure></testcase>
          <testcase><error>PRIVATE_ERROR</error></testcase><testcase><skipped/></testcase>
        </testsuite>''', encoding='utf-8')
        status, value = self.collect()
        self.assertEqual(0, status, value)
        self.assertEqual({'total': 4, 'executed': 3, 'failed': 1, 'errors': 1, 'skipped': 1}, value['summary'])
        self.assertEqual('failed', value['input']['result'])
        self.assertIn('失败 1', value['input']['checks'][0]['result'])
        self.assertNotIn('PRIVATE_FAILURE', json.dumps(value))
        self.assertNotIn('PRIVATE_ERROR', json.dumps(value))
        for xml, exit_status in ((PASSED, 7), ('<testsuite tests="0"/>', 0),
                                  ('<testsuite><testcase><skipped/></testcase></testsuite>', 0)):
            with self.subTest(xml=xml, exit_status=exit_status):
                self.report.write_text(xml, encoding='utf-8')
                status, value = self.collect(exit_status=exit_status)
                self.assertEqual(0, status, value)
                self.assertEqual('failed', value['input']['result'])
                self.assertEqual(exit_status, value['input']['checks'][0]['exitStatus'])

    def test_aliases_and_ambiguous_unknown_results(self):
        outcomes = {'passed': (0, 0, 0), 'success': (0, 0, 0), 'ok': (0, 0, 0),
                    'skipped': (0, 0, 1), 'notrun': (0, 0, 1),
                    'failed': (1, 0, 0), 'failure': (1, 0, 0), 'error': (0, 1, 0)}
        for attribute in ('status', 'result'):
            for outcome, expected in outcomes.items():
                counts = collector.parse_junit(f'<testsuite><testcase {attribute}="{outcome}"/></testsuite>'.encode())
                self.assertEqual(expected, tuple(counts[key] for key in ('failed', 'errors', 'skipped')))
        for case in ('<testcase status="running"/>', '<testcase status="passed" result="failed"/>',
                     '<testcase status="passed"><failure/></testcase>',
                     '<testcase><skipped/><error/></testcase>', '<testcase><failure/><error/></testcase>',
                     '<testcase><flakyFailure/></testcase>'):
            with self.subTest(case=case):
                self.report.write_text('<testsuite>' + case + '</testsuite>', encoding='utf-8')
                self.assert_collect_error('JUNIT_INVALID')

    def test_invalid_and_inconsistent_suite_counts(self):
        invalid = ['<testsuite tests="1"/>', '<testsuite tests="2"><testcase/></testsuite>',
                   '<testsuite failures="1"><testcase/></testsuite>',
                   '<testsuites tests="2"><testsuite tests="1"><testcase/></testsuite></testsuites>',
                   '<testsuites><testsuite tests="2"><testcase/></testsuite></testsuites>']
        invalid += [f'<testsuite {key}="{number}"><testcase/></testsuite>'
                    for key in ('tests', 'failures', 'errors', 'skipped') for number in ('-1', '1.5', 'unknown', '')]
        for xml in invalid:
            with self.subTest(xml=xml):
                self.report.write_text(xml, encoding='utf-8')
                self.assert_collect_error('JUNIT_INVALID')

    def test_malformed_xml_root_and_dtd_entities_in_utf8_and_utf16(self):
        for xml in ('<testsuite>', '<report><testcase/></report>', '<!ENTITY x "value"><testsuite/>'):
            self.report.write_text(xml, encoding='utf-8')
            self.assert_collect_error('JUNIT_INVALID')
        for encoding in ('utf-8', 'utf-16'):
            xml = f'<?xml version="1.0" encoding="{encoding}"?><!DOCTYPE testsuite [<!ENTITY x "value">]><testsuite><testcase name="&x;"/></testsuite>'
            self.report.write_bytes(xml.encode(encoding))
            self.assert_collect_error('JUNIT_INVALID')

    def test_path_boundaries_regular_files_symlinks_and_read_limits(self):
        for path in ('../outside.xml', str(self.report), 'artifacts\\report.xml'):
            self.assert_collect_error('UNSAFE_PATH', reports=[path])
            self.assert_collect_error('UNSAFE_PATH', artifacts=[path])
        link = self.report.parent / 'link.xml'
        link.symlink_to(self.report)
        self.assert_collect_error('UNSAFE_PATH', reports=['artifacts/link.xml'])
        self.assert_collect_error('UNSAFE_PATH', artifacts=['artifacts/link.xml'])
        link.unlink()
        self.assert_collect_error('FILE_UNAVAILABLE', reports=['artifacts'])
        self.assert_collect_error('FILE_UNAVAILABLE', artifacts=['artifacts'])
        large = self.report.parent / 'large.xml'
        large.write_bytes(b' ' * (MAX_BYTES + 1))
        self.assert_collect_error('READ_LIMIT', reports=['artifacts/large.xml'])
        self.assert_collect_error('READ_LIMIT', artifacts=['artifacts/large.xml'])
        self.snapshot.write_bytes(b' ' * (MAX_BYTES + 1))
        self.assert_collect_error('READ_LIMIT')

    def test_duplicate_reports_attachments_and_twenty_file_limit(self):
        attachment = self.report.parent / 'note.txt'
        attachment.write_text('fixture note\n', encoding='utf-8')
        status, value = self.collect(reports=['artifacts/report.xml', 'artifacts/./report.xml'],
                                     artifacts=['artifacts/report.xml', 'artifacts/note.txt', 'artifacts/./note.txt'])
        self.assertEqual(0, status, value)
        self.assertEqual(1, value['summary']['total'])
        self.assertEqual(1, len(value['input']['checks']))
        self.assertEqual(2, len(value['input']['artifactRefs']))
        self.assertEqual(['artifact'], [row['type'] for row in value['artifacts']])
        paths = []
        for index in range(20):
            path = self.report.parent / f'attachment-{index}.txt'
            path.write_text(str(index), encoding='utf-8')
            paths.append(path.relative_to(self.item).as_posix())
        status, value = self.collect(artifacts=paths[:19])
        self.assertEqual(0, status, value)
        self.assertEqual(20, len(value['input']['artifactRefs']))
        self.assert_collect_error('EVIDENCE_REFERENCE_LIMIT', artifacts=paths)

    def test_snapshot_fields_identity_repositories_and_stale_revision(self):
        original = json.loads(self.snapshot.read_text())
        for key in ('itemSlug', 'stateRevision', 'codeState'):
            value = copy.deepcopy(original)
            del value[key]
            self.snapshot.write_text(json.dumps(value), encoding='utf-8')
            self.assert_collect_error('INPUT_INVALID')
        for field, replacement, code in [('itemSlug', 'other', 'EVIDENCE_SUBJECT_MISMATCH'),
                                         ('stateRevision', 'sha256:' + '0' * 64, 'STATE_CHANGED'),
                                         ('codeState', {}, 'EVIDENCE_CODE_INVALID'),
                                         ('codeState', {'kit': 'bad'}, 'EVIDENCE_CODE_INVALID'),
                                         ('codeState', {**original['codeState'], 'other': 'sha256:' + '0' * 64}, 'EVIDENCE_CODE_INVALID')]:
            self.snapshot.write_text(json.dumps({**original, field: replacement}), encoding='utf-8')
            self.assert_collect_error(code)
        self.snapshot.write_text(json.dumps(original), encoding='utf-8')
        commands.update(self.root, 'demo', {'title': 'Updated'}, reason='调整标题',
                        expected_revision=self.fixture.state()['stateRevision'])
        self.assert_collect_error('STATE_CHANGED')

    def test_code_drift_and_inactive_item(self):
        (self.root / 'changed.py').write_text('# changed\n', encoding='utf-8')
        self.assert_collect_error('CODE_CHANGED')
        self.save_snapshot()
        commands.lifecycle(self.root, 'demo', 'paused', expected_revision=self.fixture.state()['stateRevision'])
        self.assert_collect_error('ITEM_INACTIVE')

    def test_snapshot_task_identity_is_exact_even_with_one_repository(self):
        self.fixture.plan(('T01', 'T02'))
        self.fixture.review()
        item_snapshot = self.save_snapshot()
        self.assert_collect_error('EVIDENCE_SUBJECT_MISMATCH', task='T01')
        self.assertIsNone(item_snapshot['taskId'])
        task_snapshot = self.save_snapshot('T01')
        self.assertEqual('T01', task_snapshot['taskId'])
        status, value = self.collect(task='T01')
        self.assertEqual(0, status, value)
        self.assertEqual('T01', value['input']['taskId'])
        self.assert_collect_error('EVIDENCE_SUBJECT_MISMATCH', task='T02')
        self.assert_collect_error('EVIDENCE_SUBJECT_MISMATCH')
        del task_snapshot['taskId']
        self.snapshot.write_text(json.dumps(task_snapshot), encoding='utf-8')
        self.assert_collect_error('EVIDENCE_SUBJECT_MISMATCH', task='T01')
        del item_snapshot['taskId']
        self.snapshot.write_text(json.dumps(item_snapshot), encoding='utf-8')
        self.assert_collect_error('EVIDENCE_SUBJECT_MISMATCH', task='T01')
        status, value = self.collect()
        self.assertEqual(0, status, value)
        self.assertEqual('item', value['input']['scope'])
        self.assertEqual('T02', self.save_snapshot('T02')['taskId'])
        status, value = self.collect(task='T02')
        self.assertEqual(0, status, value)
        self.assertEqual('T02', value['input']['taskId'])

    def test_task_uses_only_its_repository_and_rejects_wrong_task(self):
        service = self.root.parent / 'service'
        service.mkdir()
        (service / 'source.txt').write_text('one\n', encoding='utf-8')
        (service / 'AGENTS.md').write_text('# Service rules\n', encoding='utf-8')
        for args in (['init', '-q', '--initial-branch=main'], ['add', '.'],
                     ['-c', 'user.name=Fixture', '-c', 'user.email=test@example.test', 'commit', '-qm', 'fixture']):
            subprocess.run(['git', '-C', str(service), *args], check=True, capture_output=True)
        bindings = [*self.fixture.state()['bindings'], {'repository': 'service', 'workBranch': 'main', 'baseBranch': 'main'}]
        commands.update(self.root, 'demo', {'bindings': bindings}, reason='登记任务仓',
                        expected_revision=self.fixture.state()['stateRevision'])
        self.fixture.plan()
        plan = self.item / 'plan.md'
        plan.write_text(plan.read_text().replace('目标仓：kit', '目标仓：service').replace('AGENTS.md', 'source.txt'), encoding='utf-8')
        self.fixture.review()
        self.save_snapshot()
        self.assert_collect_error('EVIDENCE_CODE_INVALID', task='T01')
        saved = self.save_snapshot('T01')
        self.assertEqual({'service'}, set(saved['codeState']))
        (self.root / 'unrelated.py').write_text('# unrelated\n', encoding='utf-8')
        status, value = self.collect(task='T01')
        self.assertEqual(0, status, value)
        self.assertEqual(saved['codeState'], value['input']['codeState'])
        self.assertEqual('task', value['input']['scope'])
        self.assertEqual('T01', value['input']['taskId'])
        recorded = self.cli('verify', 'record', 'demo', '--input', '-', '--state-revision', value['stateRevision'],
                            input_text=json.dumps(value['input']))
        self.assertEqual(0, recorded.returncode, recorded.stdout + recorded.stderr)
        self.save_snapshot('T01')
        self.assert_collect_error('TASK_NOT_FOUND', task='T99')
        (service / 'source.txt').write_text('two\n', encoding='utf-8')
        self.assert_collect_error('CODE_CHANGED', task='T01')

    def test_literal_command_not_executed_and_default_output_json_is_read_only(self):
        marker = Path(self.fixture.temp.name) / 'must-not-exist'
        literal = f'  fixture-check; touch {marker}  '
        before = self.files()
        snapshot_before = (self.snapshot.read_bytes(), self.snapshot.stat().st_mtime_ns)
        status, value = self.collect(json_flag=False, command=literal, working_directory=' actual cwd ')
        self.assertEqual(0, status, value)
        self.assertEqual(literal, value['input']['checks'][0]['command'])
        self.assertEqual(' actual cwd ', value['input']['checks'][0]['workingDirectory'])
        self.assertFalse(marker.exists())
        self.assertEqual(before, self.files())
        self.assertEqual(snapshot_before, (self.snapshot.read_bytes(), self.snapshot.stat().st_mtime_ns))

    def test_collect_manual_review_record_and_changed_artifact_rejection(self):
        attachment = self.report.parent / 'note.txt'
        attachment.write_text('original\n', encoding='utf-8')
        status, value = self.collect(artifacts=['artifacts/note.txt'])
        self.assertEqual(0, status, value)
        payload = value['input']
        result = self.cli('verify', 'record', 'demo', '--input', '-', '--state-revision', value['stateRevision'],
                          input_text=json.dumps(payload))
        self.assertEqual(1, result.returncode)
        self.assertEqual('REVIEW_RESULT_REQUIRED', json.loads(result.stdout)['error']['code'])
        payload['reviewResult'] = 'passed'
        for target in (self.report, attachment):
            original = target.read_bytes()
            target.write_bytes(original + b'\nChanged\n')
            result = self.cli('verify', 'record', 'demo', '--input', '-', '--state-revision', value['stateRevision'],
                              input_text=json.dumps(payload))
            self.assertEqual(1, result.returncode)
            self.assertEqual('ARTIFACT_CHANGED', json.loads(result.stdout)['error']['code'])
            target.write_bytes(original)
        result = self.cli('verify', 'record', 'demo', '--input', '-', '--state-revision', value['stateRevision'],
                          input_text=json.dumps(payload))
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIsNotNone(self.fixture.state()['verification'])
        self.assertEqual('active', self.fixture.state()['lifecycle'])

    def test_state_change_during_report_read_is_rejected(self):
        def concurrent_read(path, *args, **kwargs):
            data = read_bytes(path, *args, **kwargs)
            if path == self.report:
                commands.update(self.root, 'demo', {'title': 'Changed during read'}, reason='并发修改',
                                expected_revision=self.fixture.state()['stateRevision'])
            return data
        with mock.patch.object(collector, 'read_bytes', side_effect=concurrent_read):
            with self.assertRaises(WorkItemError) as caught:
                collector.collect_result(self.root, 'demo', snapshot=self.snapshot,
                                         junit=['artifacts/report.xml'], artifacts=[], command='fixture-check',
                                         working_directory=str(self.root), exit_status=0)
        self.assertEqual('STATE_CHANGED', caught.exception.code)


if __name__ == '__main__':
    unittest.main()
