import unittest
from types import SimpleNamespace

from tests.support.items import ItemFixture
from workbench.extensions.item_outputs import normalize, context, check_conflicts, output_path
from workbench.extensions.model import ExtensionError


class ItemOutputsTest(ItemFixture, unittest.TestCase):
    def setUp(self):
        self.open(); self.addCleanup(self.close)

    def row(self, path='artifacts/runs/demo/{runId}/case-results.json', kind='evidence', mode='append-run'):
        return {'path':path, 'kind':kind, 'writeMode':mode, 'purpose':'Case evidence'}

    def test_safe_paths_and_legacy_missing_declaration(self):
        outputs=normalize({'run':[self.row()], 'query':[]},'demo',{'run','query'})
        manifest=SimpleNamespace(id='demo',item_outputs=outputs)
        self.assertTrue(context(self.root,'demo',manifest,'run',['filesystem.write'])['declared'])
        self.assertEqual(self.item/'artifacts/runs/demo/B01/case-results.json',output_path(self.item,self.row(),'B01'))
        manifest.item_outputs=None
        with self.assertRaisesRegex(ExtensionError,'ITEM_OUTPUTS_UNDECLARED'):
            context(self.root,'demo',manifest,'run',['filesystem.write'])
        self.assertFalse(context(self.root,None,manifest,'run',['filesystem.write'])['declared'])
        from workbench.work_items.store import save_state
        state=self.state();state.pop('documentPolicy');save_state(self.item,state)
        self.assertEqual('legacy',context(self.root,'demo',manifest,'run',['filesystem.write'])['documentPolicy'])

    def test_rejects_wrong_ownership_traversal_versions_and_placeholders(self):
        for row in [self.row('../bad'),self.row('/tmp/x'),self.row('README.md'),self.row('artifacts/runs/other/{runId}/x'),
                    self.row('artifacts/runs/demo/{runId}/{any}.json'),self.row('artifacts/runs/demo/{runId}/../x'),
                    self.row('artifacts/front-final.md','handoff','update'),self.row('artifacts/guide.md','evidence','update')]:
            with self.subTest(row=row):
                with self.assertRaises(ExtensionError):normalize({'run':[row]},'demo',{'run'})
        for run in ('../other','a/b','a.b',None):
            with self.assertRaises(ExtensionError):output_path(self.item,self.row(),run)

    def test_conflicting_owners_and_symlink_escape(self):
        row=self.row('artifacts/guide.md','handoff','update')
        with self.assertRaises(ExtensionError):normalize({'one':[row],'two':[row]},'demo')
        with self.assertRaises(ExtensionError):check_conflicts([SimpleNamespace(id='a',item_outputs={'run':[row]}),SimpleNamespace(id='b',item_outputs={'run':[row]})])
        (self.item/'artifacts').symlink_to(self.root)
        with self.assertRaises(ValueError):output_path(self.item,self.row(),'B01')

    def test_manifest_roundtrip_and_declared_lock_without_migration(self):
        import json
        import shutil
        from tests.support import ROOT
        from workbench.extensions.model import load_manifest, normalize_extensions_lock
        from workbench.extensions.management import DesiredState, _lock_for_desired
        from workbench.extensions.adapters import AdapterPlan
        path=self.root/'example-webhook-notify'
        shutil.copytree(ROOT/'examples/extensions/example-webhook-notify',path)
        manifest=load_manifest(path/'workspace-extension.json')
        self.assertEqual({'notify':[]},manifest.item_outputs)
        lock=_lock_for_desired(DesiredState((manifest,),{'providers':{},'config':{}}), AdapterPlan((), (), ()))
        self.assertEqual({'notify':[]},normalize_extensions_lock(lock)['extensions'][0]['itemOutputs'])


if __name__ == '__main__':unittest.main()
