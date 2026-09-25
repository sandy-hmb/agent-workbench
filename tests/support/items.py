"""Behavioral acceptance tests for the single-state workflow."""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.support import ROOT
sys.path.insert(0, str(ROOT))
import workbench.work_items.commands as actions
from workbench.work_items.query import WorkItemQuery
from workbench.work_items.store import WorkItemError, load_state


class ItemFixture:
    def open(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve() / 'kit'
        self.root.mkdir()
        (self.root / '.gitignore').write_text('/docs/development/\n/.workspace/\n')
        (self.root / 'AGENTS.md').write_text('# Rules\n')
        subprocess.run(['git', 'init', '-q', '--initial-branch=main', str(self.root)], check=True)
        subprocess.run(['git', '-C', str(self.root), 'add', '.'], check=True)
        subprocess.run(['git', '-C', str(self.root), '-c', 'user.name=Fixture', '-c', 'user.email=test@example.test', 'commit', '-qm', 'fixture'], check=True)
        self.item = actions.create(self.root, 'demo', title='Demo', repositories=['kit'])['path']
        self.item = self.root / self.item

    def state(self):
        return load_state(self.item)

    def review(self, decision='approved', affected=None):
        return actions.review(self.root, 'demo', decision=decision, reason='User approved the concrete content' if decision == 'approved' else 'Formatting only', expected_revision=self.state()['stateRevision'], affected_tasks=affected or [])

    def payload(self, task=None, result='passed'):
        reader = WorkItemQuery(self.root, 'demo')
        return {'scope': 'task' if task else 'item', 'taskId': task,
                'recordedAt': '2026-09-25T10:00:00+08:00', 'result': result,
                'codeState': reader.code_state(), 'reviewResult': 'passed',
                'checks': [{'type': '测试', 'workingDirectory': 'kit', 'command': 'fixture-check', 'target': 'behavior', 'executed': 1, 'skipped': 0, 'exitStatus': 0 if result == 'passed' else 1, 'result': result}]}

    def record(self, task=None, result='passed'):
        return actions.record(self.root, 'demo', self.payload(task, result), expected_revision=self.state()['stateRevision'])

    def plan(self, ids=('T01',)):
        (self.item / 'plan.md').write_text('# Plan\n\n' + '\n\n'.join(
            f'### {task} Behavior\n\n依据：R1\n依赖：无\n目标仓：kit\n验证性质：行为\n\n'
            '**文件**\n\n- Modify：`AGENTS.md`\n\n**验证**\n\n`fixture-check`\n\n通过条件：行为符合验收。'
            for task in ids))

    def close(self):
        self.temp.cleanup()
