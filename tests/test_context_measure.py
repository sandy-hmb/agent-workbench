from __future__ import annotations
import sys
import unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from workbench.context_measure import estimate_tokens,build_report,scenario_report
from tests.support.items import ItemFixture

class ContextCostTest(unittest.TestCase):
    def test_estimator_retains_baseline_units(self):
        self.assertEqual(100,estimate_tokens('a'*400)['estTokens'])
        self.assertEqual(50,estimate_tokens('中'*50)['estTokens'])
        self.assertEqual(0,estimate_tokens('')['estTokens'])
    def test_actual_default_rules_include_evidence_reference_and_meet_budget(self):
        report=build_report(ROOT)
        self.assertTrue(report['targetMet'])
        self.assertLessEqual(report['totalRuleEstTokens'],9000)
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
