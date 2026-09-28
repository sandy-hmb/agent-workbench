from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from tests.support.items import ItemFixture
from workbench.work_items import migration
from workbench.work_items.commands import render
from workbench.work_items.store import load_state


class WorkItemStorageTest(ItemFixture, unittest.TestCase):
    def setUp(self):
        self.open()
        self.addCleanup(self.close)

    def test_new_item_keeps_machine_state_under_state_directory(self):
        self.assertTrue((self.item / '.state/state.json').is_file())
        self.assertTrue((self.item / '.state').is_dir())
        self.assertFalse((self.item / 'state.json').exists())
        self.assertFalse((self.item / '.state.lock').exists())

    def test_legacy_item_migrates_with_backup_and_readable_evidence(self):
        self.review()
        self.record()
        modern = self.item / '.state'
        legacy_evidence = self.item / 'evidence'
        shutil.copytree(modern / 'evidence', legacy_evidence)
        shutil.move(str(modern / 'state.json'), str(self.item / 'state.json'))
        shutil.move(str(modern / 'lock'), str(self.item / '.state.lock'))
        shutil.rmtree(modern)
        (self.item / 'merge-verification.json').write_text('{"legacy": true}\n', encoding='utf-8')
        render(self.root, 'demo')

        plan = migration.preview(self.root, slug='demo')
        self.assertFalse(plan['blocked'])
        backup = Path(self.temp.name) / 'item-backup'
        result = migration.apply(self.root, plan['planHash'], backup, slug='demo')

        self.assertEqual(['demo'], result['migrated'])
        self.assertTrue((self.item / '.state/state.json').is_file())
        self.assertTrue((self.item / '.state/evidence').is_dir())
        self.assertTrue((self.item / '.state/inputs/legacy/merge-verification.json').is_file())
        self.assertFalse((self.item / 'state.json').exists())
        self.assertFalse((self.item / 'evidence').exists())
        self.assertIn('验证摘要', (self.item / 'README.md').read_text(encoding='utf-8'))
        self.assertTrue((backup / 'demo/state.json').is_file())
        self.assertEqual(load_state(self.item)['slug'], 'demo')


if __name__ == '__main__':
    unittest.main()
