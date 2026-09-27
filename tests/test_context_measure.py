from __future__ import annotations
import sys
import unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from workbench.context_measure import (DEFAULT_RULE_TOKEN_BUDGET, TASK_REFERENCE_TOKEN_BUDGET,
                                       RULE_TOKEN_BUDGET, estimate_tokens, build_report, scenario_report)
from tests.support.items import ItemFixture

class ContextCostTest(unittest.TestCase):
    def test_estimator_retains_baseline_units(self):
        self.assertEqual(100,estimate_tokens('a'*400)['estTokens'])
        self.assertEqual(50,estimate_tokens('中'*50)['estTokens'])
        self.assertEqual(0,estimate_tokens('')['estTokens'])
    def test_actual_default_rules_include_evidence_reference_and_meet_budget(self):
        report=build_report(ROOT)
        self.assertTrue(report['targetMet'])
        self.assertEqual(DEFAULT_RULE_TOKEN_BUDGET,5200)
        self.assertEqual(TASK_REFERENCE_TOKEN_BUDGET,700)
        self.assertEqual(RULE_TOKEN_BUDGET,5700)
        self.assertEqual(DEFAULT_RULE_TOKEN_BUDGET,report['defaultRuleTokenBudget'])
        self.assertEqual(TASK_REFERENCE_TOKEN_BUDGET,report['taskReferenceTokenBudget'])
        self.assertEqual(RULE_TOKEN_BUDGET,report['ruleTokenBudget'])
        self.assertLessEqual(report['defaultRuleEstTokens'],DEFAULT_RULE_TOKEN_BUDGET)
        self.assertLessEqual(report['taskReferenceEstTokens'],TASK_REFERENCE_TOKEN_BUDGET)
        self.assertLessEqual(report['totalRuleEstTokens'],RULE_TOKEN_BUDGET)
        self.assertTrue(any('references/evidence.md' in row['path'] for row in report['rules']))
        self.assertEqual(0,report['commands'][0]['exitCode'])

    def test_four_scenarios_measure_real_sources_and_session_reuse(self):
        fixture = ItemFixture(); fixture.open(); self.addCleanup(fixture.close)
        fixture.review()
        ordinary = scenario_report(fixture.root, 'demo')
        self.assertTrue(ordinary['lightweight']['applicable'])
        self.assertTrue(ordinary['ordinaryResume']['applicable'])
        self.assertGreater(ordinary['ordinaryResume']['sourceEstTokens'], 0)
        fixture.plan(('T01', 'T02', 'T03')); fixture.review()
        measured = scenario_report(fixture.root, 'demo')
        self.assertTrue(measured['threeTasks']['applicable'])
        self.assertGreater(measured['threeTasks']['reusedSourceEstTokens'], 0)
        self.assertEqual(0, measured['freshResume']['reusedSourceEstTokens'])
        self.assertLess(measured['threeTasks']['totalEstTokens'], measured['freshResume']['totalEstTokens'] * 3)
        self.assertEqual([], fixture.state()['evidence'])

    def test_aegis_inspired_guidance_stays_conditional_and_compact(self):
        skill_root = ROOT / '.agents' / 'skills'
        design = (skill_root / 'workspace-item-design' / 'SKILL.md').read_text()
        planning = (skill_root / 'workspace-writing-plan' / 'SKILL.md').read_text()
        execution = (skill_root / 'workspace-execute-plan' / 'SKILL.md').read_text()
        verification = (skill_root / 'workspace-verify' / 'SKILL.md').read_text()
        evidence = (skill_root / 'workspace-verify' / 'references' / 'evidence.md').read_text()

        self.assertIn('小改不落文件', design)
        self.assertIn('Old path、Disposition、Reason、Verification', planning)
        self.assertIn('无法复现时说明原因', execution)
        self.assertIn('其他任务不加检查', verification)
        self.assertIn('主路径、残留引用和兼容边界', evidence)
