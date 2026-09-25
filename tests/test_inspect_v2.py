"""Real CLI producer contract, targeted reads, and consumer fixture generation."""
from __future__ import annotations
import argparse
import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock
import tests.support.items as fixtures
ROOT=Path(__file__).resolve().parents[1]
import workbench.inspection.api as inspect
from workbench.work_items.store import WorkItemError

class InspectV2Test(unittest.TestCase):
    def setUp(self):
        self.fixture=fixtures.ItemFixture();self.fixture.open();self.addCleanup(self.fixture.close)
        self.root=self.fixture.root
    def cli(self,*args,major=2):
        result=subprocess.run([sys.executable,'-B',str(ROOT/'scripts/kit.py'),'inspect','--root',str(self.root),'--api-major',str(major),'--json',*args],capture_output=True,text=True)
        return result.returncode,json.loads(result.stdout)
    def test_real_cli_projects_documents_and_one_task_state(self):
        self.fixture.plan();self.fixture.review();self.fixture.record('T01')
        code,response=self.cli('projection','demo','--view','task')
        self.assertEqual(0,code)
        self.assertEqual(2,response['apiVersion']['major'])
        data=response['data']
        self.assertEqual('completed',data['tasks'][0]['status'])
        self.assertNotIn('trusted',data['tasks'][0])
        self.assertNotIn('trustedProgress',data['summary'])
        self.assertEqual('change.md',next(d['path'] for d in data['documents'] if d['role']=='change'))
    def test_list_does_not_hash_code_or_load_document_bodies(self):
        with mock.patch.object(inspect.WorkItemQuery,'code_state',side_effect=AssertionError('unrelated git')):
            self.assertEqual('demo',inspect.list_items(self.root)['items'][0]['slug'])
    def test_change_projection_does_not_read_plan(self):
        with mock.patch.object(inspect.WorkItemQuery,'tasks',side_effect=AssertionError('plan read')):
            self.assertEqual('kit',inspect.projection(self.root,'demo','change')['repositories'][0]['repository'])
    def test_protocol_rejects_old_major_and_unsafe_document(self):
        code,response=self.cli('workspace',major=1)
        self.assertEqual(1,code);self.assertEqual('error',response['status'])
        code,response=self.cli('document','demo','--path','../outside.md')
        self.assertEqual(1,code);self.assertEqual('error',response['status'])
    def test_document_revision_and_read_only_behavior(self):
        before={p:p.read_bytes() for p in self.fixture.item.rglob('*') if p.is_file()}
        code,value=self.cli('document','demo','--path','change.md')
        self.assertEqual(0,code)
        self.assertEqual(before,{p:p.read_bytes() for p in self.fixture.item.rglob('*') if p.is_file()})
        (self.fixture.item/'change.md').write_text('# changed\n')
        code,response=self.cli('document','demo','--path','change.md','--document-revision',value['data']['documentRevision'])
        self.assertEqual(1,code)
    def test_symlinked_or_oversized_documents_rejected(self):
        target=self.fixture.item/'change.md';target.unlink();target.symlink_to(self.root/'AGENTS.md')
        self.assertEqual(1,self.cli('document','demo','--path','change.md')[0])
        target.unlink();target.write_text('a'*(1024*1024+1))
        self.assertEqual(1,self.cli('document','demo','--path','change.md')[0])
    def test_current_verification_does_not_claim_unchecked_code(self):
        self.fixture.review();self.fixture.record()
        self.assertEqual('not_checked',self.cli('verification','demo')[1]['data']['applicability'])
        (self.root/'changed.py').write_text('# changed')
        self.assertEqual('invalid',self.cli('verification','demo','--check-code')[1]['data']['applicability'])

    def test_expired_request_stops_before_git(self):
        import time
        self.fixture.review(); self.fixture.record()
        with mock.patch('workbench.work_items.query.git_fingerprint', side_effect=AssertionError('expired request ran git')):
            with self.assertRaisesRegex(WorkItemError, 'INSPECT_TIMEOUT'):
                inspect.verification(self.root, 'demo', True, deadline=time.monotonic() - 1)

    def test_argument_errors_use_the_versioned_envelope(self):
        code, value = self.cli('projection', 'demo', '--view', 'unrecognized')
        self.assertEqual(2, code)
        self.assertEqual('error', value['status'])
        self.assertEqual('INSPECT_ARGUMENT_INVALID', value['diagnostics'][0]['code'])

    def test_actual_response_contract_matches_schema(self):
        from workbench.schema_validation import validate
        schema = json.loads((ROOT / 'schemas/inspect-result.schema.json').read_text())
        for args in [('workspace',), ('items',), ('projection', 'demo', '--view', 'summary'),
                     ('document', 'demo', '--path', 'change.md'), ('verification', 'demo')]:
            code, value = self.cli(*args)
            self.assertEqual(0, code)
            validate(value, schema)
            validate(value['data'], schema['$defs'][args[0]], root=schema)

    def test_real_pagination_uses_collection_version_separate_from_response(self):
        from workbench.work_items.store import save_state
        import copy
        for index in range(200):
            slug = f'page-{index:03d}'
            directory = self.fixture.item.parent / slug; directory.mkdir()
            state = copy.deepcopy(self.fixture.state()); state['slug'] = slug
            save_state(directory, state)
        first = self.cli('items', '--limit', '200', '--offset', '0')[1]
        second = self.cli('items', '--limit', '200', '--offset', '200')[1]
        self.assertEqual(200, len(first['data']['items']))
        self.assertEqual(1, len(second['data']['items']))
        self.assertEqual(first['data']['collectionRevision'], second['data']['collectionRevision'])
        self.assertNotEqual(first['responseRevision'], second['responseRevision'])

    def test_run_pages_share_collection_revision(self):
        directory = self.root / '.workspace/runs'; directory.mkdir(parents=True)
        for index in range(201):
            identifier = f'run-{index:03d}'
            (directory / (identifier + '.json')).write_text(json.dumps({'schemaVersion': 3, 'id': identifier,
                'workflow': 'item-development', 'itemSlug': None, 'repository': 'kit', 'branch': 'main',
                'iteration': None, 'bindingRevision': None}))
        first = self.cli('runs', '--limit', '200', '--offset', '0')[1]
        second = self.cli('runs', '--limit', '200', '--offset', '200')[1]
        self.assertEqual(first['data']['collectionRevision'], second['data']['collectionRevision'])
        self.assertEqual(1, len(second['data']['items']))


def generate():
    case=InspectV2Test();case.setUp()
    try:
        case.fixture.plan();case.fixture.review();case.fixture.record('T01');case.fixture.record()
        requests={'workspace':['workspace'],'items':['items'],'task':['projection','demo','--view','task'],
                  'summary':['projection','demo','--view','summary'],'change':['projection','demo','--view','change'],
                  'flow':['projection','demo','--view','flow'],'document':['document','demo','--path','change.md'],
                  'verification':['verification','demo'],'handoff':['handoff','demo'],'search':['search','--query','Demo'],
                  'workflow':['workflow'],'runs':['runs']}
        destination=ROOT/'tests/fixtures/inspect-v2';destination.mkdir(exist_ok=True)
        def normalize(value):
            if isinstance(value,str):return value.replace(str(case.root),'/synthetic/kit')
            if isinstance(value,list):return [normalize(x) for x in value]
            if isinstance(value,dict):return {k:normalize(v) for k,v in value.items()}
            return value
        for name,args in requests.items():
            code,value=case.cli(*args)
            if code:raise AssertionError(value)
            value=normalize(value);value['observedAt']='2026-09-25T00:00:00Z'
            (destination/(name+'.json')).write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n')
        from workbench.work_items import commands
        commands.block(case.root,'demo',reason='等待验收环境',owner='测试负责人',condition='环境恢复',expected_revision=case.fixture.state()['stateRevision'])
        for name in ['blocked','cancelled']:
            if name == 'cancelled':
                commands.cancel(case.root,'demo',reason='目标已被替代',expected_revision=case.fixture.state()['stateRevision'])
            code,value=case.cli('projection','demo','--view','task')
            if code:raise AssertionError(value)
            value=normalize(value);value['observedAt']='2026-09-25T00:00:00Z'
            (destination/(name+'.json')).write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n')
    finally:case.doCleanups()

if __name__=='__main__':
    if '--update-examples' in sys.argv:generate()
    else:unittest.main()
